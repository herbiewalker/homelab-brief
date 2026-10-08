"""The first-run wizard and the support bundle download.

The wizard lists the senders the product has seen, with their busiest programs and a kind suggestion,
and turns your answers into systems through the config editor (validated, versioned, audited). The support
bundle is the same file `brief support-bundle` writes, with every name and address replaced.
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from starlette.background import BackgroundTask

from brief import backup, configedit, netinfo, setup, supportbundle, sysedit
from brief.admin import Deps
from brief.config import ConfigError
from brief.jsonlog import log

DONE = {"saved": "Saved. The pipeline picks the new systems up on its next cycle."}
MAX_ROWS = 300


def register(admin: APIRouter, d: Deps) -> None:
    def page(request: Request, status: int = 200, **extra) -> HTMLResponse:
        now = datetime.now(UTC)
        doc, systems, _ = d.current_status()
        found = setup.senders(d.store, doc, now, systems)[:MAX_ROWS]
        net = netinfo.network_info(d.settings)
        text, customised = configedit.current("systems.yaml")
        existing = [r["id"] for r in sysedit.rows_from_text(text)[0]] if customised else []
        invalid = sum(
            1 for name in configedit.EDITABLE if configedit.validate_text(name, configedit.current(name)[0])
        )
        return d.render(
            request,
            "admin_setup.html",
            status,
            csrf=d.session(request).csrf,
            active="wizard",
            rows=found,
            unknown=sum(1 for r in found if not r["known"]),
            existing=existing,
            kinds=setup.KINDS,
            send_to=f"{net['send_syslog_to'] or '<the host address>'}:{d.settings.syslog_port}",
            invalid_files=invalid,
            **extra,
        )

    @admin.get("/wizard", response_class=HTMLResponse)
    def wizard(request: Request, done: str = ""):
        return page(request, notice=DONE.get(done))

    @admin.post("/wizard")
    async def wizard_save(request: Request):
        session = d.session(request)
        form = await request.form()
        assignments = []
        for i in range(min(int(form.get("count", "0") or 0), MAX_ROWS)):
            host = str(form.get(f"host-{i}", "")).strip()
            system = str(form.get(f"system-{i}", "")).strip()
            if host and system:
                assignments.append(setup.Assignment(host, system, "", str(form.get(f"kind-{i}", ""))))
        try:
            setup.apply(d.store, assignments, session.username, d.settings.state_dir)
        except (configedit.EditError, ConfigError) as exc:
            return page(request, getattr(exc, "status", 422), error=str(exc))
        return RedirectResponse("/admin/wizard?done=saved", status_code=303)

    @admin.get("/backups/support-bundle")
    def support_bundle(request: Request):
        session = d.session(request)
        try:
            path, red = supportbundle.build(d.store, d.settings.state_dir, d.settings)
        except supportbundle.BundleError as exc:
            return Response(str(exc), status_code=500, media_type="text/plain")
        except Exception as exc:
            log("support_bundle_failed", error=type(exc).__name__)
            return Response("The support bundle could not be made.", status_code=500, media_type="text/plain")
        d.store.audit(
            f"admin:{session.username}", "support.bundle", f"downloaded; {len(red.map)} values aliased"
        )
        return FileResponse(
            path,
            media_type="application/gzip",
            filename=f"homelab-brief-support-{datetime.now(UTC):%Y%m%d-%H%M%S}.tar.gz",
            headers={"Cache-Control": "no-store"},
            background=BackgroundTask(backup.discard, path),
        )
