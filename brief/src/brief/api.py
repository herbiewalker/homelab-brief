"""The versioned read-only JSON API: `/api/v1/...`.

Every endpoint is a GET and needs no login, like `/status.json`; nothing here changes anything (writes
exist only under `/admin`, behind login, CSRF, and the audit log). All of it is built from the one
status document and registry the MQTT sensors and `/metrics` use, so the interfaces show the same run.
A failed run says so (`run_ok: false`) and carries no values.

  /api/v1             index: version and the endpoints below
  /api/v1/status      the status document (same as /status.json)
  /api/v1/systems     each system with its state and summary
  /api/v1/findings    ranked stories and developing issues from the last daily run
  /api/v1/metrics     every sensor: id, name, unit, kind, and its current value
  /api/v1/alerts      open, acknowledged, and snoozed alerts
  /api/v1/map         the network map: nodes, links, status colours, saved layout (when map.yaml is enabled)
"""

from collections.abc import Callable

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from brief import __version__, alerts, registry
from brief.store import Store
from brief.systems import Systems

SCHEMA = 1
NO_CACHE = {"Cache-Control": "no-cache"}
ENDPOINTS = ("status", "systems", "findings", "metrics", "alerts")


def systems_doc(status: dict, systems: Systems) -> dict:
    states = {s["id"]: s for s in status.get("systems") or []}
    out = []
    for s in systems.systems:
        known = states.get(s.id) or {}
        out.append(
            {
                "id": s.id,
                "name": s.label,
                "kind": s.kind,
                "expected": s.expected,
                "host_count": len(s.hosts),
                "state": known.get("state", "unknown"),
                "summary": known.get("summary", "no daily run yet"),
            }
        )
    return {"schema": SCHEMA, "run_ok": bool(status.get("run_ok")), "systems": out}


def findings_doc(status: dict) -> dict:
    return {
        "schema": SCHEMA,
        "run_ok": bool(status.get("run_ok")),
        "generated_at": status.get("daily_generated_at") or status.get("generated_at"),
        "headline": status.get("headline"),
        "stories": status.get("stories") or [],
        "developing": status.get("developing") or [],
        "quiet": status.get("quiet") or [],
    }


def metrics_doc(status: dict, metrics: list[dict]) -> dict:
    values = registry.values(status, metrics)
    return {
        "schema": SCHEMA,
        "run_ok": bool(status.get("run_ok")),
        "metrics": [
            {
                "id": m["id"],
                "name": m.get("name", m["id"]),
                "group": m.get("group"),
                "kind": m.get("kind", "number"),
                "unit": m.get("unit"),
                "value": values.get(m["id"]),
            }
            for m in metrics
        ],
    }


def alerts_doc(store: Store) -> dict:
    book = alerts.load_book(store, include_resolved=False)
    rows = [
        {
            "id": a.finding_id,
            "kind": a.kind,
            "title": a.title,
            "level": a.level,
            "route": a.route,
            "state": a.state,
            "opened": a.opened,
            "last_sent": a.last_sent,
            "snooze_until": a.snooze_until,
            "pushed": a.pushed,
        }
        for a in sorted(book.values(), key=lambda a: a.opened or "", reverse=True)
    ]
    return {"schema": SCHEMA, "alerts": rows}


def open_alert_count(store: Store) -> int:
    with store.read() as db:
        return db.execute("SELECT COUNT(*) FROM alert WHERE state != 'resolved'").fetchone()[0]


def build_router(
    current: Callable[[], tuple[dict, Systems, list[dict]]],
    store: Store,
    map_doc: Callable[[], dict | None] | None = None,
) -> APIRouter:
    """`current()` gives (status document, systems, registry metrics) for this moment."""
    router = APIRouter(prefix="/api/v1")

    def reply(doc: dict) -> JSONResponse:
        return JSONResponse(doc, headers=NO_CACHE)

    @router.get("")
    def index() -> JSONResponse:
        endpoints = [*ENDPOINTS, "map"] if map_doc and map_doc() is not None else ENDPOINTS
        return reply(
            {"schema": SCHEMA, "version": __version__, "endpoints": [f"/api/v1/{e}" for e in endpoints]}
        )

    @router.get("/status")
    def status() -> JSONResponse:
        return reply(current()[0])

    @router.get("/systems")
    def systems() -> JSONResponse:
        doc, known, _ = current()
        return reply(systems_doc(doc, known))

    @router.get("/findings")
    def findings() -> JSONResponse:
        return reply(findings_doc(current()[0]))

    @router.get("/metrics")
    def metrics() -> JSONResponse:
        doc, _, defined = current()
        return reply(metrics_doc(doc, defined))

    @router.get("/alerts")
    def open_alerts() -> JSONResponse:
        return reply(alerts_doc(store))

    if map_doc:

        @router.get("/map")
        def network_map() -> JSONResponse:
            doc = map_doc()
            if doc is None:
                return JSONResponse({"error": "The network map is switched off (map.yaml)."}, status_code=404)
            return reply(doc)

    return router
