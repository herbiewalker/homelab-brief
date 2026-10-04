#!/usr/bin/env python3
"""Report on normalized events (NDJSON written by Vector): parse failures and severity sources.

  python3 scripts/parse_report.py <events file or folder> [...] [--max-failure-rate 0.02]

Prints how many events there are, the parse-failure rate, how many severities came from the wire
(real) versus the keyword fallback (inferred), and which branches fired. Exits 1 when the failure
rate is above --max-failure-rate (default 0.02), so it can gate CI.
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

SEVERITY_NAMES = ["emerg", "alert", "crit", "err", "warning", "notice", "info", "debug"]


def read_events(paths):
    for p in paths:
        files = sorted(p.glob("*.ndjson")) if p.is_dir() else [p]
        for f in files:
            for line in f.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    yield json.loads(line)


def summarize(events) -> dict:
    c: Counter = Counter()
    sev: Counter = Counter()
    apps: Counter = Counter()
    senders: Counter = Counter()
    for e in events:
        c["total"] += 1
        c["parse_failed"] += bool(e.get("parse_failed"))
        c["severity_real"] += e.get("severity_inferred") is False
        c["severity_inferred"] += e.get("severity_inferred") is True
        for key, label in (
            ("cef", "cef"),
            ("dns", "coredns_json"),
            ("device_mac", "device_token"),
            ("header_host_dup", "duplicated_host"),
            ("unknown_sender", "unknown_sender"),
            ("epoch_ms", "epoch_time"),
        ):
            c[label] += key in e and e[key] not in (None, False)
        s = e.get("severity")
        if isinstance(s, int) and 0 <= s <= 7:
            sev[SEVERITY_NAMES[s]] += 1
        apps[e.get("app", "(none)")] += 1
        senders[e.get("source_ip", "?")] += 1
    return {"counts": c, "severity": sev, "apps": apps, "senders": senders}


def failure_rate(counts) -> float:
    return counts["parse_failed"] / counts["total"] if counts["total"] else 1.0


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("paths", nargs="+", type=Path)
    ap.add_argument("--max-failure-rate", type=float, default=0.02)
    args = ap.parse_args(argv)
    r = summarize(read_events(args.paths))
    c = r["counts"]
    total = c["total"]
    print(f"events: {total}")
    if not total:
        print("no events found", file=sys.stderr)
        return 1
    rate = failure_rate(c)
    print(f"parse failures: {c['parse_failed']} ({rate:.2%})")
    print(f"severity from the wire (real): {c['severity_real']}")
    print(f"severity from keywords (inferred): {c['severity_inferred']}")
    print("severity: " + ", ".join(f"{k}={v}" for k, v in r["severity"].most_common()))
    print(
        "branches: "
        + ", ".join(
            f"{k}={c[k]}"
            for k in (
                "cef",
                "coredns_json",
                "device_token",
                "duplicated_host",
                "epoch_time",
                "unknown_sender",
            )
        )
    )
    print("top apps: " + ", ".join(f"{k}={v}" for k, v in r["apps"].most_common(8)))
    print(f"senders: {len(r['senders'])}")
    if rate > args.max_failure_rate:
        print(f"FAIL: failure rate {rate:.2%} is above {args.max_failure_rate:.2%}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
