"""The Collectors page: which Phase 2 data sources are on, how their last run went, and a test button for each.

A collector is switched on or off by editing `collectors.yaml` through the config editor (checked, kept as a
version, audited), so this page never writes a file any other way. "Test" asks the source whether the connection
works and stores nothing; "Run now" does one real run. The page shows which template variables a collector needs
and whether each is set (never its value). Every route is on the guarded admin router.
"""

import os

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from brief import configedit, yamlscalar
from brief.admin import Deps
from brief.collectors import framework
from brief.config import ConfigError


def register(admin: APIRouter, d: Deps) -> None:
    def rows() -> tuple[list[dict], str | None]:
        try:
            settings = framework.load()
        except (ConfigError, OSError) as exc:
            return [], str(exc)
        state = framework.read_state(d.store)
        out = []
        for name in sorted(settings):
            s, last = settings[name], state.get(name) or {}
            out.append(
                {
                    "name": name,
                    "enabled": s.enabled,
                    "interval": str(s.interval),
                    "available": name in framework.REGISTRY,
                    "variables": [
                        {"name": v, "set": bool(os.environ.get(v))} for v in framework.required_vars(s.config)
                    ],
                    "last_run": last.get("last_run"),
                    "ok": last.get("ok"),
                    "error": last.get("error"),
                }
            )
        return out, None

    def page(request: Request, status: int = 200, **extra) -> HTMLResponse:
        found, problem = rows()
        return d.render(
            request,
            "admin_collectors.html",
            status,
            csrf=d.session(request).csrf,
            active="collectors",
            collectors=found,
            problem=problem,
            notice=extra.pop("notice", None),
            error=extra.pop("error", None),
        )

    def known(name: str) -> bool:
        try:
            return name in framework.load()
        except (ConfigError, OSError):
            return False

    @admin.get("/collectors", response_class=HTMLResponse)
    def collectors_page(request: Request, done: str = ""):
        return page(request, notice={"saved": "Saved. The worker picks it up on its next cycle."}.get(done))

    @admin.post("/collectors/{name}/switch")
    async def switch(request: Request, name: str):
        form = await request.form()
        on = str(form.get("state")) == "on"
        if not known(name):
            return Response("There is no such collector.", status_code=404)
        user = d.session(request).username
        text, _ = configedit.current("collectors.yaml")
        try:
            new = yamlscalar.set_path(text, ["collectors", name, "enabled"], on)
            configedit.save(
                d.store, "collectors.yaml", new, user, configedit.digest(text), d.settings.state_dir
            )
        except (yamlscalar.EditError, configedit.EditError) as exc:
            return page(request, getattr(exc, "status", 422), error=str(exc))
        return RedirectResponse("/admin/collectors?done=saved", status_code=303)

    @admin.post("/collectors/{name}/test", response_class=HTMLResponse)
    def test(request: Request, name: str):
        if not known(name):
            return Response("There is no such collector.", status_code=404)
        ok, message = framework.test_connection(name, d.store)
        who = f"admin:{d.session(request).username}"
        d.store.audit(who, "collector.test", f"{name}: {'ok' if ok else 'failed'}")
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
        message = "ran and stored its numbers" if ok else str(result.get("error"))
        return d.render(request, "_collector_result.html", ok=ok, message=message)
