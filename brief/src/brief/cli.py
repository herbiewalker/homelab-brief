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
        result = detect.run(
            store, systems, now, kinds=tuple(args.kind or detect.ALL), ignore_learning=args.ignore_learning
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
    args = parser.parse_args(argv)
    state = Path(os.environ.get("BRIEF_STATE", "/state"))
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
