"""The config editor pages: list, edit with a diff before saving, history, restore, reset.

Routes only; the rules (validation, history, audit, conflict detection) are in configedit.py. All routes
are on the guarded admin router, so a session is needed for every page and a CSRF token for every write.
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from brief import admin_data, agenteval, configedit, configguard
from brief.admin import Deps

DONE = {
    "saved": "Saved. The pipeline picks it up on its next cycle.",
    "restored": "Restored. The pipeline picks it up on its next cycle.",
    "reset": "Back to the shipped default.",
}


def register(admin: APIRouter, d: Deps) -> None:
    def env():
        return None  # the process environment, like every other reader of config_file()

    def state():
        return d.settings.state_dir

    def editor(request: Request, name: str, text=None, base=None, status=200, **extra) -> HTMLResponse:
        """The editor page. `text` is what is in the box (the user's unsaved edit, or the file)."""
        on_disk, customised = configedit.current(name, env())
        error = configguard.read_errors(state()).get(name)
        return d.render(
            request,
            "admin_config_edit.html",
            status,
            csrf=d.session(request).csrf,
            active="config",
            file=name,
            description=configedit.DESCRIPTIONS[name],
            text=on_disk if text is None else text,
            base=configedit.digest(on_disk) if base is None else base,
            customised=customised,
            versions=configedit.versions(state(), name),
            guard_error=error,
            eval_files=agenteval.GATED,
            error=extra.pop("error", None),
            diff=extra.pop("diff", None),
            notice=extra.pop("notice", None),
            **extra,
        )

    @admin.get("/config", response_class=HTMLResponse)
    def config_list(request: Request):
        errors = configguard.read_errors(state())
        rows = []
        for name in configedit.EDITABLE:
            try:
                _, customised = configedit.current(name, env())
            except configedit.EditError:
                customised = False
            saved = configedit.versions(state(), name)
            rows.append(
                {
                    "name": name,
                    "description": configedit.DESCRIPTIONS[name],
                    "customised": customised,
                    "changed": admin_data.ago(_stamp_time(saved[0].stamp), datetime.now(UTC))
                    if saved
                    else "",
                    "error": errors.get(name),
                }
            )
        return d.render(
            request, "admin_config_list.html", csrf=d.session(request).csrf, active="config", rows=rows
        )

    @admin.get("/config/{name}", response_class=HTMLResponse)
    def config_edit(request: Request, name: str, done: str = ""):
        try:
            return editor(request, name, notice=DONE.get(done))
        except configedit.EditError as exc:
            return Response(str(exc), status_code=exc.status)

    @admin.post("/config/{name}")
    async def config_submit(request: Request, name: str):
        session = d.session(request)
        form = await request.form()
        text, base = str(form.get("text", "")), str(form.get("base", ""))
        action = str(form.get("action", "review"))
        try:
            configedit.checked_name(name)
            text = configedit.normalize(text)
            if action == "save":
                agenteval.OVERRIDE.set(bool(form.get("accept_eval")))
                configedit.save(d.store, name, text, session.username, base, state(), env())
                return RedirectResponse(f"/admin/config/{name}?done=saved", status_code=303)
            problem = configedit.validate_text(name, text)
            if problem:
                return editor(request, name, text, base, 422, error=problem)
            old, _ = configedit.current(name, env())
            if configedit.digest(old) != base:
                raise configedit.EditError(
                    "The file changed while you were editing it. Reload it first.", 409
                )
            if text == old:
                return editor(request, name, text, base, notice="Nothing changed.")
            return editor(request, name, text, base, diff=configedit.diff_lines(old, text), review=True)
        except configedit.EditError as exc:
            if exc.status == 404:
                return Response(str(exc), status_code=404)
            return editor(request, name, text, base, exc.status, error=str(exc))

    @admin.get("/config/{name}/versions/{stamp}", response_class=HTMLResponse)
    def config_version(request: Request, name: str, stamp: str):
        try:
            old = configedit.version_text(state(), name, stamp)
            now, _ = configedit.current(name, env())
        except configedit.EditError as exc:
            return Response(str(exc), status_code=exc.status)
        meta = next((v for v in configedit.versions(state(), name) if v.stamp == stamp), None)
        return d.render(
            request,
            "admin_config_version.html",
            csrf=d.session(request).csrf,
            active="config",
            file=name,
            stamp=stamp,
            meta=meta,
            diff=configedit.diff_lines(now, old),
            same=old == now,
            base=configedit.digest(now),
        )

    @admin.post("/config/{name}/restore")
    async def config_restore(request: Request, name: str):
        session = d.session(request)
        form = await request.form()
        stamp, base = str(form.get("stamp", "")), str(form.get("base", ""))
        try:
            configedit.rollback(d.store, name, stamp, session.username, base, state(), env())
        except configedit.EditError as exc:
            if exc.status == 404:
                return Response(str(exc), status_code=404)
            return editor(request, name, status=exc.status, error=str(exc))
        return RedirectResponse(f"/admin/config/{name}?done=restored", status_code=303)

    @admin.post("/config/{name}/reset")
    async def config_reset(request: Request, name: str):
        session = d.session(request)
        try:
            configedit.reset(d.store, name, session.username, state(), env())
        except configedit.EditError as exc:
            if exc.status == 404:
                return Response(str(exc), status_code=404)
            return editor(request, name, status=exc.status, error=str(exc))
        return RedirectResponse(f"/admin/config/{name}?done=reset", status_code=303)


def _stamp_time(stamp: str) -> str:
    """'20261006T231500Z' -> '2026-10-06T23:15:00Z' (the format ago() reads)."""
    return f"{stamp[0:4]}-{stamp[4:6]}-{stamp[6:8]}T{stamp[9:11]}:{stamp[11:13]}:{stamp[13:15]}Z"
