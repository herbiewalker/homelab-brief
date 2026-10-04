import json
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from brief import ingest
from brief.ignore import IgnoreRules
from brief.learning import learning_status
from brief.signatures import Signatures
from brief.store import Store, Tx
from brief.systems import Systems

BRIEF = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 26, 18, 0, 0, tzinfo=UTC)
RECEIVED = "2026-09-26T17:23:00.5Z"
SSH = "Accepted publickey for user-one from 198.51.100.{ip} port {port}"


def ev(msg="hello", app="sshd", host="198.51.100.1", ts="Sep 26 13:22:33", severity=6, **extra):
    event = {"source_ip": host, "header_host": "host-01", "app": app, "msg": msg, "severity": severity,
             "severity_inferred": False, "timestamp": RECEIVED}  # fmt: skip
    if ts is not None:
        event["ts"] = ts
    return {**event, **extra}


class Rig:
    def __init__(self, tmp, systems=None, ignore=None, **kw):
        self.tmp = tmp
        self.events = tmp / "events"
        self.events.mkdir(exist_ok=True)
        self.systems = systems or Systems()
        self.ignore = ignore or IgnoreRules()
        self.kw = kw
        self.open()

    def open(self):
        self.store = Store(self.tmp / "state" / "baseline.db")
        self.sigs = Signatures(self.tmp / "state" / "drain3.bin", BRIEF / "drain3.ini")
        self.pipe = ingest.Pipeline(
            self.store, self.events, self.sigs, self.systems, self.ignore, clock=lambda: NOW, **self.kw
        )

    def close(self):
        self.store.close()

    def add(self, name, events, partial=None):
        with (self.events / name).open("ab") as f:
            for e in events:
                f.write((e if isinstance(e, str) else json.dumps(e)).encode() + b"\n")
            if partial:
                f.write(partial.encode())
        return self.events / name

    def rows(self, sql, *args):
        with self.store.read() as db:
            return [dict(r) for r in db.execute(sql, args)]

    def total(self):
        return self.rows("SELECT COALESCE(SUM(n), 0) AS n FROM hourly")[0]["n"]


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    yield r
    r.close()


# ---------- the basics ----------


def test_events_become_a_signature_hourly_counts_a_host_and_samples(rig):
    rig.add("2026-09-26.ndjson", [ev(SSH.format(ip=i, port=50000 + i)) for i in range(3)])
    r = rig.pipe.run_cycle()
    assert (r.events, r.counted, r.new_signatures, r.malformed) == (3, 3, 1, 0)
    (sig,) = rig.rows("SELECT * FROM signature")
    assert sig["app"] == "sshd" and sig["template"].startswith("Accepted publickey for user-one from")
    (hourly,) = rig.rows("SELECT * FROM hourly")
    assert (hourly["day"], hourly["hour"], hourly["host"], hourly["severity"], hourly["n"]) == (
        "2026-09-26", 13, "198.51.100.1", "info", 3,
    )  # fmt: skip
    assert rig.rows("SELECT first_seen, last_seen FROM host")[0] == {
        "first_seen": "2026-09-26T13:22:33Z", "last_seen": "2026-09-26T13:22:33Z",
    }  # fmt: skip
    assert len(rig.rows("SELECT * FROM sample")) == 3
    assert (
        rig.rows("SELECT offset FROM ingest_offset")[0]["offset"]
        == (rig.events / "2026-09-26.ndjson").stat().st_size
    )


def test_lines_that_differ_only_by_ips_and_numbers_share_one_signature(rig):
    rig.add("a.ndjson", [ev(SSH.format(ip=7, port=1111)), ev(SSH.format(ip=99, port=22222))])
    rig.pipe.run_cycle()
    assert len(rig.rows("SELECT * FROM signature")) == 1


def test_the_same_text_from_different_apps_is_a_different_signature(rig):
    rig.add("a.ndjson", [ev("it broke", app="one"), ev("it broke", app="two")])
    rig.pipe.run_cycle()
    assert {r["app"] for r in rig.rows("SELECT app FROM signature")} == {"one", "two"}


def test_severity_names_and_a_default_for_anything_odd(rig):
    rig.add("a.ndjson", [ev("a", severity=3), ev("b", severity=99), ev("c", severity=None)])
    rig.pipe.run_cycle()
    got = {r["severity"] for r in rig.rows("SELECT severity FROM hourly")}
    assert got == {"err", "info"}


def test_only_the_newest_samples_are_kept_per_signature(rig):
    rig.add("a.ndjson", [ev(f"same shape {i}", ts=f"Sep 26 13:0{i}:00") for i in range(8)])
    rig.pipe.run_cycle()
    assert len(rig.rows("SELECT * FROM sample")) == 5
    assert rig.rows("SELECT MIN(ts) AS t FROM sample")[0]["t"] == "2026-09-26T13:03:00Z"


def test_a_storm_of_identical_lines_is_counted_exactly(rig):
    rig.add("a.ndjson", [ev("open() failed: no such file")] * 3000)
    started = time.monotonic()
    r = rig.pipe.run_cycle()
    assert r.counted == 3000 and rig.total() == 3000 and len(rig.rows("SELECT * FROM signature")) == 1
    assert time.monotonic() - started < 5


def test_unparsed_lines_are_counted_under_a_placeholder_app(rig):
    rig.add("a.ndjson", [{"source_ip": "198.51.100.1", "parse_failed": True, "raw": "junk", "msg": "junk",
                          "severity": 6, "timestamp": RECEIVED}])  # fmt: skip
    r = rig.pipe.run_cycle()
    assert r.parse_failed == 1 and r.counted == 1
    assert rig.rows("SELECT app FROM signature")[0]["app"] == "(unparsed)"


def test_metrics_are_recorded_each_cycle(rig):
    rig.add("a.ndjson", [ev("x")])
    rig.pipe.run_cycle()
    got = {r["metric_id"]: r["value"] for r in rig.rows("SELECT metric_id, value FROM metric")}
    assert got["ingest.events"] == 1.0 and "ingest.backlog_bytes" in got and "ingest.lag_seconds" in got


# ---------- replay, restarts, and partial lines ----------


def test_running_the_same_files_again_does_not_double_count(rig):
    rig.add("a.ndjson", [ev(SSH.format(ip=i, port=i + 1000)) for i in range(5)])
    rig.pipe.run_cycle()
    again = rig.pipe.run_cycle()
    assert again.events == 0 and rig.total() == 5


def test_a_restart_continues_without_double_counting_and_keeps_signature_ids(rig):
    rig.add("a.ndjson", [ev("first thing happened")])
    rig.pipe.run_cycle()
    sid = rig.rows("SELECT sig_id FROM signature")[0]["sig_id"]
    rig.close()
    rig.open()  # a brand new store, miner, and pipeline over the same files
    rig.add("a.ndjson", [ev("first thing happened"), ev("a second thing happened")])
    r = rig.pipe.run_cycle()
    assert r.events == 2 and rig.total() == 3
    assert rig.rows("SELECT n FROM hourly WHERE sig_id = ?", sid)[0]["n"] == 2
    assert len(rig.rows("SELECT * FROM signature")) == 2


def test_a_half_written_last_line_waits_for_the_rest(rig):
    line = json.dumps(ev("the third line"))
    path = rig.add("a.ndjson", [ev("one"), ev("two")], partial=line[:20])
    r = rig.pipe.run_cycle()
    assert r.events == 2
    with path.open("ab") as f:
        f.write(line[20:].encode() + b"\n")
    r = rig.pipe.run_cycle()
    assert r.events == 1 and r.malformed == 0 and rig.total() == 3


def test_a_new_days_file_and_late_writes_to_the_old_one_are_both_read_once(rig):
    rig.add("2026-09-25.ndjson", [ev("before midnight", ts="Sep 25 23:59:00")])
    rig.pipe.run_cycle()
    rig.add("2026-09-26.ndjson", [ev("after midnight", ts="Sep 26 00:00:05")])
    rig.add("2026-09-25.ndjson", [ev("a late line for yesterday", ts="Sep 25 23:59:59")])
    r = rig.pipe.run_cycle()
    assert r.events == 2 and rig.total() == 3
    assert {r["day"] for r in rig.rows("SELECT day FROM hourly")} == {"2026-09-25", "2026-09-26"}


def test_a_byte_budget_spreads_a_backlog_over_cycles_without_losing_or_repeating(tmp_path):
    rig = Rig(tmp_path, max_bytes_per_cycle=2000)
    rig.add("a.ndjson", [ev(f"line number {i}") for i in range(60)])
    cycles, last = 0, None
    while last is None or last.backlog_bytes:
        last = rig.pipe.run_cycle()
        cycles += 1
        assert cycles < 50
    assert cycles > 2 and rig.total() == 60
    rig.close()


def test_a_failed_flush_changes_nothing_and_the_next_cycle_counts_once(rig, monkeypatch):
    rig.add("a.ndjson", [ev("x"), ev("y")])
    real = Tx.add_hourly
    monkeypatch.setattr(Tx, "add_hourly", lambda self, rows: (_ for _ in ()).throw(RuntimeError("disk full")))
    with pytest.raises(RuntimeError):
        rig.pipe.run_cycle()
    assert rig.total() == 0 and rig.rows("SELECT * FROM ingest_offset") == []
    monkeypatch.setattr(Tx, "add_hourly", real)
    rig.pipe.run_cycle()
    assert rig.total() == 2


def test_a_truncated_file_is_read_again_from_the_start(rig):
    path = rig.add("a.ndjson", [ev("one"), ev("two"), ev("three")])
    rig.pipe.run_cycle()
    path.write_bytes((json.dumps(ev("brand new")) + "\n").encode())
    r = rig.pipe.run_cycle()
    assert r.truncated == ["a.ndjson"] and r.events == 1


def test_offsets_for_deleted_files_are_forgotten(rig):
    path = rig.add("old.ndjson", [ev("x")])
    rig.pipe.run_cycle()
    path.unlink()
    rig.pipe.run_cycle()
    assert rig.rows("SELECT * FROM ingest_offset") == []


def test_bad_lines_are_skipped_and_do_not_wedge_the_reader(rig):
    rig.add("a.ndjson", ["this is not json", "[1, 2, 3]", "", ev("good one")])
    r = rig.pipe.run_cycle()
    assert r.malformed == 2 and r.events == 1 and rig.total() == 1
    assert rig.pipe.run_cycle().events == 0


# ---------- event time ----------


def test_the_host_timezone_decides_the_utc_hour(tmp_path):
    systems = Systems.from_mapping(
        {"systems": [{"id": "gw", "hosts": ["198.51.100.1"], "source_tz": "America/New_York"}]}
    )
    rig = Rig(tmp_path, systems=systems)
    rig.add("a.ndjson", [ev("x", ts="Sep 26 13:22:33")])
    rig.pipe.run_cycle()
    assert rig.rows("SELECT hour FROM hourly")[0]["hour"] == 17
    rig.close()


def test_a_precise_epoch_time_beats_the_header(rig):
    rig.add("a.ndjson", [ev("x", ts="Sep 26 01:00:00", epoch_ms=1790442597425)])
    rig.pipe.run_cycle()
    assert rig.rows("SELECT day, hour FROM hourly")[0] == {"day": "2026-09-26", "hour": 17}


def test_backfilled_events_land_in_their_own_hours_not_todays(rig):
    old = [
        ev("old thing", ts="Aug 25 02:29:39", imported=True),
        ev("old thing", ts="Aug 25 02:51:00", imported=True),
    ]
    rig.add("import.ndjson", old)
    rig.add("2026-09-26.ndjson", [ev("old thing", ts="Sep 26 13:00:00")])
    rig.pipe.run_cycle()
    got = {(r["day"], r["hour"]): r["n"] for r in rig.rows("SELECT day, hour, n FROM hourly")}
    assert got == {("2026-08-25", 2): 2, ("2026-09-26", 13): 1}
    sig = rig.rows("SELECT first_seen, last_seen FROM signature")[0]
    assert sig == {"first_seen": "2026-08-25T02:29:39Z", "last_seen": "2026-09-26T13:00:00Z"}


def test_an_event_with_no_usable_time_falls_back_to_receive_time_and_is_flagged(rig):
    rig.add("a.ndjson", [ev("x", ts=None)])
    r = rig.pipe.run_cycle()
    assert r.time_fallback == 1
    assert rig.rows("SELECT day, hour FROM hourly")[0] == {"day": "2026-09-26", "hour": 17}


def test_a_signature_back_after_a_long_quiet_period_remembers_when_it_was_last_seen(rig):
    rig.add("a.ndjson", [ev("rare event", ts="Sep  1 10:00:00"), ev("rare event", ts="Sep 20 08:00:00")])
    rig.pipe.run_cycle()
    sig = rig.rows("SELECT first_seen, last_seen, prev_last_seen FROM signature")[0]
    assert sig == {"first_seen": "2026-09-01T10:00:00Z", "last_seen": "2026-09-20T08:00:00Z",
                   "prev_last_seen": "2026-09-01T10:00:00Z"}  # fmt: skip


# ---------- systems and unknown hosts ----------


def test_known_hosts_map_to_their_system_and_unknown_ones_are_flagged(tmp_path):
    systems = Systems.from_mapping(
        {"default_system": "other", "systems": [{"id": "gw", "hosts": ["198.51.100.1"]}]}
    )
    rig = Rig(tmp_path, systems=systems)
    rig.add("a.ndjson", [ev("a", host="198.51.100.1"), ev("b", host="203.0.113.50")])
    r = rig.pipe.run_cycle()
    hosts = {h["host"]: h for h in rig.rows("SELECT * FROM host")}
    assert (hosts["198.51.100.1"]["system_id"], hosts["198.51.100.1"]["known"]) == ("gw", 1)
    assert (hosts["203.0.113.50"]["system_id"], hosts["203.0.113.50"]["known"]) == ("other", 0)
    assert r.unknown_hosts == 1
    rig.close()


def test_a_host_becomes_known_when_systems_yaml_is_updated(tmp_path):
    rig = Rig(tmp_path)
    rig.add("a.ndjson", [ev("a")])
    rig.pipe.run_cycle()
    assert rig.rows("SELECT known FROM host")[0]["known"] == 0
    rig.pipe.set_config(
        Systems.from_mapping({"systems": [{"id": "gw", "hosts": ["198.51.100.1"]}]}), rig.ignore
    )
    rig.add("a.ndjson", [ev("b")])
    rig.pipe.run_cycle()
    assert rig.rows("SELECT known, system_id FROM host")[0] == {"known": 1, "system_id": "gw"}
    rig.close()


# ---------- ignore rules ----------


def test_ignored_events_are_counted_per_rule_but_not_mined_or_counted(tmp_path):
    ignore = IgnoreRules.from_mapping({"ignore": [{"name": "flaps", "match": "(?i)link (up|down)"}]})
    rig = Rig(tmp_path, ignore=ignore)
    rig.add(
        "a.ndjson",
        [ev("Link Down on port 3", app="kernel"), ev("link up on port 3", app="kernel"), ev("real news")],
    )
    r = rig.pipe.run_cycle()
    assert (r.suppressed, r.counted) == (2, 1)
    assert rig.rows("SELECT rule, n FROM suppressed") == [{"rule": "flaps", "n": 2}]
    assert len(rig.rows("SELECT * FROM signature")) == 1 and rig.total() == 1
    rig.close()


def test_an_ignored_source_still_shows_as_alive(tmp_path):
    ignore = IgnoreRules.from_mapping({"ignore": [{"name": "everything", "match": ".*"}]})
    rig = Rig(tmp_path, ignore=ignore)
    rig.add("a.ndjson", [ev("anything", ts="Sep 26 13:00:00")])
    rig.pipe.run_cycle()
    assert rig.rows("SELECT last_seen FROM host")[0]["last_seen"] == "2026-09-26T13:00:00Z"
    assert rig.total() == 0
    rig.close()


def test_a_rule_can_target_a_system(tmp_path):
    systems = Systems.from_mapping({"systems": [{"id": "switches", "hosts": ["198.51.100.1"]}]})
    ignore = IgnoreRules.from_mapping({"ignore": [{"name": "r", "host": "switches", "match": "flap"}]})
    rig = Rig(tmp_path, systems=systems, ignore=ignore)
    rig.add("a.ndjson", [ev("flap", host="198.51.100.1"), ev("flap", host="203.0.113.9")])
    r = rig.pipe.run_cycle()
    assert (r.suppressed, r.counted) == (1, 1)
    rig.close()


def test_an_ignored_signature_is_suppressed_from_then_on(rig):
    rig.add("a.ndjson", [ev("noisy repeating thing 1")])
    rig.pipe.run_cycle()
    sid = rig.rows("SELECT sig_id FROM signature")[0]["sig_id"]
    with rig.store.write() as tx:
        tx.set_ignored(sid, True)
    rig.add("a.ndjson", [ev("noisy repeating thing 2"), ev("noisy repeating thing 3")])
    r = rig.pipe.run_cycle()
    assert r.suppressed == 2 and rig.total() == 1
    assert rig.rows("SELECT rule, n FROM suppressed") == [{"rule": f"signature:{sid}", "n": 2}]


# ---------- the miner and its state ----------


def test_losing_the_miner_state_starts_a_new_generation_instead_of_colliding(rig):
    rig.add("a.ndjson", [ev("first message")])
    rig.pipe.run_cycle()
    old = rig.rows("SELECT sig_id, miner_generation FROM signature")[0]
    rig.close()
    (rig.tmp / "state" / "drain3.bin").unlink()
    rig.open()
    assert rig.pipe.generation == old["miner_generation"] + 1
    rig.add("a.ndjson", [ev("a different message")])
    rig.pipe.run_cycle()
    sigs = rig.rows("SELECT sig_id, miner_generation, cluster_id FROM signature ORDER BY sig_id")
    assert (
        len(sigs) == 2
        and sigs[0]["sig_id"] == old["sig_id"]
        and sigs[1]["miner_generation"] == sigs[0]["miner_generation"] + 1
    )
    assert rig.rows("SELECT n FROM hourly WHERE sig_id = ?", old["sig_id"])[0]["n"] == 1
    assert rig.rows("SELECT action FROM audit_log")[0]["action"] == "miner.generation"


def test_a_corrupt_miner_state_is_set_aside_and_treated_as_lost(rig):
    rig.add("a.ndjson", [ev("first message")])
    rig.pipe.run_cycle()
    rig.close()
    (rig.tmp / "state" / "drain3.bin").write_bytes(b"this is not a miner state")
    rig.open()
    assert rig.sigs.recovered and list((rig.tmp / "state").glob("drain3.bin.corrupt-*"))
    assert rig.pipe.generation == 2


def test_the_miner_is_not_reset_by_an_ordinary_restart(rig):
    rig.add("a.ndjson", [ev("first message")])
    rig.pipe.run_cycle()
    rig.close()
    rig.open()
    assert rig.pipe.generation == 1 and not rig.rows("SELECT * FROM audit_log")


# ---------- learning period ----------


def test_the_learning_period_starts_with_live_data_not_with_a_backfill(rig):
    rig.add("import.ndjson", [ev("old", ts="Aug 25 02:00:00", imported=True)])
    rig.pipe.run_cycle()
    assert not rig.rows("SELECT * FROM meta WHERE key = 'live_since'")
    assert learning_status(None, NOW) == {"active": True, "day": 0, "of": 7}
    rig.add("2026-09-26.ndjson", [ev("live")])
    rig.pipe.run_cycle()
    since = rig.rows("SELECT value FROM meta WHERE key = 'live_since'")[0]["value"]
    assert since == "2026-09-26T17:23:00Z"


def test_the_learning_banner_counts_days_and_then_clears():
    start = "2026-09-26T17:23:00Z"
    assert learning_status(start, datetime(2026, 9, 26, 20, 0, tzinfo=UTC)) == {
        "active": True,
        "day": 1,
        "of": 7,
    }
    assert learning_status(start, datetime(2026, 10, 3, 17, 22, tzinfo=UTC))["day"] == 7
    assert learning_status(start, datetime(2026, 10, 3, 17, 22, tzinfo=UTC))["active"] is True
    assert learning_status(start, datetime(2026, 10, 3, 17, 23, tzinfo=UTC)) == {
        "active": False,
        "day": 8,
        "of": 7,
    }
    assert learning_status(start, datetime(2026, 9, 29, tzinfo=UTC), days=2)["active"] is False
