"""Event time: turn what a device sent into a UTC datetime, and say where it came from.

Order of trust:
  epoch   a precise UTC time in the event (CEF timestamp, coredns JSON, a UniFi OS ISO prefix)
  header  the classic "Mon dd hh:mm:ss" header, read in the host's source_tz, with the year inferred
  received  Vector's receive time (flagged: a backfill read today would land in today's hours)
  now     nothing usable at all (flagged the same way)

The classic header has no year. The year is the one that puts the time no more than a day ahead of
the reference (the receive time), so December lines read in January belong to last year.
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

HEADER = re.compile(r"^([A-Z][a-z]{2}) +(\d{1,2}) (\d\d):(\d\d):(\d\d)$")
MONTHS = {
    m: i
    for i, m in enumerate(
        ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1
    )
}
FRACTION = re.compile(r"(\.\d{1,6})\d*")
FUTURE_SLACK = timedelta(days=1)
MIN_EPOCH_MS = 946_684_800_000  # 2000-01-01
MAX_EPOCH_MS = 4_102_444_800_000  # 2100-01-01


@dataclass(frozen=True)
class EventTime:
    when: datetime
    source: str  # epoch | header | received | now

    @property
    def fallback(self) -> bool:
        return self.source in ("received", "now")


def parse_received(value) -> datetime | None:
    """Vector's own timestamp, e.g. 2026-10-04T00:43:24.077737562Z (nanoseconds are trimmed)."""
    if not isinstance(value, str) or not value:
        return None
    text = FRACTION.sub(r"\1", value.strip()).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _from_header(text: str, tz: ZoneInfo, reference: datetime) -> datetime | None:
    m = HEADER.match(text)
    if not m or m.group(1) not in MONTHS:
        return None
    month, day = MONTHS[m.group(1)], int(m.group(2))
    hour, minute, second = int(m.group(3)), int(m.group(4)), int(m.group(5))
    year = reference.astimezone(tz).year
    for candidate_year in range(year, year - 5, -1):  # walk back past impossible dates such as Feb 29
        try:
            local = datetime(candidate_year, month, day, hour, minute, second, tzinfo=tz)
        except ValueError:
            continue
        utc = local.astimezone(UTC)
        if utc <= reference + FUTURE_SLACK:
            return utc
    return None


def event_time(event: dict, tz: str | ZoneInfo = "UTC", now: datetime | None = None) -> EventTime:
    zone = tz if isinstance(tz, ZoneInfo) else ZoneInfo(tz)
    received = parse_received(event.get("timestamp"))
    reference = received or now or datetime.now(UTC)

    ms = event.get("epoch_ms")
    if isinstance(ms, int) and not isinstance(ms, bool) and MIN_EPOCH_MS <= ms <= MAX_EPOCH_MS:
        return EventTime(datetime.fromtimestamp(ms / 1000, UTC), "epoch")

    ts = event.get("ts")
    if isinstance(ts, str):
        parsed = _from_header(ts.strip(), zone, reference)
        if parsed is not None:
            return EventTime(parsed, "header")

    if received is not None:
        return EventTime(received, "received")
    return EventTime(reference, "now")
