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
from datetime import UTC, datetime, timedelta
from pathlib import Path

from brief import __version__, detect, registry, selfmon, weekly
from brief.alerts import drain_feedback
from brief.config import ConfigError, config_file, load_mapping
from brief.configguard import ConfigGuard
from brief.ignore import IgnoreRules
from brief.ingest import CycleResult, Pipeline
from brief.jsonlog import log
from brief.netinfo import network_info
from brief.notify import AppriseChannel, Channels
from brief.outputs import read_json, run_daily, run_hourly_alerts
from brief.publish import Publisher
from brief.schedule import Schedule
from brief.settings import Settings
from brief.signatures import Signatures
from brief.status import build_status, health_problems, write_status
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
        self.guard = ConfigGuard(self.state, self.env)
        self.archive_dir = Path(self.env.get("BRIEF_ARCHIVE", "/archive"))
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

    @property
    def notifier(self):
        """Where alerts go: MQTT (unless notify.yaml turns it off for alerts) and Apprise when it is on.
        None when neither is available, which makes the lifecycle log the decision instead."""
        try:
            cfg = load_mapping(config_file("notify.yaml", self.env))
        except (ConfigError, OSError):
            cfg = {}
        channels = []
        if self.publisher is not None and (cfg.get("mqtt") or {}).get("enabled", True):
            channels.append(self.publisher)
        apprise = self._apprise(cfg)
        if apprise is not None:
            channels.append(apprise)
        return Channels(channels) if len(channels) > 1 else (channels[0] if channels else None)

    def _apprise(self, cfg: dict):
        """The Apprise channel, rebuilt only when its settings or URLs change (config reloads safely)."""
        key = (
            repr(cfg.get("apprise")),
            self.env.get((cfg.get("apprise") or {}).get("urls_var") or "APPRISE_URLS"),
        )
        if getattr(self, "_apprise_key", None) != key:
            self._apprise_key = key
            try:
                self._apprise_channel = AppriseChannel.from_env(self.env)
            except Exception as exc:  # a broken Apprise setup must not stop alerts through MQTT
                log("apprise_config_error", error=type(exc).__name__)
                self._apprise_channel = None
            if self._apprise_channel is not None:
                log("apprise_enabled", destinations=self._apprise_channel.destinations)
        return self._apprise_channel

    def _make_publisher(self) -> Publisher | None:
        try:
            return Publisher.from_env(self.env, registry.load(self.env, self.systems))
        except Exception as exc:  # a bad MQTT setting must not stop ingest
            log("mqtt_config_error", error=f"{type(exc).__name__}: {exc}")
            return None

    def _reload(self) -> None:
        """Reload config every cycle. The guard first checks every file: a broken edit is replaced by the last
        good copy for every reader, logged once, and reported as a config_error finding until it is fixed."""
        try:
            self.guard.check()
        except Exception as exc:  # the guard itself must never stop ingest
            log("config_guard_failed", error=f"{type(exc).__name__}: {exc}")
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
        try:  # archive size, free space and memory, once an hour (the projection reads these samples)
            selfmon.record(self.store, datetime.now(UTC), self.archive_dir, self.state)
        except Exception as exc:  # self-monitoring must not stop the status
            log("selfmon_failed", error=f"{type(exc).__name__}: {exc}")
        status = build_status(
            self.store,
            self.systems,
            self.settings.syslog_port,
            self.settings.interval,
            time.monotonic() - self.started,
            self.env,
            network=network_info(self.settings),
            previous=read_json(self.out_dir / "latest" / "status.json"),
            state_dir=self.state,
        )
        write_status(status, self.out_dir)
        try:
            run_hourly_alerts(
                self.store, self.systems, datetime.now(UTC), self.env, status, self.notifier, self.state
            )
        except Exception as exc:  # alerts are secondary to ingest and status; the next hour tries again
            log("alerts_failed", error=f"{type(exc).__name__}: {exc}")
        try:
            metrics = registry.load(self.env, self.systems)
        except (ConfigError, OSError) as exc:
            log("config_error", file="registry.yaml", error=str(exc), kept="last good version")
            metrics = self.publisher.metrics if self.publisher is not None else []
        values = registry.values(status, metrics)
        if self.publisher is not None:
            self.publisher.set_metrics(metrics)
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
        if now < self._schedule().daily_at(now):
            return False
        with self.store.read() as db:
            # a brand-new install that has never received a line is not a failed night: wait for data
            if db.execute("SELECT 1 FROM hourly LIMIT 1").fetchone() is None:
                return False
            row = db.execute("SELECT value FROM meta WHERE key = 'last_daily_date'").fetchone()
        return not row or row["value"] != now.strftime("%Y-%m-%d")

    def _schedule(self) -> Schedule:
        """schedule.yaml, or the defaults (said once per call) when it cannot be read."""
        try:
            return Schedule.from_mapping(load_mapping(config_file("schedule.yaml", self.env)))
        except (ConfigError, OSError) as exc:
            log("config_error", file="schedule.yaml", error=str(exc), kept="the defaults")
            return Schedule()

    def weekly_due(self, now: datetime | None = None) -> bool:
        """True when this week's report time has passed and the report has not been written yet."""
        now = now or datetime.now(UTC)
        schedule = self._schedule()
        if now < schedule.weekly_at(now):
            return False
        with self.store.read() as db:
            if db.execute("SELECT 1 FROM hourly LIMIT 1").fetchone() is None:
                return False  # nothing has ever been received: nothing to report
            row = db.execute("SELECT value FROM meta WHERE key = 'last_weekly_key'").fetchone()
        return not row or row["value"] != schedule.weekly_key(now)

    def run_weekly(self, now: datetime | None = None) -> Path:
        """The trend report for the last complete week, written to /out/weekly."""
        now = now or datetime.now(UTC)
        monday = weekly.current_monday(now) - timedelta(days=7)
        path = weekly.write_weekly(self.store, self.systems, self.out_dir, monday, self.env)
        with self.store.write() as tx:
            tx.set_meta("last_weekly_key", self._schedule().weekly_key(now))
        log("weekly_written", week=monday.isoformat(), path=str(path.name))
        return path

    def _weekly_if_due(self, backlog: bool) -> None:
        if backlog:
            return
        try:
            if self.weekly_due():
                self.run_weekly()
        except Exception as exc:  # the weekly report is secondary; it is tried again next cycle
            log("weekly_failed", error=f"{type(exc).__name__}: {exc}")

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
            notifier=self.notifier,
            alerts_on=True,
        )
        with self.store.write() as tx:
            tx.set_meta("last_daily_date", now.strftime("%Y-%m-%d"))
        return digest

    def _apply_feedback(self) -> None:
        """Home Assistant button presses (ack, snooze) that arrived since the last cycle."""
        if self.publisher is None:
            return
        try:
            drain_feedback(self.store, self.publisher.next_feedback)
        except Exception as exc:  # a bad message must not stop ingest
            log("feedback_failed", error=f"{type(exc).__name__}: {exc}")

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
                self._weekly_if_due(bool(result.backlog_bytes))
                self._apply_feedback()
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
