#!/usr/bin/env python3
"""End-to-end check of the Phase 2 collector path against the built image (Linux or WSL, Docker).

Starts a fake Unraid GraphQL server (scripts/fake_unraid.py) and the app on one Docker network, enables the
unraid collector in collectors.yaml, and checks over real HTTP and the real CLI that:
  - the config guard accepts the file, the hourly job runs the collector, and /status.json carries its numbers
  - /metrics and /api/v1/metrics list the sensors the collector declared (no code outside the collector)
  - `brief collector test unraid` and `brief collector list` work, and a wrong key is reported, not hidden
  - the fake server saw the right key on every request and only queries
Then switches the collector off and checks `brief collector list` says so.

  python3 scripts/e2e_collector.py [--image homelab-brief:dev] [--http-port 18082]
"""

import argparse
import json
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NET, FAKE, APP = "hb-e2e-col-net", "hb-e2e-col-fake", "hb-e2e-col-app"
YAML = """schema: 1
collectors:
  unraid:
    enabled: {enabled}
    servers:
      - {{ id: server-1, url: "http://{fake}:8080/graphql", key_var: UNRAID_SERVER_1_API_KEY }}
"""


def docker(*args, check=True, **kw):
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check, **kw)


def get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as reply:
        return reply.read().decode()


def until(what, check, timeout=90):
    for _ in range(timeout):
        try:
            if check():
                return
        except Exception:  # not up yet
            pass
        time.sleep(1)
    raise RuntimeError(f"timed out waiting for {what}")


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--image", default="homelab-brief:dev")
    ap.add_argument("--http-port", type=int, default=18082)
    args = ap.parse_args(argv)
    tmp = Path(tempfile.mkdtemp(prefix="hb-e2e-col-"))
    for d in ("state", "archive", "config/brief"):
        (tmp / d).mkdir(parents=True)
        (tmp / d).chmod(0o777)
    (tmp / "config").chmod(0o777)
    conf = tmp / "config/brief/collectors.yaml"
    conf.write_text(YAML.format(enabled="true", fake=FAKE))
    problems: list[str] = []

    def check(ok, message):
        if not ok:
            problems.append(message)
            print(f"[collector] FAIL: {message}")

    try:
        docker("network", "rm", NET, check=False)
        docker("network", "create", NET)
        docker("rm", "-f", FAKE, APP, check=False)
        docker("run", "-d", "--name", FAKE, "--network", NET, "-v", f"{ROOT}/scripts/fake_unraid.py:/fake.py:ro",
               "--entrypoint", "python", args.image, "/fake.py")  # fmt: skip
        docker("run", "-d", "--name", APP, "--network", NET, "-e", "SYSLOG_PORT=5514", "-e", "BRIEF_INGEST_INTERVAL=1",
               "-e", "UNRAID_SERVER_1_API_KEY=e2e-test-key", "-v", f"{tmp}/state:/state", "-v", f"{tmp}/config:/config",
               "-v", f"{tmp}/archive:/archive", "-p", f"{args.http_port}:80", args.image)  # fmt: skip

        def status():
            return json.loads(get(args.http_port, "/status.json"))

        until(
            "the collector to run", lambda: status().get("collectors", {}).get("unraid", {}).get("last_run")
        )
        block = status()["collectors"]["unraid"]
        check(block["ok"] is True, f"the collector did not succeed: {block}")
        numbers = block["metrics"]
        check(numbers.get("unraid_server_1_reachable") == 1, f"numbers: {numbers}")
        check(numbers.get("unraid_server_1_array_started") == 1, f"numbers: {numbers}")
        check(numbers.get("unraid_server_1_disk_temp_max_c") == 33, f"hottest disk: {numbers}")
        metrics = get(args.http_port, "/metrics")
        check("unraid_server_1_reachable" in metrics, "/metrics lacks the collector's sensors")
        check("collector_unraid_ok" in metrics, "/metrics lacks the collector's ok flag")
        api = get(args.http_port, "/api/v1/metrics")
        check("unraid_server_1_array_started" in api, "/api/v1/metrics lacks the collector's sensors")

        test = docker("exec", APP, "brief", "collector", "test", "unraid", check=False)
        check(
            test.returncode == 0 and "1 of 1 servers answered" in test.stdout,
            f"collector test: {test.stdout} {test.stderr}",
        )
        check("vms: not available" in test.stdout, "the test did not report that VMs are unavailable")
        listing = docker("exec", APP, "brief", "collector", "list", check=False).stdout
        check("unraid" in listing and " ok" in listing, f"collector list: {listing}")
        logs = docker("logs", APP, check=False).stdout
        events = [json.loads(x).get("event") for x in logs.splitlines() if x.startswith("{")]
        check(
            "config_error" not in events and "collector_failed" not in events,
            "the log shows a config or collector error",
        )

        seen = docker("logs", FAKE, check=False)
        lines = [x for x in (seen.stdout + seen.stderr).splitlines() if x.startswith("POST")]
        check(
            len(lines) >= 16 and all(x.endswith("key_ok=True") for x in lines), f"the fake saw: {lines[-3:]}"
        )

        bad = docker(
            "exec",
            "-e",
            "UNRAID_SERVER_1_API_KEY=wrong",
            APP,
            "brief",
            "collector",
            "test",
            "unraid",
            check=False,
        )
        check(
            bad.returncode == 1 and "rejected" in bad.stdout + bad.stderr,
            f"a wrong key: {bad.stdout} {bad.stderr}",
        )

        conf.write_text(YAML.format(enabled="false", fake=FAKE))
        after = docker("exec", APP, "brief", "collector", "list", check=False).stdout
        check("off" in after, f"collector list after disabling: {after}")
        print(f"[collector] numbers: {numbers}")
    except Exception as exc:
        problems.append(f"{type(exc).__name__}: {exc}")
        print(f"[collector] FAIL: {problems[-1]}")
    finally:
        if problems:
            print(docker("logs", APP, check=False).stdout[-1500:])
        docker("rm", "-f", FAKE, APP, check=False)
        docker("network", "rm", NET, check=False)
        docker("run", "--rm", "--entrypoint", "rm", "-v", f"{tmp}:/t", args.image, "-rf",
               "/t/state", "/t/archive", "/t/config", check=False)  # fmt: skip
        try:
            tmp.rmdir()
        except OSError:
            pass
    print("[collector] " + ("PASS" if not problems else f"{len(problems)} problem(s)"))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
