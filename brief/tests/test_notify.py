"""Optional Apprise notifications: off by default, routed by notify.yaml, no credentials in logs."""

import pytest
from test_worker import make_env

from brief import cli, notify
from brief import worker as w
from brief.notify import AppriseChannel, Channels

SECRET = "ntfys://user:s3cret-token@ntfy.example/alerts"


class FakeApprise:
    """Stands in for apprise.Apprise: records what was added and sent."""

    def __init__(self, fail=False):
        self.urls, self.sent, self.fail = [], [], fail

    def add(self, url):
        self.urls.append(url)
        return "bad" not in url

    def notify(self, title, body, notify_type):
        if self.fail:
            raise RuntimeError(f"cannot reach {SECRET}")
        self.sent.append((title, body, notify_type))
        return True


def channel(urls=(SECRET,), **kw):
    fake = FakeApprise()
    return AppriseChannel(list(urls), factory=lambda: fake, **kw), fake


def write_notify(user, text):
    (user / "notify.yaml").write_text("schema: 1\n" + text, encoding="utf-8")


def alert(state="new", level="warning", **kw):
    return {
        "finding_id": "f1",
        "state": state,
        "level": level,
        "title": "A host is silent",
        "body": "host 198.51.100.7 last seen 2 days ago",
        "system": "Camera",
        **kw,
    }


# ---------------------------------------------------------------- the channel


def test_an_alert_goes_out_with_a_plain_title_and_no_detail_by_default():
    ch, fake = channel()
    assert ch.send_alert(alert()) is True
    assert fake.sent == [("A host is silent", "warning: Camera", "warning")]
    assert "198.51.100.7" not in str(fake.sent)


def test_the_body_is_added_only_when_asked_for():
    ch, fake = channel(include_body=True)
    ch.send_alert(alert())
    assert fake.sent[0][1] == "host 198.51.100.7 last seen 2 days ago"


@pytest.mark.parametrize(
    "payload, title, kind",
    [
        (alert("repeat"), "Still: A host is silent", "warning"),
        (alert("escalated", "critical"), "Worse: A host is silent", "failure"),
        (alert("new", "info", urgent=True), "A host is silent", "failure"),
        (alert("resolved"), "Resolved: A host is silent", "success"),
        (alert("new", "info"), "A host is silent", "info"),
    ],
)
def test_the_title_and_the_notification_type_follow_the_alert_state(payload, title, kind):
    ch, fake = channel()
    ch.send_alert(payload)
    assert fake.sent[0][0] == title and fake.sent[0][2] == kind


def test_resolved_messages_can_be_turned_off():
    ch, fake = channel(include_resolved=False)
    assert ch.send_alert(alert("resolved")) is False and fake.sent == []
    assert ch.send_alert(alert("new")) is True


def test_routes_decide_what_goes_to_apprise():
    only_alerts, fake_a = channel(routes=("push_now",))
    assert only_alerts.send_headline("5 things need a look") is False and fake_a.sent == []
    only_headline, fake_h = channel(routes=("daily_headline",))
    assert only_headline.send_alert(alert()) is False and fake_h.sent == []
    assert only_headline.send_headline("5 things need a look") is True
    assert fake_h.sent == [("Homelab brief", "5 things need a look", "info")]


def test_a_dead_destination_is_a_false_not_an_exception_and_the_url_is_never_logged(capsys):
    ch = AppriseChannel([SECRET], factory=lambda: FakeApprise(fail=True))
    assert ch.send_alert(alert()) is False
    out = capsys.readouterr().out
    assert "apprise_failed" in out and "s3cret" not in out and "ntfy.example" not in out


def test_an_unusable_url_is_counted_not_printed(capsys):
    ch = AppriseChannel([SECRET, "bad://credentials@host"], factory=FakeApprise)
    assert ch.destinations == 1
    out = capsys.readouterr().out
    assert '"number": 2' in out and "credentials" not in out


def test_real_apprise_accepts_a_good_url_and_refuses_nonsense():
    assert AppriseChannel(["json://localhost:9"]).destinations == 1
    assert AppriseChannel(["definitely-not-a-service://x"]).destinations == 0


# ---------------------------------------------------------------- notify.yaml


def test_apprise_is_off_unless_enabled_and_given_urls(tmp_path, capsys):
    env, user = make_env(tmp_path)
    assert AppriseChannel.from_env(env, factory=FakeApprise) is None  # no file: off
    write_notify(user, "apprise:\n  enabled: true\n")
    assert AppriseChannel.from_env(env, factory=FakeApprise) is None  # on, but no URLs
    assert "apprise_no_urls" in capsys.readouterr().out
    write_notify(user, "apprise:\n  enabled: false\n")
    assert AppriseChannel.from_env({**env, "APPRISE_URLS": SECRET}, factory=FakeApprise) is None


def test_the_urls_come_from_the_named_variable_and_the_routes_from_the_file(tmp_path):
    env, user = make_env(tmp_path)
    write_notify(user, "apprise:\n  enabled: true\n  urls_var: MY_URLS\n  routes: [daily_headline, bogus]\n")
    ch = AppriseChannel.from_env({**env, "MY_URLS": f"{SECRET}, json://localhost:9 ,"}, factory=FakeApprise)
    assert ch.destinations == 2 and ch.routes == ("daily_headline",)


# ---------------------------------------------------------------- several channels


class Spy:
    def __init__(self, result):
        self.result, self.alerts, self.headlines = result, [], []

    def send_alert(self, payload):
        self.alerts.append(payload)
        return self.result

    def send_headline(self, text):
        self.headlines.append(text)
        return self.result


def test_every_channel_is_tried_and_one_success_counts_as_delivered():
    first, second = Spy(False), Spy(True)
    both = Channels([first, second])
    assert both.send_alert({"x": 1}) is True and both.send_headline("h") is True
    assert first.alerts == second.alerts == [{"x": 1}] and first.headlines == ["h"]
    assert Channels([Spy(False), Spy(False)]).send_alert({}) is False
    last = Spy(True)
    assert Channels([Spy(True), last]).send_alert({}) and last.alerts == [{}]  # the later one still gets it


# ---------------------------------------------------------------- the worker


def worker(tmp_path, notify_text=None, urls="", monkeypatch=None):
    env, user = make_env(tmp_path)
    if notify_text:
        write_notify(user, notify_text)
    if urls and monkeypatch:

        def fake_from_env(cls, e, factory=None):
            return AppriseChannel([urls], factory=FakeApprise)

        monkeypatch.setattr(notify.AppriseChannel, "from_env", classmethod(fake_from_env))
    return w.Worker(tmp_path / "state", {**env, "APPRISE_URLS": urls})


def test_alerts_go_to_mqtt_alone_by_default_and_to_the_log_with_nothing(tmp_path):
    wk = worker(tmp_path)
    assert wk.notifier is None  # no MQTT, no Apprise: the lifecycle logs the decision
    wk.publisher = Spy(True)
    assert wk.notifier is wk.publisher


def test_apprise_joins_mqtt_when_enabled_and_mqtt_can_be_left_for_sensors(tmp_path, monkeypatch):
    text = "apprise:\n  enabled: true\n"
    wk = worker(tmp_path, text, SECRET, monkeypatch)
    wk.publisher = Spy(True)
    assert isinstance(wk.notifier, Channels) and wk.notifier.channels[0] is wk.publisher
    off = worker(tmp_path / "b", text + "mqtt:\n  enabled: false\n", SECRET, monkeypatch)
    off.publisher = Spy(True)
    assert isinstance(off.notifier, AppriseChannel)  # sensors still publish; alerts only go to Apprise


# ---------------------------------------------------------------- the command


def test_notify_test_says_how_to_turn_apprise_on_when_it_is_off(tmp_path, monkeypatch, capsys):
    env, _ = make_env(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert cli.main(["notify", "test"]) == 1
    assert "notify.yaml" in capsys.readouterr().out


def test_notify_test_sends_one_message_when_it_is_on(tmp_path, monkeypatch, capsys):
    env, user = make_env(tmp_path)
    write_notify(user, "apprise:\n  enabled: true\n")
    for key, value in {**env, "APPRISE_URLS": "json://localhost:9"}.items():
        monkeypatch.setenv(key, value)
    fake = FakeApprise()
    monkeypatch.setattr("apprise.Apprise", lambda: fake)
    assert cli.main(["notify", "test"]) == 0
    assert fake.sent == [("Homelab Brief test", "If you can read this, notifications work.", "info")]
    assert "1 destination" in capsys.readouterr().out
