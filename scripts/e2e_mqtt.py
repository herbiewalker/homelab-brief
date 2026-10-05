#!/usr/bin/env python3
"""End-to-end check of the MQTT sensors and status.json against the built image (Linux or WSL, Docker).

Starts the pinned dev broker with a throwaway password and the repository's ACL, then the image with
MQTT_HOST set and a 5-second publish interval, and checks what a Home Assistant would see:
  - availability is `online` and retained once the container is up;
  - a retained discovery config and a retained numeric state exist for each derived sensor;
  - /status.json answers on the HTTP port and agrees with the sensors' run;
  - `docker stop` (clean shutdown) flips availability to `offline`;
  - `docker kill` (a crash) flips it to `offline` through the last will;
  - the ACL holds: the `ha` user can read but not write the sensor topics.
  - a Home Assistant button press (the `ha` user writing homelab/brief/feedback/<id>) reaches the worker,
    which applies it (the broker ACL allows it, the container is subscribed, the callback queues it).

  python3 scripts/e2e_mqtt.py [--image homelab-brief:dev] [--broker eclipse-mosquitto:2.1.2-alpine]
"""

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NET, BROKER, APP = "hb-e2e-mqtt-net", "hb-e2e-mqtt-broker", "hb-e2e-mqtt-app"
SENSORS = ("events_24h", "new_signatures_24h", "hosts_silent")
USERS = {"brief": "brief-e2e-pass", "ha": "ha-e2e-pass"}


def docker(*args, check=True, **kw):
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check, **kw)


def sub(user, topics, count=None, wait=6):
    """Messages (topic -> payload) the broker has for `user` on these topics; retained ones arrive at once."""
    cmd = [
        "exec",
        BROKER,
        "mosquitto_sub",
        "-h",
        "localhost",
        "-u",
        user,
        "-P",
        USERS[user],
        "-v",
        "-W",
        str(wait),
    ]
    for t in topics:
        cmd += ["-t", t]
    out = docker(*cmd, check=False).stdout
    return dict(line.split(" ", 1) for line in out.splitlines() if " " in line)


def until(what, check, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        value = check()
        if value:
            return value
        time.sleep(1)
    raise AssertionError(f"timed out waiting for {what}")


def availability():
    return sub("brief", ["homelab/brief/availability"], wait=2).get("homelab/brief/availability")


def start_app(image, http_port, state):
    docker("rm", "-f", APP, check=False)
    docker(
        *("run", "-d", "--name", APP, "--network", NET, "-p", f"{http_port}:80"),
        *("-e", "MQTT_HOST=" + BROKER, "-e", "MQTT_USER=brief", "-e", "MQTT_PASSWORD=" + USERS["brief"]),
        *("-e", "BRIEF_PUBLISH_INTERVAL=5", "-e", "BRIEF_INGEST_INTERVAL=2", "-e", "SYSLOG_PORT=5514"),
        *("-v", f"{state}:/state"),
        image,
    )


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--image", default="homelab-brief:dev")
    ap.add_argument("--broker", default="eclipse-mosquitto:2.1.2-alpine")
    ap.add_argument("--http-port", type=int, default=18081)
    args = ap.parse_args(argv)

    work = Path(tempfile.mkdtemp(prefix="hb-e2e-mqtt-"))
    state = work / "state"
    state.mkdir()
    state.chmod(0o777)
    cfg = work / "mosquitto"
    cfg.mkdir()
    for name in ("mosquitto.conf", "acl"):
        shutil.copy(ROOT / "mosquitto" / name, cfg / name)
    # the broker runs as the mosquitto user, so that user must own the password file
    script = " && ".join(
        f"mosquitto_passwd -b {'-c ' if i == 0 else ''}/mosquitto/config/passwd {user} {password}"
        for i, (user, password) in enumerate(USERS.items())
    )
    docker(
        *("run", "--rm", "-v", f"{cfg}:/mosquitto/config", "--entrypoint", "sh", args.broker),
        *("-c", script + " && chown mosquitto:mosquitto /mosquitto/config/passwd"),
    )
    problems: list[str] = []
    try:
        docker("network", "rm", NET, check=False)
        docker("network", "create", NET)
        docker("rm", "-f", BROKER, check=False)
        docker("run", "-d", "--name", BROKER, "--network", NET, "-v", f"{cfg}:/mosquitto/config", args.broker)
        until("the broker", lambda: docker("exec", BROKER, "mosquitto_sub", "-h", "localhost", "-u", "brief",
                                           "-P", USERS["brief"], "-t", "x", "-W", "1", check=False).returncode in (0, 27))  # fmt: skip
        start_app(args.image, args.http_port, state)
        until("availability online", lambda: availability() == "online", 90)
        until("all sensor states", lambda: all(
            f"homelab/brief/{s}/state" in sub("brief", ["homelab/brief/#"], wait=2) for s in SENSORS), 60)  # fmt: skip

        got = sub("ha", ["homelab/brief/#", "homeassistant/#"], wait=3)
        for s in SENSORS:
            cfgs = got.get(f"homeassistant/sensor/homelab_brief/{s}/config")
            if not cfgs:
                problems.append(f"no discovery config for {s}")
                continue
            d = json.loads(cfgs)
            if (
                d["state_topic"] != f"homelab/brief/{s}/state"
                or d["availability_topic"] != "homelab/brief/availability"
            ):
                problems.append(f"discovery for {s} points at the wrong topics")
            if not d.get("expire_after"):
                problems.append(f"discovery for {s} has no expire_after")
            try:
                float(got[f"homelab/brief/{s}/state"])
            except (KeyError, ValueError):
                problems.append(f"state for {s} is missing or not a number")
        print(f"[mqtt] sensors seen: {sorted(k.split('/')[-2] for k in got if k.endswith('/state'))}")

        with urllib.request.urlopen(f"http://127.0.0.1:{args.http_port}/status.json", timeout=10) as r:
            doc = json.load(r)
            if (
                r.headers.get("Cache-Control") != "no-cache"
                or doc.get("schema") != 1
                or doc.get("run_ok") is not True
            ):
                problems.append(f"status.json looks wrong: {doc}")
        print(f"[status.json] run_ok={doc['run_ok']} headline={doc['headline']!r}")

        denied = docker("exec", BROKER, "mosquitto_pub", "-h", "localhost", "-u", "ha", "-P", USERS["ha"],
                        "-t", "homelab/brief/events_24h/state", "-m", "999999", "-r", check=False)  # fmt: skip
        time.sleep(1)
        after = sub("brief", ["homelab/brief/events_24h/state"], wait=2).get("homelab/brief/events_24h/state")
        if after == "999999":
            problems.append("the ha user could overwrite a sensor state (ACL not enforced)")
        print(f"[acl] ha write attempt exit={denied.returncode}, state still {after!r}")

        # a button press: allowed by the ACL, delivered to the container, applied by the worker (an unknown
        # alert id, so nothing changes, but the log line proves the whole path)
        docker("exec", BROKER, "mosquitto_pub", "-h", "localhost", "-u", "ha", "-P", USERS["ha"],
               "-t", "homelab/brief/feedback/e2e-unknown-alert", "-m", "ack")  # fmt: skip
        until(
            "the feedback to be applied",
            lambda: "e2e-unknown-alert" in docker("logs", APP, check=False).stdout,
            30,
        )
        print("[feedback] a button press from the ha user reached the worker")

        docker("stop", "-t", "10", APP)
        if availability() != "offline":
            problems.append("a clean stop did not publish availability offline")
        start_app(args.image, args.http_port, state)
        until("availability online again", lambda: availability() == "online", 90)
        docker("kill", APP)
        until("last will after a crash", lambda: availability() == "offline", 30)
        print("[availability] online -> offline on clean stop and on kill (last will)")
    except Exception as exc:
        problems.append(str(exc))
    finally:
        if problems:
            print(docker("logs", "--tail", "40", APP, check=False).stdout[-2500:])
        docker("rm", "-f", APP, BROKER, check=False)
        docker("network", "rm", NET, check=False)
        shutil.rmtree(work, ignore_errors=True)
    if problems:
        print("FAIL\n  " + "\n  ".join(problems))
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
