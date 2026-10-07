"""Admin: the systems form editor, backups, and downloads of reports and exports.

Every route is on the guarded admin router. The systems form saves through the config editor (validated,
versioned, audited). A backup download and a config restore are audited too. Downloads are by fixed kind
and a strictly matched file name, never by a path.
"""

import re
from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from starlette.background import BackgroundTask

from brief import admin_data, backup, configedit, sysedit
from brief.admin import Deps
from brief.config import ConfigError
from brief.jsonlog import log

HOST = re.compile(r"^[A-Za-z0-9.:_-]{1,80}$")
DONE = {"saved": "Saved. The pipeline picks the new systems up on its next cycle."}


def register(admin: APIRouter, d: Deps) -> None:
    def state():
        return d.settings.state_dir

    # ------------------------------------------------------------------ systems form

    def systems_page(
        request: Request, status: int = 200, rows=None, default_system=None, **extra
    ) -> HTMLResponse:
        text, customised = configedit.current("systems.yaml")
        saved_rows, saved_default = sysedit.rows_from_text(text) if customised else ([], "other")
        rows = saved_rows if rows is None else rows
        add = extra.pop("add", "")
        if add and HOST.match(add):
            rows = [*rows, sysedit.blank_row(add)]
        rows = [*rows, *(sysedit.blank_row() for _ in range(sysedit.EXTRA_BLANK_ROWS))]
        now = datetime.now(UTC)
        unknown = [
            r["host"] for r in admin_data.sources(d.store, d.current_status()[0], now) if not r["known"]
        ][:20]
        return d.render(
            request,
            "admin_systems.html",
            status,
            csrf=d.session(request).csrf,
            active="systems",
            rows=rows,
            default_system=saved_default if default_system is None else default_system,
            base=configedit.digest(text),
            unknown=unknown,
            customised=customised,
            **extra,
        )

    @admin.get("/systems", response_class=HTMLResponse)
    def systems(request: Request, add: str = "", done: str = ""):
        return systems_page(request, add=add, notice=DONE.get(done))

    @admin.post("/systems")
    async def systems_save(request: Request):
        session = d.session(request)
        form = await request.form()
        base = str(form.get("base", ""))
        text, customised = configedit.current("systems.yaml")
        existing, _ = sysedit.rows_from_text(text) if customised else ([], "other")
        try:
            rows = sysedit.rows_from_form(form, existing)
        except ValueError:
            return Response("Not a valid form.", status_code=400)
        default_system = str(form.get("default_system", "other")).strip() or "other"
        try:
            new_text = sysedit.build_text(rows, default_system)
            configedit.save(d.store, "systems.yaml", new_text, session.username, base, state())
        except (configedit.EditError, ConfigError) as exc:
            status = getattr(exc, "status", 422)
            return systems_page(request, status, rows=rows, default_system=default_system, error=str(exc))
        return RedirectResponse("/admin/systems?done=saved", status_code=303)

    # ------------------------------------------------------------------ backups and downloads

    def backups_page(request: Request, status: int = 200, **extra) -> HTMLResponse:
        return d.render(
            request,
            "admin_backups.html",
            status,
            csrf=d.session(request).csrf,
            active="backups",
            files=backup.reports(d.settings.out_dir),
            max_mb=backup.MAX_UPLOAD // (1024 * 1024),
            **extra,
        )

    @admin.get("/backups", response_class=HTMLResponse)
    def backups(request: Request):
        return backups_page(request)

    @admin.get("/backups/download")
    def backup_download(request: Request):
        session = d.session(request)
        try:
            path = backup.create(state(), None)
        except Exception as exc:
            log("backup_failed", error=f"{type(exc).__name__}: {exc}")
            return Response("The backup could not be made.", status_code=500, media_type="text/plain")
        d.store.audit(f"admin:{session.username}", "backup.download", f"{path.stat().st_size} bytes")
        name = f"homelab-brief-backup-{datetime.now(UTC):%Y%m%d-%H%M%S}.tar.gz"
        return FileResponse(
            path,
            media_type="application/gzip",
            filename=name,
            headers={"Cache-Control": "no-store"},
            background=BackgroundTask(backup.discard, path),
        )

    @admin.post("/backups/restore")
    async def backup_restore(request: Request):
        session = d.session(request)
        form = await request.form()
        upload = form.get("backup")
        if upload is None or not hasattr(upload, "read"):
            return backups_page(request, 422, error="Choose a backup file first.")
        data = await upload.read(backup.MAX_UPLOAD + 1)
        try:
            result = backup.restore_config(d.store, data, f"admin:{session.username}", state())
        except (backup.BackupError, configedit.EditError) as exc:
            return backups_page(request, 422, error=str(exc))
        d.store.audit(
            f"admin:{session.username}",
            "backup.restore",
            f"applied {', '.join(result.applied) or 'nothing'}; unchanged {len(result.unchanged)}",
        )
        return backups_page(request, restored=result)

    @admin.get("/files/{kind}/{name}")
    def download(kind: str, name: str):
        path = backup.report_path(d.settings.out_dir, kind, name)
        if path is None:
            return Response("Not found.", status_code=404, media_type="text/plain")
        media = "text/csv; charset=utf-8" if path.suffix == ".csv" else "text/markdown; charset=utf-8"
        return FileResponse(
            path,
            media_type=media,
            filename=f"{name}-digest.md" if kind == "brief" else name,
        )
