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


# ---------------------------------------------------------------- Task 34: playbook thresholds and advice


def test_an_untouched_playbook_form_changes_nothing_in_the_shipped_file():
    from brief import settingsui

    text = (_brief() / "playbook.yaml").read_text()
    for section in (settingspages.THRESHOLDS, settingspages.ADVICE):
        state = settingsui.load(section, text)
        values, errors, on = settingsui.parse(section, form_of(section, state))
        assert not errors, errors
        assert settingsui.apply(section, text, values, on) == text, section.id


def test_every_threshold_and_rule_in_the_shipped_file_has_a_control():
    shipped = yaml.safe_load((_brief() / "playbook.yaml").read_text())
    have = {f.key for f in settingspages.THRESHOLDS.scalar_fields}
    for group, params in shipped["thresholds"].items():
        for param in params:
            assert f"thresholds.{group}.{param}" in have, (group, param)
    assert set(shipped["rules"]) == {rule for rule, _ in settingspages.RULE_IDS}


def test_a_threshold_is_edited_in_its_one_line_mapping(lab):  # noqa: F811
    from brief import settingsui

    ship(lab, "playbook.yaml")
    text = (_brief() / "playbook.yaml").read_text()
    form = form_of(settingspages.THRESHOLDS, settingsui.load(settingspages.THRESHOLDS, text))
    form["f.thresholds.deviation.min_ratio"] = "3.5"
    form["f.thresholds.storage_low.min_free_pct"] = "20"
    r = save(lab, "/admin/thresholds", "playbook-thresholds", **form)
    assert r.status_code == 200, r.text[-500:]
    out = (lab.cfg / "playbook.yaml").read_text()
    data = yaml.safe_load(out)["thresholds"]
    assert data["deviation"]["min_ratio"] == 3.5 and data["deviation"]["mad_multiple"] == 4
    assert data["storage_low"] == {"min_free_pct": 20, "min_days_until_full": 14}
    assert "# the baseline counts only windows" in out  # comments between lines survive


def test_changing_one_advice_text_rewrites_only_that_block(lab):  # noqa: F811
    from brief import settingsui

    ship(lab, "playbook.yaml")
    text = (_brief() / "playbook.yaml").read_text()
    form = form_of(settingspages.ADVICE, settingsui.load(settingspages.ADVICE, text))
    form["f.rules.clock_skew.advice"] = "{system} clock is {minutes} min off.\nFix NTP."
    form["f.rules.clock_skew.bucket"] = "plan"
    r = save(lab, "/admin/thresholds", "playbook-advice", **form)
    assert r.status_code == 200, r.text[-500:]
    before, after = (
        yaml.safe_load(text)["rules"],
        yaml.safe_load((lab.cfg / "playbook.yaml").read_text())["rules"],
    )
    assert after["clock_skew"]["advice"].strip() == "{system} clock is {minutes} min off. Fix NTP."
    assert after["clock_skew"]["bucket"] == "plan"
    for rule in before:
        if rule != "clock_skew":
            assert after[rule] == before[rule], rule  # including the match lists of the others


def test_a_threshold_out_of_range_is_refused(lab):  # noqa: F811
    ship(lab, "playbook.yaml")
    r = save(
        lab, "/admin/thresholds", "playbook-thresholds", **{"f.thresholds.odd_hours.min_online_min": "500"}
    )
    assert r.status_code == 422 and "at most" in r.text and not (lab.cfg / "playbook.yaml").exists()


# ---------------------------------------------------------------- Task 34: devices, topology, map, views, sensors


def test_an_untouched_form_changes_nothing_in_each_shipped_file():
    from brief import settingsui

    for file, sections in (
        ("map.yaml", (settingspages.MAP,)),
        ("views.yaml", (settingspages.VIEWS,)),
    ):
        text = (_brief() / file).read_text()
        for section in sections:
            state = settingsui.load(section, text)
            values, errors, on = settingsui.parse(section, form_of(section, state))
            assert not errors, errors
            assert settingsui.apply(section, text, values, on) == text, section.id


def test_the_example_devices_are_not_shown_as_yours_and_a_device_saves_clean(lab):  # noqa: F811
    ship(lab, "devices.example.yaml")
    html = lab.client.get("/admin/devices").text
    assert "Example laptop" not in html and "No devices yet" in html
    data = {
        "r.devices.count": "2",
        "r.devices.0.mac": "00:00:5E:00:53:01", "r.devices.0.label": "Laptop", "r.devices.0.owner": "Me",
        "r.devices.0.profile": "full", "r.devices.0.quiet_hours": "23:00-06:00",
        "r.devices.1.mac": "00:00:5E:00:53:02", "r.devices.1.label": "Plug", "r.devices.1.profile": "ignore",
    }  # fmt: skip
    r = save(lab, "/admin/devices", "devices", **data)
    assert r.status_code == 200, r.text[-500:]
    rows = yaml.safe_load((lab.cfg / "devices.yaml").read_text())["devices"]
    assert rows[0] == {
        "mac": "00:00:5E:00:53:01",
        "label": "Laptop",
        "owner": "Me",
        "profile": "full",
        "quiet_hours": {"start": "23:00", "end": "06:00"},
    }
    assert rows[1]["profile"] == "ignore" and "quiet_hours" not in rows[1]
    again = lab.client.get("/admin/devices").text
    assert "23:00-06:00" in again and "00:00:5E:00:53:02" in again


def test_a_bad_mac_or_hours_value_is_refused(lab):  # noqa: F811
    ship(lab, "devices.example.yaml")
    row = {"r.devices.count": "1", "r.devices.0.mac": "not-a-mac", "r.devices.0.quiet_hours": "late"}
    r = save(lab, "/admin/devices", "devices", **row)
    assert r.status_code == 422 and "MAC address" in r.text and "two times" in r.text
    assert not (lab.cfg / "devices.yaml").exists()


def test_topology_nodes_links_and_changes_are_three_row_lists(lab):  # noqa: F811
    ship(lab, "topology.example.yaml")
    data = {
        "r.nodes.count": "1", "r.nodes.0.id": "isp", "r.nodes.0.kind": "internet", "r.nodes.0.label": "ISP",
        "r.edges.count": "1", "r.edges.0.from": "isp", "r.edges.0.to": "udm", "r.edges.0.kind": "uplink",
        "r.overrides.count": "1", "r.overrides.0.id": "aa-bb", "r.overrides.0.label": "Office switch", "r.overrides.0.hide": "on",
    }  # fmt: skip
    r = save(lab, "/admin/devices", "topology", **data)
    assert r.status_code == 200, r.text[-500:]
    out = yaml.safe_load((lab.cfg / "topology.yaml").read_text())
    assert out["nodes"] == [{"id": "isp", "kind": "internet", "label": "ISP"}]
    assert out["edges"] == [{"from": "isp", "to": "udm", "kind": "uplink"}]
    assert out["overrides"] == [{"id": "aa-bb", "label": "Office switch", "hide": True}]


def test_the_map_card_edits_scalars_a_tick_group_and_a_nested_value(lab):  # noqa: F811
    from brief import settingsui

    ship(lab, "map.yaml")
    form = form_of(settingspages.MAP, settingsui.load(settingspages.MAP, (_brief() / "map.yaml").read_text()))
    form.update(
        {
            "f.enabled": "on",
            "f.layers": ["network", "power"],
            "f.static_svg.width": "1200",
            "f.group_clients": "none",
        }
    )
    r = save(lab, "/admin/devices", "map", **form)
    assert r.status_code == 200, r.text[-400:]
    data = yaml.safe_load((lab.cfg / "map.yaml").read_text())
    assert (
        data["enabled"] is True and data["layers"] == ["network", "power"] and data["group_clients"] == "none"
    )
    assert data["static_svg"] == {"enabled": True, "width": 1200, "height": 1000}


def test_views_and_sensors_save(lab):  # noqa: F811
    from brief import settingsui

    ship(lab, "views.yaml")
    ship(lab, "registry.yaml")
    form = form_of(
        settingspages.VIEWS, settingsui.load(settingspages.VIEWS, (_brief() / "views.yaml").read_text())
    )
    form.update({"f.png.width": "1280", "f.board.stories": "5"})
    assert save(lab, "/admin/display", "views", **form).status_code == 200
    data = yaml.safe_load((lab.cfg / "views.yaml").read_text())
    assert data["png"]["width"] == 1280 and data["board"] == {"refresh_s": 300, "stories": 5}
    page = lab.client.get("/admin/display").text
    assert "events_24h" in page and "Log events, last 24h" in page
    sensors = settingsui.load(settingspages.REGISTRY, (_brief() / "registry.yaml").read_text())["rows"][
        "metrics"
    ]
    form = {"r.metrics.count": str(len(sensors))}
    for i, row in enumerate(sensors):
        form[f"r.metrics.{i}.orig"] = str(row["_orig"])
        for key in ("id", "name", "group", "kind", "unit", "interval"):
            form[f"r.metrics.{i}.{key}"] = str(row.get(key, ""))
    form["r.metrics.0.name"] = "Renamed in the form"
    assert save(lab, "/admin/display", "registry", **form).status_code == 200
    saved = yaml.safe_load((lab.cfg / "registry.yaml").read_text())["metrics"]
    assert saved[0]["name"] == "Renamed in the form" and saved[2]["thresholds"] == {
        "warn": 1,
        "crit": 3,
        "direction": "high",
    }


# ---------------------------------------------------------------- Task 36: AI, MCP, and agents


def test_untouched_ai_and_agent_forms_change_nothing_in_the_shipped_files():
    from brief import settingsui

    for file, sections in (
        ("ai.yaml", (settingspages.AI_OTHER,)),
        ("agents.yaml", (settingspages.AGENT_DEFAULTS, *settingspages.AGENT_SECTIONS)),
    ):
        text = (_brief() / file).read_text()
        for section in sections:
            state = settingsui.load(section, text)
            values, errors, on = settingsui.parse(section, form_of(section, state))
            assert not errors, (section.id, errors)
            assert settingsui.apply(section, text, values, on) == text, section.id


def test_every_agent_in_the_shipped_file_has_a_card_and_its_tools_are_real():
    from brief.agenttools import TOOLS

    shipped = yaml.safe_load((_brief() / "agents.yaml").read_text())["agents"]
    cards = {s.id for s in settingspages.AGENT_SECTIONS}
    assert cards == {f"agent-{name}" for name in shipped}
    tools = next(f for f in settingspages.AGENT_SECTIONS[0].scalar_fields if f.key == "tools").choices
    assert set(tools) == set(TOOLS) and all(t in tools for a in shipped.values() for t in a["tools"])


def test_the_ai_tabs_render_and_need_a_login(lab):  # noqa: F811
    ship(lab, "ai.yaml")
    ship(lab, "agents.yaml")
    for path in (
        "/admin/ai",
        "/admin/ai/agents",
        "/admin/ai/proposals",
        "/admin/ai/mcp",
        "/admin/ai/activity",
    ):
        assert lab.https().get(path).status_code == 303, path
        page = lab.client.get(path)
        assert (
            page.status_code == 200
            and 'aria-label="AI sections"' in page.text
            and 'aria-current="page"' in page.text
        ), path
        assert " style=" not in page.text and "<script>" not in page.text, path
        for table in re.findall(r"<table[^>]*>.*?</table>", page.text, re.S):
            assert "<caption" in table, path


PROVIDER_FORM = {
    "r.providers.count": "2",
    "r.providers.0.orig": "0", "r.providers.0.id": "local", "r.providers.0.type": "openai_compatible",
    "r.providers.0.base_url": "http://198.51.100.20:11434/v1", "r.providers.0.api_key_var": "AI_LOCAL_API_KEY",
    "r.providers.0.model": "llama3.1:8b", "r.providers.0.locality": "lan", "r.providers.0.timeout_s": "90",
    "r.providers.0.max_output_tokens": "1200", "r.providers.0.context_tokens": "8192",
    "r.providers.0.supports_tools": "on", "r.providers.0.enabled": "on",
    "r.providers.1.orig": "1", "r.providers.1.id": "cloud", "r.providers.1.type": "openai_compatible",
    "r.providers.1.locality": "cloud", "r.providers.1.timeout_s": "60", "r.providers.1.max_output_tokens": "1200",
    "r.providers.1.context_tokens": "32000", "r.providers.1.supports_tools": "on",
}  # fmt: skip


def test_the_provider_card_shows_the_shipped_example_and_saves_blocks(lab):  # noqa: F811
    ship(lab, "ai.yaml")
    html = lab.client.get("/admin/ai").text
    assert 'value="local"' in html and 'value="cloud"' in html  # the shipped examples are shown, to fill in
    assert "model-server-ip" not in html  # the placeholder address is an empty box
    r = save(lab, "/admin/ai", "ai-providers", **PROVIDER_FORM)
    assert r.status_code == 200, r.text[-600:]
    text = (lab.cfg / "ai.yaml").read_text()
    providers = yaml.safe_load(text)["providers"]
    assert providers[0]["base_url"] == "http://198.51.100.20:11434/v1" and providers[0]["enabled"] is True
    assert providers[0]["model"] == "llama3.1:8b" and providers[0]["timeout_s"] == 90
    assert (
        providers[1]["id"] == "cloud" and "base_url" not in providers[1] and providers[1]["enabled"] is False
    )
    assert "    base_url: " in text  # written one setting per line
    assert yaml.safe_load(text)["tasks"]["morning_brief"]["chain"] == [
        "local",
        "template",
    ]  # the rest is untouched


def test_a_provider_switched_on_without_an_address_or_with_a_bad_one_is_refused(lab):  # noqa: F811
    ship(lab, "ai.yaml")
    row = {
        "r.providers.count": "1",
        "r.providers.0.orig": "0",
        "r.providers.0.id": "local",
        "r.providers.0.enabled": "on",
    }
    r = save(lab, "/admin/ai", "ai-providers", **row)
    assert r.status_code == 422 and "no real base_url" in r.text and not (lab.cfg / "ai.yaml").exists()
    bad = {**row, "r.providers.0.base_url": "ftp://x", "r.providers.0.enabled": ""}
    r = save(lab, "/admin/ai", "ai-providers", **bad)
    assert r.status_code == 422 and "address starting with http" in r.text


def test_removing_a_provider_that_a_chain_uses_is_refused_with_the_reason(lab):  # noqa: F811
    ship(lab, "ai.yaml")
    row = {"r.providers.count": "1", "r.providers.0.orig": "1", "r.providers.0.id": "cloud"}
    r = save(lab, "/admin/ai", "ai-providers", **row)
    assert r.status_code == 422 and "not a provider" in r.text


def test_the_mcp_default_scopes_and_call_logging_save(lab):  # noqa: F811
    from brief import settingsui

    ship(lab, "ai.yaml")
    form = form_of(
        settingspages.AI_OTHER, settingsui.load(settingspages.AI_OTHER, (_brief() / "ai.yaml").read_text())
    )
    form["f.mcp.default_scope"] = ["status", "findings"]
    form.pop("f.logging.record_calls")
    r = save(lab, "/admin/ai", "ai-other", **form)
    assert r.status_code == 200, r.text[-400:]
    data = yaml.safe_load((lab.cfg / "ai.yaml").read_text())
    assert data["mcp"]["default_scope"] == ["status", "findings"] and data["logging"]["record_calls"] is False


def test_an_agent_is_switched_on_and_limited_from_its_card(lab):  # noqa: F811
    from brief import settingsui

    ship(lab, "agents.yaml")
    section = next(s for s in settingspages.AGENT_SECTIONS if s.id == "agent-investigator")
    form = form_of(section, settingsui.load(section, (_brief() / "agents.yaml").read_text()))
    form.update(
        {
            "f.enabled": "on",
            "f.max_steps": "5",
            "f.trigger.on_finding_rank_at_least": "70",
            "f.tools": ["get_status", "list_findings", "get_finding"],
        }
    )
    r = save(lab, "/admin/ai/agents", "agent-investigator", **form)
    assert r.status_code == 200, r.text[-500:]
    out = (lab.cfg / "agents.yaml").read_text()
    agent = yaml.safe_load(out)["agents"]["investigator"]
    assert (
        agent["enabled"] is True
        and agent["max_steps"] == 5
        and agent["trigger"]["on_finding_rank_at_least"] == 70
    )
    assert set(agent["tools"]) == {"get_status", "list_findings", "get_finding"}
    assert agent["prompt"] == "prompts/agent_investigator.md"
    assert yaml.safe_load(out)["agents"]["triage"]["enabled"] is False  # the others are untouched
    assert "# Phase 3 agents" in out


def test_agent_limits_are_checked_and_the_tuner_schedule_is_a_choice(lab):  # noqa: F811
    from brief import settingsui

    ship(lab, "agents.yaml")
    section = next(s for s in settingspages.AGENT_SECTIONS if s.id == "agent-tuner")
    form = form_of(section, settingsui.load(section, (_brief() / "agents.yaml").read_text()))
    bad = save(lab, "/admin/ai/agents", "agent-tuner", **{**form, "f.max_steps": "99"})
    assert bad.status_code == 422 and "at most" in bad.text
    ok = save(lab, "/admin/ai/agents", "agent-tuner", **{**form, "f.trigger.schedule": "never"})
    tuner = yaml.safe_load((lab.cfg / "agents.yaml").read_text())["agents"]["tuner"]
    assert ok.status_code == 200 and tuner["trigger"]["schedule"] == "never"


def test_the_agent_defaults_card_edits_the_limits_and_the_provider_list(lab):  # noqa: F811
    from brief import settingsui

    ship(lab, "agents.yaml")
    text = (_brief() / "agents.yaml").read_text()
    form = form_of(settingspages.AGENT_DEFAULTS, settingsui.load(settingspages.AGENT_DEFAULTS, text))
    form.update({"f.max_runs_per_day": "40", "f.chain": "local, cloud"})
    assert save(lab, "/admin/ai/agents", "agents-defaults", **form).status_code == 200
    defaults = yaml.safe_load((lab.cfg / "agents.yaml").read_text())["defaults"]
    assert defaults["max_runs_per_day"] == 40 and defaults["chain"] == ["local", "cloud"]
    assert defaults["require_citations"] is True
