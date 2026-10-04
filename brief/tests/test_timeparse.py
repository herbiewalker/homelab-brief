from datetime import UTC, datetime

import pytest

from brief.timeparse import event_time, parse_received

RECEIVED = "2026-09-26T17:22:40.123456789Z"


def at(**event):
    return {"timestamp": RECEIVED, **event}


def test_a_precise_epoch_time_wins_over_the_header():
    e = event_time(at(epoch_ms=1790442597425, ts="Sep 26 01:00:00"), "America/New_York")
    assert e.source == "epoch" and not e.fallback
    assert e.when == datetime(2026, 9, 26, 17, 9, 57, 425000, tzinfo=UTC)


def test_the_header_is_read_in_the_source_timezone():
    e = event_time(at(ts="Sep 26 13:22:33"), "America/New_York")
    assert e.source == "header" and e.when == datetime(2026, 9, 26, 17, 22, 33, tzinfo=UTC)
    assert event_time(at(ts="Sep 26 13:22:33"), "UTC").when == datetime(2026, 9, 26, 13, 22, 33, tzinfo=UTC)


def test_a_space_padded_day_is_read():
    assert event_time(at(ts="Sep  9 03:14:10"), "UTC").when == datetime(2026, 9, 9, 3, 14, 10, tzinfo=UTC)


def test_the_year_is_inferred_from_the_receive_time():
    january = {"timestamp": "2027-01-02T00:00:00Z", "ts": "Dec 31 23:59:00"}
    assert event_time(january, "UTC").when.year == 2026
    assert event_time({"timestamp": "2027-01-02T00:00:00Z", "ts": "Jan  1 12:00:00"}, "UTC").when.year == 2027


def test_a_device_clock_slightly_ahead_stays_in_the_same_year():
    assert event_time(at(ts="Sep 26 20:00:00"), "UTC").when.year == 2026  # within the one-day slack


def test_a_header_far_in_the_future_belongs_to_last_year():
    assert event_time(at(ts="Dec 25 08:00:00"), "UTC").when.year == 2025


def test_feb_29_walks_back_to_a_leap_year():
    e = event_time({"timestamp": "2026-03-05T00:00:00Z", "ts": "Feb 29 10:00:00"}, "UTC")
    assert e.when == datetime(2024, 2, 29, 10, 0, 0, tzinfo=UTC)


def test_an_ambiguous_local_time_resolves_to_its_first_occurrence():
    e = event_time({"timestamp": "2026-11-02T00:00:00Z", "ts": "Nov  1 01:30:00"}, "America/New_York")
    assert e.when == datetime(2026, 11, 1, 5, 30, 0, tzinfo=UTC)  # EDT, before the clocks go back


def test_falling_back_to_the_receive_time_is_flagged():
    e = event_time(at(ts="garbage"), "UTC")
    assert e.source == "received" and e.fallback and e.when.year == 2026
    e = event_time(at(), "UTC")
    assert e.source == "received" and e.fallback


def test_with_nothing_usable_it_uses_now_and_says_so():
    now = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
    e = event_time({}, "UTC", now=now)
    assert e.source == "now" and e.fallback and e.when == now


@pytest.mark.parametrize("bad", [0, True, 10**15, "1790442597425", -5])
def test_an_implausible_epoch_is_ignored(bad):
    e = event_time(at(epoch_ms=bad, ts="Sep 26 13:22:33"), "UTC")
    assert e.source == "header"


def test_nanosecond_timestamps_and_odd_zones_parse():
    assert parse_received(RECEIVED) == datetime(2026, 9, 26, 17, 22, 40, 123456, tzinfo=UTC)
    assert parse_received("2026-09-26T13:22:40-04:00") == datetime(2026, 9, 26, 17, 22, 40, tzinfo=UTC)
    assert parse_received("2026-09-26T13:22:40") == datetime(2026, 9, 26, 13, 22, 40, tzinfo=UTC)
    assert parse_received("nonsense") is None and parse_received(None) is None
