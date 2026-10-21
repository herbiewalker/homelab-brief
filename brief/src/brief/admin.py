"""The admin pages (HTTPS, behind the guard in web.py): overview, sources, findings and alerts, audit log.

Each page is a thin route over `admin_data`, which does the reading. Every route here is registered on
the guarded admin router, so a session is required for all of them and a CSRF token for every write.
The only writes are an acknowledgement or snooze of an alert, which brief.alerts records in the audit log.
"""

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from brief import __version__, admin_data, agentnotes, alerts, glossary, health, overviewdata
from brief.settings import Settings
from brief.store import Store

# The side menu: (group, [(page key, address, label, icon)]). A page names its own key as `active`.
NAV_GROUPS = [
    ("Monitor", [
        ("overview", "/admin", "Overview", "activity"),
        ("findings", "/admin/findings", "Findings and alerts", "alert"),
        ("sources", "/admin/sources", "Sources", "radio"),
        ("map", "/admin/map", "Network map", "map"),
    ]),
    ("Configure", [
        ("wizard", "/admin/wizard", "Setup wizard", "wand"),
        ("systems", "/admin/systems", "Systems", "server"),
        ("collectors", "/admin/collectors", "Collectors", "plug"),
        ("network", "/admin/network", "Network and access", "network"),
        ("schedule", "/admin/schedule", "Schedule and retention", "clock"),
        ("notifications", "/admin/notifications", "Notifications", "bell"),
        ("devices", "/admin/devices", "Devices and map", "box"),
        ("display", "/admin/display", "Pages and sensors", "monitor"),
        ("config", "/admin/config", "Config files", "file-text"),
    ]),
    ("Detection", [
        ("ignore", "/admin/ignore", "Ignore rules", "list"),
        ("priorities", "/admin/priorities", "Priorities and alerts", "sliders"),
        ("thresholds", "/admin/thresholds", "Thresholds and advice", "zap"),
    ]),
    ("Intelligence", [
        ("ai", "/admin/ai", "AI and agents", "sparkles"),
    ]),
    ("System", [
        ("backups", "/admin/backups", "Backups and exports", "archive"),
        ("signatures", "/admin/signatures", "Signatures", "list"),
        ("logs", "/admin/logs", "Log search", "search"),
        ("replay", "/admin/replay", "Replay and tuning", "history"),
        ("audit", "/admin/audit", "Audit log", "clock"),
        ("account", "/admin/account", "Account", "user"),
        ("help", "/admin/help", "Help and glossary", "info"),
    ]),
]  # fmt: skip
NAV = [(key, href, label) for _, links in NAV_GROUPS for key, href, label, _ in links]

# The simple menu (the default): the pages most people use. Everything else sits one click away on the
# Settings page, in the full menu ("Show all pages"), and in the Ctrl K palette. Same pages, same addresses.
NAV_SIMPLE = [
    ("overview", "/admin", "Overview", "activity"),
    ("findings", "/admin/findings", "Findings and alerts", "alert"),
    ("systems", "/admin/systems", "Systems", "server"),
    ("brief", "/brief.html", "Today's brief", "file-text"),
    ("sources", "/admin/sources", "Sources", "radio"),
    ("settings", "/admin/settings", "Settings", "sliders"),
]
SIMPLE_KEYS = frozenset(key for key, _, _, _ in NAV_SIMPLE)

ALERT_ACTIONS = ("ack", "snooze_24h", "snooze_7d")
BULK_MAX = 200
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
            "data": overviewdata.build(
                d.store,
                d.settings.out_dir,
                status,
                report,
                d.settings.state_dir,
                now,
                admin_data.shell_status(d.store, d.settings.out_dir)[1],
            ),
            "version": __version__,
            "uptime": uptime(up),
        }

    @admin.get("", response_class=HTMLResponse)
    def overview(request: Request):
        return d.render(
            request, "dashboard.html", csrf=d.session(request).csrf, active="overview", **overview_context()
        )

    @admin.get("/settings", response_class=HTMLResponse)
    def settings_hub(request: Request):
        """Every page that is not in the simple menu, grouped, with one line saying what it is for."""
        groups = [
            (group, [(href, label, glyph, glossary.PAGES.get(key, "")) for key, href, label, glyph in links])
            for group, links in NAV_GROUPS
            for links in [[link for link in links if link[0] not in SIMPLE_KEYS and link[0] != "help"]]
            if links
        ]
        return d.render(
            request, "admin_settings_hub.html", csrf=d.session(request).csrf, active="settings", groups=groups
        )

    @admin.get("/help", response_class=HTMLResponse)
    def help_page(request: Request):
        return d.render(
            request,
            "admin_help.html",
            csrf=d.session(request).csrf,
            active="help",
            terms=glossary.help_terms(),
        )

    @admin.get("/overview/live", response_class=HTMLResponse)
    def overview_live(request: Request):
        return d.render(request, "_overview_live.html", csrf=d.session(request).csrf, **overview_context())

    @admin.get("/sources", response_class=HTMLResponse)
    def sources(request: Request):
        now = datetime.now(UTC)
        rows = admin_data.sources(d.store, d.current_status()[0], now)
        return d.render(
            request,
            "admin_sources.html",
            csrf=d.session(request).csrf,
            active="sources",
            **admin_data.sources_view(rows, dict(request.query_params)),
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
            notes=agentnotes.notes_by_story(
                d.store, [s["id"] for s in status.get("stories") or [] if isinstance(s.get("id"), str)]
            ),
            developing=status.get("developing") or [],
            **admin_data.alerts_view(d.store, now, dict(request.query_params)),
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
            page = "_overview_alert.html" if form.get("compact") else "_alert_row.html"
            return d.render(request, page, csrf=session.csrf, a=row)
        return RedirectResponse("/admin/findings", status_code=303)

    @admin.post("/alerts/bulk")
    async def alert_bulk(request: Request):
        """Acknowledge or snooze every ticked alert, then show the alert list again with the same filters."""
        from urllib.parse import parse_qsl

        session = d.session(request)
        form = await request.form()
        action = str(form.get("action", ""))
        if action not in ALERT_ACTIONS:
            return Response("That is not an action on an alert.", status_code=400)
        ids = [str(i) for i in form.getlist("finding_id")][:BULK_MAX]
        book = alerts.load_book(d.store, include_resolved=True)
        now = datetime.now(UTC)
        done = 0
        for finding_id in ids:
            if FINDING_ID.match(finding_id) and finding_id in book:
                alerts.apply_feedback(d.store, finding_id, action, now, source=f"admin:{session.username}")
                done += 1
        params = dict(parse_qsl(str(form.get("view_query", ""))[:600]))
        words = {
            "ack": "acknowledged",
            "snooze_24h": "snoozed for 24 hours",
            "snooze_7d": "snoozed for 7 days",
        }
        response = d.render(
            request,
            "_alerts_card.html",
            csrf=session.csrf,
            **admin_data.alerts_view(d.store, now, params),
        )
        response.headers["HX-Trigger"] = json.dumps(
            {
                "toast": {
                    "kind": "ok" if done else "info",
                    "text": f"{done} alert{'s' if done != 1 else ''} {words[action]}.",
                }
            }
        )
        return response

    @admin.get("/audit", response_class=HTMLResponse)
    def audit(request: Request, user: str = "", q: str = "", before: int | None = None, kind: str = ""):
        page = admin_data.audit(d.store, user=user, text=q, before=before, kind=kind)
        return d.render(
            request,
            "admin_audit.html",
            csrf=d.session(request).csrf,
            active="audit",
            user=user,
            q=q,
            kind=kind,
            **page,
        )
