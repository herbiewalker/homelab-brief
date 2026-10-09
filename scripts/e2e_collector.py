#!/usr/bin/env python3
"""End-to-end check of the Phase 2 collector path against the built image (Linux or WSL, Docker).

Starts a fake Unraid GraphQL server (scripts/fake_unraid.py) and the app on one Docker network, enables the
unraid collector in collectors.yaml, and checks over real HTTP and the real CLI that:
  - the config guard accepts the file, the hourly job runs the collector, and /status.json carries its numbers
  - /metrics and /api/v1/metrics list the sensors the collector declared (no code outside the collector)
  - `brief collector test unraid` and `brief collector list` work, and a wrong key is reported, not hidden
  - the fake server saw the right key on every request and only queries
Then the UniFi collector the same way, with firmware arriving in a syslog header; and switches the collectors off and checks `brief collector list` says so.

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
NET, FAKE, FAKE2, APP = "hb-e2e-col-net", "hb-e2e-col-fake", "hb-e2e-col-unifi", "hb-e2e-col-app"
YAML = """schema: 1
collectors:
  unraid:
    enabled: {enabled}
    servers:
      - {{ id: server-1, url: "http://{fake}:8080/graphql", key_var: UNRAID_SERVER_1_API_KEY }}
  unifi:
    enabled: {enabled}
    url_var: UNIFI_URL
    key_var: UNIFI_API_KEY
    clients: true
    client_logs: true
"""
DEVICES = """schema: 1
devices:
  - { mac: "00:00:5e:00:53:41", label: "Full laptop", profile: full }
  - { mac: "00:00:5e:00:53:42", label: "Security console", profile: security_only }
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
    (tmp / "config/brief/devices.yaml").write_text(DEVICES)
    problems: list[str] = []

    def check(ok, message):
        if not ok:
            problems.append(message)
            print(f"[collector] FAIL: {message}")

    try:
        docker("network", "rm", NET, check=False)
        docker("network", "create", NET)
        docker("rm", "-f", FAKE, FAKE2, APP, check=False)
        docker("run", "-d", "--name", FAKE2, "--network", NET, "-v", f"{ROOT}/scripts/fake_unifi.py:/fake.py:ro",
               "--entrypoint", "python", args.image, "/fake.py")  # fmt: skip
        docker("run", "-d", "--name", FAKE, "--network", NET, "-v", f"{ROOT}/scripts/fake_unraid.py:/fake.py:ro",
               "--entrypoint", "python", args.image, "/fake.py")  # fmt: skip
        docker("run", "-d", "--name", APP, "--network", NET, "-e", "SYSLOG_PORT=5514", "-e", "BRIEF_INGEST_INTERVAL=1",
               "-e", "UNRAID_SERVER_1_API_KEY=e2e-test-key", "-e", f"UNIFI_URL=http://{FAKE2}:8081",
               "-e", "UNIFI_API_KEY=e2e-unifi-key", "-v", f"{tmp}/state:/state", "-v", f"{tmp}/config:/config",
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

        # ---- UniFi: the API, the topology, and the firmware that arrives in a syslog header
        until(
            "the UniFi collector to run",
            lambda: status().get("collectors", {}).get("unifi", {}).get("last_run"),
        )
        unifi = status()["collectors"]["unifi"]
        check(unifi["ok"] is True, f"the UniFi collector did not succeed: {unifi}")
        check(unifi["metrics"].get("unifi_devices") == 3, f"UniFi numbers: {unifi['metrics']}")
        check(unifi["metrics"].get("unifi_updates_available") == 1, f"UniFi numbers: {unifi['metrics']}")
        check("unifi_devices" in get(args.http_port, "/metrics"), "/metrics lacks the UniFi sensors")
        utest = docker("exec", APP, "brief", "collector", "test", "unifi", check=False)
        check(
            utest.returncode == 0 and "3 devices, 1 with a firmware update" in utest.stdout,
            f"unifi test: {utest.stdout} {utest.stderr}",
        )
        send = (
            "import socket,time;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);t=time.strftime('%b %e %H:%M:%S');"
            "s.sendto(f'<14>{t} host-x 00005e005399,USW-Flex-7.5.10+17129: : app[1]: hello'.encode(),('127.0.0.1',5514))"
        )
        docker("exec", APP, "python", "-c", send)
        query = (
            "import sqlite3;c=sqlite3.connect('file:/state/baseline.db?mode=ro',uri=True);"
            "print(c.execute(\"select firmware from device_header\").fetchone() or '')"
        )
        until(
            "the header firmware to be stored",
            lambda: "7.5.10+17129" in docker("exec", APP, "python", "-c", query).stdout,
        )
        docker("exec", APP, "brief", "collector", "run", "unifi")
        inventory = (
            "import sqlite3;c=sqlite3.connect('file:/state/baseline.db?mode=ro',uri=True);"
            "print(c.execute(\"select current_version from inventory where item_id='unifi:00005e005399'\").fetchone(),"
            "c.execute(\"select count(*) from topo_edge where kind='uplink'\").fetchone())"
        )
        result = docker("exec", APP, "python", "-c", inventory).stdout
        check(
            "7.5.10+17129" in result and "(2,)" in result,
            f"inventory and topology after the header: {result}",
        )
        # ---- clients: presence from the API, DNS and AP lines through Vector, each limited by profile
        check(unifi["metrics"].get("unifi_clients") == 2, f"UniFi clients: {unifi['metrics']}")
        lines = []
        for mac in ("00:00:5e:00:53:41", "00:00:5e:00:53:42"):
            dns = (
                '{"type":"dnsAdBlock","category":"ADVERTISEMENT","domain":"ads.example","ip":"198.51.100.19",'
                f'"mac":"{mac}","src_ip":"198.51.100.19"}}'
            )
            lines.append(f"host-gw host-gw coredns[4650]: {dns}")
            lines.append(
                f"host-ap 00005e005310,U7-Pro-Max-8.7.11+19419: wevent[7482]: wevent.ubnt_custom_event(): EVENT_STA_JOIN wifi2ap12: {mac} / 1"
            )
        send2 = (
            "import socket,time;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);t=time.strftime('%b %e %H:%M:%S');"
            f"[s.sendto(f'<14>{{t}} {{l}}'.encode(),('127.0.0.1',5514)) for l in {lines!r}]"
        )
        docker("exec", APP, "python", "-c", send2)
        sql = (
            "import sqlite3,json;c=sqlite3.connect('file:/state/baseline.db?mode=ro',uri=True);"
            "print(json.dumps({'dns':[tuple(r) for r in c.execute(\"select client,queries,blocked from dns_hourly\")],"
            "'clients':sorted(r[0] for r in c.execute(\"select mac from client\")),"
            "'minutes':[tuple(r) for r in c.execute(\"select mac,online_min from client_hourly\")],"
            "'suppressed':sorted(r[0] for r in c.execute(\"select rule from suppressed\") if r[0].startswith('device:'))}))"
        )
        until(
            "the log lines to be ingested",
            lambda: (
                lambda seen: all(x in seen for x in ('"dns": [["00:00:5e:00:53:41"', "device:security_only"))
            )(docker("exec", APP, "python", "-c", sql).stdout),
        )
        got = json.loads(docker("exec", APP, "python", "-c", sql).stdout)
        check(got["dns"] == [["00:00:5e:00:53:41", 1, 1]], f"DNS rows: {got['dns']}")
        check(got["clients"] == ["00:00:5e:00:53:41", "00:00:5e:00:53:42"], f"client rows: {got['clients']}")
        check(got["minutes"] == [["00:00:5e:00:53:41", 60]], f"presence minutes: {got['minutes']}")
        check(
            got["suppressed"] == ["device:security_only"],
            f"suppressed: {got['suppressed']}",
        )
        bad2 = docker(
            "exec", "-e", "UNIFI_API_KEY=wrong", APP, "brief", "collector", "test", "unifi", check=False
        )
        check(
            bad2.returncode == 1 and "rejected" in bad2.stdout + bad2.stderr,
            f"a wrong UniFi key: {bad2.stdout} {bad2.stderr}",
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
        docker("rm", "-f", FAKE, FAKE2, APP, check=False)
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
