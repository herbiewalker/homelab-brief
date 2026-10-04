"""The worker: runs the ingest cycle on a timer.

Each cycle reloads systems.yaml and ignore.yaml, validates them, and keeps the last good version
if an edit is broken (the error is logged), then reads new events and flushes once. When there is
a backlog (a backfill) the next cycle starts straight away instead of waiting.
"""

import os
import signal
import sys
import threading
from pathlib import Path

from brief import __version__
from brief.config import ConfigError, config_file, load_mapping
from brief.ignore import IgnoreRules
from brief.ingest import CycleResult, Pipeline
from brief.jsonlog import log
from brief.signatures import Signatures
from brief.store import Store
from brief.systems import Systems

HEARTBEAT_EVERY = 10  # quiet cycles between "still running" log lines


class Worker:
    def __init__(self, state_dir: Path, env=None):
        self.env = os.environ if env is None else env
        self.state = Path(state_dir)
        self.store = Store(self.state / "baseline.db")
        self.signatures = Signatures(self.state / "drain3.bin", config_file("drain3.ini", self.env))
        self.systems, self.ignore = Systems(), IgnoreRules()
        self._reload()
        retention = load_mapping(config_file("retention.yaml", self.env))
        self.pipeline = Pipeline(
            self.store,
            self.state / "events",
            self.signatures,
            self.systems,
            self.ignore,
            samples_per_signature=int(retention.get("samples_per_signature", 5)),
        )
        self._quiet = 0

    def _reload(self) -> None:
        """Reload config every cycle; a broken edit keeps the last good version and is logged."""
        for name, loader, attr in (
            ("systems.yaml", Systems.load, "systems"),
            ("ignore.yaml", IgnoreRules.load, "ignore"),
        ):
            try:
                setattr(self, attr, loader(config_file(name, self.env)))
            except (ConfigError, OSError) as exc:
                log("config_error", file=name, error=str(exc), kept="last good version")
        if hasattr(self, "pipeline"):
            self.pipeline.set_config(self.systems, self.ignore)

    def run_once(self) -> CycleResult:
        self._reload()
        result = self.pipeline.run_cycle()
        if result.events or result.malformed or result.truncated:
            self._quiet = 0
            log(
                "ingest_cycle",
                events=result.events,
                counted=result.counted,
                suppressed=result.suppressed,
                malformed=result.malformed,
                parse_failed=result.parse_failed,
                time_fallback=result.time_fallback,
                new_signatures=result.new_signatures,
                backlog_bytes=result.backlog_bytes,
                seconds=round(result.seconds, 2),
                truncated=result.truncated or None,
            )
        else:
            self._quiet += 1
            if self._quiet % HEARTBEAT_EVERY == 0:
                log("ingest_idle", cycles=self._quiet)
        return result

    def run_forever(self, stop: threading.Event, interval: float) -> None:
        while not stop.is_set():
            try:
                result = self.run_once()
                if result.backlog_bytes:
                    continue
            except Exception as exc:  # keep going: one bad cycle must not stop the pipeline
                log("cycle_failed", error=f"{type(exc).__name__}: {exc}")
                stop.wait(min(interval, 30))
                continue
            stop.wait(interval)


def main() -> int:
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    interval = float(os.environ.get("BRIEF_INGEST_INTERVAL", "60"))
    worker = Worker(Path(os.environ.get("BRIEF_STATE", "/state")))
    log("worker_started", version=__version__, interval=interval, miner_generation=worker.pipeline.generation)
    worker.run_forever(stop, interval)
    log("worker_stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
