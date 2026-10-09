"""The AI page: every AI setting, test, prompt, and the call log in one place (docs/guide/09-ai-integration).

Every change goes through the config editor (cli_ai.edit): validated by aiconfig, kept as a version for
rollback, and audited. The page holds no key and shows none; it shows only whether a key is set.
Tests, previews, and explanations are real model calls: they are written to the call log and count toward the
daily budget like any other. Routes only; the logic is in aiconfig, aitools, aitasks, and cli_ai.
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from brief import agentrun, aiconfig, aitasks, aitools, cli_ai, configedit, mcpserver, mcptokens, outputs
from brief.admin import Deps
from brief.config import ConfigError
from brief.systems import Systems

PROMPT_KEYS = {n.removeprefix("prompts/").removesuffix(".md"): n for n in configedit.PROMPTS}
FEATURE_TASK = {  # which task chain each feature switch controls (agents and MCP have none)
    "morning_brief": "morning_brief",
    "explain_finding": "explain_finding",
    "weekly_summary": "weekly_summary",
    "ask_the_lab": "ask_the_lab",
}
DONE = {
    "saved": "Saved. The change applies on the next task.",
    "revoked": "Token revoked. It no longer works.",
}


def register(admin: APIRouter, d: Deps) -> None:
    def systems() -> Systems:
        from brief.config import config_file

        try:
            return Systems.load(config_file("systems.yaml"))
        except (ConfigError, OSError):
            return Systems()

    def usage_by_task() -> dict[str, int]:
        with d.store.read() as db:
            return {
                r["task"]: r["n"]
                for r in db.execute(
                    "SELECT task, COUNT(*) n FROM ai_call WHERE ts >= datetime('now', '-1 day') GROUP BY task"
                )
            }

    def page(request: Request, status: int = 200, **extra) -> HTMLResponse:
        data = cli_ai.status(d.store)
        text, _ = configedit.current("ai.yaml")
        return d.render(
            request,
            "admin_ai.html",
            status,
            csrf=d.session(request).csrf,
            active="ai",
            ai=data,
            base=configedit.digest(text),
            locked=aiconfig.LOCKED,
            features=aiconfig.FEATURES,
            feature_task=FEATURE_TASK,
            used=usage_by_task(),
            calls=aitools.recent_calls(d.store, 30),
            agents=[
                {"spec": s, "runs": agentrun.runs_today(d.store, s.name, datetime.now(UTC))}
                for s in agentrun.load().values()
            ],
            agent_runs=agentrun.recent_runs(d.store, 10),
            agent_queue=agentrun.queue(d.store),
            tokens=mcptokens.listing(d.store),
            all_scopes=mcptokens.SCOPES,
            default_scopes=mcpserver.default_scopes(),
            prompts=sorted(PROMPT_KEYS),
            task_names=[t for t in aiconfig.TASKS],
            notice=extra.pop("notice", None),
            error=extra.pop("error", None),
            **extra,
        )

    def apply(request: Request, change, status_on_error: int = 422):
        """Run an ai.yaml edit; redirect back on success, or show the page with the reason."""
        user = d.session(request).username
        try:
            cli_ai.edit(d.store, d.settings.state_dir, user, change)
        except (configedit.EditError, ConfigError, ValueError) as exc:
            return page(request, getattr(exc, "status", status_on_error), error=str(exc))
        return RedirectResponse("/admin/ai?done=saved", status_code=303)

    @admin.post("/ai/mcp/create", response_class=HTMLResponse)
    async def mcp_create(request: Request):
        form = await request.form()
        user = d.session(request).username
        locality = str(form.get("locality", "lan"))
        scopes = [str(s) for s in form.getlist("scope")]
        name = str(form.get("name", ""))
        try:
            token_id, secret = mcptokens.create(d.store, name, scopes, locality, by=f"admin:{user}")
        except mcptokens.TokenError as exc:
            return page(request, 422, error=str(exc))
        d.store.audit(
            f"admin:{user}",
            "mcp.token.create",
            f"{name.strip()} ({token_id}): {','.join(sorted(set(scopes)))}; {locality}",
        )
        return d.render(
            request, "admin_mcp_created.html", csrf=d.session(request).csrf, active="ai",
            client_name=name.strip(), secret=secret, scopes=sorted(set(scopes)), locality=locality,
        )  # fmt: skip

    @admin.post("/ai/mcp/revoke")
    async def mcp_revoke(request: Request):
        form = await request.form()
        user = d.session(request).username
        try:
            name = mcptokens.revoke(d.store, str(form.get("token_id", "")))
        except mcptokens.TokenError as exc:
            return page(request, 404, error=str(exc))
        d.store.audit(f"admin:{user}", "mcp.token.revoke", name)
        return RedirectResponse("/admin/ai?done=revoked", status_code=303)

    @admin.get("/ai", response_class=HTMLResponse)
    def ai_page(request: Request, done: str = ""):
        return page(request, notice=DONE.get(done))

    @admin.post("/ai/master")
    async def master(request: Request):
        on = str((await request.form()).get("state")) == "on"
        return apply(request, lambda t: aiconfig.set_master(t, on))

    @admin.post("/ai/feature")
    async def feature(request: Request):
        form = await request.form()
        name, on = str(form.get("name", "")), str(form.get("state")) == "on"
        return apply(request, lambda t: aiconfig.set_feature(t, name, on))

    @admin.post("/ai/provider")
    async def provider(request: Request):
        form = await request.form()
        pid, action, value = (
            str(form.get("id", "")),
            str(form.get("action", "")),
            str(form.get("value", "")).strip(),
        )
        if action in ("on", "off"):
            return apply(request, lambda t: aiconfig.set_provider(t, pid, "enabled", action == "on"))
        if action == "model":
            return apply(request, lambda t: aiconfig.set_provider(t, pid, "model", value))
        return page(request, 422, error="Unknown provider action.")

    @admin.post("/ai/chain")
    async def chain(request: Request):
        form = await request.form()
        task = str(form.get("task", ""))
        steps = [s.strip() for s in str(form.get("chain", "")).replace(",", " ").split() if s.strip()]
        return apply(request, lambda t: aiconfig.set_chain(t, task, steps))

    @admin.post("/ai/guardrail")
    async def guardrail(request: Request):
        form = await request.form()
        key, raw = str(form.get("key", "")), str(form.get("value", "")).strip()
        kind = aiconfig.ADJUSTABLE.get(key)
        if kind is None:
            return page(request, 422, error="That is not an adjustable guardrail.")
        try:
            value = (
                [x.strip() for x in raw.split(",") if x.strip()]
                if kind is list
                else (
                    raw.lower() in ("true", "on", "yes", "1")
                    if kind is bool
                    else (int(raw) if kind is int else raw)
                )
            )
        except ValueError:
            return page(request, 422, error=f"{key} needs a whole number.")
        return apply(request, lambda t: aiconfig.set_guardrail(t, key, value))

    # ---- real calls, shown in place by htmx

    def partial(request: Request, **ctx) -> HTMLResponse:
        return d.render(request, "_ai_result.html", csrf=d.session(request).csrf, **ctx)

    @admin.post("/ai/test/{pid}", response_class=HTMLResponse)
    def test(request: Request, pid: str):
        try:
            result = aitools.test_provider(d.store, aitools.build_provider(pid))
        except ConfigError as exc:
            return partial(request, kind="error", message=str(exc))
        d.store.audit(
            d.session(request).username, "ai.test", f"provider {pid}: {'ok' if result['ok'] else 'failed'}"
        )
        return partial(request, kind="test", pid=pid, result=result)

    @admin.post("/ai/models/{pid}", response_class=HTMLResponse)
    def models(request: Request, pid: str):
        try:
            found = aitools.list_models(aitools.build_provider(pid))
        except ConfigError as exc:
            return partial(request, kind="error", message=str(exc))
        except Exception as exc:  # ProviderError: the server did not answer
            return partial(
                request,
                kind="error",
                message=f"The server did not list models ({getattr(exc, 'detail', type(exc).__name__)}).",
            )
        return partial(request, kind="models", pid=pid, models=found)

    @admin.post("/ai/preview", response_class=HTMLResponse)
    async def preview(request: Request):
        pid = str((await request.form()).get("provider", ""))
        now = datetime.now(UTC)
        digest = outputs.run_daily(d.store, systems(), now, d.settings.out_dir, write=False)
        result = aitools.preview_morning_brief(d.store, digest, now.strftime("%Y-%m-%d"), pid, now)
        d.store.audit(d.session(request).username, "ai.preview", f"provider {pid}")
        return partial(request, kind="preview", pid=pid, result=result)

    @admin.post("/ai/explain", response_class=HTMLResponse)
    async def explain(request: Request):
        finding_id = str((await request.form()).get("finding_id", "")).strip()
        now = datetime.now(UTC)
        finding = aitasks.find_finding(d.store, systems(), finding_id, now) if finding_id else None
        if finding is None:
            return partial(request, kind="error", message="No finding with that id is open right now.")
        explanation = aitasks.explain_finding(d.store, finding, now)
        if explanation is None:
            return partial(
                request,
                kind="error",
                message=(
                    "No explanation: AI or the explain_finding feature is off, or no provider gave a usable "
                    "answer. The call log says why."
                ),
            )
        return partial(request, kind="explain", finding=finding, explanation=explanation)

    # ---- prompts: the same editor, history, and rollback as every config file

    def prompt_name(key: str) -> str | None:
        return PROMPT_KEYS.get(key)

    def prompt_page(request: Request, key: str, status: int = 200, text=None, **extra) -> HTMLResponse:
        name = PROMPT_KEYS[key]
        on_disk, customised = configedit.current(name)
        return d.render(
            request,
            "admin_ai_prompt.html",
            status,
            csrf=d.session(request).csrf,
            active="ai",
            key=key,
            file=name,
            text=on_disk if text is None else text,
            base=configedit.digest(on_disk),
            customised=customised,
            versions=configedit.versions(d.settings.state_dir, name),
            error=extra.pop("error", None),
            notice=extra.pop("notice", None),
        )

    @admin.get("/ai/prompt/{key}", response_class=HTMLResponse)
    def prompt_get(request: Request, key: str, done: str = ""):
        if not prompt_name(key):
            return Response("Not a prompt.", status_code=404)
        return prompt_page(request, key, notice="Saved." if done == "saved" else None)

    @admin.post("/ai/prompt/{key}")
    async def prompt_save(request: Request, key: str):
        name = prompt_name(key)
        if not name:
            return Response("Not a prompt.", status_code=404)
        form = await request.form()
        text, base = str(form.get("text", "")), str(form.get("base", ""))
        try:
            configedit.save(d.store, name, text, d.session(request).username, base, d.settings.state_dir)
        except configedit.EditError as exc:
            return prompt_page(request, key, exc.status, text=text, error=str(exc))
        return RedirectResponse(f"/admin/ai/prompt/{key}?done=saved", status_code=303)

    @admin.post("/ai/prompt/{key}/restore")
    async def prompt_restore(request: Request, key: str):
        name = prompt_name(key)
        if not name:
            return Response("Not a prompt.", status_code=404)
        form = await request.form()
        try:
            on_disk, _ = configedit.current(name)
            configedit.rollback(
                d.store, name, str(form.get("stamp", "")), d.session(request).username,
                configedit.digest(on_disk), d.settings.state_dir,
            )  # fmt: skip
        except configedit.EditError as exc:
            return prompt_page(request, key, exc.status, error=str(exc))
        return RedirectResponse(f"/admin/ai/prompt/{key}?done=saved", status_code=303)

    @admin.post("/ai/prompt/{key}/reset")
    def prompt_reset(request: Request, key: str):
        name = prompt_name(key)
        if not name:
            return Response("Not a prompt.", status_code=404)
        try:
            configedit.reset(d.store, name, d.session(request).username, d.settings.state_dir)
        except configedit.EditError as exc:
            return prompt_page(request, key, exc.status, error=str(exc))
        return RedirectResponse(f"/admin/ai/prompt/{key}?done=saved", status_code=303)
