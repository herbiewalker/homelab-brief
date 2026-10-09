"""The web app: read-only views on HTTP, and the admin area on HTTPS only.

Two apps are built from one codebase. The HTTP app holds only the read-only routes, so there is no
admin route there to protect (a request for /admin is redirected to HTTPS, never answered). The
HTTPS app adds /admin.

Security is enforced in one place, a router-level guard on /admin, so a new admin route is protected
by default instead of by remembering to opt in:
  - a signed-in session is required for everything except the sign-in and first-run setup pages;
  - every write (any method but GET, HEAD, OPTIONS) needs a CSRF token that matches the session
    (or, before sign-in, the double-submit cookie), and a browser's Origin or Referer must be us.
Cookies are __Host- prefixed, Secure, HttpOnly, and SameSite=Strict.
"""

import hmac
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlsplit

import jinja2
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from brief import (
    __version__,
    admin_ai,
    admin_config,
    admin_manage,
    admin_map,
    admin_network,
    admin_setup,
    admin_tools,
    api,
    health,
    mapview,
    registry,
    selfmon,
)
from brief import admin as admin_pages
from brief.auth import Auth, AuthError, BadCredentials, Session, Throttled
from brief.config import ConfigError, config_file
from brief.jsonlog import log
from brief.learning import learning_status
from brief.metrics import CONTENT_TYPE as PROMETHEUS_TYPE
from brief.metrics import render as metrics_text
from brief.netinfo import network_info, with_detected_address
from brief.outputs import read_json
from brief.settings import Settings
from brief.status import build_status
from brief.store import Store
from brief.systems import Systems

HERE = Path(__file__).parent
COOKIE_SESSION = "__Host-brief_session"
COOKIE_CSRF = "__Host-brief_csrf"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
PUBLIC_ADMIN_PATHS = {"/admin/login", "/admin/setup"}
VIEWS = {
    "brief.html": "text/html; charset=utf-8",
    "board.html": "text/html; charset=utf-8",
    "status.png": "image/png",
}
MAX_BODY = 5 * 1024 * 1024  # the largest write the admin accepts (a config text, a small backup upload)
HOST_RE = re.compile(r"^[A-Za-z0-9.\-]+$")
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",  # no-referrer makes Chromium send "Origin: null" on form posts
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'self'; img-src 'self'; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'none'"
    ),
}
# The admin pages run two small scripts of ours (htmx and Alpine, vendored): scripts and requests from this
# origin only, no inline script, no eval.
ADMIN_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
    "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
)


# Cytoscape sets inline style attributes on its canvas container, so the map pages (and only those) allow
# inline styles; scripts stay limited to our own origin.
MAP_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
)
ADMIN_MAP_CSP = ADMIN_CSP.replace("style-src 'self'", "style-src 'self' 'unsafe-inline'")


class LoginRequired(Exception):
    pass


class Forbidden(Exception):
    def __init__(self, reason: str):
        self.reason = reason


def client_ip(request: Request) -> str:
    # Only the direct peer is trusted: this is a LAN service with no proxy in front of it.
    return request.client.host if request.client else "unknown"


def _uptime(seconds: float) -> str:
    s = int(seconds)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return " ".join(p for p in (f"{d}d" if d else "", f"{h}h" if h or d else "", f"{m}m", f"{s}s") if p)


def safe_next(value: str | None) -> str:
    """Only send people to a page inside /admin after sign-in, never off-site."""
    if value and value.startswith("/admin") and "//" not in value and "\\" not in value and "\n" not in value:
        return value
    return "/admin"


def origin_ok(request: Request) -> bool:
    """A browser's Origin (or Referer) must be this site. Clients that send neither are not browsers."""
    origin = request.headers.get("origin")
    if origin is None:
        referer = request.headers.get("referer")
        if referer is None:
            return True
        parts = urlsplit(referer)
        origin = f"{parts.scheme}://{parts.netloc}"
    if origin == "null":
        # Some browsers send "null" even for a same-site form post. Sec-Fetch-Site is set by the browser
        # itself (a page cannot forge it), so "same-origin" is as good as a matching Origin.
        return request.headers.get("sec-fetch-site") == "same-origin"
    parts = urlsplit(origin)
    # The scheme counts too: a plain-HTTP page claiming to be this site is not this site.
    return (
        parts.scheme == request.url.scheme
        and parts.netloc.lower() == (request.headers.get("host") or "").lower()
    )


def create_app(
    mode: str,
    settings: Settings,
    store: Store | None = None,
    auth: Auth | None = None,
    started: float | None = None,
) -> FastAPI:
    """Build the app. mode is "http" (read-only views) or "https" (those plus /admin)."""
    if mode not in ("http", "https"):
        raise ValueError("mode must be http or https")
    store = store or Store(settings.state_dir / "baseline.db")
    auth = auth or Auth(store)
    templates = jinja2.Environment(loader=jinja2.FileSystemLoader(HERE / "templates"), autoescape=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if mode == "https" and settings.announce_setup:
            token = auth.issue_setup_token()
            if token:
                host = settings.advertised or "<this-host>"
                port = "" if settings.https_public_port == 443 else f":{settings.https_public_port}"
                log(
                    "setup_token",
                    raw=True,  # the person has to read this one to act on it
                    token=token,
                    url=f"https://{host}{port}/admin/setup",
                    note="No admin account exists yet. Open the URL and use this one-time token, "
                    "or run `docker exec -it homelab-brief brief admin set-password`.",
                )
        yield

    app = FastAPI(title="homelab-brief", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.settings, app.state.store, app.state.auth = settings, store, auth
    app.state.started = time.monotonic() if started is None else started
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    def render(request: Request, name: str, status: int = 200, **ctx) -> HTMLResponse:
        session = getattr(request.state, "session", None)
        ctx.setdefault("nav", admin_pages.NAV)
        ctx.setdefault("active", "")
        html = templates.get_template(name).render(session=session, csrf=ctx.pop("csrf", None), **ctx)
        return HTMLResponse(html, status_code=status)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        for key, value in SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)  # a view may set its own (inline CSS, no scripts)
        if request.url.path.startswith("/admin"):
            response.headers["Cache-Control"] = "no-store"
            map_page = request.url.path == "/admin/map"
            response.headers["Content-Security-Policy"] = ADMIN_MAP_CSP if map_page else ADMIN_CSP
        return response

    # ------------------------------------------------------------------ read-only views

    def status_data() -> dict:
        now = datetime.now(UTC)
        uptime = time.monotonic() - app.state.started
        report = health.check_health(store, settings.syslog_port, settings.interval, uptime, now)
        cutoff = now - timedelta(hours=24)
        key = f"{cutoff:%Y-%m-%d}{cutoff.hour:02d}"  # compares like (day, hour) pairs: 2026-10-0313
        with store.read() as db:
            counted = db.execute(
                "SELECT COALESCE(SUM(n), 0) FROM hourly WHERE day || printf('%02d', hour) >= ?", (key,)
            ).fetchone()[0]
            suppressed = db.execute(
                "SELECT COALESCE(SUM(n), 0) FROM suppressed WHERE day || printf('%02d', hour) >= ?", (key,)
            ).fetchone()[0]
            signatures = db.execute("SELECT COUNT(*) FROM signature").fetchone()[0]
            hosts = db.execute("SELECT COUNT(*), COALESCE(SUM(known = 0), 0) FROM host").fetchone()
            live = db.execute("SELECT value FROM meta WHERE key = 'live_since'").fetchone()
            cycle = db.execute(
                "SELECT ts FROM metric WHERE metric_id = 'ingest.cycle_seconds' ORDER BY ts DESC LIMIT 1"
            ).fetchone()
        return {
            "health": report,
            "uptime_s": uptime,
            "stats": {
                "counted": counted,
                "suppressed": suppressed,
                "signatures": signatures,
                "hosts": hosts[0],
                "unknown_hosts": hosts[1],
                "last_cycle": cycle["ts"] if cycle else None,
            },
            "learning": learning_status(live["value"] if live else None, now),
        }

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        uptime = time.monotonic() - app.state.started
        report = health.check_health(store, settings.syslog_port, settings.interval, uptime)
        report.update(version=__version__, uptime_s=int(uptime))
        return JSONResponse(report, status_code=200 if report["status"] == "ok" else 503)

    def load_systems() -> Systems:
        try:
            return Systems.load(config_file("systems.yaml"))
        except (ConfigError, OSError):
            return Systems()

    def current_status() -> tuple[dict, Systems, list[dict]]:
        """The status document computed now, with the systems and sensors it is read against."""
        systems = load_systems()
        doc = build_status(
            store,
            systems,
            settings.syslog_port,
            settings.interval,
            time.monotonic() - app.state.started,
            network=network_info(settings),
            previous=read_json(settings.out_dir / "latest" / "status.json"),
            state_dir=settings.state_dir,
        )
        try:
            metrics = registry.load(None, systems)
        except (ConfigError, OSError):
            metrics = []
        return doc, systems, metrics

    @app.get("/status.json")
    def status_json() -> JSONResponse:
        """The same document the worker writes hourly, computed now (read-only, no login)."""
        return JSONResponse(current_status()[0], headers={"Cache-Control": "no-cache"})

    @app.get("/metrics")
    def prometheus() -> Response:
        doc, _, metrics = current_status()
        sources = selfmon.source_rates(store, datetime.now(UTC))
        text = metrics_text(doc, metrics, __version__, api.open_alert_count(store), sources)
        return Response(text, media_type=PROMETHEUS_TYPE, headers={"Cache-Control": "no-cache"})

    def map_doc() -> dict | None:
        status, systems, _ = current_status()
        return mapview.current_map(store, settings.state_dir, status, systems)

    app.state.api_router = api.build_router(
        current_status, store, map_doc
    )  # kept so tests can walk its routes
    app.include_router(app.state.api_router)

    @app.get("/map.svg")
    def map_svg() -> Response:
        doc = map_doc()
        if doc is None:
            return Response("The network map is switched off.", status_code=404, media_type="text/plain")
        return Response(
            mapview.svg_of(doc),
            media_type="image/svg+xml",
            headers={
                "Cache-Control": "no-cache",
                "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
            },
        )

    @app.get("/map.html", response_class=HTMLResponse)
    def map_page(request: Request) -> HTMLResponse:
        page = render(request, "map.html", save_url=None)
        page.headers["Content-Security-Policy"] = MAP_CSP
        return page

    @app.get("/", response_class=HTMLResponse)
    def status_page(request: Request) -> HTMLResponse:
        data = status_data()
        return render(request, "status.html", version=__version__, uptime=_uptime(data["uptime_s"]), **data)

    def static_view(name: str, media: str) -> None:
        """One generated file from /out/latest, by fixed name only (no path comes from the request)."""

        @app.get(f"/{name}", name=f"view_{name}")
        def view() -> Response:
            try:
                data = (settings.out_dir / "latest" / name).read_bytes()
            except OSError:
                return Response("Not generated yet.", status_code=404, media_type="text/plain")
            headers = {"Cache-Control": "no-cache"}
            if media.startswith("text/html"):  # inline CSS only: still no scripts, frames, or requests out
                headers["Content-Security-Policy"] = (
                    "default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; "
                    "frame-ancestors 'none'; base-uri 'none'"
                )
            return Response(data, media_type=media, headers=headers)

    for view_name, view_media in VIEWS.items():
        static_view(view_name, view_media)

    # ------------------------------------------------------------------ HTTP: never serve /admin

    if mode == "http":

        @app.api_route("/admin", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"])
        @app.api_route("/admin/{rest:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"])
        def admin_needs_https(request: Request, rest: str = "") -> Response:
            if request.method not in ("GET", "HEAD"):
                return Response("The admin area is HTTPS only. Nothing was processed.", status_code=400)
            host = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]")
            if not host or not HOST_RE.match(host):
                return Response("Use HTTPS for the admin area.", status_code=400)
            port = "" if settings.https_public_port == 443 else f":{settings.https_public_port}"
            query = f"?{request.url.query}" if request.url.query else ""
            return RedirectResponse(f"https://{host}{port}{quote(request.url.path)}{query}", status_code=308)

        return app

    # ------------------------------------------------------------------ HTTPS: the admin area

    def current_session(request: Request) -> Session | None:
        if not hasattr(request.state, "session"):
            request.state.session = auth.session(request.cookies.get(COOKIE_SESSION))
        return request.state.session

    async def guard(request: Request) -> None:
        session = current_session(request)
        path = request.url.path.rstrip("/") or "/"
        public = path in PUBLIC_ADMIN_PATHS
        if session is None and not public:
            raise LoginRequired
        if request.method not in SAFE_METHODS:
            if not origin_ok(request):
                raise Forbidden("cross-site request")
            if (
                int(request.headers.get("content-length") or 0) > MAX_BODY
            ):  # before anything is read or spooled
                raise Forbidden("request too large")
            form = await request.form()
            expected = session.csrf if session else request.cookies.get(COOKIE_CSRF)
            provided = request.headers.get("x-csrf-token") or form.get("csrf_token")
            if not (expected and provided and hmac.compare_digest(str(expected), str(provided))):
                raise Forbidden("missing or wrong CSRF token")

    @app.exception_handler(LoginRequired)
    async def need_login(request: Request, exc: LoginRequired) -> Response:
        if request.method in SAFE_METHODS:
            target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
            return RedirectResponse(
                f"/admin/login?next={quote(safe_next(target), safe='/')}", status_code=303
            )
        return Response("Sign in first.", status_code=401)

    @app.exception_handler(Forbidden)
    async def forbidden(request: Request, exc: Forbidden) -> Response:
        log("csrf_rejected", path=request.url.path, reason=exc.reason)
        return Response("Request rejected.", status_code=403)

    admin = APIRouter(prefix="/admin", dependencies=[Depends(guard)])

    def page_csrf(request: Request) -> tuple[str, bool]:
        """The CSRF token for a form and whether a cookie has to be set for it (before sign-in)."""
        session = current_session(request)
        if session:
            return session.csrf, False
        existing = request.cookies.get(COOKIE_CSRF)
        return (existing, False) if existing else (secrets.token_urlsafe(24), True)

    def with_csrf_cookie(response: Response, token: str, new: bool) -> Response:
        if new:
            response.set_cookie(COOKIE_CSRF, token, secure=True, httponly=True, samesite="strict", path="/")
        return response

    def set_session_cookie(response: Response, token: str) -> None:
        response.set_cookie(COOKIE_SESSION, token, secure=True, httponly=True, samesite="strict", path="/")

    @admin.get("/login", response_class=HTMLResponse)
    def login_form(request: Request, next: str = "/admin", notice: str | None = None):
        if current_session(request):
            return RedirectResponse("/admin", status_code=303)
        token, new = page_csrf(request)
        return with_csrf_cookie(
            render(request, "login.html", csrf=token, next=safe_next(next), error=None, notice=notice),
            token,
            new,
        )

    @admin.post("/login")
    async def login_submit(request: Request):
        form = await request.form()
        username, password = str(form.get("username", "")), str(form.get("password", ""))
        token, new = page_csrf(request)
        target = safe_next(str(form.get("next", "")))
        try:
            cookie, session = auth.login(username, password, client_ip(request))
        except Throttled as exc:
            response = render(
                request,
                "login.html",
                429,
                csrf=token,
                next=target,
                notice=None,
                error=f"Too many failed attempts. Try again in {exc.retry_after} seconds.",
            )
            response.headers["Retry-After"] = str(exc.retry_after)
            return with_csrf_cookie(response, token, new)
        except BadCredentials as exc:
            return with_csrf_cookie(
                render(request, "login.html", 401, csrf=token, next=target, error=str(exc), notice=None),
                token,
                new,
            )
        response = RedirectResponse(target, status_code=303)
        set_session_cookie(response, cookie)
        response.delete_cookie(COOKIE_CSRF, path="/", secure=True, httponly=True, samesite="strict")
        return response

    @admin.post("/logout")
    async def logout(request: Request):
        session = current_session(request)
        auth.logout(request.cookies.get(COOKIE_SESSION, ""), session)
        response = RedirectResponse("/admin/login?notice=" + quote("Signed out."), status_code=303)
        response.delete_cookie(COOKIE_SESSION, path="/", secure=True, httponly=True, samesite="strict")
        return response

    @admin.get("/setup", response_class=HTMLResponse)
    def setup_form(request: Request):
        if auth.has_admin():
            return Response("Not found.", status_code=404)
        token, new = page_csrf(request)
        return with_csrf_cookie(render(request, "setup.html", csrf=token, error=None), token, new)

    @admin.post("/setup")
    async def setup_submit(request: Request):
        if auth.has_admin():
            return Response("Not found.", status_code=404)
        form = await request.form()
        token, new = page_csrf(request)

        def again(status: int, error: str):
            return with_csrf_cookie(
                render(request, "setup.html", status, csrf=token, error=error), token, new
            )

        if form.get("password") != form.get("confirm"):
            return again(400, "The two passwords do not match.")
        try:
            auth.setup_admin(
                str(form.get("token", "")), str(form.get("username", "")), str(form.get("password", "")),
                client_ip(request),
            )  # fmt: skip
        except Throttled as exc:
            return again(429, f"Too many attempts. Try again in {exc.retry_after} seconds.")
        except BadCredentials as exc:
            return again(401, str(exc))
        except AuthError as exc:
            return again(400, str(exc))
        return RedirectResponse("/admin/login?notice=" + quote("Account created. Sign in."), status_code=303)

    deps = admin_pages.Deps(
        store=store,
        settings=settings,
        started=lambda: app.state.started,
        current_status=current_status,
        render=render,
        session=current_session,
    )
    admin_pages.register(admin, deps)
    admin_config.register(admin, deps)
    admin_ai.register(admin, deps)
    admin_network.register(admin, deps)
    admin_map.register(admin, deps)
    admin_tools.register(admin, deps)
    admin_manage.register(admin, deps)
    admin_setup.register(admin, deps)

    @admin.get("/account", response_class=HTMLResponse)
    def account_form(request: Request):
        return render(
            request, "account.html", csrf=current_session(request).csrf, error=None, active="account"
        )

    @admin.post("/account")
    async def account_submit(request: Request):
        session = current_session(request)
        form = await request.form()

        def again(status: int, error: str):
            return render(request, "account.html", status, csrf=session.csrf, error=error, active="account")

        if form.get("password") != form.get("confirm"):
            return again(400, "The two new passwords do not match.")
        try:
            auth.change_password(
                session.username,
                str(form.get("current", "")),
                str(form.get("password", "")),
                client_ip(request),
            )
        except Throttled as exc:
            return again(429, f"Too many attempts. Try again in {exc.retry_after} seconds.")
        except BadCredentials as exc:
            return again(401, str(exc))
        except AuthError as exc:
            return again(400, str(exc))
        response = RedirectResponse(
            "/admin/login?notice=" + quote("Password changed. Sign in again."), status_code=303
        )
        response.delete_cookie(COOKIE_SESSION, path="/", secure=True, httponly=True, samesite="strict")
        return response

    app.state.admin_router = admin  # tests walk it to prove every admin route is behind the guard
    app.include_router(admin)
    return app


def _from_env(mode: str) -> FastAPI:
    return create_app(mode, with_detected_address(Settings.from_env()))


def create_http_app() -> FastAPI:
    return _from_env("http")


def create_https_app() -> FastAPI:
    return _from_env("https")
