import json
import re
import time
from datetime import UTC, datetime, timedelta

import pytest
from argon2 import PasswordHasher
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from brief import health
from brief import web as w
from brief.auth import FAIL_FREE, Auth
from brief.settings import Settings
from brief.store import Store

GOOD = "correct horse battery"
NEW = "a brand new passphrase"
FAST = PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1)


class Env:
    def __init__(self, tmp_path, monkeypatch, **settings):
        monkeypatch.setattr(health, "receiver_ok", lambda port: True)
        self.settings = Settings(state_dir=tmp_path, announce_setup=False, **settings)
        self.store = Store(tmp_path / "baseline.db")
        self.auth = Auth(self.store, hasher=FAST)

    def app(self, mode, **kw):
        return w.create_app(mode, self.settings, self.store, self.auth, **kw)

    def https(self, **kw):
        return TestClient(self.app("https", **kw), base_url="https://testserver", follow_redirects=False)

    def http(self, **kw):
        return TestClient(self.app("http", **kw), base_url="http://testserver", follow_redirects=False)

    def admin(self):
        self.auth.create_admin("admin", GOOD)


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch)
    yield e
    e.store.close()


def admin_routes(app):
    """Every route on the admin router (FastAPI keeps an included router lazy, so walk it directly)."""
    return [r for r in app.state.admin_router.routes if isinstance(r, APIRoute)]


def csrf_from(response) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', response.text).group(1)


def sign_in(client, password=GOOD, username="admin"):
    page = client.get("/admin/login")
    return client.post(
        "/admin/login",
        data={"username": username, "password": password, "csrf_token": csrf_from(page), "next": "/admin"},
    )


# ======================================================================== the HTTP app


def test_healthz_is_ok_while_the_worker_is_starting(env):
    r = env.http().get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["checks"]["worker"]["state"] == "starting" and body["version"]


def test_healthz_fails_when_the_receiver_is_down(env, monkeypatch):
    monkeypatch.setattr(health, "receiver_ok", lambda port: False)
    r = env.http().get("/healthz")
    assert r.status_code == 503 and r.json()["checks"]["receiver"]["ok"] is False


def test_healthz_fails_when_the_worker_never_reports_after_the_grace_period(env):
    r = env.http(started=time.monotonic() - 10_000).get("/healthz")
    assert r.status_code == 503 and r.json()["checks"]["worker"]["state"] == "stale"


def test_healthz_is_ok_when_the_worker_reported_recently(env):
    with env.store.write() as tx:
        tx.add_metrics([(datetime.now(UTC), "ingest.cycle_seconds", 0.1)])
    r = env.http(started=time.monotonic() - 10_000).get("/healthz")
    assert r.status_code == 200 and r.json()["checks"]["worker"]["state"] == "running"


def test_the_status_page_works_without_login_and_needs_no_javascript(env):
    r = env.http().get("/")
    assert r.status_code == 200 and "homelab-brief" in r.text and "Learning, day 0" in r.text
    assert "<script" not in r.text.lower()
    # the Docker tab's WebUI lands here, so the page offers one way in: a plain link, which the HTTP app redirects to HTTPS
    assert r.text.count('href="/admin"') == 1 and "Admin sign in" in r.text
    assert 'href="/admin/' not in r.text  # nothing else of the admin area is advertised


def test_the_status_page_shows_counts(env):
    now = datetime.now(UTC)
    with env.store.write() as tx:
        sid = tx.signature_for(1, 1, "a", "t", now)
        tx.add_hourly([(f"{now:%Y-%m-%d}", now.hour, "198.51.100.1", sid, "info", 42)])
        tx.touch_host("198.51.100.1", now, now, system_id="other", known=False)
    text = env.http().get("/").text
    assert ">42<" in text and "1 (1)" in text


def test_admin_is_never_served_over_http_it_is_redirected(env):
    http = env.http()
    r = http.get("/admin/login?next=/admin/account")
    assert r.status_code == 308
    assert r.headers["location"] == "https://testserver/admin/login?next=/admin/account"
    assert http.get("/admin").headers["location"] == "https://testserver/admin"


def test_the_redirect_uses_the_published_https_port(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch, https_public_port=8443)
    assert e.http().get("/admin").headers["location"] == "https://testserver:8443/admin"
    e.store.close()


def test_a_write_to_admin_over_http_is_refused_and_not_processed(env):
    env.admin()
    r = env.http().post("/admin/login", data={"username": "admin", "password": "wrong password!!"})
    assert r.status_code == 400
    with env.store.read() as db:
        assert db.execute("SELECT COUNT(*) FROM login_failure").fetchone()[0] == 0


def test_a_hostile_host_header_is_not_reflected_into_the_redirect(env):
    r = env.http().get("/admin", headers={"host": "evil.example/x?y"})
    assert r.status_code == 400


def test_the_http_app_has_no_admin_pages(env):
    paths = {r.path for r in env.app("http").routes if isinstance(r, APIRoute)}
    assert not {"/admin/login", "/admin/setup", "/admin/account", "/admin/logout"} & paths


def test_security_headers_are_on_every_response(env):
    r = env.http().get("/")
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "DENY"
    csp = r.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "script-src" not in csp


def test_the_stylesheet_is_served(env):
    assert "color-scheme" in env.http().get("/static/app.css").text


def test_api_docs_are_not_exposed(env):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert env.http().get(path).status_code == 404


# ======================================================================== admin: default deny


def test_admin_pages_need_a_login_and_remember_where_you_were_going(env):
    env.admin()
    https = env.https()
    r = https.get("/admin")
    assert r.status_code == 303 and r.headers["location"] == "/admin/login?next=/admin"
    assert https.get("/admin/account").headers["location"] == "/admin/login?next=/admin/account"


def test_every_admin_route_is_protected_without_being_listed_anywhere(env):
    """Walk the real routes: nothing under /admin except sign-in and setup is reachable signed out."""
    env.admin()
    app = env.app("https")
    client = TestClient(app, base_url="https://testserver", follow_redirects=False)
    checked = 0
    for route in admin_routes(app):
        if route.path in w.PUBLIC_ADMIN_PATHS:
            continue
        for method in route.methods - {"HEAD", "OPTIONS"}:
            r = client.request(method, route.path)
            assert r.status_code in (303, 401), f"{method} {route.path} answered {r.status_code} signed out"
            checked += 1
    assert checked >= 4


def test_every_admin_write_rejects_a_request_without_a_csrf_token(env):
    env.admin()
    client = env.https()
    sign_in(client)
    seen = 0
    for route in admin_routes(env.app("https")):
        for method in route.methods - {"GET", "HEAD", "OPTIONS"}:
            r = client.request(method, route.path, data={"username": "admin", "password": GOOD})
            assert r.status_code == 403, f"{method} {route.path} accepted a write with no CSRF token"
            seen += 1
    assert seen >= 4


def test_nothing_is_registered_under_admin_outside_the_guarded_router(env):
    """A route added straight to the app would skip the guard, so there must be none."""
    stray = [
        r.path for r in env.app("https").routes if isinstance(r, APIRoute) and r.path.startswith("/admin")
    ]
    assert stray == []
    assert {r.path for r in admin_routes(env.app("https"))} >= {
        "/admin/login",
        "/admin/logout",
        "/admin/account",
    }


def test_a_signed_out_write_is_401_not_a_redirect(env):
    env.admin()
    assert env.https().post("/admin/account", data={"current": "x"}).status_code == 401


def test_the_sign_in_page_sets_a_hardened_csrf_cookie(env):
    env.admin()
    r = env.https().get("/admin/login")
    cookie = r.headers["set-cookie"].lower()
    assert r.status_code == 200 and "__host-brief_csrf=" in cookie
    assert "secure" in cookie and "httponly" in cookie and "samesite=strict" in cookie and "path=/" in cookie
    assert "domain" not in cookie
    assert r.headers["cache-control"] == "no-store"


# ======================================================================== CSRF on sign-in


def test_sign_in_without_a_csrf_token_is_rejected(env):
    env.admin()
    c = env.https()
    c.get("/admin/login")
    assert c.post("/admin/login", data={"username": "admin", "password": GOOD}).status_code == 403


def test_sign_in_with_the_wrong_csrf_token_is_rejected(env):
    env.admin()
    c = env.https()
    c.get("/admin/login")
    r = c.post("/admin/login", data={"username": "admin", "password": GOOD, "csrf_token": "forged"})
    assert r.status_code == 403


def test_a_request_from_another_site_is_rejected_even_with_a_valid_token(env):
    env.admin()
    c = env.https()
    token = csrf_from(c.get("/admin/login"))
    data = {"username": "admin", "password": GOOD, "csrf_token": token}
    for origin in ("https://evil.example", "null", "http://testserver"):
        assert c.post("/admin/login", data=data, headers={"origin": origin}).status_code == 403
    assert (
        c.post("/admin/login", data=data, headers={"referer": "https://evil.example/page"}).status_code == 403
    )
    ok = c.post("/admin/login", data=data, headers={"origin": "https://testserver"})
    assert ok.status_code == 303


def test_a_browser_that_sends_origin_null_on_a_same_site_post_can_still_sign_in(env):
    """Chromium sends "Origin: null" on form posts under Referrer-Policy: no-referrer. The browser's own
    Sec-Fetch-Site header (which a page cannot forge) vouches for a post that really came from this site."""
    env.admin()
    c = env.https()
    token = csrf_from(c.get("/admin/login"))
    data = {"username": "admin", "password": GOOD, "csrf_token": token}
    for site in ("cross-site", "same-site", "none", None):
        headers = {"origin": "null", **({"sec-fetch-site": site} if site else {})}
        assert c.post("/admin/login", data=data, headers=headers).status_code == 403, site
    ok = c.post("/admin/login", data=data, headers={"origin": "null", "sec-fetch-site": "same-origin"})
    assert ok.status_code == 303


def test_the_referrer_policy_lets_the_browser_send_a_real_origin_to_this_site(env):
    assert env.https().get("/admin/login").headers["referrer-policy"] == "same-origin"


# ======================================================================== signing in and out


def test_a_good_login_sets_a_hardened_session_cookie_and_opens_the_admin_page(env):
    env.admin()
    c = env.https()
    r = sign_in(c)
    cookie = r.headers["set-cookie"].lower()
    assert r.status_code == 303 and r.headers["location"] == "/admin"
    assert "__host-brief_session=" in cookie and "secure" in cookie and "httponly" in cookie
    assert "samesite=strict" in cookie and "path=/" in cookie and "domain" not in cookie
    page = c.get("/admin")
    assert page.status_code == 200 and "Overview" in page.text and "admin" in page.text


def test_the_session_cookie_value_is_not_in_the_database(env):
    env.admin()
    c = env.https()
    sign_in(c)
    token = c.cookies.get("__Host-brief_session")
    with env.store.read() as db:
        assert token not in "".join(str(tuple(r)) for r in db.execute("SELECT * FROM session"))


def test_wrong_passwords_and_unknown_users_get_the_same_answer(env):
    env.admin()
    a = sign_in(env.https(), "not the password!")
    b = sign_in(env.https(), "not the password!", username="nobody")
    assert a.status_code == b.status_code == 401
    assert "Invalid username or password." in a.text and "Invalid username or password." in b.text


def test_repeated_failed_logins_are_throttled_and_even_the_right_password_waits(env):
    env.admin()
    c = env.https()
    for _ in range(FAIL_FREE):
        assert sign_in(c, "wrong password here").status_code == 401
    r = sign_in(c, GOOD)
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0
    assert "Too many failed attempts" in r.text


def test_after_sign_in_you_go_back_to_the_page_you_wanted_but_never_off_site(env):
    env.admin()
    cases = (
        ("/admin/account", "/admin/account"),
        ("https://evil.example/", "/admin"),
        ("//evil.example", "/admin"),
        ("/elsewhere", "/admin"),
    )
    for wanted, expected in cases:
        c = env.https()
        token = csrf_from(c.get("/admin/login"))
        data = {"username": "admin", "password": GOOD, "csrf_token": token, "next": wanted}
        assert c.post("/admin/login", data=data).headers["location"] == expected


def test_signing_out_needs_the_csrf_token_ends_the_session_and_clears_the_cookie(env):
    env.admin()
    c = env.https()
    sign_in(c)
    token = csrf_from(c.get("/admin/account"))
    assert c.post("/admin/logout", data={}).status_code == 403
    r = c.post("/admin/logout", data={"csrf_token": token})
    assert r.status_code == 303 and "max-age=0" in r.headers["set-cookie"].lower()
    assert c.get("/admin").status_code == 303
    assert env.https().get("/admin/logout").status_code == 405


def test_the_csrf_header_works_for_script_clients_like_htmx(env):
    env.admin()
    c = env.https()
    sign_in(c)
    token = csrf_from(c.get("/admin/account"))
    assert c.post("/admin/logout", headers={"X-CSRF-Token": token}).status_code == 303


def test_a_stolen_csrf_token_from_another_session_does_not_work(env):
    env.admin()
    one, two = env.https(), env.https()
    sign_in(one)
    sign_in(two)
    token_one = csrf_from(one.get("/admin/account"))
    assert two.post("/admin/logout", data={"csrf_token": token_one}).status_code == 403


# ======================================================================== account and setup


def test_changing_the_password_checks_the_old_one_and_signs_you_out_everywhere(env):
    env.admin()
    c, other = env.https(), env.https()
    sign_in(c)
    sign_in(other)
    token = csrf_from(c.get("/admin/account"))
    form = {"csrf_token": token, "current": GOOD, "password": NEW, "confirm": NEW}
    assert c.post("/admin/account", data={**form, "current": "wrong wrong wrong"}).status_code == 401
    assert c.post("/admin/account", data={**form, "confirm": "different"}).status_code == 400
    assert c.post("/admin/account", data={**form, "password": "short", "confirm": "short"}).status_code == 400
    r = c.post("/admin/account", data=form)
    assert r.status_code == 303 and "/admin/login" in r.headers["location"]
    assert c.get("/admin").status_code == 303 and other.get("/admin").status_code == 303
    assert sign_in(env.https(), NEW).status_code == 303


def test_first_run_setup_creates_the_admin_with_the_one_time_token(env):
    token = env.auth.issue_setup_token()
    c = env.https()
    page = c.get("/admin/setup")
    assert page.status_code == 200 and "First-time setup" in page.text
    data = {
        "csrf_token": csrf_from(page),
        "token": token,
        "username": "admin",
        "password": GOOD,
        "confirm": GOOD,
    }
    assert c.post("/admin/setup", data={**data, "token": "wrong"}).status_code == 401
    assert c.post("/admin/setup", data={**data, "confirm": "nope"}).status_code == 400
    r = c.post("/admin/setup", data=data)
    assert r.status_code == 303 and "/admin/login" in r.headers["location"]
    assert sign_in(env.https()).status_code == 303
    assert env.https().get("/admin/setup").status_code == 404
    assert env.https().post("/admin/setup", data=data).status_code in (403, 404)


def test_setup_attempts_with_bad_tokens_are_throttled(env):
    env.auth.issue_setup_token()
    c = env.https()
    page = c.get("/admin/setup")
    data = {
        "csrf_token": csrf_from(page),
        "token": "wrong",
        "username": "admin",
        "password": GOOD,
        "confirm": GOOD,
    }
    for _ in range(FAIL_FREE):
        assert c.post("/admin/setup", data=data).status_code == 401
    assert c.post("/admin/setup", data=data).status_code == 429


def test_the_setup_token_is_announced_in_the_log_once_and_only_without_an_admin(
    tmp_path, monkeypatch, capsys
):
    e = Env(tmp_path, monkeypatch)
    e.settings = Settings(
        state_dir=tmp_path, announce_setup=True, advertised="nas.example", https_public_port=8443
    )
    with TestClient(e.app("https")):
        pass
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines() if x.startswith("{")]
    (announce,) = [x for x in lines if x["event"] == "setup_token"]
    assert announce["url"] == "https://nas.example:8443/admin/setup" and len(announce["token"]) >= 24
    e.admin()
    with TestClient(e.app("https")):
        pass
    assert "setup_token" not in capsys.readouterr().out
    e.store.close()


def test_the_http_app_never_announces_a_token(tmp_path, monkeypatch, capsys):
    e = Env(tmp_path, monkeypatch)
    e.settings = Settings(state_dir=tmp_path, announce_setup=True)
    with TestClient(e.app("http")):
        pass
    assert "setup_token" not in capsys.readouterr().out
    e.store.close()


def test_pages_escape_user_input(env):
    env.admin()
    r = sign_in(env.https(), "x", username='"><script>alert(1)</script>')
    assert "<script>alert(1)</script>" not in r.text


def test_sessions_expire_after_idle_time(env):
    class Clock:
        now = datetime.now(UTC)

        def __call__(self):
            return self.now

    clock = Clock()
    env.auth.clock = clock
    env.admin()
    c = env.https()
    sign_in(c)
    assert c.get("/admin").status_code == 200
    clock.now += timedelta(minutes=31)
    assert c.get("/admin").status_code == 303


def test_the_app_factories_read_their_settings_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIEF_STATE", str(tmp_path))
    monkeypatch.setenv("BRIEF_CONFIG_DIR", str(tmp_path / "none"))
    monkeypatch.setenv("BRIEF_DEFAULTS_DIR", str(tmp_path / "none"))
    monkeypatch.setattr(health, "receiver_ok", lambda port: True)
    assert TestClient(w.create_http_app()).get("/healthz").status_code == 200
    assert TestClient(w.create_https_app(), base_url="https://testserver").get("/healthz").status_code == 200
