"""The list pages (Task 35): filters, sorting, paging, bulk actions, and what a hostile address cannot do."""

import re
from datetime import UTC, datetime, timedelta

import pytest
from test_admin import lab  # noqa: F401  (the fixture: an HTTPS app with a signed-in admin)

from brief import listing
from brief.store import iso


def csrf(env, path):
    return re.search(r'name="csrf_token" value="([^"]+)"', env.client.get(path).text).group(1)


def seed_alerts(env, count=30):
    now = datetime.now(UTC)
    levels = ["info", "warning", "critical"]
    with env.store.write() as tx:
        for i in range(count):
            tx.upsert_alert(
                f"alert-{i:02d}", kind="absence" if i % 2 else "deviation", title=f"Thing {i:02d} broke",
                level=levels[i % 3], route="push_now", state="open" if i % 5 else "acked",
                opened=iso(now - timedelta(hours=i)), pushed=i % 4,
            )  # fmt: skip


# ---------------------------------------------------------------- the helpers


def test_a_listing_builds_links_that_keep_the_filters_and_drop_the_defaults():
    view = listing.Listing(
        "/x",
        {"q": "disk", "sort": "title", "dir": "asc", "page": "3"},
        {"sort": "opened", "dir": "desc", "page": "1"},
    )
    assert view.url(page=4) == "/x?q=disk&sort=title&dir=asc&page=4"
    assert view.url(q="", page=1) == "/x?sort=title&dir=asc"
    assert (
        view.sort_url("title") == "/x?q=disk&sort=title"
    )  # the same column flips to the default direction, which is not written
    assert view.sort_url("opened") == "/x?q=disk"  # back to the default sort, which is not written
    assert view.aria_sort("title") == "ascending" and view.aria_sort("opened") == "none"


def test_paging_clamps_and_counts():
    page = listing.paginate(list(range(120)), 99, 50)
    assert page["page"] == 3 and page["pages"] == 3 and page["first"] == 101 and page["last"] == 120
    assert listing.paginate([], 1)["first"] == 0
    assert (
        listing.number("abc") == 1 and listing.number("-5") == 1 and listing.choice("x", ("a",), "a") == "a"
    )


# ---------------------------------------------------------------- findings and alerts


def test_alerts_are_paged_filtered_and_sorted(lab):  # noqa: F811
    seed_alerts(lab)
    first = lab.client.get("/admin/findings").text
    assert "Showing 1 to 25 of 30 alerts" in first and "Page 1 of 2" in first and "Thing 00 broke" in first
    assert "Thing 29 broke" not in first
    second = lab.client.get("/admin/findings?page=2").text
    assert "Showing 26 to 30 of 30" in second and "Thing 29 broke" in second
    acked = lab.client.get("/admin/findings?state=acked").text
    assert "Showing 1 to 6 of 6" in acked and acked.count('name="finding_id"') == 6
    assert "Thing 07 broke" in lab.client.get("/admin/findings?q=thing%2007").text
    crit = lab.client.get("/admin/findings?level=critical").text
    assert "Showing 1 to 10 of 10" in crit
    by_title = lab.client.get("/admin/findings?sort=title&dir=asc").text
    assert (
        by_title.index("Thing 00 broke") < by_title.index("Thing 01 broke") < by_title.index("Thing 02 broke")
    )
    assert 'aria-sort="ascending"' in by_title and "<caption" in by_title


def test_alert_list_addresses_cannot_do_anything_but_choose(lab):  # noqa: F811
    seed_alerts(lab, 3)
    for query in (
        "sort=title%3BDROP%20TABLE%20alert&dir=sideways",
        "state=%27%20OR%201%3D1",
        "page=-4",
        "page=banana",
        "q=%25%5C%27%3B--",
        "level=<script>",
    ):
        r = lab.client.get(f"/admin/findings?{query}")
        assert r.status_code == 200 and "<script>alert" not in r.text, query
    assert lab.client.get("/admin/findings?page=999").status_code == 200


def test_the_empty_alert_list_and_an_empty_filter_say_different_things(lab):  # noqa: F811
    assert "No open alerts" in lab.client.get("/admin/findings").text
    seed_alerts(lab, 3)
    assert "No alert matches" in lab.client.get("/admin/findings?q=zzzz").text


def test_bulk_acknowledge_applies_to_each_ticked_alert_and_audits(lab):  # noqa: F811
    seed_alerts(lab, 6)
    token = csrf(lab, "/admin/findings")
    data = {
        "csrf_token": token,
        "action": "snooze_24h",
        "finding_id": ["alert-01", "alert-02", "alert-03", "no-such", "x" * 400],
        "view_query": "state=open",
    }
    r = lab.client.post("/admin/alerts/bulk", data=data, headers={"HX-Request": "true"})
    assert r.status_code == 200 and 'id="alerts-card"' in r.text
    assert "3 alerts snoozed for 24 hours" in r.headers["hx-trigger"]
    with lab.store.read() as db:
        states = {
            row["finding_id"]: row["state"] for row in db.execute("SELECT finding_id, state FROM alert")
        }
        n = db.execute("SELECT COUNT(*) FROM feedback WHERE action = 'snooze_24h'").fetchone()[0]
    assert (
        states["alert-01"] == "snoozed"
        and states["alert-02"] == "snoozed"
        and states["alert-04"] == "open"
        and n == 3
    )
    assert "alert-01" not in re.findall(r'value="(alert-\d+)"', r.text)  # the open filter no longer lists it


def test_a_bulk_action_needs_a_real_action_and_a_token(lab):  # noqa: F811
    seed_alerts(lab, 2)
    token = csrf(lab, "/admin/findings")
    assert (
        lab.client.post(
            "/admin/alerts/bulk", data={"csrf_token": token, "action": "delete", "finding_id": "alert-01"}
        ).status_code
        == 400
    )
    assert lab.client.post("/admin/alerts/bulk", data={"action": "ack"}).status_code == 403
    assert lab.https().post("/admin/alerts/bulk", data={"action": "ack"}).status_code == 401


def test_one_alert_row_acts_through_its_own_button(lab):  # noqa: F811
    seed_alerts(lab, 2)
    token = csrf(lab, "/admin/findings")
    row = lab.client.post(
        "/admin/alerts/act",
        data={"csrf_token": token, "finding_id": "alert-01", "action": "ack"},
        headers={"HX-Request": "true"},
    )
    assert row.status_code == 200 and row.text.lstrip().startswith("<tr>") and "acked" in row.text


# ---------------------------------------------------------------- sources


def seed_hosts(env, count=60):
    now = datetime.now(UTC)
    with env.store.write() as tx:
        for i in range(count):
            tx.touch_host(
                f"198.51.100.{i + 1}",
                now - timedelta(days=2),
                now - timedelta(minutes=i),
                system_id="gw" if i % 3 == 0 else "other",
                known=i % 3 == 0,
            )


def test_sources_filter_sort_and_page(lab):  # noqa: F811
    seed_hosts(lab)
    page = lab.client.get("/admin/sources").text
    assert "Showing 1 to 50 of 60 senders" in page and "Name it" in page
    unknown = lab.client.get("/admin/sources?show=unknown").text
    assert "Showing 1 to 40 of 40" in unknown and "198.51.100.1<" not in unknown  # that one is known
    known = lab.client.get("/admin/sources?show=known").text
    assert "Showing 1 to 20 of 20" in known
    one = lab.client.get("/admin/sources?q=198.51.100.7").text
    assert "198.51.100.7<" in one and "198.51.100.8<" not in one
    by_host = lab.client.get("/admin/sources?sort=host&dir=asc").text
    assert by_host.index("198.51.100.1<") < by_host.index("198.51.100.10<") < by_host.index("198.51.100.2<")
    assert (
        "Page 1 of 2" in lab.client.get("/admin/sources").text
        and "Showing 51 to 60" in lab.client.get("/admin/sources?page=2").text
    )
    assert lab.client.get("/admin/sources?sort=%3B--&show=%27&page=x").status_code == 200


# ---------------------------------------------------------------- signatures


def seed_many_signatures(env, count=12):
    now = datetime.now(UTC)
    ids = []
    with env.store.write() as tx:
        for i in range(count):
            sid = tx.signature_for(0, i + 1, f"app{i:02d}", f"message number {i:02d}", now)
            ids.append(sid)
            tx.add_hourly([(f"{now:%Y-%m-%d}", now.hour, "198.51.100.1", sid, "info", 10 + i)])
    return ids


def test_signatures_sort_by_name_and_filter_by_ignored(lab):  # noqa: F811
    ids = seed_many_signatures(lab)
    by_name = lab.client.get("/admin/signatures?sort=name&dir=asc").text
    assert by_name.index("app00") < by_name.index("app01") < by_name.index("app02")
    default = lab.client.get("/admin/signatures").text
    assert default.index("app11") < default.index("app00")  # busiest first
    with lab.store.write() as tx:
        tx.set_ignored(ids[0], True)
    assert "Showing 1 to 1 of 1" in lab.client.get("/admin/signatures?show=ignored").text
    assert "app00" not in lab.client.get("/admin/signatures?show=active").text
    assert "app00" in lab.client.get("/admin/signatures?ignored=1").text  # the older link still works


def test_bulk_ignore_changes_only_what_differs_and_audits_each(lab):  # noqa: F811
    ids = seed_many_signatures(lab, 4)
    token = csrf(lab, "/admin/signatures")
    with lab.store.write() as tx:
        tx.set_ignored(ids[1], True)  # already ignored: not counted, not audited again
    data = {
        "csrf_token": token,
        "ignored": "1",
        "sig_id": [str(ids[0]), str(ids[1]), str(ids[2]), "99999", "abc"],
        "view_query": "sort=name&dir=asc",
    }
    r = lab.client.post("/admin/signatures/bulk", data=data, headers={"HX-Request": "true"})
    assert (
        r.status_code == 200
        and 'id="signatures-card"' in r.text
        and "2 signatures ignored" in r.headers["hx-trigger"]
    )
    with lab.store.read() as db:
        flags = {row["sig_id"]: row["ignored"] for row in db.execute("SELECT sig_id, ignored FROM signature")}
        audited = db.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'signature.ignore'").fetchone()[0]
    assert flags[ids[0]] == 1 and flags[ids[2]] == 1 and flags[ids[3]] == 0 and audited == 2
    back = lab.client.post(
        "/admin/signatures/bulk",
        data={"csrf_token": token, "ignored": "0", "sig_id": [str(ids[0])]},
        headers={"HX-Request": "true"},
    )
    assert "1 signature no longer ignored" in back.headers["hx-trigger"]


def test_a_bulk_signature_action_needs_a_valid_choice(lab):  # noqa: F811
    token = csrf(lab, "/admin/signatures")
    assert (
        lab.client.post("/admin/signatures/bulk", data={"csrf_token": token, "ignored": "maybe"}).status_code
        == 400
    )
    assert lab.client.post("/admin/signatures/bulk", data={"ignored": "1"}).status_code == 403


# ---------------------------------------------------------------- audit log


def test_the_audit_log_filters_by_kind_of_change(lab):  # noqa: F811
    lab.store.audit("admin", "config.save", "x")
    lab.store.audit("admin", "signature.ignore", "y")
    lab.store.audit("admin", "alert.ack", "z")
    only = lab.client.get("/admin/audit?kind=config").text
    assert "config.save" in only and "signature.ignore" not in only
    page = lab.client.get("/admin/audit").text
    assert '<option value="signature"' in page and '<option value="config"' in page
    assert lab.client.get("/admin/audit?kind=%27%3B--").status_code == 200


# ---------------------------------------------------------------- every list page


@pytest.mark.parametrize(
    "path",
    [
        "/admin/findings",
        "/admin/sources",
        "/admin/signatures",
        "/admin/audit",
        "/admin/logs",
        "/admin/replay",
        "/admin/backups",
    ],
)
def test_list_pages_have_no_inline_style_or_script_and_label_their_tables(lab, path):  # noqa: F811
    seed_alerts(lab, 5)
    seed_hosts(lab, 5)
    seed_many_signatures(lab, 3)
    lab.store.audit("admin", "x.y", "<b>hostile</b>")
    html = lab.client.get(path).text
    assert " style=" not in html and "<script>" not in html and "<b>hostile</b>" not in html
    for table in re.findall(r"<table[^>]*>.*?</table>", html, re.S):
        assert "<caption" in table, path


def test_bulk_bars_start_hidden_and_checkboxes_have_labels(lab):  # noqa: F811
    seed_alerts(lab, 3)
    html = lab.client.get("/admin/findings").text
    assert re.search(r'id="bulk-alerts"[^>]*hidden', html)
    for box in re.findall(r"<input[^>]*type=\"checkbox\"[^>]*>", html):
        if "data-bulk" in box:
            assert "aria-label=" in box
