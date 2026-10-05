"""The `brief` command. Today: `brief admin set-password` and `brief import`; the rest arrives in Task 16.

Run it inside the container (`docker exec -it homelab-brief brief admin set-password`). The
password is read without echo, checked against the same rules as the web form, and never printed.
"""

import argparse
import getpass
import os
import sys
from pathlib import Path

from brief.auth import Auth, AuthError
from brief.detect import ALL as ALL_KINDS
from brief.store import Store


def set_password(user: str, state: Path, password: str | None = None, prompt=getpass.getpass) -> int:
    store = Store(state / "baseline.db")
    auth = Auth(store)
    try:
        if password is None:
            password = prompt("New password (at least 12 characters): ")
            if prompt("Repeat the password: ") != password:
                print("The two passwords do not match.", file=sys.stderr)
                return 1
        with store.read() as db:
            exists = db.execute("SELECT 1 FROM admin_user WHERE username = ?", (user,)).fetchone() is not None
        try:
            if exists:
                auth.set_password(user, password, source="cli")
                print(f"Password changed for {user}. Any signed-in sessions were ended.")
            elif auth.has_admin():
                print(
                    "An admin account already exists under another name. Use --user NAME to change that one.",
                    file=sys.stderr,
                )
                return 1
            else:
                auth.create_admin(user, password, source="cli")
                print(f"Admin account {user} created. Sign in at /admin on the HTTPS port.")
        except AuthError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        return 0
    finally:
        store.close()


def run_detect(args, state: Path, out=print) -> int:
    from datetime import UTC, datetime

    from brief import detect
    from brief.config import config_file
    from brief.store import parse_time
    from brief.systems import Systems

    try:
        systems = Systems.load(config_file("systems.yaml"))
    except Exception as exc:  # a broken systems.yaml should not hide the findings
        print(f"systems.yaml not loaded ({exc}); hosts are treated as unknown", file=sys.stderr)
        systems = Systems()
    now = parse_time(args.now) if args.now else datetime.now(UTC)
    store = Store(state / "baseline.db")
    try:
        # an explicit --now is a look at the past: use the inputs as they were then, like replay does
        activity = detect.HostActivity.load(store) if args.now else None
        result = detect.run(
            store,
            systems,
            now,
            kinds=tuple(args.kind or detect.ALL),
            ignore_learning=args.ignore_learning,
            activity=activity,
        )
    finally:
        store.close()
    if args.json:
        out(detect.as_json(result))
        return 0
    findings = result["findings"]
    out(
        f"{len(findings)} findings"
        + (" (learning period: novelty, deviation and off-hours are quiet)" if result["learning"] else "")
    )
    for kind in detect.ALL:
        group = [f for f in findings if f.kind == kind]
        if group:
            out(f"\n{kind} ({len(group)})")
            for f in group:
                out(
                    f"  {'' if f.story else '(count only) '}{f.title}"
                    + (f"  [{f.system}]" if f.system else "")
                )
    return 0


def run_replay(args, state: Path, out=print) -> int:
    import json
    from datetime import date, time

    from brief import replay
    from brief.config import config_file
    from brief.systems import Systems

    try:
        start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
        hh, mm = args.at.split(":")
        at = time(int(hh), int(mm))
    except ValueError:
        print("Dates look like 2026-10-01 and --at like 06:00.", file=sys.stderr)
        return 2
    try:
        systems = Systems.load(config_file("systems.yaml"))
    except Exception as exc:  # a broken systems.yaml should not hide the findings
        print(f"systems.yaml not loaded ({exc}); hosts are treated as unknown", file=sys.stderr)
        systems = Systems()
    store = Store(state / "baseline.db")
    try:
        rep = replay.replay(
            store, systems, start, end, at, not args.no_hourly, ignore_learning=args.ignore_learning
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        store.close()
    if args.json:
        out(json.dumps(replay.as_dict(rep), indent=2))
    else:
        for line in replay.format_text(rep, verbose=args.verbose):
            out(line)
    return 0


def run_alerts(args, state: Path, out=print) -> int:
    from datetime import UTC, datetime

    from brief import alerts
    from brief.lifecycle import Policy
    from brief.routing import Priorities

    store = Store(state / "baseline.db")
    try:
        book = alerts.load_book(store, include_resolved=args.all)
        policy = Policy.from_priorities(Priorities.load())
        used = alerts.pushes_today(store, datetime.now(UTC))
    finally:
        store.close()
    shown = sorted(book.values(), key=lambda a: (a.state == "resolved", a.finding_id))
    for a in shown:
        out(alerts.describe(a))
    out(f"{len(shown)} alerts. Non-urgent pushes today: {used} of {policy.daily_push_budget}.")
    return 0


def run_ack(args, state: Path, out=print) -> int:
    from brief import alerts

    store = Store(state / "baseline.db")
    try:
        action = "ack" if args.group == "ack" else _snooze_action(args.hours)
        outcome = alerts.apply_feedback(store, args.finding_id, action, source="cli")
    finally:
        store.close()
    if outcome is None:
        print(
            f"Nothing changed: no open alert {args.finding_id}, or that snooze length is not allowed.",
            file=sys.stderr,
        )
        return 1
    out(f"{args.finding_id}: {outcome}")
    return 0


def _snooze_action(hours: int) -> str:
    return "snooze_7d" if hours == 168 else f"snooze_{hours}h"


def _systems():
    from brief.config import config_file
    from brief.systems import Systems

    try:
        return Systems.load(config_file("systems.yaml"))
    except Exception as exc:  # a broken systems.yaml should not hide the results
        print(f"systems.yaml not loaded ({exc}); hosts are treated as unknown", file=sys.stderr)
        return Systems()


def run_digest(args, state: Path, out=print, out_dir: Path | None = None) -> int:
    from datetime import UTC, datetime

    from brief import outputs
    from brief.render_md import digest_markdown
    from brief.store import parse_time

    out_dir = out_dir or Path(os.environ.get("BRIEF_OUT", "/out"))
    now = parse_time(args.now) if args.now else datetime.now(UTC)
    store = Store(state / "baseline.db")
    try:
        # the CLI cannot see the receiver's uptime, so only the database and event flow are judged here
        digest = outputs.run_daily(
            store,
            _systems(),
            now,
            out_dir,
            state_dir=state,
            write=not args.dry_run,
            extra_problems=[],
            ignore_learning=getattr(args, "ignore_learning", False),
        )
    finally:
        store.close()
    if args.dry_run:
        for line in digest_markdown(digest, now.strftime("%Y-%m-%d")):
            out(line)
    else:
        out(digest.headline)
        out(f"Wrote {out_dir}/latest/digest.md and status.json, {now:%Y-%m-%d}/ and state.json.")
    return 0 if digest.run_ok else 1


def run_export(args, state: Path, out=print, out_dir: Path | None = None) -> int:
    from datetime import UTC, datetime

    from brief import export

    if (args.sig is None) == (args.top is None):
        print("Give either --sig ID or --top N.", file=sys.stderr)
        return 2
    out_dir = out_dir or Path(os.environ.get("BRIEF_OUT", "/out"))
    today = datetime.now(UTC).date()
    store = Store(state / "baseline.db")
    try:
        ids = (
            [args.sig]
            if args.sig is not None
            else export.top_signatures(store, args.host, today, args.days, args.top)
        )
        if not ids:
            print(f"No signatures for {args.host} in the last {args.days} days.", file=sys.stderr)
            return 1
        for sid in ids:
            out(str(export.write_export(store, out_dir, args.host, sid, today, args.days)))
    finally:
        store.close()
    return 0


def run_weekly(args, state: Path, out=print, out_dir: Path | None = None) -> int:
    from datetime import timedelta

    from brief import weekly

    out_dir = out_dir or Path(os.environ.get("BRIEF_OUT", "/out"))
    try:
        monday = weekly.parse_week(args.week) if args.week else weekly.current_monday() - timedelta(days=7)
    except (ValueError, TypeError):
        print("--week looks like 2026-W40.", file=sys.stderr)
        return 2
    store = Store(state / "baseline.db")
    try:
        systems = _systems()
        if args.dry_run:
            for line in weekly.weekly_markdown(store, systems, monday):
                out(line)
        else:
            out(str(weekly.write_weekly(store, systems, out_dir, monday)))
    finally:
        store.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="brief", description="homelab-brief command line")
    sub = parser.add_subparsers(dest="group", required=True)
    admin = sub.add_parser("admin", help="the admin account").add_subparsers(dest="cmd", required=True)
    sp = admin.add_parser("set-password", help="create the admin account or change its password")
    sp.add_argument("--user", default="admin")
    sp.add_argument("--password-stdin", action="store_true", help="read the password from one line of stdin")
    imp = sub.add_parser("import", help="one-off backfill of old syslog-<ip>.log files (read-only)")
    imp.add_argument("--from", dest="source", default="/import", help="folder with syslog-<ip>.log files")
    det = sub.add_parser(
        "detect", help="run the detectors over the baseline and print the findings (read-only)"
    )
    det.add_argument("--kind", action="append", choices=ALL_KINDS, help="only this detector (repeatable)")
    det.add_argument("--json", action="store_true", help="machine-readable output")
    det.add_argument(
        "--ignore-learning", action="store_true", help="show what would fire after the learning period"
    )
    det.add_argument("--now", help="run as if it were this UTC time, like 2026-10-04T12:00:00Z")
    rp = sub.add_parser(
        "replay", help="run the detectors over past days and show what would have fired (read-only)"
    )
    rp.add_argument("--from", dest="start", required=True, help="first day, like 2026-10-01")
    rp.add_argument("--to", dest="end", required=True, help="last day, like 2026-10-03")
    rp.add_argument("--at", default="06:00", help="UTC time of the daily run (default 06:00)")
    rp.add_argument("--no-hourly", action="store_true", help="check absence only at the daily run")
    rp.add_argument("--ignore-learning", action="store_true", help="ignore the learning period")
    rp.add_argument("--json", action="store_true", help="machine-readable output")
    rp.add_argument("--verbose", action="store_true", help="list every new finding, not the top few")
    rp.add_argument("--dry-run", action="store_true", help="accepted for completeness: a replay never writes")
    dg = sub.add_parser(
        "digest", help="run the daily job now: detectors, digest, host files, state.json, status.json"
    )
    dg.add_argument("--now", help="run as if it were this UTC time, like 2026-10-04T06:00:00Z")
    dg.add_argument("--dry-run", action="store_true", help="print the digest, write nothing")
    dg.add_argument(
        "--ignore-learning", action="store_true", help="ignore the learning period (for imported history)"
    )
    ex = sub.add_parser("export", help="write a CSV history of one signature on one host to /out/export")
    ex.add_argument("--host", required=True, help="the sender, like 198.51.100.1")
    ex.add_argument("--sig", type=int, help="the signature id")
    ex.add_argument("--top", type=int, help="instead of --sig: export the host's N busiest signatures")
    ex.add_argument("--days", type=int, default=30)
    al = sub.add_parser("alerts", help="list the alerts and today's push budget")
    al.add_argument("--all", action="store_true", help="include resolved alerts")
    ak = sub.add_parser("ack", help="acknowledge an alert: no repeats until it gets worse")
    ak.add_argument("finding_id")
    sn = sub.add_parser("snooze", help="snooze an alert: quiet for a while, or until it gets worse")
    sn.add_argument("finding_id")
    sn.add_argument(
        "--hours", type=int, default=24, help="24 or 168 (7 days) unless priorities.yaml says otherwise"
    )
    wk = sub.add_parser("weekly", help="write the weekly trend report to /out/weekly")
    wk.add_argument("--week", help="an ISO week like 2026-W40 (default: the last complete week)")
    wk.add_argument("--dry-run", action="store_true", help="print the report, write nothing")
    args = parser.parse_args(argv)
    state = Path(os.environ.get("BRIEF_STATE", "/state"))
    if args.group == "replay":
        return run_replay(args, state)
    if args.group == "alerts":
        return run_alerts(args, state)
    if args.group in ("ack", "snooze"):
        return run_ack(args, state)
    if args.group in ("digest", "export", "weekly"):
        return {"digest": run_digest, "export": run_export, "weekly": run_weekly}[args.group](args, state)
    if args.group == "detect":
        return run_detect(args, state)
    if args.group == "import":
        from brief.importer import main as run_import

        return run_import(args.source, state, Path(os.environ.get("BRIEF_ARCHIVE", "/archive")))
    if args.group == "admin" and args.cmd == "set-password":
        password = sys.stdin.readline().rstrip("\r\n") if args.password_stdin else None
        return set_password(args.user, state, password)
    return 2


if __name__ == "__main__":
    sys.exit(main())
