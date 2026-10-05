"""Clock-skew capture in the ingest cycle."""

import json

from test_worker import make_env

from brief import worker as w


def ev(host, ts, received, **extra):
    base = {"source_ip": host, "app": "app", "msg": "hello", "severity": 6, "ts": ts, "timestamp": received}
    return json.dumps({**base, **extra})


def write(state, name, *events):
    folder = state / "events"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text("\n".join(events) + "\n", encoding="utf-8")


def skew_of(worker, host):
    with worker.store.read() as db:
        row = db.execute("SELECT clock_skew_s, skew_updated FROM host WHERE host = ?", (host,)).fetchone()
    return row["clock_skew_s"], row["skew_updated"]


def test_the_median_of_event_minus_receive_is_stored_per_host(tmp_path):
    env, _ = make_env(tmp_path)
    state = tmp_path / "state"
    # header 13:00:00 UTC but received at 13:10:00: the device clock is 10 minutes behind
    write(state, "a.ndjson", *[ev("198.51.100.1", "Sep 26 13:00:00", "2026-09-26T13:10:00Z") for _ in range(3)],
          ev("198.51.100.1", "Sep 26 13:00:00", "2026-09-26T13:00:30Z"))  # fmt: skip
    worker = w.Worker(state, env)
    worker.run_once()
    skew, updated = skew_of(worker, "198.51.100.1")
    assert skew == -600.0 and updated is not None  # the median ignores the one near-correct line


def test_imported_events_and_time_fallbacks_are_not_measured(tmp_path):
    env, _ = make_env(tmp_path)
    state = tmp_path / "state"
    write(state, "a.ndjson",
          ev("198.51.100.2", "Sep 26 13:00:00", "2026-09-26T13:10:00Z", imported=True),  # backfill: receive time is the import
          ev("198.51.100.3", "not a header", "2026-09-26T13:10:00Z"))  # no usable device time: we used receive time  # fmt: skip
    worker = w.Worker(state, env)
    worker.run_once()
    assert skew_of(worker, "198.51.100.2") == (None, None)
    assert skew_of(worker, "198.51.100.3") == (None, None)


def test_a_cycle_without_live_events_leaves_the_last_measurement_alone(tmp_path):
    env, _ = make_env(tmp_path)
    state = tmp_path / "state"
    write(state, "a.ndjson", ev("198.51.100.1", "Sep 26 13:00:00", "2026-09-26T13:01:00Z"))
    worker = w.Worker(state, env)
    worker.run_once()
    first = skew_of(worker, "198.51.100.1")
    worker.run_once()  # nothing new
    assert skew_of(worker, "198.51.100.1") == first
