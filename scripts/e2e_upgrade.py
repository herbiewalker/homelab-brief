#!/usr/bin/env python3
"""Upgrade check: an install made by an older image keeps its config, history, and admin login on a newer one.

Starts the OLD image on fresh config and state folders, makes an admin account, adds a config file, sends a
few syslog lines and waits until they are counted. Stops it, starts the NEW image on the same folders, and
checks that: it becomes healthy, the config file is byte-for-byte unchanged, the counted events and the
audit log are still there, the admin can still sign in with the same password, the database schema did not
go backwards, and the program version was recorded. (Linux or WSL with Docker.)

  python3 scripts/e2e_upgrade.py --old homelab-brief:0.0.25 --new homelab-brief:ci [--http-port 18081] [--https-port 18444]
"""

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import e2e_web  # noqa: E402  (reuses its HTTPS client)

NAME = "hb-e2e-upgrade"
PASSWORD = "correct horse battery staple"
SYSTEMS = "schema: 1\nsystems:\n  - id: gw\n    label: Gateway\n    hosts: [198.51.100.1]\n"
SEND = (
    "import socket,time;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);"
    "t=time.strftime('%b %e %H:%M:%S');"
    "[s.sendto(('<14>%s gw app: upgrade check line %d'%(t,i)).encode(),('127.0.0.1',5514)) for i in range(6)]"
)
DB = "import sqlite3;c=sqlite3.connect('file:/state/baseline.db?mode=ro',uri=True);"


def docker(*args, check=True, **kw):
    return e2e_web.docker(*args, check=check, **kw)


def inside(code: str) -> str:
    return docker("exec", NAME, "python", "-c", code, check=False).stdout.strip()


def start(image, tmp, http_port, https_port):
    docker("rm", "-f", NAME, check=False)
    docker("run", "-d", "--name", NAME, "-e", "SYSLOG_PORT=5514", "-e", "BRIEF_INGEST_INTERVAL=1",
           "-e", f"HTTPS_PUBLIC_PORT={https_port}", "-v", f"{tmp}/state:/state", "-v", f"{tmp}/config:/config",
           "-v", f"{tmp}/archive:/archive", "-p", f"{http_port}:80", "-p", f"{https_port}:443", image)  # fmt: skip


def until(what, check, timeout=90):
    for _ in range(timeout):
        try:
            if check():
                return
        except Exception:  # not up yet
            pass
        time.sleep(1)
    raise RuntimeError(f"timed out waiting for {what}")


def events_24h() -> int:
    out = inside(
        "import json,urllib.request as u;print(json.load(u.urlopen('http://127.0.0.1:80/status.json'))['pipeline']['events_24h'])"
    )
    return int(out)


def healthy() -> bool:
    return docker("exec", NAME, "python", "-m", "brief.healthcheck", check=False).returncode == 0


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--old", required=True)
    ap.add_argument("--new", required=True)
    ap.add_argument("--http-port", type=int, default=18081)
    ap.add_argument("--https-port", type=int, default=18444)
    args = ap.parse_args(argv)
    tmp = Path(tempfile.mkdtemp(prefix="hb-e2e-upgrade-"))
    for d in ("state", "archive", "config/brief"):
        (tmp / d).mkdir(parents=True)
        (tmp / d).chmod(0o777)
    (tmp / "config").chmod(0o777)
    (tmp / "config/brief/systems.yaml").write_text(SYSTEMS)
    problems: list[str] = []

    def check(ok, message):
        if not ok:
            problems.append(message)
            print(f"[upgrade] FAIL: {message}")

    try:
        start(args.old, tmp, args.http_port, args.https_port)
        until("the old image to be healthy", healthy)
        cli = subprocess.run(["docker", "exec", "-i", NAME, "brief", "admin", "set-password", "--password-stdin"],
                             input=PASSWORD + "\n", capture_output=True, text=True)  # fmt: skip
        check(cli.returncode == 0, f"set-password on the old image: {cli.stdout} {cli.stderr}")
        inside(SEND)
        until("the old image to count the events", lambda: events_24h() >= 6)
        before_events = events_24h()
        schema_before = int(inside(DB + "print(c.execute('pragma user_version').fetchone()[0])"))
        audit_before = int(inside(DB + "print(c.execute('select count(*) from audit_log').fetchone()[0])"))
        docker("stop", "-t", "15", NAME)
        docker("rm", NAME)
        print(
            f"[upgrade] old image: {before_events} events, schema {schema_before}, {audit_before} audit rows"
        )

        start(args.new, tmp, args.http_port, args.https_port)
        until("the new image to be healthy", healthy)
        check((tmp / "config/brief/systems.yaml").read_text() == SYSTEMS, "systems.yaml changed on upgrade")
        until("the new image to report the old events", lambda: events_24h() >= before_events)
        schema_after = int(inside(DB + "print(c.execute('pragma user_version').fetchone()[0])"))
        check(schema_after >= schema_before, f"schema went backwards: {schema_before} -> {schema_after}")
        audit_after = int(inside(DB + "print(c.execute('select count(*) from audit_log').fetchone()[0])"))
        check(audit_after >= audit_before, f"audit log lost rows: {audit_before} -> {audit_after}")
        version = inside("print(open('/state/app-version').read().strip())")
        check(bool(version), "the new image did not record its version")

        client = e2e_web.Client()
        https_url = f"https://127.0.0.1:{args.https_port}"
        until("HTTPS", lambda: client.get(f"{https_url}/admin/login")[0] == 200)
        code, _, page = client.get(f"{https_url}/admin/login")
        code, _, _ = client.post(f"{https_url}/admin/login",
                                 {"csrf_token": e2e_web.csrf_in(page), "username": "admin", "password": PASSWORD})  # fmt: skip
        check(code == 303, f"signing in with the old password after the upgrade was {code}")
        events = [
            json.loads(x).get("event")
            for x in docker("logs", NAME, check=False).stdout.splitlines()
            if x.startswith("{")
        ]
        check("config_migration_failed" not in events, "a config migration failed")
        print(
            f"[upgrade] new image: events kept, schema {schema_after}, version {version}, audit {audit_after} rows"
        )
    except Exception as exc:
        problems.append(f"{type(exc).__name__}: {exc}")
        print(f"[upgrade] FAIL: {problems[-1]}")
    finally:
        if problems:
            log = docker("logs", NAME, check=False)
            print((log.stdout + log.stderr)[-1800:])
        docker("rm", "-f", NAME, check=False)
        docker("run", "--rm", "--entrypoint", "rm", "-v", f"{tmp}:/t", args.new,
               "-rf", "/t/state", "/t/archive", "/t/config", check=False)  # fmt: skip
        try:
            tmp.rmdir()
        except OSError:
            pass
    print("[upgrade] " + ("PASS" if not problems else f"{len(problems)} problem(s)"))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
