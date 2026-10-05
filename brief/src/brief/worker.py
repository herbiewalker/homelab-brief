"""The worker: runs the ingest cycle on a timer.

Each cycle reloads systems.yaml and ignore.yaml, validates them, and keeps the last good version
if an edit is broken (the error is logged), then reads new events and flushes once. When there is
a backlog (a backfill) the next cycle starts straight away instead of waiting.
"""

import os
import signal
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from brief import __version__, detect
from brief.config import ConfigError, config_file, load_mapping
from brief.ignore import IgnoreRules
from brief.ingest import CycleResult, Pipeline
from brief.jsonlog import log
from brief.netinfo import network_info
from brief.outputs import read_json, run_daily
from brief.publish import Publisher, load_registry
from brief.settings import Settings
from brief.signatures import Signatures
from brief.status import DERIVED_METRICS, build_status, health_problems, metric_values, write_status
from brief.store import Store
from brief.systems import Systems

HEARTBEAT_EVERY = 10  # quiet cycles between "still running" log lines
PUBLISH_EVERY = 3600.0  # seconds between status.json writes and MQTT publishes (hourly)


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
        self.settings = Settings.from_env(self.env)
        self.out_dir = Path(self.env.get("BRIEF_OUT", "/out"))
        self.started = time.monotonic()
        self.publisher = self._make_publisher()

    def _make_publisher(self) -> Publisher | None:
        try:
            return Publisher.from_env(self.env, load_registry(self.env, only=DERIVED_METRICS))
        except Exception as exc:  # a bad MQTT setting must not stop ingest
            log("mqtt_config_error", error=f"{type(exc).__name__}: {exc}")
            return None

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

    def publish_status(self) -> dict:
        """The hourly job: write /out/latest/status.json and send the sensor values over MQTT.

        A failed run publishes no values, so the sensors expire instead of reporting a calm network.
        """
        try:
            learned = detect.learn_gaps(self.store)  # normal gaps feed the absence detector
            log("gaps_learned", hosts=learned)
        except Exception as exc:  # status must still go out without them
            log("gaps_failed", error=f"{type(exc).__name__}: {exc}")
        status = build_status(
            self.store,
            self.systems,
            self.settings.syslog_port,
            self.settings.interval,
            time.monotonic() - self.started,
            self.env,
            network=network_info(self.settings),
            previous=read_json(self.out_dir / "latest" / "status.json"),
        )
        write_status(status, self.out_dir)
        values = metric_values(status)
        if self.publisher is not None:
            if values:
                self.publisher.publish(values)
            else:
                self.publisher.forget()
        log(
            "status_published", run_ok=status["run_ok"], mqtt=self.publisher is not None, **status["pipeline"]
        )
        return status

    def daily_due(self, now: datetime | None = None) -> bool:
        """True when today's daily run has not happened and its time (schedule.yaml, UTC) has passed."""
        now = now or datetime.now(UTC)
        try:
            text = str(load_mapping(config_file("schedule.yaml", self.env)).get("daily_run_utc") or "06:00")
            hh, mm = text.split(":")
            at = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        except (ConfigError, OSError, ValueError):
            log(
                "config_error", file="schedule.yaml", error="daily_run_utc must look like 06:00", kept="06:00"
            )
            at = now.replace(hour=6, minute=0, second=0, microsecond=0)
        if now < at:
            return False
        with self.store.read() as db:
            # a brand-new install that has never received a line is not a failed night: wait for data
            if db.execute("SELECT 1 FROM hourly LIMIT 1").fetchone() is None:
                return False
            row = db.execute("SELECT value FROM meta WHERE key = 'last_daily_date'").fetchone()
        return not row or row["value"] != now.strftime("%Y-%m-%d")

    def run_daily(self, now: datetime | None = None):
        """The daily full run: detectors, digest, host files, state.json, status.json (see outputs.py)."""
        now = now or datetime.now(UTC)
        problems = health_problems(
            self.store,
            self.settings.syslog_port,
            self.settings.interval,
            time.monotonic() - self.started,
            now,
        )
        digest = run_daily(
            self.store,
            self.systems,
            now,
            self.out_dir,
            self.env,
            state_dir=self.state,
            extra_problems=problems,
            network=network_info(self.settings),
        )
        with self.store.write() as tx:
            tx.set_meta("last_daily_date", now.strftime("%Y-%m-%d"))
        return digest

    def _daily_if_due(self, backlog: bool) -> None:
        if backlog:  # a digest of half-ingested history would be wrong; wait for the backlog to clear
            return
        try:
            if self.daily_due():
                self.run_daily()
        except Exception as exc:  # the daily run is secondary to ingest; it is tried again next cycle
            log("daily_failed", error=f"{type(exc).__name__}: {exc}")

    def _publish_if_due(self, next_publish: float, every: float) -> float:
        if time.monotonic() < next_publish:
            return next_publish
        try:
            self.publish_status()
        except Exception as exc:  # status is secondary to ingest; try again next hour
            log("status_failed", error=f"{type(exc).__name__}: {exc}")
        return time.monotonic() + every

    def run_forever(
        self, stop: threading.Event, interval: float, publish_every: float = PUBLISH_EVERY
    ) -> None:
        if self.publisher is not None:
            self.publisher.start()
        next_publish = 0.0
        while not stop.is_set():
            try:
                result = self.run_once()
                # after the cycle, so the first numbers include whatever was waiting at start-up
                next_publish = self._publish_if_due(next_publish, publish_every)
                self._daily_if_due(bool(result.backlog_bytes))
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
    try:
        worker.run_forever(stop, interval, float(os.environ.get("BRIEF_PUBLISH_INTERVAL", PUBLISH_EVERY)))
    finally:
        if worker.publisher is not None:
            worker.publisher.stop()
    log("worker_stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
