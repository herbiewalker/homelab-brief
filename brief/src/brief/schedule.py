"""When the scheduled jobs run (brief/schedule.yaml, all UTC).

  hourly   status, sensors, absence alerts, storage and memory samples (every hour the worker is up)
  daily    detectors, digest, host pages, views, alerts, the headline (daily_run_utc)
  weekly   the trend report for the last complete week (weekly_day at weekly_run_utc)

A missing file means the defaults. An invalid file raises ConfigError, which the config guard turns into
a `config_error` finding while the last good copy keeps the schedule running.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from brief.config import ConfigError, config_file, load_mapping

DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def parse_clock(text, name: str) -> tuple[int, int]:
    try:
        hh, mm = str(text).split(":")
        hour, minute = int(hh), int(mm)
    except ValueError:
        raise ConfigError(f"schedule.yaml: {name} must look like 06:00, got {text!r}") from None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ConfigError(f"schedule.yaml: {name} is not a time of day: {text!r}")
    return hour, minute


@dataclass(frozen=True)
class Schedule:
    daily: tuple[int, int] = (6, 0)
    weekly_day: int = 0  # Monday
    weekly: tuple[int, int] = (6, 30)

    @classmethod
    def from_mapping(cls, data: Mapping) -> "Schedule":
        day = str(data.get("weekly_day") or "monday").strip().lower()
        if day not in DAYS:
            raise ConfigError(f"schedule.yaml: weekly_day must be one of {', '.join(DAYS)}, got {day!r}")
        return cls(
            parse_clock(data.get("daily_run_utc") or "06:00", "daily_run_utc"),
            DAYS.index(day),
            parse_clock(data.get("weekly_run_utc") or "06:30", "weekly_run_utc"),
        )

    @classmethod
    def load(cls, env: Mapping[str, str] | None = None) -> "Schedule":
        """The schedule, or the defaults when the file is missing or unreadable."""
        try:
            return cls.from_mapping(load_mapping(config_file("schedule.yaml", env)))
        except (ConfigError, OSError):
            return cls()

    def daily_at(self, now: datetime) -> datetime:
        return now.replace(hour=self.daily[0], minute=self.daily[1], second=0, microsecond=0)

    def weekly_key(self, now: datetime) -> str:
        """Which week's report is due (the Monday that week starts on), so each week runs once."""
        return (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")

    def weekly_at(self, now: datetime) -> datetime:
        monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        return monday + timedelta(days=self.weekly_day, hours=self.weekly[0], minutes=self.weekly[1])
