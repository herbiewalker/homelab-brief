"""Admin tools: replay and tuning, signatures (with "ignore this signature"), and log search.

Replay and log search only read. The one write here is switching a signature's ignored flag, which is
audited. Every route is on the guarded admin router (a session for all, CSRF for every write).
"""

import json
import re
from datetime import UTC, date, datetime, time, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from brief import export, listing, logsearch, replay
from brief.admin import Deps
from brief.config import ConfigError, config_file
from brief.jsonlog import log
from brief.schedule import Schedule
from brief.systems import Systems

MAX_REPLAY_DAYS = 31
MAX_HOURLY_DAYS = 14
SIGNATURE_PAGE = 100
SIGNATURE_LIST = 1000  # the most the list page reads at once; narrow it with a search
SIGNATURE_SORTS = ("n24", "last_seen", "name")
BULK_MAX = 200
Q_MAX = 120
HOST_TEXT = re.compile(r"^[A-Za-z0-9.:_-]{1,80}$")


def _day(value: str, default: date) -> date:
    try:
        return date.fromisoformat(value) if value else default
    except ValueError:
        raise ValueError(f"{value!r} is not a date like 2026-10-01.") from None


def _clock(value: str, default: time) -> time:
    try:
        hh, mm = (value or default.strftime("%H:%M")).split(":")
        return time(int(hh), int(mm))
    except ValueError:
        raise ValueError(f"{value!r} is not a time like 06:00.") from None


def register(admin: APIRouter, d: Deps) -> None:
    def load_systems() -> Systems:
        try:
            return Systems.load(config_file("systems.yaml"))
        except (ConfigError, OSError):
            return Systems()

    # ------------------------------------------------------------------ replay and tuning

    def replay_page(request: Request, status: int = 200, **extra) -> HTMLResponse:
        today = datetime.now(UTC).date()
        hour, minute = Schedule.load().daily
        form = extra.pop("form", None) or {
            "start": (today - timedelta(days=7)).isoformat(),
            "end": today.isoformat(),
            "at": f"{hour:02d}:{minute:02d}",
            "hourly": True,
            "ignore_learning": False,
        }
        return d.render(
            request,
            "admin_replay.html",
            status,
            csrf=d.session(request).csrf,
            active="replay",
            form=form,
            **extra,
        )

    @admin.get("/replay", response_class=HTMLResponse)
    def replay_form(request: Request):
        return replay_page(request)

    @admin.post("/replay")
    async def replay_run(request: Request):
        form = await request.form()
        today = datetime.now(UTC).date()
        values = {
            "start": str(form.get("start", "")),
            "end": str(form.get("end", "")),
            "at": str(form.get("at", "")),
            "hourly": form.get("hourly") == "on",
            "ignore_learning": form.get("ignore_learning") == "on",
        }
        try:
            start = _day(values["start"], today - timedelta(days=7))
            end = _day(values["end"], today)
            at = _clock(values["at"], time(6, 0))
            span = (end - start).days + 1
            if end < start:
                raise ValueError("The end day is before the start day.")
            if span > MAX_REPLAY_DAYS:
                raise ValueError(f"Replay at most {MAX_REPLAY_DAYS} days at a time.")
            if values["hourly"] and span > MAX_HOURLY_DAYS:
                raise ValueError(
                    f"With hourly absence checks replay at most {MAX_HOURLY_DAYS} days; untick it for more."
                )
            rep = replay.replay(
                d.store,
                load_systems(),
                start,
                end,
                at,
                values["hourly"],
                ignore_learning=values["ignore_learning"],
            )
        except ValueError as exc:
            return replay_page(request, 422, form=values, error=str(exc))
        data = replay.as_dict(rep)
        return replay_page(
            request,
            form=values,
            result=data,
            shown=data["runs"][:200],
            hidden=max(len(data["runs"]) - 200, 0),
        )

    # ------------------------------------------------------------------ signatures

    def signature_rows(
        q: str, only_ignored: bool, limit: int = SIGNATURE_PAGE, only_active: bool = False
    ) -> list[dict]:
        low = datetime.now(UTC) - timedelta(hours=24)
        key = f"{low:%Y-%m-%d}{low.hour:02d}"
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where = ["(s.template LIKE ? ESCAPE '\\' OR s.app LIKE ? ESCAPE '\\')"]
        args: list = [key, like, like]
        if only_ignored:
            where.append("s.ignored = 1")
        if only_active:
            where.append("s.ignored = 0")
        sql = (
            "SELECT s.sig_id, s.app, s.template, s.first_seen, s.last_seen, s.ignored, "
            "COALESCE(SUM(CASE WHEN h.day || printf('%02d', h.hour) >= ? THEN h.n END), 0) AS n24, "
            "(SELECT x.host FROM hourly x WHERE x.sig_id = s.sig_id "
            "GROUP BY x.host ORDER BY SUM(x.n) DESC LIMIT 1) AS top_host "
            "FROM signature s LEFT JOIN hourly h ON h.sig_id = s.sig_id "
            f"WHERE {' AND '.join(where)} GROUP BY s.sig_id ORDER BY n24 DESC, s.last_seen DESC LIMIT ?"
        )
        with d.store.read() as db:
            rows = db.execute(sql, (*args, limit)).fetchall()
        return [
            {
                "id": r["sig_id"],
                "app": r["app"] or "",
                "template": (r["template"] or "")[:300],
                "last_seen": r["last_seen"],
                "ignored": bool(r["ignored"]),
                "n24": int(r["n24"]),
                "top_host": r["top_host"],
            }
            for r in rows
        ]

    def signatures_context(params: dict) -> dict:
        """The signature list for a set of (untrusted) address parameters: filtered, sorted, and paged."""
        q = str(params.get("q", "")).strip()[:Q_MAX]
        show = listing.choice(str(params.get("show", "")), ("ignored", "active"), "")
        if params.get("ignored") == "1":  # the older link form
            show = "ignored"
        sort = listing.choice(str(params.get("sort", "")), SIGNATURE_SORTS, "n24")
        direction = listing.choice(str(params.get("dir", "")), ("asc", "desc"), "desc")
        view = listing.Listing(
            "/admin/signatures",
            {"q": q, "show": show, "sort": sort, "dir": direction, "page": params.get("page")},
            {"sort": "n24", "dir": "desc", "page": "1"},
        )
        rows = signature_rows(q, show == "ignored", SIGNATURE_LIST, show == "active")
        key = {
            "n24": lambda r: r["n24"],
            "last_seen": lambda r: r["last_seen"] or "",
            "name": lambda r: f"{r['app']} {r['template']}".lower(),
        }[sort]
        rows.sort(key=key, reverse=direction == "desc")
        page = listing.paginate(rows, listing.number(params.get("page")), 50)
        return {"view": view, "page": page, "shows": ("ignored", "active")}

    @admin.get("/signatures", response_class=HTMLResponse)
    def signatures(request: Request):
        return d.render(
            request,
            "admin_signatures.html",
            csrf=d.session(request).csrf,
            active="signatures",
            **signatures_context(dict(request.query_params)),
        )

    @admin.post("/signatures/bulk")
    async def signatures_bulk(request: Request):
        """Ignore or stop ignoring every ticked signature, then show the list again with the same filters."""
        from urllib.parse import parse_qsl

        session = d.session(request)
        form = await request.form()
        if str(form.get("ignored")) not in ("0", "1"):
            return Response("That is not an action on signatures.", status_code=400)
        want = form.get("ignored") == "1"
        ids = [int(i) for i in form.getlist("sig_id") if str(i).isdigit()][:BULK_MAX]
        changed = 0
        with d.store.write() as tx:
            for sig_id in ids:
                row = tx.db.execute(
                    "SELECT app, template, ignored FROM signature WHERE sig_id = ?", (sig_id,)
                ).fetchone()
                if row is None or bool(row["ignored"]) == want:
                    continue
                label = f"{row['app'] + ': ' if row['app'] else ''}{row['template'] or ''}"[:120]
                tx.set_ignored(sig_id, want)
                tx.audit(
                    f"admin:{session.username}",
                    "signature.ignore" if want else "signature.unignore",
                    f"signature {sig_id}: {label}",
                )
                changed += 1
        params = dict(parse_qsl(str(form.get("view_query", ""))[:600]))
        response = d.render(request, "_signatures_card.html", csrf=session.csrf, **signatures_context(params))
        verb = "ignored" if want else "no longer ignored"
        text = f"{changed} signature{'s' if changed != 1 else ''} {verb}."
        response.headers["HX-Trigger"] = json.dumps(
            {"toast": {"kind": "ok" if changed else "info", "text": text}}
        )
        return response

    @admin.post("/signatures/{sig_id}/ignore")
    async def signature_ignore(request: Request, sig_id: int):
        session = d.session(request)
        form = await request.form()
        want = form.get("ignored") == "1"
        with d.store.read() as db:
            row = db.execute(
                "SELECT app, template, ignored FROM signature WHERE sig_id = ?", (sig_id,)
            ).fetchone()
        if row is None:
            return Response("There is no such signature.", status_code=404)
        if bool(row["ignored"]) != want:
            label = f"{row['app'] + ': ' if row['app'] else ''}{row['template'] or ''}"[:120]
            with d.store.write() as tx:
                tx.set_ignored(sig_id, want)
                tx.audit(
                    f"admin:{session.username}",
                    "signature.ignore" if want else "signature.unignore",
                    f"signature {sig_id}: {label}",
                )
        if request.headers.get("hx-request"):
            found = next((r for r in signature_rows("", False) if r["id"] == sig_id), None)
            if found is None:  # not among the busiest 100: show it from the database directly
                found = {
                    "id": sig_id, "app": row["app"] or "", "template": (row["template"] or "")[:300],
                    "last_seen": "", "ignored": want, "n24": 0, "top_host": None,
                }  # fmt: skip
            return d.render(request, "_signature_row.html", csrf=session.csrf, r=found)
        return RedirectResponse("/admin/signatures", status_code=303)

    @admin.get("/export/signature.csv")
    def signature_csv(host: str, sig: int, days: int = 30):
        if not HOST_TEXT.match(host) or not 1 <= days <= 365:
            return Response("Not a valid export request.", status_code=400)
        text = export.to_csv(export.series(d.store, host, sig, datetime.now(UTC).date(), days))
        name = f"{host.replace(':', '-')}-{sig}.csv"
        return Response(
            text,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"},
        )

    # ------------------------------------------------------------------ log search

    def logs_page(request: Request, form: dict, result=None, error=None) -> HTMLResponse:
        return d.render(
            request,
            "admin_logs.html",
            422 if error else 200,
            csrf=d.session(request).csrf,
            active="logs",
            form=form,
            result=result,
            error=error,
            span=logsearch.days_available(d.settings.archive_dir),
            max_days=logsearch.MAX_DAYS,
            max_lines=logsearch.MAX_LINES,
        )

    @admin.get("/logs", response_class=HTMLResponse)
    def logs(request: Request):
        today = datetime.now(UTC).date().isoformat()
        return logs_page(request, {"host": "", "q": "", "start": today, "end": today})

    @admin.post("/logs")
    async def logs_search(request: Request):
        """A POST, not a GET, so what you search for never lands in a URL, a history, or an access log."""
        form = await request.form()
        today = datetime.now(UTC).date()
        values = {
            "host": str(form.get("host", "")).strip(),
            "q": str(form.get("q", "")),
            "start": str(form.get("start", "")) or today.isoformat(),
            "end": str(form.get("end", "")) or today.isoformat(),
        }
        try:
            result = logsearch.search(
                d.settings.archive_dir,
                values["host"],
                _day(values["start"], today),
                _day(values["end"], today),
                values["q"],
            )
        except (logsearch.SearchError, ValueError) as exc:
            return logs_page(request, values, error=str(exc))
        log("log_search", user=d.session(request).username, files=result.files)
        return logs_page(request, values, result=result)
