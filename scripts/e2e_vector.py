#!/usr/bin/env python3
"""End-to-end check of the syslog receiver, using the built image (Linux or WSL, with Docker).

For each scenario it starts a container, replays the sanitized samples over UDP and TCP, and
checks the raw archive and the normalized events:

  default  no network.yaml            everything is received, archived, and parsed
  allowed  an allowlist that fits     same, and nothing is flagged
  flag     an allowlist that excludes the sender, unknown_senders: flag    kept and flagged
  drop     the same allowlist, unknown_senders: drop                       nothing written
  backfill the one-off import of old syslog-<ip>.log files (no priority field)

  python3 scripts/e2e_vector.py [--image homelab-brief:dev] [--scenario NAME] [--port 15514]
"""

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import parse_report  # noqa: E402
import replay_samples  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
EXCLUDING = "allowed_senders: [203.0.113.0/24]\n"
SCENARIOS = {
    "default": None,
    "allowed": "allowed_senders: [0.0.0.0/0]\n",
    "flag": EXCLUDING + "unknown_senders: flag\n",
    "drop": EXCLUDING + "unknown_senders: drop\n",
}


def docker(*args, check=True):
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check)


def container_logs(name):
    log = docker("logs", name, check=False)
    return log.stdout + log.stderr


def wait_for_port(port, name, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        if docker("inspect", "-f", "{{.State.Running}}", name, check=False).stdout.strip() != "true":
            raise RuntimeError(
                "container stopped early:\n" + docker("logs", name, check=False).stdout[-1500:]
            )
        # Docker's port proxy accepts connections before the backend does, so trust Vector's log.
        log = docker("logs", name, check=False)
        text = log.stdout + log.stderr
        if "udp: Listening" in text and "tcp: Listening" in text:
            return
        time.sleep(0.5)
    raise RuntimeError("receiver did not start listening:\n" + (text[-1500:] if "text" in dir() else ""))


def event_lines(state: Path) -> list[str]:
    out = []
    for f in sorted((state / "events").glob("*.ndjson")) if (state / "events").is_dir() else []:
        out += [ln for ln in f.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return out


def archive_lines(archive: Path) -> list[str]:
    out = []
    for f in sorted(archive.glob("*/*.log")):
        out += [ln for ln in f.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return out


def settle(state: Path, want: int, timeout=40):
    end = time.time() + timeout
    last, stable_since = -1, time.time()
    while time.time() < end:
        n = len(event_lines(state))
        if n != last:
            last, stable_since = n, time.time()
        if n >= want and time.time() - stable_since > 2:
            return
        if want == 0 and time.time() - stable_since > 6:
            return
        time.sleep(0.5)


def run_scenario(name, network, image, port, lines):
    tmp = Path(tempfile.mkdtemp(prefix=f"hb-e2e-{name}-"))
    for d in ("state", "archive", "brief"):
        (tmp / d).mkdir()
        (tmp / d).chmod(0o777)
    if network:
        (tmp / "brief" / "network.yaml").write_text(network, encoding="utf-8")
    cname = f"hb-e2e-{name}"
    docker("rm", "-f", cname, check=False)
    docker(
        "run",
        "-d",
        "--rm",
        "--name",
        cname,
        "-e",
        "SYSLOG_PORT=5514",
        "-v",
        f"{tmp}/state:/state",
        "-v",
        f"{tmp}/archive:/archive",
        "-v",
        f"{tmp}/brief:/config/brief:ro",
        "-v",
        f"{ROOT}/vector:/app/vector:ro",
        "-p",
        f"{port}:5514/udp",
        "-p",
        f"{port}:5514/tcp",
        image,
    )
    problems: list[str] = []
    try:
        wait_for_port(port, cname)
        sent = replay_samples.replay(lines, "127.0.0.1", port, "both", 134, 5)
        want = 0 if name == "drop" else len(lines)
        settle(tmp / "state", want)
        events = [json.loads(x) for x in event_lines(tmp / "state")]
        archived = archive_lines(tmp / "archive")
        summary = parse_report.summarize(events)
        c = summary["counts"]
        print(
            f"[{name}] sent={len(lines)} (udp={sent['udp']} tcp={sent['tcp']}) events={len(events)} "
            f"archived={len(archived)} parse_failed={c['parse_failed']} real={c['severity_real']} "
            f"inferred={c['severity_inferred']} unknown={c['unknown_sender']}"
        )
        if name == "drop":
            if events or archived:
                problems.append(f"drop wrote {len(events)} events and {len(archived)} archive lines")
        else:
            if len(events) != len(lines):
                problems.append(f"expected {len(lines)} events, got {len(events)}")
            if len(archived) != len(lines):
                problems.append(f"expected {len(lines)} archive lines, got {len(archived)}")
            if c["parse_failed"]:
                problems.append(f"{c['parse_failed']} parse failures")
            if not (c["severity_real"] and c["severity_inferred"]):
                problems.append("expected both real and inferred severities")
            if not any(ln.startswith("<134>") for ln in archived) or all(
                ln.startswith("<") for ln in archived
            ):
                problems.append("archive should keep the raw wire form, with and without <PRI>")
            if name == "flag" and c["unknown_sender"] != len(events):
                problems.append(f"flag: {c['unknown_sender']} of {len(events)} events flagged")
            if name != "flag" and c["unknown_sender"]:
                problems.append(f"{c['unknown_sender']} events flagged unexpectedly")
            if name in ("default", "allowed") and not (
                c["cef"] and c["device_token"] and c["duplicated_host"]
            ):
                problems.append("expected CEF, device-token, and duplicated-host branches to fire")
    except Exception as exc:  # report and keep going with the other scenarios
        problems.append(str(exc))
    finally:
        if problems:
            print(container_logs(cname)[-1500:])
        docker("stop", "-t", "3", cname, check=False)
        docker(
            "run",
            "--rm",
            "--entrypoint",
            "rm",
            "-v",
            f"{tmp}:/t",
            image,
            "-rf",
            "/t/state",
            "/t/archive",
            check=False,
        )
        subprocess.run(["rm", "-rf", str(tmp)], check=False)
    for p in problems:
        print(f"[{name}] FAIL: {p}")
    return not problems


def run_backfill(image, src: Path, lines):
    """The one-off import: the old syslog-<ip>.log files go through the same parser, with the host
    taken from the file name and severity inferred (those files dropped the priority field)."""
    tmp = Path(tempfile.mkdtemp(prefix="hb-e2e-backfill-"))
    for d in ("state", "archive"):
        (tmp / d).mkdir()
        (tmp / d).chmod(0o777)
    (tmp / "state" / "vector-import").mkdir()  # the container's init step creates this; Vector will not
    (tmp / "state" / "vector-import").chmod(0o777)
    cname = "hb-e2e-backfill"
    docker("rm", "-f", cname, check=False)
    docker(
        "run",
        "-d",
        "--rm",
        "--name",
        cname,
        "--entrypoint",
        "/usr/local/bin/vector",
        "-v",
        f"{tmp}/state:/state",
        "-v",
        f"{tmp}/archive:/archive",
        "-v",
        f"{src}:/import:ro",
        "-v",
        f"{ROOT}/vector:/app/vector:ro",
        image,
        "--config",
        "/app/vector/backfill.yaml",
    )
    problems: list[str] = []
    try:
        settle(tmp / "state", len(lines))
        events = [json.loads(x) for x in event_lines(tmp / "state")]
        archived = archive_lines(tmp / "archive")
        c = parse_report.summarize(events)["counts"]
        names = {p.stem.split("-", 1)[1] for p in src.glob("syslog-*.log")}
        print(
            f"[backfill] files={len(names)} lines={len(lines)} events={len(events)} archived={len(archived)} "
            f"parse_failed={c['parse_failed']} inferred={c['severity_inferred']} real={c['severity_real']}"
        )
        if len(events) != len(lines):
            problems.append(f"expected {len(lines)} events, got {len(events)}")
        if len(archived) != len(lines):
            problems.append(f"expected {len(lines)} archive lines, got {len(archived)}")
        if c["parse_failed"]:
            problems.append(f"{c['parse_failed']} parse failures")
        if c["severity_real"] or c["severity_inferred"] != len(events):
            problems.append("every imported event should have an inferred severity")
        if not all(e.get("imported") is True and e.get("source_ip") in names for e in events):
            problems.append("imported events should carry imported: true and the sender from the file name")
    except Exception as exc:
        problems.append(str(exc))
    finally:
        if problems:
            print(container_logs(cname)[-1500:])
        docker("stop", "-t", "3", cname, check=False)
        docker(
            "run",
            "--rm",
            "--entrypoint",
            "rm",
            "-v",
            f"{tmp}:/t",
            image,
            "-rf",
            "/t/state",
            "/t/archive",
            check=False,
        )
        subprocess.run(["rm", "-rf", str(tmp)], check=False)
    for p in problems:
        print(f"[backfill] FAIL: {p}")
    return not problems


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--image", default="homelab-brief:dev")
    ap.add_argument("--scenario", choices=sorted(SCENARIOS) + ["backfill"], action="append")
    ap.add_argument("--port", type=int, default=15514)
    ap.add_argument("--src", type=Path, default=ROOT / "samples" / "sanitized")
    args = ap.parse_args(argv)
    lines = replay_samples.lines_from(args.src)
    if not lines:
        print(f"no samples in {args.src}", file=sys.stderr)
        return 1
    ok = True
    for name in args.scenario or [*SCENARIOS, "backfill"]:
        if name == "backfill":
            ok = run_backfill(args.image, args.src, lines) and ok
        else:
            ok = run_scenario(name, SCENARIOS[name], args.image, args.port, lines) and ok
    print("e2e: PASS" if ok else "e2e: FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
