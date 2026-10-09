"""The admin side of the network map: the page, "Save as default", and the optional draw.io download.

Every route is on the guarded admin router (a session for all, CSRF for every write). The one write is
the default layout file, which is audited. The read-only map lives on the public router in web.py.
"""

import json

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from brief import mapsvg, mapview
from brief.admin import Deps

MAX_LAYOUT_BYTES = 512 * 1024


def register(admin: APIRouter, d: Deps) -> None:
    @admin.get("/map", response_class=HTMLResponse)
    def map_page(request: Request):
        cfg = mapview.map_config()
        return d.render(
            request,
            "admin_map.html",
            csrf=d.session(request).csrf,
            active="map",
            enabled=bool(cfg.get("enabled")),
            drawio=bool(cfg.get("drawio_export")),
            save_url="/admin/map/layout",
        )

    @admin.post("/map/layout")
    async def save_layout(request: Request):
        session = d.session(request)
        raw = await request.body()
        if len(raw) > MAX_LAYOUT_BYTES:
            return Response("That layout is too large.", status_code=413, media_type="text/plain")
        try:
            saved = mapview.save_default_layout(d.settings.state_dir, json.loads(raw))
        except (ValueError, UnicodeDecodeError) as exc:
            return Response(str(exc), status_code=400, media_type="text/plain")
        d.store.audit(f"admin:{session.username}", "map.layout", f"default layout saved: {len(saved)} nodes")
        return Response("Saved.", media_type="text/plain")

    @admin.get("/map/drawio")
    def drawio(request: Request):
        d.session(request)
        status, systems, _ = d.current_status()
        doc = mapview.current_map(d.store, d.settings.state_dir, status, systems)
        if doc is None or not mapview.map_config().get("drawio_export"):
            return Response("The draw.io export is switched off (map.yaml: drawio_export).", status_code=404)
        return Response(
            mapsvg.render_drawio(doc),
            media_type="application/xml",
            headers={"Content-Disposition": 'attachment; filename="map.drawio"'},
        )
