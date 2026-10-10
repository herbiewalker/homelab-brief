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
