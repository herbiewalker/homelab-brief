"""The settings pages built on the framework: Schedule and retention, and Notifications."""

import re

import yaml
from test_admin import lab  # noqa: F401  (the fixture: an HTTPS app with a signed-in admin)

from brief import settingspages


def ship(lab, name):  # noqa: F811
    root = lab.settings.state_dir.parent
    (root / "defaults" / "brief" / name).write_bytes(
        (settingspages.__file__ and _brief() / name).read_bytes()
    )
    lab.cfg = root / "cfg" / "brief"


def _brief():
    from pathlib import Path

    return Path(__file__).resolve().parents[1]


def csrf(lab, path):  # noqa: F811
    return re.search(r'name="csrf_token" value="([^"]+)"', lab.client.get(path).text).group(1)


def base_of(lab, path, sid):  # noqa: F811
    card = lab.client.get(path).text.split(f'id="card-{sid}"', 1)[1]
    return re.search(r'name="base" value="([^"]+)"', card).group(1)


def save(lab, path, sid, **fields):  # noqa: F811
    data = {"csrf_token": csrf(lab, path), "base": base_of(lab, path, sid), **fields}
    return lab.client.post(f"/admin/settings/{sid}/save", data=data, headers={"HX-Request": "true"})


def test_the_pages_show_in_the_menu_and_list_their_cards(lab):  # noqa: F811
    ship(lab, "schedule.yaml")
    ship(lab, "retention.yaml")
    ship(lab, "notify.yaml")
    html = lab.client.get("/admin/schedule").text
    assert "Schedule and retention" in html and "Daily run" in html and "Raw syslog archive" in html
    assert 'href="/admin/notifications"' in html
    assert 'value="06:00"' in html and "Edit as text" in html
    notes = lab.client.get("/admin/notifications").text
    assert (
        "Phone and chat notifications" in notes
        and "Home Assistant (MQTT)" in notes
        and 'value="APPRISE_URLS"' in notes
    )


def test_saving_the_schedule_changes_only_those_lines_and_keeps_comments(lab):  # noqa: F811
    ship(lab, "schedule.yaml")
    ship(lab, "retention.yaml")
    r = save(
        lab,
        "/admin/schedule",
        "schedule",
        **{"f.daily_run_utc": "07:15", "f.weekly_day": "friday", "f.weekly_run_utc": "06:30"},
    )
    assert r.status_code == 200 and "saved" in r.headers["hx-trigger"]
    text = (lab.cfg / "schedule.yaml").read_text()
    data = yaml.safe_load(text)
    assert data["daily_run_utc"] == "07:15" and data["weekly_day"] == "friday"
    assert "# the daily full run" in text and "# The hourly job" in text


def test_a_bad_time_is_refused_in_place(lab):  # noqa: F811
    ship(lab, "schedule.yaml")
    r = save(lab, "/admin/schedule", "schedule", **{"f.daily_run_utc": "25:99", "f.weekly_day": "monday"})
    assert r.status_code == 422 and "HH:MM" in r.text and 'id="card-schedule"' in r.text
    assert not (lab.cfg / "schedule.yaml").exists()


def test_retention_numbers_have_limits(lab):  # noqa: F811
    ship(lab, "retention.yaml")
    r = save(lab, "/admin/schedule", "retention", **{"f.archive_days": "0"})
    assert r.status_code == 422 and "at least" in r.text
    ok = save(lab, "/admin/schedule", "retention", **{"f.archive_days": "90"})
    assert (
        ok.status_code == 200
        and yaml.safe_load((lab.cfg / "retention.yaml").read_text())["archive_days"] == 90
    )


def test_apprise_settings_switch_on_and_off_and_keep_the_other_channel(lab):  # noqa: F811
    ship(lab, "notify.yaml")
    r = save(
        lab, "/admin/notifications", "notify-apprise",
        **{"f.enabled": "on", "f.urls_var": "MY_URLS", "f.routes": ["push_now", "daily_headline"], "f.include_body": "on"},
    )  # fmt: skip
    assert r.status_code == 200
    data = yaml.safe_load((lab.cfg / "notify.yaml").read_text())
    assert data["apprise"]["enabled"] is True and data["apprise"]["urls_var"] == "MY_URLS"
    assert (
        data["apprise"]["routes"] == ["push_now", "daily_headline"]
        and data["apprise"]["include_body"] is True
    )
    assert data["apprise"]["include_resolved"] is False and data["mqtt"]["enabled"] is True
    off = save(lab, "/admin/notifications", "notify-mqtt", **{})
    assert (
        off.status_code == 200
        and yaml.safe_load((lab.cfg / "notify.yaml").read_text())["mqtt"]["enabled"] is False
    )


def test_an_unknown_card_is_a_404_and_a_secret_in_the_variable_field_is_refused(lab):  # noqa: F811
    ship(lab, "notify.yaml")
    token = csrf(lab, "/admin/notifications")
    assert lab.client.post("/admin/settings/nope/save", data={"csrf_token": token}).status_code == 404
    secret = "x" * 8 + "abcdef0123456789" * 3
    r = save(lab, "/admin/notifications", "notify-apprise", **{"f.enabled": "on", "f.urls_var": secret})
    assert r.status_code == 422 and secret not in r.text


def test_the_test_button_explains_when_apprise_is_not_ready(lab, monkeypatch):  # noqa: F811
    ship(lab, "notify.yaml")
    monkeypatch.delenv("APPRISE_URLS", raising=False)
    token = csrf(lab, "/admin/notifications")
    r = lab.client.post("/admin/settings/notify-apprise/test", data={"csrf_token": token})
    assert r.status_code == 200 and "Apprise is off" in r.text


def test_the_pages_have_no_inline_script_or_style(lab):  # noqa: F811
    ship(lab, "schedule.yaml")
    ship(lab, "retention.yaml")
    ship(lab, "notify.yaml")
    for path in ("/admin/schedule", "/admin/notifications", "/admin/systems"):
        html = lab.client.get(path).text
        assert " style=" not in html and "<script>" not in html, path


def test_a_save_tells_the_page_the_new_version_stamp_so_sibling_cards_stay_current(lab):  # noqa: F811
    import json

    from brief import configedit

    ship(lab, "schedule.yaml")
    r = save(lab, "/admin/schedule", "schedule", **{"f.daily_run_utc": "07:00", "f.weekly_day": "monday"})
    toast = json.loads(r.headers["hx-trigger"])["toast"]
    assert toast["file"] == "schedule.yaml" and toast["digest"] == configedit.digest(
        (lab.cfg / "schedule.yaml").read_text()
    )


# ---------------------------------------------------------------- Task 34: ignore rules and priorities


def form_of(section, state):
    """The form a browser would post for the values the page shows, untouched."""
    form = {}
    for f in section.scalar_fields:
        value = state["values"][f.key]
        if f.kind == "switch":
            if value:
                form[f"f.{f.key}"] = "on"
        else:
            form[f"f.{f.key}"] = value
    return form


def test_an_untouched_priorities_form_changes_nothing_in_the_shipped_file():
    from brief import settingsui

    text = (_brief() / "priorities.yaml").read_text()
    for section in (settingspages.RANKING, settingspages.ALERTING, settingspages.KINDS):
        state = settingsui.load(section, text)
        values, errors, on = settingsui.parse(section, form_of(section, state))
        assert not errors, errors
        assert settingsui.apply(section, text, values, on) == text, section.id


def test_every_kind_in_the_shipped_file_has_a_row_in_the_table():
    shipped = set(yaml.safe_load((_brief() / "priorities.yaml").read_text())["kinds"])
    assert shipped == set(settingspages.KIND_NAMES)


def test_changing_a_kind_edits_its_one_line_and_adds_urgent(lab):  # noqa: F811
    ship(lab, "priorities.yaml")
    form = {}
    from brief import settingsui

    state = settingsui.load(settingspages.KINDS, (_brief() / "priorities.yaml").read_text())
    form = form_of(settingspages.KINDS, state)
    form["f.kinds.novelty.rank"] = "47"
    form["f.kinds.novelty.route"] = "daily_headline"
    form["f.kinds.novelty.urgent"] = "on"
    r = save(lab, "/admin/priorities", "priorities-kinds", **form)
    assert r.status_code == 200, r.text[-500:]
    text = (lab.cfg / "priorities.yaml").read_text()
    kinds = yaml.safe_load(text)["kinds"]
    assert kinds["novelty"] == {"rank": 47, "route": "daily_headline", "urgent": True}
    assert kinds["absence"] == {"rank": 80, "route": "push_now"}  # the others are untouched
    assert "# Ranking, routing" in text


def test_alert_behaviour_saves_times_numbers_switches_and_the_snooze_list(lab):  # noqa: F811
    ship(lab, "priorities.yaml")
    from brief import settingsui

    state = settingsui.load(settingspages.ALERTING, (_brief() / "priorities.yaml").read_text())
    form = form_of(settingspages.ALERTING, state)
    form.update(
        {
            "f.quiet_hours.start": "21:30",
            "f.lifecycle.daily_push_budget": "3",
            "f.lifecycle.snooze_options_h": "12, 48, 168",
        }
    )
    form.pop("f.lifecycle.send_resolved")  # untick
    r = save(lab, "/admin/priorities", "priorities-alerts", **form)
    assert r.status_code == 200, r.text[-500:]
    data = yaml.safe_load((lab.cfg / "priorities.yaml").read_text())
    assert data["quiet_hours"] == {"start": "21:30", "end": "06:30"}
    assert data["lifecycle"]["daily_push_budget"] == 3 and data["lifecycle"]["send_resolved"] is False
    assert data["lifecycle"]["snooze_options_h"] == [12, 48, 168]


def test_a_bad_priority_value_is_refused_where_it_is(lab):  # noqa: F811
    ship(lab, "priorities.yaml")
    r = save(
        lab, "/admin/priorities", "priorities-ranking", **{"f.default.route": "shout", "f.default.rank": "x"}
    )
    assert r.status_code == 422 and "Choose one of" in r.text and "whole number" in r.text
    assert not (lab.cfg / "priorities.yaml").exists()


def test_the_priorities_page_shows_a_table_a_preview_and_a_local_time_hint(lab):  # noqa: F811
    ship(lab, "priorities.yaml")
    html = lab.client.get("/admin/priorities").text
    assert (
        'class="grid-table"' in html
        and "smart failing" in html
        and "What would this have changed last week?" in html
    )
    assert 'data-local-time="priorities-alerts-quiet_hours.start"' in html


def test_the_preview_replays_the_week_with_and_without_the_change(lab):  # noqa: F811
    ship(lab, "priorities.yaml")
    from brief import settingsui

    state = settingsui.load(settingspages.RANKING, (_brief() / "priorities.yaml").read_text())
    form = form_of(settingspages.RANKING, state)
    token = csrf(lab, "/admin/priorities")
    same = lab.client.post("/admin/settings/priorities-ranking/preview", data={"csrf_token": token, **form})
    assert same.status_code == 200 and "Nothing is changed" in same.text
    form["f.default.rank"] = "99"
    r = lab.client.post("/admin/settings/priorities-ranking/preview", data={"csrf_token": token, **form})
    assert r.status_code == 200 and "Replay of the last" in r.text
    assert not (lab.cfg / "priorities.yaml").exists()  # a preview never saves


def test_ignore_rules_hide_the_shipped_examples_and_save_clean_rules(lab):  # noqa: F811
    ship(lab, "ignore.yaml")
    html = lab.client.get("/admin/ignore").text
    assert "No rules yet" in html and "&lt;" not in html.split("Rules", 1)[1][:600]
    data = {
        "r.ignore.count": "1", "r.ignore.0.name": "Port Flaps", "r.ignore.0.host": "198.51.100.2",
        "r.ignore.0.match": "(?i)link (up|down)", "r.ignore.0.note": "sleeping devices",
    }  # fmt: skip
    r = save(lab, "/admin/ignore", "ignore", **data)
    assert r.status_code == 200, r.text[-400:]
    rule = yaml.safe_load((lab.cfg / "ignore.yaml").read_text())["ignore"][0]
    assert (
        rule["name"] == "port-flaps"
        and rule["match"] == "(?i)link (up|down)"
        and rule["host"] == "198.51.100.2"
    )


def test_a_bad_pattern_is_refused_with_the_reason(lab):  # noqa: F811
    ship(lab, "ignore.yaml")
    r = save(
        lab,
        "/admin/ignore",
        "ignore",
        **{"r.ignore.count": "1", "r.ignore.0.name": "x", "r.ignore.0.match": "(unclosed"},
    )
    assert r.status_code == 422 and "Not a valid pattern" in r.text


def test_a_rule_is_tried_against_recent_lines_before_saving(lab):  # noqa: F811
    import json

    ship(lab, "ignore.yaml")
    folder = lab.settings.state_dir / "events"
    folder.mkdir()
    lines = [
        {"source_ip": "198.51.100.2", "header_host": "sw1", "app": "kernel", "msg": "eth0 link down"},
        {"source_ip": "198.51.100.2", "header_host": "sw1", "app": "kernel", "msg": "eth0 link up"},
        {"source_ip": "198.51.100.3", "header_host": "cam", "app": "app", "msg": "link down on cam"},
        {"source_ip": "198.51.100.2", "header_host": "sw1", "app": "sshd", "msg": "accepted"},
    ]
    (folder / "2026-10-10.ndjson").write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    token = csrf(lab, "/admin/ignore")

    def test(match, host=""):
        data = {
            "csrf_token": token,
            "row": "0",
            "row_key": "ignore",
            "r.ignore.0.match": match,
            "r.ignore.0.host": host,
        }
        return lab.client.post("/admin/settings/ignore/rowtest", data=data).text

    assert "2 of 4" in test("(?i)link (up|down)", "198.51.100.2") or "2 of 3" in test(
        "(?i)link (up|down)", "198.51.100.2"
    )
    assert "3 of 4" in test("(?i)link (up|down)")
    assert "Not a valid pattern" in test("(bad")
    assert "nothing to test against" in test("x", "no-such-host")
    assert "link down" in test("link down")  # an example line is shown
