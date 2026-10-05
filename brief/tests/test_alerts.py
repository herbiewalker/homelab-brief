"""The lifecycle on a real database, and wired into the daily and hourly runs."""

import json
from datetime import UTC, datetime, timedelta

import golden_day as g
import pytest

from brief import alerts, cli, detect, grouping, outputs
from brief.detectors import Finding
from brief.lifecycle import Policy
from brief.routing import Priorities
from brief.store import Store

T0 = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
PRIORITIES = Priorities(
    {
        "default": {"rank": 10, "route": "digest_only"},
        "kinds": {
            "absence": {"rank": 80, "route": "push_now"},
            "deviation": {"rank": 50, "route": "push_now"},
            "clock_skew": {"rank": 50, "route": "daily_headline"},
            "pipeline_error": {"rank": 90, "route": "push_now"},
        },
        "lifecycle": {"renotify_hours": 24, "daily_push_budget": 3, "resolve_after_quiet_runs": 2},
        "quiet_hours": {"start": "22:00", "end": "06:30"},
    }
)


class Notifier:
    """A channel that records what it was sent, and can be told to accept or refuse."""

    def __init__(self, accept=True):
        self.accept, self.alerts, self.headlines = accept, [], []

    def send_alert(self, payload):
        self.alerts.append(payload)
        return self.accept

    def send_headline(self, text):
        self.headlines.append(text)
        return self.accept


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "baseline.db")
    yield s
    s.close()


def stories(*specs):
    """Findings as stories. spec = (kind, id, value, host)."""
    found = [
        Finding(fid, kind, f"{fid} title", "sys", host, None, True, value, escalates=(kind != "absence"))
        for kind, fid, value, host in specs
    ]
    return grouping.build_stories(found, PRIORITIES, min_members=99)


def go(store, specs, when, evaluated=("absence",), notifier=None, headline=None):
    return alerts.run_lifecycle(
        store, PRIORITIES, stories(*specs), set(evaluated), when, headline is not None, headline, notifier
    )


def rows(store, sql, *args):
    with store.read() as db:
        return [dict(r) for r in db.execute(sql, args)]


ONE = ("absence", "absence-h1", 100.0, "h1")


# ---------------------------------------------------------------- persistence and ordering


def test_the_decision_is_saved_and_a_second_run_does_not_push_again(store):
    n = Notifier()
    go(store, [ONE], T0, notifier=n)
    go(store, [ONE], T0 + timedelta(hours=1), notifier=n)
    assert [a["state"] for a in n.alerts] == ["new"]  # the second run found it already notified
    (alert,) = rows(store, "SELECT * FROM alert")
    assert alert["finding_id"] == "absence-h1" and alert["pushed"] == 1 and alert["state"] == "open"
    assert alert["first_sent"] == "2026-10-04T12:00:00Z" and alert["level"] == "warning"
    assert rows(store, "SELECT state, channel, delivered FROM push") == [
        {"state": "new", "channel": "alert", "delivered": 1}
    ]


def test_delivered_stays_zero_when_the_channel_is_down_and_nothing_is_retried(store):
    down = Notifier(accept=False)
    go(store, [ONE], T0, notifier=down)
    go(store, [ONE], T0 + timedelta(hours=2), notifier=down)
    assert len(down.alerts) == 1  # a late "new" would be stale news: it is not retried
    assert rows(store, "SELECT delivered FROM push") == [{"delivered": 0}]


def test_with_no_channel_configured_the_decision_is_logged_and_kept(store, capsys):
    go(store, [ONE], T0)
    assert '"event": "alert"' in capsys.readouterr().out
    assert rows(store, "SELECT delivered FROM push") == [{"delivered": 0}]


def test_decisions_are_saved_before_sending_so_a_crash_cannot_send_twice(store):
    class Crash(Notifier):
        def send_alert(self, payload):
            raise RuntimeError("broker exploded mid-send")

    with pytest.raises(RuntimeError):
        go(store, [ONE], T0, notifier=Crash())
    n = Notifier()
    go(store, [ONE], T0 + timedelta(hours=1), notifier=n)
    assert n.alerts == []  # it was recorded as notified before the crash: no duplicate


def test_the_alert_row_round_trips_every_field(store):
    go(store, [("deviation", "deviation-x", 12.5, "h1")], T0)
    book = alerts.load_book(store)
    a = book["deviation-x"]
    assert (a.kind, a.route, a.level, a.last_value, a.pushed, a.urgent) == (
        "deviation",
        "push_now",
        "warning",
        12.5,
        1,
        False,
    )
    assert alerts.load_book(store, include_resolved=False).keys() == {"deviation-x"}


# ---------------------------------------------------------------- escalation, resolution, budget


def test_a_growing_deviation_escalates_but_a_longer_silence_does_not(store):
    n = Notifier()
    go(store, [ONE, ("deviation", "deviation-1", 100.0, "h2")], T0, ("absence", "deviation"), n)
    n.alerts.clear()
    big = [("absence", "absence-h1", 900.0, "h1"), ("deviation", "deviation-1", 300.0, "h2")]
    go(store, big, T0 + timedelta(hours=2), ("absence", "deviation"), n)
    assert [(a["state"], a["finding_id"]) for a in n.alerts] == [("escalated", "deviation-1")]


def test_a_cleared_alert_resolves_after_the_configured_quiet_runs(store):
    n = Notifier()
    go(store, [ONE], T0, notifier=n)
    go(store, [], T0 + timedelta(hours=1), notifier=n)
    assert [a["state"] for a in n.alerts] == ["new"]
    go(store, [], T0 + timedelta(hours=2), notifier=n)  # resolve_after_quiet_runs is 2 here
    assert [a["state"] for a in n.alerts] == ["new", "resolved"]
    assert rows(store, "SELECT state FROM alert") == [{"state": "resolved"}]


def test_the_daily_budget_is_counted_across_runs_from_the_push_log(store):
    n = Notifier()
    specs = [("absence", f"absence-h{i}", 1.0, f"h{i}") for i in range(5)]
    go(store, specs[:2], T0, notifier=n)
    go(store, specs, T0 + timedelta(hours=1), notifier=n)  # three more are wanted, one slot is left
    assert [a["finding_id"] for a in n.alerts] == ["absence-h0", "absence-h1", "absence-h2"]
    assert alerts.pushes_today(store, T0) == 3
    assert alerts.pushes_today(store, T0 + timedelta(days=1)) == 0  # a new day, a new budget


def test_urgent_pushes_and_resolved_messages_do_not_count(store):
    urgent = Priorities({"kinds": {"smart": {"rank": 100, "route": "push_now", "urgent": True}}})
    found = [Finding("smart-1", "smart", "disk failing", story=True)]
    alerts.run_lifecycle(store, urgent, grouping.build_stories(found, urgent), {"smart"}, T0)
    assert alerts.pushes_today(store, T0) == 0


# ---------------------------------------------------------------- the daily headline


def test_the_headline_lists_what_was_folded_or_routed_to_it(store):
    n = Notifier()
    specs = [("absence", f"absence-h{i}", 1.0, f"h{i}") for i in range(5)]  # budget 3: two are folded
    go(store, specs, T0, notifier=n)
    skew = [("clock_skew", "clock_skew-h9", 400.0, "h9")]
    result = go(
        store,
        specs + skew,
        T0 + timedelta(hours=18),
        ("absence", "clock_skew"),
        n,
        headline="2 things need a look",
    )
    assert n.headlines == [
        "2 things need a look Also: absence-h3 title; absence-h4 title; clock_skew-h9 title"
    ]
    assert [x.channel for x in result.notifications] == ["headline"] * 3


def test_a_quiet_headline_is_just_the_headline(store):
    n = Notifier()
    go(store, [], T0, notifier=n, headline="Quiet: 3 hosts checked, nothing needs a look")
    assert n.headlines == ["Quiet: 3 hosts checked, nothing needs a look"] and n.alerts == []


def test_the_headline_text_is_bounded():
    from brief.lifecycle import Notification

    many = [Notification(f"x{i}", "new", "headline", "notice", "t" * 200) for i in range(9)]
    text = alerts.headline_text("Base.", many)
    assert len(text) <= alerts.MAX_HEADLINE_CHARS and text.startswith("Base. Also: ")
    worse = Notification("x", "escalated", "headline", "notice", "thing")
    assert alerts.headline_text("B.", [worse]) == "B. Also: thing (worse)"


# ---------------------------------------------------------------- feedback


def test_an_ack_is_applied_recorded_and_audited(store):
    go(store, [ONE], T0, notifier=Notifier())
    assert alerts.apply_feedback(store, "absence-h1", "ack", T0 + timedelta(hours=1), Policy()) == "acked"
    assert rows(store, "SELECT state FROM alert") == [{"state": "acked"}]
    assert rows(store, "SELECT finding_id, action FROM feedback") == [
        {"finding_id": "absence-h1", "action": "ack"}
    ]
    assert rows(store, "SELECT user, action FROM audit_log WHERE action LIKE 'alert.%'") == [
        {"user": "mqtt", "action": "alert.ack"}
    ]
    n = Notifier()
    go(store, [ONE], T0 + timedelta(days=3), notifier=n)
    assert n.alerts == []  # an ack stops repeats, even days later


def test_a_snooze_sets_its_end_and_a_disallowed_length_is_recorded_but_ignored(store):
    go(store, [ONE], T0, notifier=Notifier())
    assert alerts.apply_feedback(store, "absence-h1", "snooze_24h", T0, Policy()) == "snoozed 24h"
    assert rows(store, "SELECT state, snooze_until FROM alert") == [
        {"state": "snoozed", "snooze_until": "2026-10-05T12:00:00Z"}
    ]
    assert alerts.apply_feedback(store, "absence-h1", "snooze_3h", T0, Policy()) is None
    assert len(rows(store, "SELECT * FROM feedback")) == 2  # both presses are on record


def test_feedback_for_an_unknown_alert_is_recorded_and_changes_nothing(store):
    assert alerts.apply_feedback(store, "no-such-alert", "ack", T0, Policy()) is None
    assert rows(store, "SELECT finding_id FROM feedback") == [{"finding_id": "no-such-alert"}]
    assert rows(store, "SELECT * FROM alert") == []


def test_queued_feedback_is_drained_in_order(store):
    go(store, [ONE, ("absence", "absence-h2", 1.0, "h2")], T0, notifier=Notifier())
    queue = [("absence-h1", "ack"), ("absence-h2", "snooze_7d"), ("nope", "ack")]
    applied = alerts.drain_feedback(store, lambda: queue.pop(0) if queue else None, T0)
    assert applied == 2 and queue == []
    assert {r["finding_id"]: r["state"] for r in rows(store, "SELECT finding_id, state FROM alert")} == {
        "absence-h1": "acked",
        "absence-h2": "snoozed",
    }


# ---------------------------------------------------------------- the daily and hourly runs


@pytest.fixture
def month(tmp_path):
    store = g.build_store(tmp_path / "work")
    yield store
    store.close()


def daily(month, tmp_path, when, notifier, **kw):
    return outputs.run_daily(
        month, g.SYSTEMS, when, tmp_path / "out", env=g.golden_env(tmp_path), state_dir=tmp_path / "none",
        notifier=notifier, alerts_on=True, **kw,
    )  # fmt: skip


def test_the_daily_run_pushes_the_camera_and_sends_the_headline(month, tmp_path):
    n = Notifier()
    digest = daily(month, tmp_path, g.RUN_AT + timedelta(hours=1), n)  # 07:00, after the quiet hours
    assert [(a["state"], a["finding_id"], a["kind"]) for a in n.alerts] == [
        ("new", f"absence-{g.CAM}", "absence")
    ]
    assert n.alerts[0]["detail"] == f"hosts/{g.CAM}.md" and "silent" in n.alerts[0]["title"]
    assert n.headlines == [digest.headline]


def test_the_daily_run_inside_quiet_hours_holds_the_push_but_still_sends_the_headline(month, tmp_path):
    n = Notifier()
    daily(month, tmp_path, g.RUN_AT, n)  # 06:00 is still quiet
    assert n.alerts == [] and len(n.headlines) == 1
    again = Notifier()
    daily(month, tmp_path, g.RUN_AT + timedelta(hours=1), again)
    assert [a["state"] for a in again.alerts] == ["new"]  # the next run after 06:30 sends it


def test_a_failed_daily_run_raises_a_pipeline_alert_and_a_good_one_resolves_it(month, tmp_path):
    n = Notifier()
    later = g.RUN_AT + timedelta(days=11, hours=1)  # no events in the last 24 hours there
    digest = daily(month, tmp_path, later, n)
    assert not digest.run_ok
    assert [(a["state"], a["kind"]) for a in n.alerts] == [("new", "pipeline_error")]
    assert n.alerts[0]["title"].startswith("No data today:") and n.headlines[0].startswith("No data today")
    clear = Notifier()
    for _ in range(3):  # three good daily runs clear it (resolve_after_quiet_runs is 3 in the shipped file)
        daily(month, tmp_path, g.RUN_AT + timedelta(hours=1), clear)
    assert ("resolved", "pipeline_error-daily") in {(a["state"], a["finding_id"]) for a in clear.alerts}


def test_the_command_line_digest_never_sends_alerts(month, tmp_path, monkeypatch):
    month.close()
    monkeypatch.setenv("BRIEF_CONFIG_DIR", str(tmp_path / "none"))
    monkeypatch.setenv("BRIEF_DEFAULTS_DIR", str(tmp_path / "none"))
    ns = cli.argparse.Namespace
    now = (g.RUN_AT + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cli.run_digest(ns(now=now, dry_run=False), tmp_path / "work", out=lambda *_: None, out_dir=tmp_path / "o")
    s = Store(tmp_path / "work" / "baseline.db")
    try:
        assert rows(s, "SELECT * FROM alert") == [] and rows(s, "SELECT * FROM push") == []
    finally:
        s.close()


def test_the_hourly_run_alerts_on_absence_and_on_an_unhealthy_pipeline(month, tmp_path):
    n = Notifier()
    when = g.RUN_AT + timedelta(hours=1)
    env = g.golden_env(tmp_path)
    outputs.run_hourly_alerts(month, g.SYSTEMS, when, env, {"run_ok": True}, n)
    assert [a["finding_id"] for a in n.alerts] == [f"absence-{g.CAM}"]
    outputs.run_hourly_alerts(
        month,
        g.SYSTEMS,
        when + timedelta(hours=1),
        env,
        {"run_ok": False, "headline": "No data: receiver down"},
        n,
    )
    assert [(a["state"], a["kind"]) for a in n.alerts][-1] == ("new", "pipeline_error")
    assert n.alerts[-1]["title"] == "No data: receiver down"


def test_alerts_listing_ack_and_snooze_commands(month, tmp_path, monkeypatch, capsys):
    daily(month, tmp_path, g.RUN_AT + timedelta(hours=1), Notifier())
    month.close()
    state = tmp_path / "work"
    ns = cli.argparse.Namespace
    lines = []
    assert cli.run_alerts(ns(all=False), state, out=lines.append) == 0
    assert any(f"absence-{g.CAM}" in line and "[open]" in line for line in lines) and "of 5" in lines[-1]
    done = []
    assert cli.run_ack(ns(group="ack", finding_id=f"absence-{g.CAM}", hours=24), state, out=done.append) == 0
    assert done == [f"absence-{g.CAM}: acked"]
    lines.clear()
    cli.run_alerts(ns(all=False), state, out=lines.append)
    assert any("[acked]" in line for line in lines)
    assert (
        cli.run_ack(ns(group="snooze", finding_id=f"absence-{g.CAM}", hours=168), state, out=done.append) == 0
    )
    assert done[-1].endswith("snoozed 168h")
    assert cli.run_ack(ns(group="snooze", finding_id="nope", hours=24), state, out=done.append) == 1
    assert (
        cli.run_ack(ns(group="snooze", finding_id=f"absence-{g.CAM}", hours=5), state, out=done.append) == 1
    )
    assert "Nothing changed" in capsys.readouterr().err
    assert cli._snooze_action(168) == "snooze_7d" and cli._snooze_action(24) == "snooze_24h"


def test_the_parser_accepts_the_new_commands(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIEF_STATE", str(tmp_path))
    Store(tmp_path / "baseline.db").close()
    assert cli.main(["alerts", "--all"]) == 0
    assert cli.main(["ack", "nothing-here"]) == 1
    assert cli.main(["snooze", "nothing-here", "--hours", "24"]) == 1


def test_what_reaches_the_channel_is_json_the_phone_can_use(store):
    n = Notifier()
    go(store, [ONE], T0, notifier=n)
    payload = n.alerts[0]
    json.dumps(payload)  # serialisable as is
    assert set(payload) == {
        "finding_id",
        "state",
        "level",
        "title",
        "body",
        "kind",
        "system",
        "host",
        "urgent",
        "detail",
        "at",
        "actions",
    }
    assert (
        detect.ALL
    )  # (keeps the import honest: the detectors and the lifecycle share one set of kind names)
