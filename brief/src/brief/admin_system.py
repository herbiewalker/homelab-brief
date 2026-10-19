"""Admin: one system's page, and naming a device. Both routes are on the guarded admin router (login and CSRF
checked by it); the one write is audited like every other admin change.
"""

import re
from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from brief import systemdetail
from brief.admin import Deps

SYSTEM_ID = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
HOST = re.compile(r"^[A-Za-z0-9.:_-]{1,80}$")
MAX_ALIAS = 60


def register(admin: APIRouter, d: Deps) -> None:
    def build(system_id: str) -> dict | None:
        status, systems, _ = d.current_status()
        return systemdetail.detail(d.store, systems, status, system_id, datetime.now(UTC))

    @admin.get("/system/{system_id}", response_class=HTMLResponse)
    def system_page(request: Request, system_id: str):
        page = build(system_id) if SYSTEM_ID.match(system_id) else None
        if page is None:
            return d.render(
                request, "error.html", 404, csrf=d.session(request).csrf, active="systems",
                heading="No such system", message="That system is not in systems.yaml or in the status.",
                hint="Systems are named on the Systems page.",
            )  # fmt: skip
        return d.render(request, "admin_system.html", csrf=d.session(request).csrf, active="systems", s=page)

    @admin.post("/system/{system_id}/alias", response_class=HTMLResponse)
    async def alias(request: Request, system_id: str):
        form = await request.form()
        host, name = str(form.get("host", "")), " ".join(str(form.get("alias", "")).split())
        if not SYSTEM_ID.match(system_id) or not HOST.match(host):
            return Response("That is not a system or a device address.", status_code=400)
        if len(name) > MAX_ALIAS:
            return Response(f"A name is at most {MAX_ALIAS} characters.", status_code=422)
        page = build(system_id)
        if page is None or host not in {dev["host"] for dev in page["devices"]}:
            return Response("That device is not in this system.", status_code=404)
        user = f"admin:{d.session(request).username}"
        with d.store.write() as tx:
            tx.set_host_alias(host, name or None, datetime.now(UTC))
            tx.audit(user, "device.rename", f"{host} -> {name or '(its own name)'}")
        page = build(system_id)
        device = next(dev for dev in page["devices"] if dev["host"] == host)
        response = d.render(
            request, "_system_device.html", csrf=d.session(request).csrf, dev=device, s=page, open_now=True
        )
        response.headers["HX-Trigger"] = '{"toast": {"kind": "ok", "text": "Saved the name."}}'
        return response
