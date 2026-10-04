"""Worker process placeholder. Task 5 onward replaces the idle loop with the real cycle."""

import json
import signal
import sys
import threading
from datetime import UTC, datetime

from brief import __version__


def log(event: str, **fields) -> None:
    record = {"ts": datetime.now(UTC).isoformat(), "event": event, **fields}
    print(json.dumps(record), flush=True)


def main() -> int:
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    log("worker_started", version=__version__, note="placeholder, no work yet")
    stop.wait()
    log("worker_stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
