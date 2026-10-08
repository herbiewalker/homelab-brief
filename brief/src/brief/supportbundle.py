"""`brief support-bundle`: a diagnostics file you can send to someone, with no names in it.

Safe by construction: the bundle holds structure and numbers, never log lines, message text, signature
text, alert titles, or user names. What it does hold (config files, status, sender and system names) goes
through the redactor, which gives every IP, MAC, address, name, and secret a stable alias (`<ip-1>`,
`<name-2>`) so a reader can still follow "this sender appears in three places". Before anything is
written, the finished text is scanned for what should not be there (an IP, a MAC, an address, a domain, any
private name this install knows); if anything is found, no bundle is made, and the message says how many
and in which file, never what.

The alias map stays on this machine and is written only when you ask for it (`--map`), so a reply that
says `<name-2>` can be turned back into a name.
"""

import csv
import io
import json
import os
import platform
import tarfile
import tempfile
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

from brief import __version__, admin_data, configedit, configguard, netinfo, status
from brief.config import ConfigError, user_file
from brief.redact import Redactor
from brief.settings import Settings
from brief.store import Store, iso
from brief.systems import Systems

README = """homelab-brief support bundle

What is in it: a summary of the install's health and settings, the status document, the shape of the
config files, counts of senders, alerts, audit actions and AI calls, and how many rows each table holds.

What is not in it: log lines, message text, signature text, alert titles, user names, addresses, or
passwords. Every IP, MAC, e-mail address, domain, host or device name, and secret is replaced by an alias
such as <ip-1> or <name-2>. The same value has the same alias everywhere in this bundle.

The bundle was checked before it was written: nothing that looks like an address or a known private name
is left in it. A name the product was never told about could still be inside a config file's free text,
so look through it before you send it.
"""


class BundleError(Exception):
    """The bundle was not written, because the self-check found something it should not contain."""


def private_names(store: Store, state: Path, env: Mapping[str, str] | None = None) -> list[str]:
    """Every word of this install that should never appear: system ids, labels and hosts, device and
    topology labels and owners. (IP and MAC shapes are caught by pattern, not listed here.)"""
    names: set[str] = set()
    try:  # system ids the database remembers, even ones no longer in systems.yaml
        with store.read() as db:
            names.update(
                r[0] for r in db.execute("SELECT DISTINCT system_id FROM host WHERE system_id IS NOT NULL")
            )
    except Exception:  # an unreadable database is reported elsewhere; the bundle still redacts what it can
        pass
    names.discard("other")  # the generic default system holds no one's name
    for file in ("systems.yaml", "devices.yaml", "topology.yaml"):
        path = user_file(file, env)
        if not path.is_file():
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (yaml.YAMLError, OSError):
            continue
        _collect_strings(data, names)
    return sorted(names)


def _collect_strings(obj, out: set[str], depth: int = 0) -> None:
    if depth > 8:
        return
    if isinstance(obj, str):
        out.add(obj.strip())
    elif isinstance(obj, list):
        for x in obj:
            _collect_strings(x, out, depth + 1)
    elif isinstance(obj, dict):
        for key, value in obj.items():
            if key in ("id", "label", "hosts", "owner", "name", "from", "to") or isinstance(
                value, (list, dict)
            ):
                _collect_strings(value, out, depth + 1)


def _shape(doc: dict) -> dict:
    """The status document without anything that is message text: story titles, the headline, and
    developing-issue ids become placeholders; kinds, ranks, routes, counts, and states stay."""
    out = dict(doc)
    out["headline"] = "(headline not included)"
    out["stories"] = [
        {
            "rank": s.get("rank"),
            "kind": s.get("kind"),
            "route": s.get("route"),
            "count": s.get("count"),
            "system": s.get("system"),
        }
        for s in doc.get("stories") or []
    ]
    out["developing"] = [
        {"days": d.get("days"), "trend": d.get("trend")} for d in doc.get("developing") or []
    ]
    return out


def _csv(rows: list[list], header: list[str]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return buf.getvalue()


def _systems(env) -> Systems:
    try:
        from brief.config import config_file

        return Systems.load(config_file("systems.yaml", env))
    except (ConfigError, OSError):
        return Systems()


def collect(
    store: Store,
    state: Path,
    settings: Settings,
    env: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> tuple[dict[str, str], Redactor]:
    """The bundle's files (name -> text), already redacted, and the redactor that made the aliases."""
    env = os.environ if env is None else env
    now = now or datetime.now(UTC)
    red = Redactor(private_names(store, state, env))
    files: dict[str, str] = {"README.txt": README}

    # the status document, as the product sees itself right now
    doc = status.build_status(
        store,
        _systems(env),
        settings.syslog_port,
        settings.interval,
        3600.0,
        env,
        now=now,
        network=netinfo.network_info(settings),
        state_dir=state,
    )
    files["status.json"] = json.dumps(red.value(_shape(doc)), indent=2) + "\n"

    # senders: aliases only, never addresses
    senders = admin_data.sources(store, doc, now)
    files["senders.csv"] = _csv(
        [
            [
                red.alias("ip", r["host"]) if r["host"] else "",
                red.alias("system", str(r["system"]))
                if r["system"] and r["system"] != "other"
                else str(r["system"] or ""),
                int(r["known"]),
                r["events_24h"],
                r["events_hour"],
                r["gap_min"] or "",
                r["last_seen_text"],
                int(r["silent"]),
            ]
            for r in senders
        ],
        ["sender", "system", "known", "events_24h", "events_1h", "normal_gap_min", "last_line", "silent"],
    )

    # config files: your copy, redacted; and whether each one loads
    notes, loads = [], []
    for name in configedit.EDITABLE:
        path = user_file(name, env)
        if not path.is_file():
            notes.append(f"{name}: default (no file of yours)")
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        files[f"config/{name}"] = red.text(text)
        problem = configedit.validate_text(name, text)
        loads.append(f"{name}: " + ("ok" if not problem else "NOT VALID: " + red.text(problem)))
    files["config-validation.txt"] = "\n".join([*loads, *notes]) + "\n"
    errors = {
        k: {"error": red.text(v.get("error", "")), "using": v.get("using")}
        for k, v in configguard.read_errors(state).items()
    }
    files["config-errors.json"] = json.dumps(errors, indent=2) + "\n"

    with store.read() as db:
        tables = [
            r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
        ]
        counts = [[t, db.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]] for t in tables]
        version = db.execute("PRAGMA user_version").fetchone()[0]
        alert_rows = db.execute("SELECT kind, level, state, route, opened, pushed FROM alert").fetchall()
        ai = db.execute(
            "SELECT task, provider, locality, outcome, COUNT(*) AS n, SUM(COALESCE(tokens_in, 0) + COALESCE(tokens_out, 0)) AS tokens "
            "FROM ai_call GROUP BY task, provider, locality, outcome"
        ).fetchall()
        audit = db.execute(
            "SELECT substr(ts, 1, 10) AS day, action, COUNT(*) AS n FROM audit_log WHERE ts >= ? GROUP BY day, action",
            (iso(now - timedelta(days=7)),),
        ).fetchall()
    files["tables.csv"] = _csv(counts, ["table", "rows"])
    files["alerts.csv"] = _csv(
        [
            [r["kind"], r["level"], r["state"], r["route"], admin_data.ago(r["opened"], now), r["pushed"]]
            for r in alert_rows
        ],
        ["kind", "level", "state", "route", "opened", "times_notified"],
    )
    files["ai-calls.csv"] = _csv(
        [
            [
                red.text(r["task"] or ""),
                red.text(r["provider"] or ""),
                r["locality"],
                r["outcome"],
                r["n"],
                r["tokens"] or 0,
            ]
            for r in ai
        ],
        ["task", "provider", "locality", "outcome", "calls", "tokens"],
    )
    files["audit-actions.csv"] = _csv(
        [[r["day"], r["action"], r["n"]] for r in audit], ["day", "action", "count"]
    )

    p = doc["pipeline"]
    kinds = Counter(r["kind"] for r in alert_rows)
    lines = [
        "# homelab-brief support bundle",
        "",
        f"- made: {iso(now)}",
        f"- version: {__version__}; python {platform.python_version()} on {platform.system()}",
        f"- database schema version: {version}",
        f"- run ok: {doc['run_ok']}; learning: {doc['learning']}",
        f"- network mode: {doc['network']['mode'] if doc.get('network') else 'unknown'}"
        + (
            f" (detected {doc['network']['detected_mode']})"
            if doc.get("network") and doc["network"]["mode"] != doc["network"]["detected_mode"]
            else ""
        ),
        f"- network warnings: {len((doc.get('network') or {}).get('warnings') or [])}",
        f"- senders: {len(senders)} ({sum(1 for s in senders if not s['known'])} unknown, {sum(1 for s in senders if s['silent'])} silent)",
        f"- events 24h: {p.get('events_24h')}; new signatures 24h: {p.get('new_signatures_24h')}; parse failures 24h: {p.get('parse_failed_24h')}",
        f"- memory: {p.get('memory_mb')} of {p.get('memory_limit_mb')} MB; database {p.get('db_mb')} MB; archive {p.get('archive_mb')} MB",
        f"- archive disk free: {p.get('archive_free_gb')} GB ({p.get('archive_free_pct')}%); days until full: {p.get('storage_days_left')}",
        f"- alerts open by kind: {dict(kinds) or 'none'}",
        f"- config errors: {len(errors)}",
        f"- mqtt: {'on' if (env.get('MQTT_HOST') or '').strip() else 'off'}; ssh: {'on' if (env.get('SSH_ENABLED') or '').lower() in ('true', '1', 'yes') else 'off'}",
        f"- python packages: {', '.join(sorted(_package_versions()))}",
        "",
    ]
    files["summary.md"] = red.text("\n".join(lines))
    check(files, red)
    return files, red


def _package_versions() -> list[str]:
    from importlib import metadata

    out = []
    for name in (
        "fastapi",
        "uvicorn",
        "pydantic",
        "jinja2",
        "pillow",
        "pyyaml",
        "drain3",
        "paho-mqtt",
        "apprise",
    ):
        try:
            out.append(f"{name} {metadata.version(name)}")
        except metadata.PackageNotFoundError:
            continue
    return out


def check(files: Mapping[str, str], red: Redactor) -> None:
    """Refuse to go on when any file still holds something that looks like an address, a MAC, a domain, or a
    private name. The message counts them per file and never repeats them."""
    problems = []
    for name, text in files.items():
        if name == "README.txt":
            continue
        found = red.leaks(text)
        if found:
            problems.append(f"{name}: {len(found)}")
    if problems:
        raise BundleError(
            "No bundle was written: the self-check still found something that looks private ("
            + ", ".join(problems)
            + "). Nothing was sent anywhere."
        )


def build(
    store: Store,
    state: Path,
    settings: Settings,
    env: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> tuple[Path, Redactor]:
    """Write the .tar.gz to a temporary file and return it with the redactor. Raises BundleError."""
    files, red = collect(store, state, settings, env, now)
    fd, name = tempfile.mkstemp(prefix="homelab-brief-support-", suffix=".tar.gz")
    os.close(fd)
    out = Path(name)
    stamp = int((now or datetime.now(UTC)).timestamp())
    try:
        with tarfile.open(out, "w:gz") as tar:
            for fname, text in sorted(files.items()):
                data = text.encode("utf-8")
                info = tarfile.TarInfo(f"support-bundle/{fname}")
                info.size, info.mtime, info.mode = len(data), stamp, 0o600
                tar.addfile(info, io.BytesIO(data))
    except Exception:
        out.unlink(missing_ok=True)
        raise
    return out, red


def write_map(red: Redactor, path: Path) -> None:
    """The alias map, for you to keep: `<ip-1>` -> the real value. Written only on request, readable by you."""
    rows = {alias: key.split(":", 1)[1] for key, alias in red.map.items()}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)
