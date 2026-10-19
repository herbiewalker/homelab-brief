"""A system's page lists its devices by name (Task 39): the name an admin gave, the UniFi name, the name in the
device's own header, or the address, in that order; and a name can be set from the page."""

import json
import re
from datetime import UTC, datetime, timedelta

import pytest
from test_admin import fill, lab  # noqa: F401  (the fixture)

SYSTEM = "gw"
HOST = "198.51.100.1"  # fill() puts this sender in the gw system
MAC = "00:00:5e:00:53:21"
PAGE = f"/admin/system/{SYSTEM}"


def give_header(lab, name="Gateway-A", unifi=None):  # noqa: F811
    now = datetime.now(UTC)
    with lab.store.write() as tx:
        tx.set_host_name(HOST, name, now)
        tx.set_device_header(HOST, MAC.replace(":", ""), "USW-Pro-24", "7.1.26+15650", now)
        if unifi:
            tx.db.execute(
                "INSERT INTO topo_node (node_id, kind, label, source, attrs_json, first_seen, last_seen) "
                "VALUES (?, 'switch', ?, 'unifi', ?, ?, ?)",
                (f"unifi:{MAC.replace(':', '')}", unifi, json.dumps({"model": "USW-Pro-24", "firmware": "7.1.26", "online": True}),
                 now.isoformat(), now.isoformat()),
            )  # fmt: skip


def csrf(lab):  # noqa: F811
    return re.search(r'name="csrf_token" value="([^"]+)"', lab.client.get(PAGE).text).group(1)


def test_the_page_lists_the_device_under_the_name_it_calls_itself(lab):  # noqa: F811
    fill(lab)
    give_header(lab)
    html = lab.client.get(PAGE).text
    assert "Gateway-A" in html and HOST in html and MAC in html and "USW-Pro-24" in html and "7.1.26" in html
    assert "the device" in html  # where the name came from


def test_the_unifi_name_beats_the_header_name_and_an_alias_beats_both(lab):  # noqa: F811
    fill(lab)
    give_header(lab, unifi="Rack switch")
    assert "Rack switch" in lab.client.get(PAGE).text
    r = lab.client.post(
        f"{PAGE}/alias", data={"host": HOST, "alias": "  Garage   switch ", "csrf_token": csrf(lab)}, headers={"HX-Request": "true"}
    )  # fmt: skip
    assert r.status_code == 200 and "Garage switch" in r.text and "Saved the name" in r.headers["hx-trigger"]
    assert "Garage switch" in lab.client.get(PAGE).text
    with lab.store.read() as db:
        assert (
            db.execute("SELECT alias FROM host_alias WHERE host = ?", (HOST,)).fetchone()[0]
            == "Garage switch"
        )
        audit = db.execute("SELECT user, detail FROM audit_log WHERE action = 'device.rename'").fetchone()
    assert audit["user"] == "admin:admin" and HOST in audit["detail"]


def test_an_empty_name_goes_back_to_the_devices_own_name(lab):  # noqa: F811
    fill(lab)
    give_header(lab)
    for alias in ("Mine", ""):
        lab.client.post(f"{PAGE}/alias", data={"host": HOST, "alias": alias, "csrf_token": csrf(lab)})
    html = lab.client.get(PAGE).text
    assert "Gateway-A" in html and "Mine" not in html
    with lab.store.read() as db:
        assert db.execute("SELECT COUNT(*) FROM host_alias").fetchone()[0] == 0


@pytest.mark.parametrize(
    "data, status",
    [
        ({"host": "198.51.100.250", "alias": "x"}, 404),  # not in this system
        ({"host": "bad host!", "alias": "x"}, 400),
        ({"host": HOST, "alias": "x" * 61}, 422),
    ],
)
def test_a_bad_rename_is_refused_and_changes_nothing(lab, data, status):  # noqa: F811
    fill(lab)
    r = lab.client.post(f"{PAGE}/alias", data={**data, "csrf_token": csrf(lab)})
    assert r.status_code == status
    with lab.store.read() as db:
        assert db.execute("SELECT COUNT(*) FROM host_alias").fetchone()[0] == 0


def test_the_rename_needs_a_session_and_a_csrf_token(lab):  # noqa: F811
    fill(lab)
    assert lab.https().post(f"{PAGE}/alias", data={"host": HOST, "alias": "x"}).status_code == 401
    assert lab.client.post(f"{PAGE}/alias", data={"host": HOST, "alias": "x"}).status_code == 403


def test_an_unknown_system_is_a_friendly_404_and_the_overview_links_each_system(lab):  # noqa: F811
    fill(lab)
    r = lab.client.get("/admin/system/nonesuch")
    assert r.status_code == 404 and "No such system" in r.text
    assert f'href="/admin/system/{SYSTEM}"' in lab.client.get("/admin").text


def test_a_silent_device_is_listed_first(lab):  # noqa: F811
    fill(lab)
    now = datetime.now(UTC)
    with lab.store.write() as tx:
        tx.touch_host(
            "198.51.100.2", now - timedelta(days=9), now - timedelta(days=8), system_id=SYSTEM, known=True
        )
    from brief import systemdetail

    status = lab.http().get("/status.json").json()
    status["silent_hosts"] = [{"host": "198.51.100.2", "system": SYSTEM}]
    names = [d["host"] for d in systemdetail.devices(lab.store, status, SYSTEM, now)]
    assert names[0] == "198.51.100.2" and HOST in names
