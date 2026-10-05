"""A synthetic month of logs, pushed through the real ingest pipeline, for the golden-day test.

Three invented hosts (documentation addresses) log for 30 days. The story the data tells, which the
detectors and the replay must keep telling the same way:

  gw   (198.51.100.1)  logs every hour; its firewall signature is steady at 100 a day, then 700 in
                       one hour on the day before the run  -> a deviation
  nas  (198.51.100.2)  logs every hour; a nightly cron job at 03:00 shows up five extra times at 14:00
                       -> off-hours. A new disk-error signature appears (err) -> novelty, new. A
                       warning that was regular for ten days and then silent for twenty comes back
                       -> novelty, returned
  cam  (198.51.100.3)  logs every hour, then stops three days before the run -> absence, pushed

The run is at 06:00 UTC on DAY; the replay covers the two days before it as well.
"""

import json
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from brief import detect, ingest, replay
from brief.ignore import IgnoreRules
from brief.signatures import Signatures
from brief.store import Store
from brief.systems import System, Systems

BRIEF = Path(__file__).resolve().parents[1]
GW, NAS, CAM = "198.51.100.1", "198.51.100.2", "198.51.100.3"
DAY = date(2026, 9, 20)
RUN_AT = datetime.combine(DAY, time(6, 0), tzinfo=UTC)
START = RUN_AT - timedelta(days=30)
END = datetime.combine(
    DAY + timedelta(days=1), time(0, 0), tzinfo=UTC
)  # logs keep coming after the 06:00 run
CAM_LAST_HOUR = datetime.combine(DAY - timedelta(days=3), time(22, 0), tzinfo=UTC)
SYSTEMS = Systems(
    [
        System("gw", "Gateway", "gateway", (GW,), "UTC", True),
        System("nas", "Storage server", "server", (NAS,), "UTC", True),
        System("cam", "Camera", "camera", (CAM,), "UTC", True),
    ]
)


def ev(host: str, when: datetime, app: str, msg: str, severity: int = 6) -> dict:
    return {
        "source_ip": host,
        "app": app,
        "msg": msg,
        "severity": severity,
        "severity_inferred": False,
        "epoch_ms": int(when.timestamp() * 1000),
        "timestamp": when.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    }


def at(day: date, hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute, second), tzinfo=UTC)


def events() -> list[dict]:
    out: list[dict] = []
    t = START
    while t < END:  # one line from each host every hour, so a host going quiet is visible
        out.append(
            ev(
                GW,
                t + timedelta(minutes=5),
                "dhcpd",
                f"DHCPACK on 192.0.2.{t.hour + 10} to 00:00:5e:00:53:{t.hour:02x}",
            )
        )
        out.append(ev(NAS, t + timedelta(minutes=7), "smbd", f"heartbeat {t.hour}"))
        if t <= CAM_LAST_HOUR:
            out.append(ev(CAM, t + timedelta(minutes=9), "streamer", f"frame ok {t.hour}"))
        t += timedelta(hours=1)
    for k in range(30, 0, -1):  # the 30 days before DAY, oldest first
        day = DAY - timedelta(days=k)
        spike = k == 1
        for i in range(700 if spike else 100):
            out.append(ev(GW, at(day, 10, 0, 0) + timedelta(seconds=i * (3599 // (700 if spike else 100))), "firewall",
                          f"DROP IN=eth0 SRC=203.0.113.{i % 200 + 1} DST=192.0.2.1 LEN={60 + i % 40}", 4))  # fmt: skip
        for i in range(3):
            out.append(
                ev(
                    GW,
                    at(day, 9, 10 * i),
                    "sshd",
                    f"Accepted publickey for admin from 203.0.113.{i + 1} port {40000 + i}",
                )
            )
        out.append(ev(NAS, at(day, 3, 0, 30), "cron", "job backup started"))
        if k == 1:
            for i in range(5):
                out.append(ev(NAS, at(day, 14, 10 * i), "cron", "job backup started"))  # off-hours
            for i in range(5):
                out.append(
                    ev(
                        NAS,
                        at(day, 20, 5 * i),
                        "kernel",
                        f"blk_update_request: I/O error, dev sdb, sector {1000 + i}",
                        3,
                    )
                )
            out.append(
                ev(NAS, at(day, 7, 30), "rsyslogd", "action 'omfwd' resumed after 3 seconds", 4)
            )  # returned
        if 21 <= k <= 30:
            out.append(
                ev(NAS, at(day, 5, 15), "rsyslogd", f"action 'omfwd' resumed after {k} seconds", 4)
            )  # regular, then gone
    out.sort(key=lambda e: (e["epoch_ms"], e["source_ip"], e["msg"]))
    return out


def build_store(tmp: Path) -> Store:
    """The month, ingested by the real pipeline, with each host's normal gap learned."""
    (tmp / "events").mkdir(parents=True)
    (tmp / "events" / "golden.ndjson").write_text(
        "\n".join(json.dumps(e, sort_keys=True) for e in events()) + "\n", encoding="utf-8"
    )
    store = Store(tmp / "baseline.db")
    pipe = ingest.Pipeline(
        store,
        tmp / "events",
        Signatures(tmp / "drain3.bin", BRIEF / "drain3.ini"),
        SYSTEMS,
        IgnoreRules(),
        max_bytes_per_cycle=1 << 30,
        clock=lambda: RUN_AT,
    )
    while pipe.run_cycle().backlog_bytes:
        pass
    detect.learn_gaps(store, RUN_AT)
    return store


def golden_env(tmp: Path) -> dict:
    """The shipped playbook and priorities, so a change to either shows up in the snapshot."""
    folder = tmp / "defaults" / "brief"
    folder.mkdir(parents=True)
    for name in ("playbook.yaml", "priorities.yaml"):
        (folder / name).write_bytes((BRIEF / name).read_bytes())
    return {"BRIEF_DEFAULTS_DIR": str(tmp / "defaults"), "BRIEF_CONFIG_DIR": str(tmp / "none")}


def snapshot(tmp: Path) -> dict:
    store = build_store(tmp / "work")
    try:
        rep = replay.replay(store, SYSTEMS, DAY - timedelta(days=2), DAY, env=golden_env(tmp))
    finally:
        store.close()
    return replay.as_dict(rep)
