"""The setup pages built on the settings framework: Schedule and retention, Notifications, and the cards
that the Network and Systems pages embed. Every card is one `Section` (see settingspages.py) shown as a form.
Saving writes the values back into the file through the config editor, so the file is checked, versioned with
an Undo, and audited, and the text editor shows the same file afterwards.
"""

import json
import os

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from brief import configedit, settingspages, settingsui, yamlscalar
from brief.admin import Deps

RESTART = {
    "network": " Restart the container for the address to take effect.",
    "network-ports": " Restart the container (and match the Docker template) for new ports to take effect.",
}


def basis_text(file: str) -> str:
    """The text a form edits: the user's file, or for a file that ships only as an example (devices, topology,
    systems) an empty document, so the example's made-up rows are not shown as if they were yours."""
    text, customised = configedit.current(file)
    return text if customised or file not in settingspages.EXAMPLE_ONLY else "schema: 1\n"


def vars_set(state: dict) -> dict[str, bool]:
    wanted = {v for v in state["values"].values() if isinstance(v, str) and settingsui.VAR_NAME.match(v)}
    return {v: bool(os.environ.get(v)) for v in wanted}


def render_card(
    d: Deps,
    request: Request,
    section: settingsui.Section,
    state: dict | None = None,
    errors: dict | None = None,
    general: str | None = None,
    status: int = 200,
    toast: dict | None = None,
    extra_hidden: dict | None = None,
) -> HTMLResponse:
    text = configedit.current(section.file)[0]
    loaded = state or settingsui.load(
        section, basis_text(section.file), configedit.starting_text(section.file)
    )
    response = d.render(
        request,
        "_settings_card.html",
        status,
        csrf=d.session(request).csrf,
        section=section,
        state=loaded,
        errors=errors or {},
        general=general,
        base=configedit.digest(text),
        vars_set=vars_set(loaded),
        action=f"/admin/settings/{section.id}",
        extra_hidden=extra_hidden or {},
    )
    if toast:
        response.headers["HX-Trigger"] = json.dumps({"toast": toast})
    return response


def cards_html(d: Deps, request: Request, ids, states: dict | None = None) -> list[str]:
    """The cards of several sections as HTML, for a page that embeds them."""
    return [
        render_card(d, request, settingspages.SECTIONS[i], (states or {}).get(i)).body.decode() for i in ids
    ]


def register(admin: APIRouter, d: Deps) -> None:
    def section_or_none(sid: str):
        return settingspages.SECTIONS.get(sid)

    def make_page(page: settingspages.Page):
        @admin.get(f"/{page.key}", response_class=HTMLResponse, name=f"settings_{page.key}")
        def show(request: Request):
            cards = cards_html(d, request, [s.id for s in page.sections])
            return d.render(
                request,
                "admin_settings.html",
                csrf=d.session(request).csrf,
                active=page.nav,
                page=page,
                cards=cards,
            )

    for page in settingspages.PAGES.values():
        make_page(page)

    @admin.post("/settings/{sid}/rows", response_class=HTMLResponse)
    async def rows(request: Request, sid: str):
        section = section_or_none(sid)
        if section is None:
            return Response("There is no such settings card.", status_code=404)
        form = await request.form()
        state = settingsui.posted(section, form)
        settingsui.row_action(section, state, str(form.get("row_action", "")))
        return render_card(d, request, section, state=state)

    @admin.post("/settings/{sid}/save", response_class=HTMLResponse)
    async def save(request: Request, sid: str):
        section = section_or_none(sid)
        if section is None:
            return Response("There is no such settings card.", status_code=404)
        form = await request.form()
        user = f"admin:{d.session(request).username}"
        values, errors, on = settingsui.parse(section, form)
        state = settingsui.posted(section, form)
        if errors:
            return render_card(
                d,
                request,
                section,
                state,
                errors,
                "Some settings need attention. They are marked below; nothing was saved.",
                422,
            )
        file = section.file
        text = basis_text(file)
        real = configedit.current(file)[0]
        try:
            new = settingsui.apply(section, text, values, on)
            if new == text:
                return render_card(
                    d,
                    request,
                    section,
                    toast={"kind": "info", "text": "Nothing to save: no setting was changed."},
                )
            problem = configedit.validate_text(file, new)
            if problem:
                return render_card(d, request, section, state, {}, problem, 422)
            configedit.save(
                d.store,
                file,
                new,
                user,
                str(form.get("base") or configedit.digest(real)),
                d.settings.state_dir,
            )
        except (yamlscalar.EditError, configedit.EditError) as exc:
            return render_card(d, request, section, state, {}, str(exc), getattr(exc, "status", 422))
        undo = {}
        versions = configedit.versions(d.settings.state_dir, file)
        if versions:
            undo = {
                "undo": f"/admin/config/{file}/restore",
                "fields": {"stamp": versions[0].stamp, "base": configedit.digest(new)},
            }
        text_out = f"{section.title}: saved." + RESTART.get(section.id, "")
        undo = {**undo, "file": file, "digest": configedit.digest(new)}
        return render_card(d, request, section, toast={"kind": "ok", "text": text_out, **undo})

    @admin.post("/settings/{sid}/test", response_class=HTMLResponse)
    def test(request: Request, sid: str):
        if sid != "notify-apprise":
            return Response("This card has no test.", status_code=404)
        from brief.notify import AppriseChannel

        channel = AppriseChannel.from_env(os.environ)
        if channel is None:
            ok, message = (
                False,
                "Apprise is off, or no destination is set. Save with it switched on, and put your "
                "destination URLs in the container variable named above.",
            )
        else:
            ok = channel.test()
            message = (
                f"sent a test message to {channel.destinations} destination(s)"
                if ok
                else "a destination did not accept the message"
            )
        d.store.audit(f"admin:{d.session(request).username}", "notify.test", "ok" if ok else "failed")
        return d.render(request, "_collector_result.html", ok=ok, message=message)

    @admin.post("/settings/{sid}/rowtest", response_class=HTMLResponse)
    async def rowtest(request: Request, sid: str):
        """Try one row of the ignore rules against recent log lines (read-only; nothing is saved)."""
        if sid != "ignore":
            return Response("This card has no row test.", status_code=404)
        from brief import ignoretest, livestatus

        form = await request.form()
        row = str(form.get("row", ""))
        if not row.isdigit():
            return Response("Not a row.", status_code=400)
        pattern = str(form.get(f"r.ignore.{row}.match", "")).strip()
        host = str(form.get(f"r.ignore.{row}.host", "")).strip()
        events = ignoretest.recent_events(d.settings.state_dir)
        try:
            systems = livestatus.load_systems()
        except Exception:  # without systems.yaml the host still matches by address or header name
            systems = None
        result = ignoretest.test_rule(events, pattern, host, systems)
        return d.render(request, "_row_test.html", result=result, any_events=bool(events))

    @admin.post("/settings/{sid}/preview", response_class=HTMLResponse)
    async def preview(request: Request, sid: str):
        """What the posted values would have done to the last week, by replaying it with and without them."""
        section = section_or_none(sid)
        if section is None or not section.preview:
            return Response("This card has no preview.", status_code=404)
        from datetime import UTC, datetime

        from brief import agentguards

        form = await request.form()
        values, errors, on = settingsui.parse(section, form)
        if errors:
            return d.render(
                request,
                "_collector_result.html",
                ok=False,
                message="Fix the marked settings first, then preview.",
            )
        old = basis_text(section.file)
        try:
            new = settingsui.apply(section, old, values, on)
        except yamlscalar.EditError as exc:
            return d.render(request, "_collector_result.html", ok=False, message=str(exc))
        if new == old:
            return d.render(
                request,
                "_collector_result.html",
                ok=True,
                message="Nothing is changed, so there is nothing to preview.",
            )
        problem = configedit.validate_text(section.file, new)
        if problem:
            return d.render(request, "_collector_result.html", ok=False, message=problem)
        result = agentguards.replay_evidence(d.store, None, section.file, old, new, None, datetime.now(UTC))
        d.store.audit(f"admin:{d.session(request).username}", "settings.preview", section.file)
        return d.render(request, "_collector_result.html", ok=not result.hidden, message=result.text)
