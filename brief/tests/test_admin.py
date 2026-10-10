"""The admin pages: overview, sources, findings and alerts, audit log, and the security around them."""

import json
import re
from datetime import UTC, datetime, timedelta

import golden_day as g
import pytest
from test_web import Env, sign_in

from brief import health
from brief.store import iso


@pytest.fixture
def lab(tmp_path, monkeypatch):
    """The HTTPS app over a store with a real golden-day daily run and an admin account."""
    monkeypatch.setattr(health, "receiver_ok", lambda port: True)
    cfg = tmp_path / "cfg" / "brief"
    cfg.mkdir(parents=True)
    monkeypatch.setenv("BRIEF_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("BRIEF_DEFAULTS_DIR", str(tmp_path / "defaults"))
    (tmp_path / "defaults" / "brief").mkdir(parents=True)
    (tmp_path / "defaults" / "brief" / "registry.yaml").write_bytes((g.BRIEF / "registry.yaml").read_bytes())
    daily = tmp_path / "daily"
    store, _, out = g.build_daily(daily)
    store.close()
    latest = tmp_path / "out" / "latest"
    latest.mkdir(parents=True)
    doc = json.loads((out / "latest" / "status.json").read_text())
    doc["generated_at"] = doc["daily_generated_at"] = iso(datetime.now(UTC))
    (latest / "status.json").write_text(json.dumps(doc))
    (tmp_path / "state").mkdir()
    (tmp_path / "archive").mkdir()
    env = Env(tmp_path / "state", monkeypatch, out_dir=tmp_path / "out", archive_dir=tmp_path / "archive")
    env.admin()
    client = env.https()
    assert sign_in(client).status_code == 303
    env.client = client
    yield env
    env.store.close()


def fill(env, now=None):
    """Hosts and an alert in the live store, as ingest and the lifecycle would leave them."""
    now = now or datetime.now(UTC)
    with env.store.write() as tx:
        tx.touch_host(
            "198.51.100.1", now - timedelta(days=3), now - timedelta(minutes=5), system_id="gw", known=True
        )
        tx.touch_host(
            "198.51.100.77", now - timedelta(days=1), now - timedelta(hours=2), system_id="other", known=False
        )
        sid = tx.signature_for(0, 1, "app", "a template", now)
        tx.add_hourly([(f"{now:%Y-%m-%d}", now.hour, "198.51.100.1", sid, "info", 25)])
        tx.upsert_alert("absence-cam", kind="absence", title="Camera is silent", level="warning", route="push_now",
                        state="open", opened=iso(now - timedelta(hours=5)), pushed=1)  # fmt: skip
        tx.upsert_alert("done", kind="absence", title="Resolved long ago", state="resolved")


# ---------------------------------------------------------------- access


@pytest.mark.parametrize(
    "path", ["/admin", "/admin/sources", "/admin/findings", "/admin/audit", "/admin/overview/live"]
)
def test_every_admin_page_needs_a_login(lab, path):
    r = lab.https().get(path)
    assert r.status_code == 303 and r.headers["location"].startswith("/admin/login")


def test_the_post_to_alerts_needs_a_session_and_a_csrf_token(lab):
    fill(lab)
    assert (
        lab.https().post("/admin/alerts/act", data={"finding_id": "absence-cam", "action": "ack"}).status_code
        == 401
    )
    r = lab.client.post("/admin/alerts/act", data={"finding_id": "absence-cam", "action": "ack"})
    assert r.status_code == 403  # signed in, but no CSRF token


def test_admin_pages_allow_scripts_from_our_own_origin_only_and_are_never_cached(lab):
    r = lab.client.get("/admin")
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert "connect-src 'self'" in csp and "default-src 'none'" in csp
    assert r.headers["cache-control"] == "no-store"
    assert "script-src" not in lab.https().get("/status.json").headers["content-security-policy"]


def test_the_pages_load_only_our_own_files(lab):
    html = lab.client.get("/admin").text
    sources = re.findall(r'(?:src|href)="([^"]+)"', html)
    assert sources and all(s.startswith("/") for s in sources if not s.startswith("#")), sources
    assert "<script>" not in html and "javascript:" not in html and " onclick=" not in html
    for path in ("/static/admin.js", "/static/theme.js", "/static/app.css", "/static/vendor/htmx-2.0.11.min.js",
                 "/static/vendor/alpine-csp-3.17.4.min.js"):  # fmt: skip
        assert lab.https().get(path).status_code == 200, path


# ---------------------------------------------------------------- overview


def test_the_overview_shows_health_runs_pipeline_and_resources(lab):
    fill(lab)
    html = lab.client.get("/admin").text
    for text in (
        "Health checks",
        "Last runs",
        "Pipeline",
        "Storage",
        "Busiest senders",
        "Memory",
        "Hourly status",
    ):
        assert text in html, text
    assert "198.51.100.1" in html  # the sender busiest in the last hour
    live = lab.client.get("/admin/overview/live")
    assert live.status_code == 200 and "<html" not in live.text and "Health" in live.text  # a fragment


def test_a_failed_run_is_not_dressed_up_as_calm(lab, monkeypatch):
    monkeypatch.setattr(health, "receiver_ok", lambda port: False)
    html = lab.client.get("/admin").text
    assert "No data" in html and "Nothing below is a statement that the network is fine" in html


def test_config_errors_are_listed_on_the_overview(lab):
    (lab.settings.state_dir / "config_errors.json").write_text(
        json.dumps(
            {"errors": {"notify.yaml": {"error": "schema 9 is not supported", "using": "the last good copy"}}}
        )
    )
    html = lab.client.get("/admin").text
    assert "Configuration errors" in html and "notify.yaml" in html and "the last good copy" in html


# ---------------------------------------------------------------- sources


def test_sources_lists_unknown_senders_first_with_their_counts(lab):
    fill(lab)
    html = lab.client.get("/admin/sources").text
    assert html.index("198.51.100.77") < html.index("198.51.100.1<")  # unknown first
    assert "unknown" in html and "not mapped to a system" in html and "25" in html


def test_sources_with_nothing_received_says_so(lab):
    assert "Nothing has been received yet." in lab.client.get("/admin/sources").text


# ---------------------------------------------------------------- findings and alerts


def test_findings_shows_the_daily_stories_and_the_open_alerts(lab):
    fill(lab)
    html = lab.client.get("/admin/findings").text
    assert "198.51.100.3 has been silent for 2 days" in html  # the golden day's top story
    assert "Camera is silent" in html and "Resolved long ago" not in html
    assert "Acknowledge" in html and "Snooze 24 h" in html


def test_acknowledging_an_alert_is_applied_audited_and_redirects(lab):
    fill(lab)
    token = re.search(r'name="csrf_token" value="([^"]+)"', lab.client.get("/admin/findings").text).group(1)
    r = lab.client.post(
        "/admin/alerts/act", data={"finding_id": "absence-cam", "action": "ack", "csrf_token": token}
    )
    assert r.status_code == 303 and r.headers["location"] == "/admin/findings"
    with lab.store.read() as db:
        assert db.execute("SELECT state FROM alert WHERE finding_id = 'absence-cam'").fetchone()[0] == "acked"
        audit = db.execute("SELECT user, action FROM audit_log WHERE action = 'alert.ack'").fetchone()
    assert audit["user"] == "admin:admin"
    assert "acked" in lab.client.get("/admin/findings").text


def test_htmx_gets_just_the_updated_row_and_a_resolved_alert_disappears(lab):
    fill(lab)
    token = re.search(r'name="csrf_token" value="([^"]+)"', lab.client.get("/admin/findings").text).group(1)
    r = lab.client.post(
        "/admin/alerts/act",
        data={"finding_id": "absence-cam", "action": "snooze_24h", "csrf_token": token},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200 and r.text.lstrip().startswith("<tr>") and "snoozed" in r.text
    gone = lab.client.post(
        "/admin/alerts/act",
        data={"finding_id": "done", "action": "ack", "csrf_token": token},
        headers={"HX-Request": "true"},
    )
    assert gone.status_code == 200 and gone.text == ""


@pytest.mark.parametrize("data", [{"finding_id": "x", "action": "delete"}, {"finding_id": "../etc", "action": "ack"},
                                  {"finding_id": "", "action": "ack"}, {"action": "ack"}])  # fmt: skip
def test_bad_alert_actions_are_refused(lab, data):
    token = re.search(r'name="csrf_token" value="([^"]+)"', lab.client.get("/admin/findings").text).group(1)
    assert lab.client.post("/admin/alerts/act", data={**data, "csrf_token": token}).status_code in (400, 404)
    with lab.store.read() as db:  # and nothing was recorded for it
        assert db.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 0


# ---------------------------------------------------------------- audit log


def test_the_audit_log_lists_recent_changes_newest_first_and_filters(lab):
    for i in range(5):
        lab.store.audit("admin", f"thing.{i}", f"detail number {i}")
    lab.store.audit("someone", "other.action", "unrelated")
    html = lab.client.get("/admin/audit").text
    assert html.index("other.action") < html.index("thing.4") < html.index("thing.0")
    only = lab.client.get("/admin/audit?user=someone").text
    assert "other.action" in only and "thing.4" not in only
    found = lab.client.get("/admin/audit?q=number%203").text
    assert "thing.3" in found and "thing.4" not in found


def test_the_audit_log_pages_and_survives_hostile_filters(lab):
    for i in range(130):
        lab.store.audit("admin", f"bulk.{i:03d}")
    first = lab.client.get("/admin/audit").text
    assert "bulk.129" in first and "bulk.029" not in first and "Older" in first
    before = int(re.search(r"before=(\d+)", first).group(1))
    second = lab.client.get(f"/admin/audit?before={before}").text
    assert "bulk.029" in second and "bulk.129" not in second
    hostile = lab.client.get("/admin/audit?q=%25%27%20OR%201%3D1%20--&user=%27%3B")
    assert hostile.status_code == 200 and "Nothing matches." in hostile.text


def test_html_in_the_audit_log_is_escaped(lab):
    lab.store.audit("admin", "x.y", "<script>alert(1)</script>")
    html = lab.client.get("/admin/audit").text
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html


def test_the_account_page_uses_the_same_layout_and_the_old_flow_still_works(lab):
    page = lab.client.get("/admin/account")
    assert page.status_code == 200 and 'class="side' in page.text and "Change the password" in page.text


def test_no_admin_page_uses_an_inline_style_the_policy_would_block(lab):
    fill(lab)
    for path in (
        "/admin",
        "/admin/overview/live",
        "/admin/sources",
        "/admin/findings",
        "/admin/audit",
        "/admin/account",
    ):
        html = lab.client.get(path).text
        assert " style=" not in html and "<style" not in html, path


# ---------------------------------------------------------------- overview (Task 32)


def test_the_overview_leads_with_a_headline_and_the_one_fix_for_a_failed_run(lab, monkeypatch):
    monkeypatch.setattr(health, "receiver_ok", lambda port: False)
    html = lab.client.get("/admin").text
    assert "hero--bad" in html and "Check the syslog port" in html and 'href="/admin/network"' in html


def test_a_healthy_overview_has_no_fix_button(lab):
    html = lab.client.get("/admin").text
    assert "hero--" in html and "Check the syslog port" not in html


def test_the_overview_has_health_tiles_system_tiles_and_a_checklist(lab):
    fill(lab)
    html = lab.client.get("/admin").text
    for text in (
        "Silent hosts",
        "Collectors",
        "AI and agents",
        "Proposals",
        "Systems",
        "What changed since yesterday",
    ):
        assert text in html, text
    assert "Point a device at this container" in html  # the checklist is there while steps are open
    assert "Camera is silent" in html and "Snooze 24 h" in html  # top alerts with inline actions


def test_an_inline_ack_on_the_overview_returns_a_compact_row(lab):
    fill(lab)
    token = re.search(r'name="csrf_token" value="([^"]+)"', lab.client.get("/admin").text).group(1)
    r = lab.client.post(
        "/admin/alerts/act",
        data={"csrf_token": token, "finding_id": "absence-cam", "action": "ack", "compact": "1"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200 and r.text.lstrip().startswith("<li") and "acked" in r.text
