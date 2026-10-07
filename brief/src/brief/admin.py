"""The admin pages (HTTPS, behind the guard in web.py): overview, sources, findings and alerts, audit log.

Each page is a thin route over `admin_data`, which does the reading. Every route here is registered on
the guarded admin router, so a session is required for all of them and a CSRF token for every write.
The only writes are an acknowledgement or snooze of an alert, which brief.alerts records in the audit log.
"""

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from brief import __version__, admin_data, alerts, health
from brief.settings import Settings
from brief.store import Store

NAV = [
    ("overview", "/admin", "Overview"),
    ("sources", "/admin/sources", "Sources"),
    ("findings", "/admin/findings", "Findings and alerts"),
    ("network", "/admin/network", "Network"),
    ("systems", "/admin/systems", "Systems"),
    ("config", "/admin/config", "Config"),
    ("backups", "/admin/backups", "Backups and exports"),
    ("signatures", "/admin/signatures", "Signatures"),
    ("logs", "/admin/logs", "Log search"),
    ("replay", "/admin/replay", "Replay and tuning"),
    ("audit", "/admin/audit", "Audit log"),
    ("account", "/admin/account", "Account"),
]
ALERT_ACTIONS = ("ack", "snooze_24h", "snooze_7d")
FINDING_ID = re.compile(r"^[A-Za-z0-9_.:@/ -]{1,300}$")


@dataclass
class Deps:
    store: Store
    settings: Settings
    started: Callable[[], float]  # when the web app started (monotonic seconds)
    current_status: Callable[[], tuple[dict, object, list]]
    render: Callable[..., HTMLResponse]
    session: Callable[[Request], object]


def uptime(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 3600:
        return f"{seconds // 60} min"
    if seconds < 172800:
        return f"{seconds // 3600} h {seconds % 3600 // 60} min"
    return f"{seconds // 86400} days"


def register(admin: APIRouter, d: Deps) -> None:
    def overview_context() -> dict:
        now = datetime.now(UTC)
        status = d.current_status()[0]
        up = time.monotonic() - d.started()
        report = health.check_health(d.store, d.settings.syslog_port, d.settings.interval, up, now)
        return {
            "data": admin_data.overview(
                d.store, d.settings.out_dir, status, report, d.settings.state_dir, now
            ),
            "version": __version__,
            "uptime": uptime(up),
        }

    @admin.get("", response_class=HTMLResponse)
    def overview(request: Request):
        return d.render(
            request, "dashboard.html", csrf=d.session(request).csrf, active="overview", **overview_context()
        )

    @admin.get("/overview/live", response_class=HTMLResponse)
    def overview_live(request: Request):
        return d.render(request, "_overview_live.html", csrf=d.session(request).csrf, **overview_context())

    @admin.get("/sources", response_class=HTMLResponse)
    def sources(request: Request):
        now = datetime.now(UTC)
        rows = admin_data.sources(d.store, d.current_status()[0], now)
        unknown = sum(1 for r in rows if not r["known"])
        return d.render(
            request,
            "admin_sources.html",
            csrf=d.session(request).csrf,
            active="sources",
            rows=rows,
            unknown=unknown,
        )

    @admin.get("/findings", response_class=HTMLResponse)
    def findings(request: Request):
        now = datetime.now(UTC)
        status = d.current_status()[0]
        return d.render(
            request,
            "admin_findings.html",
            csrf=d.session(request).csrf,
            active="findings",
            run_ok=status.get("run_ok"),
            headline=status.get("headline"),
            generated=status.get("daily_generated_at"),
            stories=status.get("stories") or [],
            developing=status.get("developing") or [],
            alerts=admin_data.alert_rows(d.store, now),
        )

    @admin.post("/alerts/act")
    async def alert_action(request: Request):
        session = d.session(request)
        form = await request.form()
        finding_id, action = str(form.get("finding_id", "")), str(form.get("action", ""))
        if action not in ALERT_ACTIONS or not FINDING_ID.match(finding_id):
            return Response("That is not an action on an alert.", status_code=400)
        if finding_id not in alerts.load_book(d.store, include_resolved=True):
            return Response(
                "There is no such alert.", status_code=404
            )  # nothing is recorded for a made-up id
        now = datetime.now(UTC)
        alerts.apply_feedback(d.store, finding_id, action, now, source=f"admin:{session.username}")
        if request.headers.get("hx-request"):
            row = admin_data.alert_row(d.store, finding_id, now)
            if row is None:  # resolved meanwhile: the row simply goes away
                return Response("", media_type="text/html")
            return d.render(request, "_alert_row.html", csrf=session.csrf, a=row)
        return RedirectResponse("/admin/findings", status_code=303)

    @admin.get("/audit", response_class=HTMLResponse)
    def audit(request: Request, user: str = "", q: str = "", before: int | None = None):
        page = admin_data.audit(d.store, user=user, text=q, before=before)
        return d.render(
            request, "admin_audit.html", csrf=d.session(request).csrf, active="audit", user=user, q=q, **page
        )
