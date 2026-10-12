#!/usr/bin/env python3
"""End-to-end check of the one-command Docker Compose install (deploy/docker-compose.quickstart.yml), the way a
stranger would run it (Linux or WSL, Docker with the compose plugin).

  - `docker compose up -d` from a clean folder brings the container up healthy, with the ports and folders the file
    promises (the ports and folder come from the HB_* variables, the same ones a .env file would set)
  - the setup link in the log carries the HTTPS port the browser must use, and the console answers there
  - syslog sent to the published port (UDP and TCP) is counted by the pipeline
  - `down` then `up -d` keeps the data (the folders are real folders, not the container's layer)

  python3 scripts/e2e_quickstart.py [--image homelab-brief:dev] [--syslog-port 15514] [--http-port 18085]
                                    [--https-port 18445]
"""

import argparse
import json
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "deploy" / "docker-compose.quickstart.yml"
PROJECT = "hbquickstart"


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--image", default="homelab-brief:dev")
    ap.add_argument("--syslog-port", type=int, default=15514)
    ap.add_argument("--http-port", type=int, default=18085)
    ap.add_argument("--https-port", type=int, default=18445)
    args = ap.parse_args(argv)
    data = Path(tempfile.mkdtemp(prefix="hb-e2e-qs-"))
    env = {
        **os.environ,
        "HB_IMAGE": args.image,
        "HB_DATA": str(data),
        "HB_SYSLOG_PORT": str(args.syslog_port),
        "HB_HTTP_PORT": str(args.http_port),
        "HB_HTTPS_PORT": str(args.https_port),
    }
    base = f"http://127.0.0.1:{args.http_port}"
    problems: list[str] = []

    def compose(*a, check=True):
        cmd = ["docker", "compose", "-p", PROJECT, "-f", str(COMPOSE), *a]
        return subprocess.run(cmd, env=env, capture_output=True, text=True, check=check)

    def get(url, tls=False):
        ctx = ssl._create_unverified_context() if tls else None
        with urllib.request.urlopen(url, timeout=5, context=ctx) as r:
            return r.status, r.read().decode("utf-8", "replace")

    def until(what, check, timeout=120):
        for _ in range(timeout):
            try:
                if check():
                    return
            except Exception:  # not up yet
                pass
            time.sleep(1)
        raise RuntimeError(f"timed out waiting for {what}")

    def healthy():
        status, body = get(f"{base}/healthz")
        return status == 200 and json.loads(body)["status"] == "ok"

    def events():
        return json.loads(get(f"{base}/status.json")[1])["pipeline"]["events_24h"] or 0

    def check(ok, message):
        if not ok:
            problems.append(message)
            print(f"[quickstart] FAIL: {message}")

    try:
        compose("up", "-d")
        until("a healthy container", healthy)
        print("[quickstart] up and healthy")

        log = compose("logs", "homelab-brief").stdout
        token_lines = [ln for ln in log.splitlines() if '"setup_token"' in ln]
        check(token_lines, "no setup_token line in the log")
        if token_lines:
            check(
                f":{args.https_port}/admin/setup" in token_lines[0],
                "the setup link does not carry the HTTPS port",
            )
        status, _ = get(f"https://127.0.0.1:{args.https_port}/admin/login", tls=True)
        check(status == 200, f"the console login page answered {status}")
        status, page = get(f"{base}/")
        check(status == 200 and "banner" in page, "the public status page did not render")

        now = datetime.now(UTC).strftime("%b %e %H:%M:%S")
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for i in range(6):
            udp.sendto(
                f"<14>{now} qs-host app[1]: quickstart udp line {i}".encode(), ("127.0.0.1", args.syslog_port)
            )
        with socket.create_connection(("127.0.0.1", args.syslog_port), timeout=5) as tcp:
            for i in range(3):
                tcp.sendall(f"<14>{now} qs-host app[1]: quickstart tcp line {i}\n".encode())
        until("the nine lines to be counted", lambda: events() >= 9)
        print(f"[quickstart] {events()} events counted from UDP and TCP")

        compose("down")
        compose("up", "-d")
        until("a healthy container after down and up", healthy)
        check(events() >= 9, f"events did not survive down and up (now {events()})")
        print("[quickstart] data survived down and up")
    except Exception as exc:
        problems.append(f"{type(exc).__name__}: {exc}")
        print(f"[quickstart] FAIL: {problems[-1]}")
    finally:
        if problems:
            print(compose("logs", "--tail", "40", "homelab-brief", check=False).stdout[-2500:])
        compose("down", "-v", check=False)
        subprocess.run(  # the folders belong to the container's user, so remove them from inside an image
            ["docker", "run", "--rm", "--entrypoint", "rm", "-v", f"{data}:/t", args.image, "-rf",
             "/t/config", "/t/state", "/t/archive", "/t/out"],
            capture_output=True, check=False,
        )  # fmt: skip
        try:
            data.rmdir()
        except OSError:
            pass
    print("[quickstart] " + ("PASS" if not problems else f"{len(problems)} problem(s)"))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
