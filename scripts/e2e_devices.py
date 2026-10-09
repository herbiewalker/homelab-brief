#!/usr/bin/env python3
"""End-to-end check of the SNMP and iDRAC collectors against the built image (Linux or WSL, Docker).

Runs the REAL `snmpget`, `snmpbulkwalk` and `ipmitool` that the image carries:
  - a tiny SNMPv2c agent (scripts/fake_snmp.py) answers a switch and a UPS target, so the collector's command
    lines and its parsing of the tools' real output are proven (the unit tests fake the tools)
  - a fake Redfish API (scripts/fake_idrac.py) answers the iDRAC collector with Basic authentication
  - a wrong SNMP community is reported as "no answer", and `ipmitool` against a host with no IPMI is reported
    safely, without the host or the password in the message

  python3 scripts/e2e_devices.py [--image homelab-brief:dev] [--http-port 18083]
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
NET, SNMP, IDRAC, APP = "hb-e2e-dev-net", "hb-e2e-dev-snmp", "hb-e2e-dev-idrac", "hb-e2e-dev-app"
YAML = f"""schema: 1
collectors:
  snmp:
    enabled: true
    version: v2c
    community_var: SNMP_COMMUNITY
    targets:
      - {{ id: switch-1, host: {SNMP}, port: 1161, profile: switch }}
      - {{ id: ups-1, host: {SNMP}, port: 1161, profile: ups }}
  idrac:
    enabled: true
    protocol: redfish
    host_var: IDRAC_HOST
    user_var: IDRAC_USER
    password_var: IDRAC_PASSWORD
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
    ap.add_argument("--http-port", type=int, default=18083)
    args = ap.parse_args(argv)
    tmp = Path(tempfile.mkdtemp(prefix="hb-e2e-dev-"))
    for d in ("state", "archive", "config/brief"):
        (tmp / d).mkdir(parents=True)
        (tmp / d).chmod(0o777)
    (tmp / "config").chmod(0o777)
    (tmp / "config/brief/collectors.yaml").write_text(YAML)
    problems: list[str] = []

    def check(ok, message):
        if not ok:
            problems.append(message)
            print(f"[devices] FAIL: {message}")

    try:
        docker("network", "rm", NET, check=False)
        docker("network", "create", NET)
        docker("rm", "-f", SNMP, IDRAC, APP, check=False)
        for name, script in ((SNMP, "fake_snmp.py"), (IDRAC, "fake_idrac.py")):
            docker("run", "-d", "--name", name, "--network", NET, "-v", f"{ROOT}/scripts/{script}:/fake.py:ro",
                   "--entrypoint", "python", args.image, "/fake.py")  # fmt: skip
        docker("run", "-d", "--name", APP, "--network", NET, "-e", "SYSLOG_PORT=5514", "-e", "BRIEF_INGEST_INTERVAL=1",
               "-e", "SNMP_COMMUNITY=e2e-comm", "-e", f"IDRAC_HOST=http://{IDRAC}:8082", "-e", "IDRAC_USER=monitor",
               "-e", "IDRAC_PASSWORD=idrac-e2e-pw", "-v", f"{tmp}/state:/state", "-v", f"{tmp}/config:/config",
               "-v", f"{tmp}/archive:/archive", "-p", f"{args.http_port}:80", args.image)  # fmt: skip

        def collectors():
            return json.loads(get(args.http_port, "/status.json")).get("collectors", {})

        until(
            "both collectors to run",
            lambda: all(collectors().get(n, {}).get("last_run") for n in ("snmp", "idrac")),
        )
        snmp_block, idrac_block = collectors()["snmp"], collectors()["idrac"]
        check(snmp_block["ok"] is True, f"snmp did not succeed: {snmp_block}")
        got = snmp_block["metrics"]
        check(
            got.get("snmp_switch_1_ports_up") == 3 and got.get("snmp_switch_1_ports_total") == 4,
            f"ports: {got}",
        )
        check(got.get("snmp_switch_1_uptime_h") == 24.0, f"uptime: {got}")
        check(
            got.get("snmp_ups_1_charge_pct") == 100 and got.get("snmp_ups_1_battery_ok") == 1, f"ups: {got}"
        )
        check(got.get("snmp_ups_1_on_battery") == 0 and got.get("snmp_ups_1_power_w") == 120, f"ups: {got}")
        check(idrac_block["ok"] is True, f"idrac did not succeed: {idrac_block}")
        check(idrac_block["metrics"].get("idrac_power_w") == 112, f"idrac: {idrac_block['metrics']}")
        check(idrac_block["metrics"].get("idrac_sel_entries") == 9, f"idrac: {idrac_block['metrics']}")
        metrics = get(args.http_port, "/metrics")
        check(
            "snmp_switch_1_ports_up" in metrics and "idrac_power_w" in metrics, "/metrics lacks the sensors"
        )

        for tool, flag in (("snmpget", "--version"), ("snmpbulkwalk", "--version"), ("ipmitool", "-V")):
            done = docker("exec", APP, tool, flag, check=False)
            check(done.returncode == 0, f"{tool} is not usable in the image: {done.stderr}")

        again = docker("exec", APP, "brief", "collector", "run", "snmp", check=False)
        check(again.returncode == 0, f"collector run snmp: {again.stdout} {again.stderr}")
        errors = collectors()["snmp"]["metrics"].get("snmp_switch_1_if_errors")
        check(errors == 0, f"interface errors since the last poll should be 0, got {errors}")

        test = docker("exec", APP, "brief", "collector", "test", "snmp", check=False)
        check(
            test.returncode == 0 and "2 of 2 targets answered" in test.stdout,
            f"snmp test: {test.stdout} {test.stderr}",
        )

        wrong = docker(
            "exec", "-e", "SNMP_COMMUNITY=wrong", APP, "brief", "collector", "test", "snmp", check=False
        )
        check(
            wrong.returncode == 1 and "no answer" in wrong.stdout + wrong.stderr,
            f"wrong community: {wrong.stdout} {wrong.stderr}",
        )
        check("e2e-comm" not in wrong.stdout + wrong.stderr, "the community appeared in the output")

        (tmp / "config/brief/collectors.yaml").write_text(YAML.replace("protocol: redfish", "protocol: ipmi"))
        ipmi = docker(
            "exec", "-e", f"IDRAC_HOST={SNMP}", APP, "brief", "collector", "test", "idrac", check=False
        )
        text = ipmi.stdout + ipmi.stderr
        check(
            ipmi.returncode == 1 and any(w in text for w in ("could not connect", "no answer", "refused")),
            f"ipmi: {text}",
        )
        check(
            SNMP not in text and "idrac-e2e-pw" not in text,
            "the address or password appeared in the ipmi message",
        )

        seen = docker("logs", IDRAC, check=False)
        lines = [x for x in (seen.stdout + seen.stderr).splitlines() if x.startswith("GET")]
        check(
            len(lines) >= 3 and all(x.endswith("auth_ok=True") for x in lines),
            f"redfish requests: {lines[-3:]}",
        )
        print(f"[devices] snmp numbers: {got}")
    except Exception as exc:
        problems.append(f"{type(exc).__name__}: {exc}")
        print(f"[devices] FAIL: {problems[-1]}")
    finally:
        if problems:
            print(docker("logs", APP, check=False).stdout[-1500:])
        docker("rm", "-f", SNMP, IDRAC, APP, check=False)
        docker("network", "rm", NET, check=False)
        docker("run", "--rm", "--entrypoint", "rm", "-v", f"{tmp}:/t", args.image, "-rf",
               "/t/state", "/t/archive", "/t/config", check=False)  # fmt: skip
        try:
            tmp.rmdir()
        except OSError:
            pass
    print("[devices] " + ("PASS" if not problems else f"{len(problems)} problem(s)"))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
