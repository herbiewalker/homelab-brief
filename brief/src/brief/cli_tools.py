"""The rest of the `brief` command: everything the admin GUI can do, from a shell.

  brief status [--json]                      health, last run, systems, silent hosts, config errors
  brief sources                              every sender, system, last line, counts
  brief config list|show|validate|history|diff|set|rollback|reset
  brief systems list|add|assign|remove       the systems form, on the command line
  brief signatures [--q TEXT] [--ignored]    and: brief signature ignore|unignore ID
  brief logs search ...                      search the raw archive
  brief audit [--user U] [--q TEXT]          the audit log
  brief network show | test [--wait N]       mode, warnings, the live syslog test
  brief backup create|restore                download-style backup, config-only restore
  brief collector list|test NAME|run [NAME]  Phase 2 data collectors: status, connection test, run now
  brief ssh show                             whether the optional SSH server is on and ready

Every change goes through the same functions as the GUI (config editor, alert lifecycle, signature
flag), so the same checks apply and each change is written to the audit log, as `cli:<unix user>`.
Exit codes: 0 done, 1 refused or failed, 2 bad usage.
"""

import getpass
import secrets
import sys
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from brief import admin_data, backup, configedit, configguard, logsearch, netinfo, netpage, sysedit
from brief.config import ConfigError, config_file
from brief.store import Store
from brief.systems import Systems

GROUPS = (
    "support-bundle",
    "init",
    "status",
    "sources",
    "config",
    "systems",
    "signatures",
    "signature",
    "logs",
    "audit",
    "network",
    "backup",
    "collector",
    "ssh",
)


def actor() -> str:
    try:
        return f"cli:{getpass.getuser()}"
    except Exception:  # no passwd entry for this uid: still say it was the command line
        return "cli"


def _settings():
    from brief.settings import Settings

    return Settings.from_env()


def _systems() -> Systems:
    try:
        return Systems.load(config_file("systems.yaml"))
    except (ConfigError, OSError):
        return Systems()


def add_parsers(sub) -> None:
    st = sub.add_parser("status", help="health, the last run, and what needs a look")
    st.add_argument("--json", action="store_true")
    sub.add_parser("sources", help="every sender with its system, last line, and counts")

    cfg = sub.add_parser("config", help="the config files: show, check, edit, undo").add_subparsers(
        dest="cmd", required=True
    )
    cfg.add_parser("list", help="the files you can edit and whether each is customised")
    sh = cfg.add_parser("show", help="print a file (yours, or the shipped default)")
    sh.add_argument("name")
    sh.add_argument("--default", action="store_true", help="the shipped default, not your copy")
    va = cfg.add_parser(
        "validate", help="check your files (all, or the named ones); exit 1 if any is not valid"
    )
    va.add_argument("names", nargs="*")
    va.add_argument("--from", dest="source", help="check this file as if it were NAME (needs one name)")
    hi = cfg.add_parser("history", help="saved earlier versions of a file")
    hi.add_argument("name")
    di = cfg.add_parser("diff", help="what changed between a saved version and the file now")
    di.add_argument("name")
    di.add_argument("version", nargs="?", help="a version from `history` (default: the newest)")
    se = cfg.add_parser("set", help="replace a file from a path or stdin (checked, versioned, audited)")
    se.add_argument("name")
    se.add_argument("--from", dest="source", default="-", help="a path, or - for stdin")
    ro = cfg.add_parser("rollback", help="restore a saved version")
    ro.add_argument("name")
    ro.add_argument("version")
    rs = cfg.add_parser("reset", help="remove your copy so the shipped default applies again")
    rs.add_argument("name")

    sy = sub.add_parser("systems", help="systems.yaml without opening an editor").add_subparsers(
        dest="cmd", required=True
    )
    sy.add_parser("list")
    ad = sy.add_parser("add", help="add a system")
    ad.add_argument("id")
    ad.add_argument("--label", default="")
    ad.add_argument("--kind", default="")
    ad.add_argument("--host", action="append", default=[], help="an address or name (repeat for more)")
    ad.add_argument("--tz", default="UTC")
    ad.add_argument("--not-expected", action="store_true", help="do not alert if it goes quiet")
    asg = sy.add_parser("assign", help="add a sender to an existing system")
    asg.add_argument("host")
    asg.add_argument("id")
    rm = sy.add_parser("remove", help="remove a system")
    rm.add_argument("id")

    sg = sub.add_parser("signatures", help="the busiest kinds of message, with their ignored state")
    sg.add_argument("--q", default="")
    sg.add_argument("--ignored", action="store_true")
    sg.add_argument("--limit", type=int, default=30)
    si = sub.add_parser("signature", help="ignore or stop ignoring one signature").add_subparsers(
        dest="cmd", required=True
    )
    for name in ("ignore", "unignore"):
        p = si.add_parser(name)
        p.add_argument("sig_id", type=int)

    lg = sub.add_parser("logs", help="search the raw syslog archive").add_subparsers(
        dest="cmd", required=True
    )
    ls = lg.add_parser("search")
    ls.add_argument("--host", default="")
    ls.add_argument("--from", dest="start", help="first day, 2026-10-01 (default: today)")
    ls.add_argument("--to", dest="end", help="last day (default: the first day)")
    ls.add_argument("--text", default="")
    ls.add_argument("--limit", type=int, default=logsearch.MAX_LINES)

    au = sub.add_parser("audit", help="the audit log, newest first")
    au.add_argument("--user", default="")
    au.add_argument("--q", default="")
    au.add_argument("--limit", type=int, default=30)

    nw = sub.add_parser("network", help="mode, address, ports, warnings, and the syslog test").add_subparsers(
        dest="cmd", required=True
    )
    nw.add_parser("show")
    nt = nw.add_parser("test", help="print a test command and wait for the line to arrive")
    nt.add_argument("--wait", type=int, default=60, help="seconds to wait (0 prints the command and stops)")

    bk = sub.add_parser("backup", help="a backup file, and restoring the config from one").add_subparsers(
        dest="cmd", required=True
    )
    bc = bk.add_parser("create")
    bc.add_argument("--out", default="", help="a file path, or - for stdout (default: /out/backups/...)")
    br = bk.add_parser("restore", help="apply the config files in a backup")
    br.add_argument("path")

    co = sub.add_parser("collector", help="Phase 2 data collectors").add_subparsers(dest="cmd", required=True)
    co.add_parser("list", help="every collector in collectors.yaml and how its last run went")
    ct = co.add_parser("test", help="check the connection (works before you enable it; stores nothing)")
    ct.add_argument("name")
    cr = co.add_parser("run", help="run enabled collectors now, whether or not they are due")
    cr.add_argument("name", nargs="?", default=None)

    ini = sub.add_parser("init", help="first-run setup: name your systems from the senders seen")
    ini.add_argument("--assign", action="append", default=[], metavar="HOST=NAME[:LABEL]",
                     help="assign a sender without prompts (repeat for more)")  # fmt: skip
    ini.add_argument("--yes", action="store_true", help="do not ask before saving")
    sb = sub.add_parser(
        "support-bundle", help="a diagnostics file with every name, address, and secret replaced"
    )
    sb.add_argument("--out", default="", help="where to write it (default: /out/support/...)")
    sb.add_argument("--map", default="", help="also write the alias map here (keep it private)")

    sh2 = sub.add_parser("ssh", help="the optional SSH server").add_subparsers(dest="cmd", required=True)
    sh2.add_parser("show")


def dispatch(args, state: Path, out=print, err=None) -> int:
    err = err or (lambda text: print(text, file=sys.stderr))
    store = Store(state / "baseline.db") if args.group != "ssh" else None
    try:
        handler = HANDLERS[args.group]
        return handler(args, state, store, out, err)
    except configedit.EditError as exc:
        err(str(exc))
        return 1
    except (backup.BackupError, logsearch.SearchError, ConfigError, ValueError, OSError) as exc:
        err(str(exc))
        return 1
    finally:
        if store is not None:
            store.close()


# ---------------------------------------------------------------- status, sources, audit


def _status(args, state, store, out, err) -> int:
    import json

    from brief.outputs import read_json
    from brief.status import build_status

    s = _settings()
    doc = build_status(
        store,
        _systems(),
        s.syslog_port,
        s.interval,
        time.monotonic() + 3600,  # a CLI has no uptime: do not report "starting"
        network=netinfo.network_info(s),
        previous=read_json(s.out_dir / "latest" / "status.json"),
        state_dir=state,
    )
    if args.json:
        out(json.dumps(doc, indent=2))
        return 0 if doc["run_ok"] else 1
    out(("OK   " if doc["run_ok"] else "FAIL ") + doc["headline"])
    p = doc["pipeline"]
    out(f"events 24h {p.get('events_24h')}, new signatures {p.get('new_signatures_24h')}, silent hosts "
        f"{p.get('hosts_silent')} of {p.get('hosts_checked')}, parse failures {p.get('parse_failed_24h')}")  # fmt: skip
    if p.get("memory_mb") is not None:
        limit = f" of {p['memory_limit_mb']}" if p.get("memory_limit_mb") else ""
        out(f"memory {p['memory_mb']}{limit} MB, database {p.get('db_mb')} MB, archive {p.get('archive_mb')} MB, "
            f"archive disk free {p.get('archive_free_gb')} GB, days until full {p.get('storage_days_left')}")  # fmt: skip
    for tile in doc.get("systems") or []:
        out(f"  {tile['state']:<8} {tile['name']}: {tile['summary']}")
    for silent in doc.get("silent_hosts") or []:
        out(f"  silent   {silent['host']} (last seen {silent.get('last_seen')})")
    for name, issue in configguard.read_errors(state).items():
        out(f"  CONFIG   {name}: {issue['error']} (using {issue['using']})")
    return 0 if doc["run_ok"] else 1


def _sources(args, state, store, out, err) -> int:
    rows = admin_data.sources(store, {}, datetime.now(UTC))
    out(f"{'SENDER':<40} {'SYSTEM':<16} {'LAST LINE':<14} {'24H':>10} {'1H':>8}")
    for r in rows:
        system = r["system"] if r["known"] else "(unknown)"
        out(
            f"{r['host']:<40} {str(system):<16} {r['last_seen_text']:<14} {r['events_24h']:>10,} {r['events_hour']:>8,}"
        )
    if not rows:
        out("Nothing has been received yet.")
    return 0


def _audit(args, state, store, out, err) -> int:
    page = admin_data.audit(store, user=args.user, text=args.q, limit=max(1, min(args.limit, 500)))
    for r in page["rows"]:
        out(f"{r['ts']}  {r['user']:<16} {r['action']:<22} {r['detail'] or ''}")
    if not page["rows"]:
        out("Nothing matches.")
    return 0


# ---------------------------------------------------------------- config


def _config(args, state, store, out, err) -> int:
    cmd = args.cmd
    if cmd == "list":
        errors = configguard.read_errors(state)
        for name in configedit.EDITABLE:
            try:
                _, custom = configedit.current(name)
            except configedit.EditError:
                custom = False
            flag = "NOT VALID" if name in errors else ("customised" if custom else "default")
            out(f"{name:<18} {flag:<11} {configedit.DESCRIPTIONS[name]}")
        return 0
    if cmd == "validate":
        return _validate(args, out, err)
    name = configedit.checked_name(args.name)
    user = actor()
    if cmd == "show":
        if args.default:
            sys.stdout.write(configedit.starting_text(name))
        else:
            sys.stdout.write(configedit.current(name)[0])
        return 0
    if cmd == "history":
        versions = configedit.versions(state, name)
        for v in versions:
            out(f"{v.stamp}  {v.action:<20} {v.user:<16} {v.size} B")
        if not versions:
            out("No earlier versions yet.")
        return 0
    if cmd == "diff":
        versions = configedit.versions(state, name)
        stamp = args.version or (versions[0].stamp if versions else "")
        if not stamp:
            out("No earlier versions yet.")
            return 0
        old = configedit.version_text(state, name, stamp)
        now, _ = configedit.current(name)
        marks = {"add": "+", "del": "-", "ctx": " ", "gap": "@"}
        for line in configedit.diff_lines(now, old):
            out(f"{marks[line['kind']]} {line['text']}")
        out(f"(lines marked + would be added back by restoring {stamp}, lines marked - would go away)")
        return 0
    if cmd == "set":
        text = sys.stdin.read() if args.source == "-" else Path(args.source).read_text(encoding="utf-8")
        current, _ = configedit.current(name)
        out(configedit.save(store, name, text, user, configedit.digest(current), state))
        return 0
    if cmd == "rollback":
        current, _ = configedit.current(name)
        out(configedit.rollback(store, name, args.version, user, configedit.digest(current), state))
        return 0
    if cmd == "reset":
        out(configedit.reset(store, name, user, state))
        return 0
    return 2


def _validate(args, out, err) -> int:
    if args.source:
        if len(args.names) != 1:
            err(
                "--from needs exactly one file name, for example: brief config validate systems.yaml --from new.yaml"
            )
            return 2
        name = configedit.checked_name(args.names[0])
        problem = configedit.validate_text(
            name, configedit.normalize(Path(args.source).read_text(encoding="utf-8"))
        )
        out(f"{name}: " + (f"NOT VALID: {problem}" if problem else "ok"))
        return 1 if problem else 0
    names = [configedit.checked_name(n) for n in args.names] or list(configedit.EDITABLE)
    bad = 0
    for name in names:
        from brief.config import user_file

        path = user_file(name)
        if not path.exists():
            out(f"{name:<18} default (no file of yours)")
            continue
        problem = configedit.validate_text(name, path.read_text(encoding="utf-8"))
        out(f"{name:<18} " + (f"NOT VALID: {problem}" if problem else "ok"))
        bad += bool(problem)
    return 1 if bad else 0


# ---------------------------------------------------------------- systems


def _systems_cmd(args, state, store, out, err) -> int:
    text, custom = configedit.current("systems.yaml")
    rows, default = sysedit.rows_from_text(text) if custom else ([], "other")
    if args.cmd == "list":
        for r in rows:
            out(
                f"{r['id']:<18} {r['label']:<24} {'expected' if r['expected'] else 'not expected':<13} {r['hosts']}"
            )
        if not rows:
            out("No systems yet. Add one with: brief systems add ID --label NAME --host ADDRESS")
        return 0
    if args.cmd == "add":
        if any(r["id"] == args.id for r in rows):
            err(f"{args.id} already exists; use `brief systems assign HOST {args.id}` to add a sender to it.")
            return 1
        rows.append({
            "id": args.id, "label": args.label or args.id, "kind": args.kind, "hosts": ", ".join(args.host),
            "source_tz": args.tz, "expected": not args.not_expected, "extra": {},
        })  # fmt: skip
        done = f"Added {args.id}."
    elif args.cmd == "assign":
        target = next((r for r in rows if r["id"] == args.id), None)
        if target is None:
            err(f"There is no system {args.id}. `brief systems list` shows them.")
            return 1
        target["hosts"] = ", ".join([*sysedit.split_hosts(target["hosts"]), args.host])
        done = f"Added {args.host} to {args.id}."
    else:  # remove
        if not any(r["id"] == args.id for r in rows):
            err(f"There is no system {args.id}.")
            return 1
        rows = [r for r in rows if r["id"] != args.id]
        done = f"Removed {args.id}."
    new_text = sysedit.build_text(rows, default)
    message = configedit.save(store, "systems.yaml", new_text, actor(), configedit.digest(text), state)
    out(done + " " + message.split(": ", 1)[-1])
    return 0


# ---------------------------------------------------------------- signatures


def _signatures(args, state, store, out, err) -> int:
    low = datetime.now(UTC) - timedelta(hours=24)
    key = f"{low:%Y-%m-%d}{low.hour:02d}"
    like = "%" + args.q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    where = "(s.template LIKE ? ESCAPE '\\' OR s.app LIKE ? ESCAPE '\\')" + (
        " AND s.ignored = 1" if args.ignored else ""
    )
    with store.read() as db:
        rows = db.execute(
            "SELECT s.sig_id, s.app, s.template, s.ignored, "
            "COALESCE(SUM(CASE WHEN h.day || printf('%02d', h.hour) >= ? THEN h.n END), 0) AS n24 "
            "FROM signature s LEFT JOIN hourly h ON h.sig_id = s.sig_id "
            f"WHERE {where} GROUP BY s.sig_id ORDER BY n24 DESC, s.last_seen DESC LIMIT ?",
            (key, like, like, max(1, min(args.limit, 500))),
        ).fetchall()
    for r in rows:
        label = f"{r['app'] + ': ' if r['app'] else ''}{r['template'] or ''}"[:90]
        out(f"{r['sig_id']:>6} {'IGNORED' if r['ignored'] else '       '} {r['n24']:>9,}  {label}")
    if not rows:
        out("No signatures match.")
    return 0


def _signature(args, state, store, out, err) -> int:
    want = args.cmd == "ignore"
    with store.read() as db:
        row = db.execute(
            "SELECT app, template, ignored FROM signature WHERE sig_id = ?", (args.sig_id,)
        ).fetchone()
    if row is None:
        err(f"There is no signature {args.sig_id}.")
        return 1
    label = f"{row['app'] + ': ' if row['app'] else ''}{row['template'] or ''}"[:120]
    if bool(row["ignored"]) == want:
        out(f"Signature {args.sig_id} is already {'ignored' if want else 'not ignored'}.")
        return 0
    with store.write() as tx:
        tx.set_ignored(args.sig_id, want)
        tx.audit(
            actor(), "signature.ignore" if want else "signature.unignore", f"signature {args.sig_id}: {label}"
        )
    out(f"Signature {args.sig_id} is now {'ignored' if want else 'counted again'}: {label}")
    return 0


# ---------------------------------------------------------------- logs


def _day(text: str | None, default: date) -> date:
    try:
        return date.fromisoformat(text) if text else default
    except ValueError:
        raise ValueError(f"{text!r} is not a date like 2026-10-01.") from None


def _logs(args, state, store, out, err) -> int:
    today = datetime.now(UTC).date()
    start = _day(args.start, today)
    end = _day(args.end, start)
    result = logsearch.search(
        _settings().archive_dir, args.host, start, end, args.text, limit=max(1, min(args.limit, 1000))
    )
    for line in result.lines:
        out(f"{line['day']} {line['host']:<20} {line['text']}")
    if not result.lines:
        out("No matching lines.")
    note = f" Stopped at {result.stopped}: narrow the search to see more." if result.stopped else ""
    err(f"{result.files} file(s) read, {result.bytes_read:,} bytes.{note}")
    return 0


# ---------------------------------------------------------------- network


def _network(args, state, store, out, err) -> int:
    s = _settings()
    net = netinfo.network_info(s)
    ports = {"syslog": s.syslog_port, "http": s.http_port, "https": s.https_port}
    now = datetime.now(UTC)
    if args.cmd == "show":
        out(
            f"mode            {net['mode']}"
            + (f" (detected: {net['detected_mode']})" if net["mode"] != net["detected_mode"] else "")
        )
        out(f"address         {net['address'] or 'no route found'}")
        out(f"advertised      {net['advertised'] or 'not set'}")
        out(f"syslog goes to  {(net['send_syslog_to'] or 'the host address') + ':' + str(ports['syslog'])}")
        for row in netpage.listening(ports):
            out(
                f"listening       {row['name']:<26} {row['port']:<6} {'answers' if row['ok'] else 'NOT ANSWERING'}"
            )
        warnings = [*net["warnings"], *netpage.port_warnings(net["mode"], ports)]
        lost = netpage.lost_sender_warning(store, now)
        warnings += [lost] if lost else []
        for w in warnings:
            out(f"WARNING         {w}")
        for n in netpage.notes(net["mode"]):
            out(f"note            {n}")
        for p in netpage.pending_restart(ports, s.advertised, netpage.configured()):
            out(f"RESTART NEEDED  {p}")
        for r in netpage.repointing(_systems(), store, now):
            out(f"sender          {r['system']:<20} {r['entry']:<30} {r['state']}")
        if not warnings:
            out(f"No network problems found for the {net['mode']} mode.")
        return 0
    token = secrets.token_hex(4)
    for command in netpage.test_commands(net["send_syslog_to"], ports["syslog"], token):
        out(command)
    if args.wait <= 0:
        return 0
    err(f"Run one of those from the server or device you want to check. Waiting up to {args.wait} s...")
    deadline = time.monotonic() + args.wait
    while time.monotonic() < deadline:
        found = netpage.find_test_line(state, f"homelab-brief-test-{token}")
        if found:
            out(f"RECEIVED from {found['source_ip']}")
            if netinfo.in_docker_range(found["source_ip"]) and found["source_ip"].endswith(".1"):
                out(
                    "WARNING the sender is a Docker gateway address: the bridge is hiding the real sender addresses."
                )
            return 0
        time.sleep(2)
    out("Not received. Check the address and port above, and the Network warnings (`brief network show`).")
    return 1


# ---------------------------------------------------------------- backup, collectors, ssh


def _backup(args, state, store, out, err) -> int:
    if args.cmd == "restore":
        result = backup.restore_config(store, Path(args.path).read_bytes(), actor(), state)
        out(f"Applied: {', '.join(result.applied) or 'nothing'}. Already the same: {len(result.unchanged)}.")
        if result.skipped:
            out(f"Not applied (only config files are restored): {', '.join(result.skipped)}")
        store.audit(actor(), "backup.restore", f"applied {', '.join(result.applied) or 'nothing'}")
        return 0
    path = backup.create(state)
    try:
        if args.out == "-":
            sys.stdout.buffer.write(path.read_bytes())
            sys.stdout.buffer.flush()
            target = "stdout"
        else:
            dest = (
                Path(args.out)
                if args.out
                else _settings().out_dir
                / "backups"
                / f"homelab-brief-backup-{datetime.now(UTC):%Y%m%d-%H%M%S}.tar.gz"
            )
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".tmp")
            tmp.write_bytes(path.read_bytes())
            tmp.replace(dest)
            dest.chmod(0o600)
            target = str(dest)
        store.audit(actor(), "backup.create", f"{path.stat().st_size} bytes to {target}")
    finally:
        backup.discard(path)
    (err if args.out == "-" else out)(
        f"Backup written to {target}. It holds the admin password hash: keep it private."
    )
    return 0


def _collector(args, state, store, out, err) -> int:
    from brief.collectors import framework

    if args.cmd == "list":
        settings = framework.load()
        state_now = framework.read_state(store)
        for name in sorted(settings):
            s, last = settings[name], state_now.get(name)
            if not s.enabled:
                out(f"{name:16} off")
            elif not last:
                out(f"{name:16} on, has not run yet")
            else:
                note = "ok" if last.get("ok") else f"FAILED: {last.get('error')}"
                out(f"{name:16} on, last run {last.get('last_run')}, {note}")
        return 0
    if args.cmd == "test":
        ok, message = framework.test_connection(args.name, store)
        (out if ok else err)(f"{args.name}: {message}")
        return 0 if ok else 1
    results = framework.run_due(store, only=args.name, force=True)
    if not results:
        err(
            "No enabled collector"
            + (f" named {args.name}" if args.name else "")
            + ". Enable one in collectors.yaml."
        )
        return 1
    failed = 0
    for name, result in sorted(results.items()):
        failed += not result.get("ok")
        out(f"{name}: " + ("ok" if result.get("ok") else f"FAILED: {result.get('error')}"))
    return 1 if failed else 0


def _ssh(args, state, store, out, err) -> int:
    from brief import sshcfg

    info = sshcfg.describe()
    out(f"SSH server       {'enabled' if info['enabled'] else 'off (set SSH_ENABLED=true to turn it on)'}")
    out(f"listens on       {info['listen'] or 'n/a'}:{info['port']}")
    out(f"authorized keys  {info['keys']} valid key(s) in {info['keys_file']}")
    for problem in info["problems"]:
        out(f"PROBLEM          {problem}")
    return 0 if not info["problems"] else 1


def _init(args, state, store, out, err) -> int:
    from brief import setup

    now = datetime.now(UTC)
    found = setup.senders(store, {}, now, _systems())
    s = _settings()
    net = netinfo.network_info(s)
    where = f"{net['send_syslog_to'] or '<the host address>'}:{s.syslog_port}"
    out(f"Devices should send syslog to {where}.")
    unknown = [r for r in found if not r["known"]]
    if not found:
        out(
            "No sender has reached the receiver yet. Point a device at that address, then run `brief network test`"
        )
        out("to prove the path, and run `brief init` again once lines arrive.")
        return 0
    out(f"{len(found)} sender(s) seen, {len(unknown)} not in a system yet.")
    assignments: list[setup.Assignment] = []
    for item in args.assign:
        host, _, rest = item.partition("=")
        name, _, label = rest.partition(":")
        if not host or not name:
            err(f"--assign wants HOST=NAME or HOST=NAME:LABEL, not {item!r}.")
            return 2
        assignments.append(setup.Assignment(host.strip(), name.strip(), label.strip(), ""))
    if not args.assign:
        out("For each sender type a system name (for example: Gateway). Enter skips it, q stops.")
        for r in unknown:
            apps = ", ".join(r["apps"]) or "no programs yet"
            hint = f" (looks like: {r['kind_guess']})" if r["kind_guess"] else ""
            try:
                answer = input(
                    f"  {r['host']}  {r['events_24h']:,} events/24h, talks about: {apps}{hint}\n  system: "
                ).strip()
            except EOFError:
                break
            if answer.lower() == "q":
                break
            if answer:
                assignments.append(setup.Assignment(r["host"], answer, "", r["kind_guess"]))
    if not assignments:
        out("Nothing assigned.")
    else:
        for a in assignments:
            out(f"  {a.host} -> {a.system}")
        if not args.yes and not args.assign:
            try:
                if input("Save these systems? [Y/n] ").strip().lower() in ("n", "no"):
                    out("Nothing was saved.")
                    return 0
            except EOFError:
                pass
        out(setup.apply(store, assignments, actor(), state))
    out("")
    out("Next steps:")
    out("  brief config validate        check every file you have")
    out("  brief network show           mode, address, warnings")
    out("  brief notify test            if you turned on notifications")
    out("  brief status                 is everything healthy")
    out(
        "Collectors for the Unraid and UniFi APIs arrive in Phase 2; the senders above are the source for now."
    )
    return 0


def _support(args, state, store, out, err) -> int:
    from brief import supportbundle

    s = _settings()
    try:
        path, red = supportbundle.build(store, state, s)
    except supportbundle.BundleError as exc:
        err(str(exc))
        return 1
    try:
        dest = (
            Path(args.out)
            if args.out
            else s.out_dir / "support" / f"homelab-brief-support-{datetime.now(UTC):%Y%m%d-%H%M%S}.tar.gz"
        )
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".tmp")
        tmp.write_bytes(path.read_bytes())
        tmp.replace(dest)
        dest.chmod(0o600)
        if args.map:
            supportbundle.write_map(red, Path(args.map))
    finally:
        backup.discard(path)
    store.audit(actor(), "support.bundle", f"written; {len(red.map)} values aliased")
    out(f"Support bundle written to {dest} ({len(red.map)} values aliased).")
    out(
        "It holds structure and numbers only: no log lines, and every address and name is replaced by an alias."
    )
    if args.map:
        out(f"The alias map is in {args.map}. Keep it private: it turns the aliases back into real values.")
    return 0


HANDLERS = {
    "status": _status,
    "sources": _sources,
    "config": _config,
    "systems": _systems_cmd,
    "signatures": _signatures,
    "signature": _signature,
    "logs": _logs,
    "audit": _audit,
    "network": _network,
    "backup": _backup,
    "collector": _collector,
    "ssh": _ssh,
    "init": _init,
    "support-bundle": _support,
}
