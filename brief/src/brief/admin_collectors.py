"""The Collectors page: every Phase 2 data source as a form, so none of it needs the text editor.

Each collector is a card: a switch, its settings (the server list, the key variable names, the options), a Test
button that asks the source whether the connection works, and Run now. Saving writes the values back into
`collectors.yaml` through the settings framework and the config editor, so the file is checked (the same validator
the worker uses), kept as a version with a one-click Undo, and audited, and the text editor shows the same file.
A key is never typed here: a field names the environment variable that holds it and shows whether it is set.
"""

import json
import os

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from brief import collector_specs, configedit, settingsui, yamlscalar
from brief.admin import Deps
from brief.collectors import framework
from brief.config import ConfigError

FILE = "collectors.yaml"


def register(admin: APIRouter, d: Deps) -> None:
    def names() -> tuple[list[str], str | None]:
        try:
            order = list(collector_specs.BY_COLLECTOR)
            return sorted(
                framework.load(), key=lambda n: (order.index(n) if n in order else len(order), n)
            ), None
        except (ConfigError, OSError) as exc:
            return [], str(exc)

    def vars_set(*states: dict) -> dict[str, bool]:
        wanted: set[str] = set()
        for s in states:
            wanted |= {v for v in s["values"].values() if isinstance(v, str) and v}
            for rows in s["rows"].values():
                for row in rows:
                    wanted |= {v for v in row.values() if isinstance(v, str) and v}
        return {v: bool(os.environ.get(v)) for v in wanted if settingsui.VAR_NAME.match(v)}

    def chip(name: str, enabled: bool) -> dict:
        if not enabled:
            return {"kind": "", "text": "Off"}
        last = framework.read_state(d.store).get(name) or {}
        if not last.get("last_run"):
            return {"kind": "info", "text": "On, has not run yet"}
        if last.get("ok"):
            return {"kind": "ok", "text": "Working"}
        return {"kind": "bad", "text": "Failing: " + str(last.get("error") or "see the log")[:80]}

    def card(
        request: Request,
        name: str,
        state: dict | None = None,
        errors: dict | None = None,
        general: str | None = None,
        status: int = 200,
        toast: dict | None = None,
    ) -> HTMLResponse:
        section = collector_specs.BY_COLLECTOR.get(name)
        text, _ = configedit.current(FILE)
        shipped = configedit.starting_text(FILE)
        loaded = settingsui.load(section, text, shipped) if section else None
        state = state or loaded
        response = d.render(
            request,
            "_collector_card.html",
            status,
            csrf=d.session(request).csrf,
            cname=name,
            section=section,
            state=state,
            errors=errors or {},
            general=general,
            base=configedit.digest(text),
            vars_set=vars_set(state) if state else {},
            status_chip=chip(name, bool(state and state["enabled"])),
            action=f"/admin/collectors/{name}",
        )
        if toast:
            response.headers["HX-Trigger"] = json.dumps({"toast": toast})
        return response

    def known(name: str) -> bool:
        return name in names()[0]

    @admin.get("/collectors", response_class=HTMLResponse)
    def collectors_page(request: Request):
        listed, problem = names()
        cards = [card(request, n).body.decode() for n in listed]
        return d.render(
            request,
            "admin_collectors.html",
            csrf=d.session(request).csrf,
            active="collectors",
            cards=cards,
            problem=problem,
        )

    @admin.post("/collectors/{name}/rows", response_class=HTMLResponse)
    async def rows(request: Request, name: str):
        section = collector_specs.BY_COLLECTOR.get(name)
        if not known(name) or section is None:
            return Response("There is no such collector.", status_code=404)
        form = await request.form()
        state = settingsui.posted(section, form)
        settingsui.row_action(section, state, str(form.get("row_action", "")))
        return card(request, name, state=state)

    @admin.post("/collectors/{name}/save", response_class=HTMLResponse)
    async def save(request: Request, name: str):
        section = collector_specs.BY_COLLECTOR.get(name)
        if not known(name) or section is None:
            return Response("There is no such collector.", status_code=404)
        form = await request.form()
        user = f"admin:{d.session(request).username}"
        values, errors, on = settingsui.parse(section, form)
        state = settingsui.posted(section, form)
        if errors:
            return card(
                request,
                name,
                state,
                errors,
                "Some settings need attention. They are marked below; nothing was saved.",
                422,
            )
        text, _ = configedit.current(FILE)
        was_on = settingsui.load(section, text)["enabled"]
        try:
            new = settingsui.apply(section, text, values, on)
            if new == text:
                return card(
                    request, name, toast={"kind": "info", "text": "Nothing to save: no setting was changed."}
                )
            problem = configedit.validate_text(FILE, new)
            if problem:
                return card(request, name, state, {}, problem, 422)
            configedit.save(
                d.store,
                FILE,
                new,
                user,
                str(form.get("base") or configedit.digest(text)),
                d.settings.state_dir,
            )
        except (yamlscalar.EditError, configedit.EditError) as exc:
            return card(request, name, state, {}, str(exc), getattr(exc, "status", 422))
        undo = None
        versions = configedit.versions(d.settings.state_dir, FILE)
        if versions:
            undo = {
                "undo": f"/admin/config/{FILE}/restore",
                "fields": {"stamp": versions[0].stamp, "base": configedit.digest(new)},
            }
        what = "switched on and saved" if on and not was_on else "saved"
        toast = {"kind": "ok", "text": f"{section.title}: {what}.", **(undo or {})}
        toast.update(file=FILE, digest=configedit.digest(new))
        return card(request, name, toast=toast)

    @admin.post("/collectors/{name}/test", response_class=HTMLResponse)
    def test(request: Request, name: str):
        if not known(name):
            return Response("There is no such collector.", status_code=404)
        ok, message = framework.test_connection(name, d.store)
        d.store.audit(
            f"admin:{d.session(request).username}", "collector.test", f"{name}: {'ok' if ok else 'failed'}"
        )
        return d.render(request, "_collector_result.html", ok=ok, message=message)

    @admin.post("/collectors/{name}/run", response_class=HTMLResponse)
    def run_now(request: Request, name: str):
        if not known(name):
            return Response("There is no such collector.", status_code=404)
        result = framework.run_due(d.store, only=name, force=True).get(name)
        if result is None:
            return d.render(
                request, "_collector_result.html", ok=False, message="It is switched off: switch it on first."
            )
        ok = bool(result.get("ok"))
        d.store.audit(
            f"admin:{d.session(request).username}", "collector.run", f"{name}: {'ok' if ok else 'failed'}"
        )
        return d.render(
            request,
            "_collector_result.html",
            ok=ok,
            message="ran and stored its numbers" if ok else str(result.get("error")),
        )
