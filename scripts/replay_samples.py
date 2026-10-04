#!/usr/bin/env python3
"""Replay sample log lines to a syslog receiver over UDP and TCP, as devices would send them.

The samples have no priority field (the old file archive dropped it), so each line is sent with
<PRI> in front, except every Nth line which is sent bare, to exercise both the real-severity and
the keyword-fallback paths.

  python3 scripts/replay_samples.py --port 5514 [--proto udp|tcp|both] [--src samples/sanitized]
"""

import argparse
import socket
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def lines_from(src: Path) -> list[str]:
    out: list[str] = []
    for f in sorted(src.glob("syslog-*.log")):
        out += [ln for ln in f.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return out


def replay(lines, host, port, proto, pri, bare_every, delay=0.0):
    sent = {"udp": 0, "tcp": 0, "bare": 0}
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM) if proto in ("udp", "both") else None
    tcp = socket.create_connection((host, port), timeout=10) if proto in ("tcp", "both") else None
    try:
        for i, text in enumerate(lines):
            bare = bare_every > 0 and i % bare_every == bare_every - 1
            wire = text if bare else f"<{pri}>{text}"
            sent["bare"] += bare
            use_udp = udp is not None and (tcp is None or i % 2 == 0)
            if use_udp:
                udp.sendto(wire.encode("utf-8"), (host, port))
                sent["udp"] += 1
                if i % 40 == 39:
                    time.sleep(0.01)  # keep a loopback UDP receiver from dropping a burst
            else:
                tcp.sendall(wire.encode("utf-8") + b"\n")
                sent["tcp"] += 1
            if delay:
                time.sleep(delay)
    finally:
        if udp:
            udp.close()
        if tcp:
            tcp.close()
    return sent


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5514)
    ap.add_argument("--proto", choices=["udp", "tcp", "both"], default="both")
    ap.add_argument("--src", type=Path, default=ROOT / "samples" / "sanitized")
    ap.add_argument(
        "--pri", type=int, default=134, help="priority for lines that carry one (134 = local0.info)"
    )
    ap.add_argument(
        "--bare-every", type=int, default=5, help="send every Nth line without a priority; 0 for none"
    )
    ap.add_argument("--delay", type=float, default=0.0, help="seconds between lines")
    args = ap.parse_args(argv)
    lines = lines_from(args.src)
    if not lines:
        print(f"no samples in {args.src}", file=sys.stderr)
        return 1
    sent = replay(lines, args.host, args.port, args.proto, args.pri, args.bare_every, args.delay)
    print(f"sent {len(lines)} lines: udp={sent['udp']} tcp={sent['tcp']} without_priority={sent['bare']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
