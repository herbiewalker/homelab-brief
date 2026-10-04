import sqlite3
import threading
from datetime import UTC, datetime, timedelta, timezone

import pytest

from brief import store as s

T0 = "2026-09-20T10:00:00Z"
MIGRATION_2 = s.Migration(2, "extra", "CREATE TABLE extra (x INTEGER);")


@pytest.fixture
def db(tmp_path):
    st = s.Store(tmp_path / "state" / "baseline.db")
    yield st
    st.close()


def rows(st, sql, *args):
    with st.read() as conn:
        return [dict(r) for r in conn.execute(sql, args)]


# ---------- schema and migrations ----------


def test_a_new_database_has_the_whole_baseline_schema_in_wal_mode(db):
    names = {r["name"] for r in rows(db, "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {
        "meta",
        "signature",
        "host",
        "hourly",
        "sample",
        "metric",
        "audit_log",
        "alert",
        "feedback",
    } <= names
    assert db.schema_version == 1
    assert rows(db, "PRAGMA journal_mode")[0]["journal_mode"] == "wal"
    assert db.check()


def test_reopening_is_a_no_op_and_keeps_the_data(tmp_path):
    path = tmp_path / "baseline.db"
    a = s.Store(path)
    with a.write() as tx:
        tx.set_meta("k", "v")
    a.close()
    b = s.Store(path)
    with b.write() as tx:
        assert tx.get_meta("k") == "v"
    assert b.schema_version == 1
    assert not list(tmp_path.glob("*.pre-v*"))
    b.close()


def test_migrations_are_numbered_without_gaps(tmp_path):
    (tmp_path / "0001_a.sql").write_text("SELECT 1;")
    (tmp_path / "0003_c.sql").write_text("SELECT 1;")
    with pytest.raises(s.StoreError, match="no gaps"):
        s.load_migrations(tmp_path)
    (tmp_path / "bad.sql").write_text("SELECT 1;")
    with pytest.raises(s.StoreError, match="file names"):
        s.load_migrations(tmp_path)


def test_a_new_migration_is_applied_and_the_old_database_is_backed_up_first(tmp_path):
    path = tmp_path / "baseline.db"
    first = s.Store(path)
    with first.write() as tx:
        tx.set_meta("keep", "me")
    first.close()
    upgraded = s.Store(path, migrations=[*s.load_migrations(), MIGRATION_2])
    assert upgraded.schema_version == 2
    assert rows(upgraded, "SELECT name FROM sqlite_master WHERE name = 'extra'")
    backup = tmp_path / "baseline.db.pre-v1"
    assert backup.exists()
    old = sqlite3.connect(backup)
    assert old.execute("SELECT value FROM meta WHERE key = 'keep'").fetchone()[0] == "me"
    assert old.execute("SELECT name FROM sqlite_master WHERE name = 'extra'").fetchone() is None
    old.close()
    upgraded.close()


def test_a_failing_migration_is_rolled_back_whole(tmp_path):
    path = tmp_path / "baseline.db"
    s.Store(path).close()
    broken = s.Migration(2, "broken", "CREATE TABLE half (x INTEGER); INSERT INTO nope VALUES (1);")
    with pytest.raises(s.StoreError, match="rolled back"):
        s.Store(path, migrations=[*s.load_migrations(), broken])
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'half'").fetchone() is None
    conn.close()


def test_a_database_from_a_newer_program_is_refused(tmp_path):
    path = tmp_path / "baseline.db"
    s.Store(path).close()
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 99")
    conn.close()
    with pytest.raises(s.StoreError, match="refusing"):
        s.Store(path)


# ---------- signatures ----------


def test_a_signature_gets_a_stable_id_from_generation_and_cluster(db):
    with db.write() as tx:
        a = tx.signature_for(1, 7, "sshd", "Accepted <*> for <*>", T0)
        b = tx.signature_for(1, 7, "sshd", "Accepted <*> for <*>", T0)
        c = tx.signature_for(1, 8, "sshd", "Failed <*>", T0)
    assert a == b and a != c


def test_a_changed_template_is_updated_in_place(db):
    with db.write() as tx:
        sid = tx.signature_for(1, 7, "app", "user <*> logged in", T0)
        assert tx.signature_for(1, 7, "app", "user <*> logged <*>", T0) == sid
    assert (
        rows(db, "SELECT template FROM signature WHERE sig_id = ?", sid)[0]["template"]
        == "user <*> logged <*>"
    )


def test_a_reset_miner_never_collides_with_existing_signatures(db):
    with db.write() as tx:
        gen1 = tx.miner_generation()
        old = tx.signature_for(gen1, 1, "app", "old template", T0)
        tx.add_hourly([("2026-09-20", 10, "198.51.100.1", old, "info", 5)])
    with db.write() as tx:
        gen2 = tx.bump_miner_generation()
        # The new miner starts counting clusters from 1 again.
        new = tx.signature_for(gen2, 1, "app", "different template", "2026-09-21T00:00:00Z")
        again_old = tx.signature_for(gen1, 1, "app", "old template", T0)
    assert gen2 == gen1 + 1
    assert new != old and again_old == old
    assert len({r["sig_id"] for r in rows(db, "SELECT sig_id FROM signature")}) == 2
    assert rows(db, "SELECT n FROM hourly WHERE sig_id = ?", old)[0]["n"] == 5


def test_the_unique_constraint_backs_up_the_id_mapping(db):
    with db.write() as tx:
        tx.signature_for(1, 1, "a", "t", T0)
    with pytest.raises(sqlite3.IntegrityError), db.write() as tx:
        tx.db.execute(
            "INSERT INTO signature (miner_generation, cluster_id, first_seen, last_seen) VALUES (1, 1, ?, ?)",
            (T0, T0),
        )


def test_miner_generation_starts_at_one(db):
    with db.write() as tx:
        assert tx.miner_generation() == 1
        assert tx.bump_miner_generation() == 2
        assert tx.miner_generation() == 2


def test_first_and_last_seen_follow_event_time_not_arrival_order(db):
    with db.write() as tx:
        sid = tx.signature_for(1, 1, "a", "t", "2026-09-20T10:00:00Z")
        tx.touch_signature(sid, "2026-09-20T10:05:00Z", "2026-09-20T10:30:00Z")
        tx.touch_signature(sid, "2026-09-10T08:00:00Z", "2026-09-10T09:00:00Z")  # a backfill arrives late
    row = rows(db, "SELECT * FROM signature WHERE sig_id = ?", sid)[0]
    assert row["first_seen"] == "2026-09-10T08:00:00Z"
    assert row["last_seen"] == "2026-09-20T10:30:00Z"


def test_prev_last_seen_is_set_only_after_a_quiet_gap(db):
    with db.write() as tx:
        sid = tx.signature_for(1, 1, "a", "t", "2026-09-01T10:00:00Z")
        tx.touch_signature(sid, "2026-09-01T10:10:00Z", "2026-09-01T10:20:00Z")  # 10 minutes: no gap
    assert rows(db, "SELECT prev_last_seen FROM signature")[0]["prev_last_seen"] is None
    with db.write() as tx:
        tx.touch_signature(sid, "2026-09-09T06:00:00Z", "2026-09-09T06:30:00Z")  # returns after 8 days
    row = rows(db, "SELECT last_seen, prev_last_seen FROM signature")[0]
    assert row["prev_last_seen"] == "2026-09-01T10:20:00Z" and row["last_seen"] == "2026-09-09T06:30:00Z"
    with db.write() as tx:
        tx.touch_signature(sid, "2026-09-09T06:40:00Z", "2026-09-09T07:00:00Z")  # still the same run
    assert rows(db, "SELECT prev_last_seen FROM signature")[0]["prev_last_seen"] == "2026-09-01T10:20:00Z"


def test_a_quiet_gap_can_be_set_per_call(db):
    with db.write() as tx:
        sid = tx.signature_for(1, 1, "a", "t", "2026-09-01T10:00:00Z")
        tx.touch_signature(
            sid, "2026-09-01T10:10:00Z", "2026-09-01T10:20:00Z", quiet_gap=timedelta(minutes=5)
        )
    assert rows(db, "SELECT prev_last_seen FROM signature")[0]["prev_last_seen"] == "2026-09-01T10:00:00Z"


def test_touching_an_unknown_signature_is_an_error(db):
    with pytest.raises(s.StoreError), db.write() as tx:
        tx.touch_signature(999, T0, T0)


# ---------- counts, hosts, samples, metrics ----------


def test_hourly_counts_accumulate_on_the_same_key(db):
    with db.write() as tx:
        sid = tx.signature_for(1, 1, "a", "t", T0)
        tx.add_hourly([("2026-09-20", 10, "198.51.100.1", sid, "info", 3)])
    with db.write() as tx:
        tx.add_hourly(
            [
                ("2026-09-20", 10, "198.51.100.1", sid, "info", 4),
                ("2026-09-20", 10, "198.51.100.1", sid, "err", 1),
            ]
        )
    got = {r["severity"]: r["n"] for r in rows(db, "SELECT severity, n FROM hourly")}
    assert got == {"info": 7, "err": 1}


def test_counts_for_different_hours_hosts_and_days_stay_separate(db):
    with db.write() as tx:
        sid = tx.signature_for(1, 1, "a", "t", T0)
        tx.add_hourly([
            ("2026-09-20", 10, "198.51.100.1", sid, "info", 1),
            ("2026-09-20", 11, "198.51.100.1", sid, "info", 1),
            ("2026-09-20", 10, "198.51.100.2", sid, "info", 1),
            ("2026-09-21", 10, "198.51.100.1", sid, "info", 1),
        ])  # fmt: skip
    assert rows(db, "SELECT COUNT(*) AS c FROM hourly")[0]["c"] == 4


def test_bad_hours_and_unknown_signatures_are_rejected(db):
    with db.write() as tx:
        sid = tx.signature_for(1, 1, "a", "t", T0)
    with pytest.raises(sqlite3.IntegrityError), db.write() as tx:
        tx.add_hourly([("2026-09-20", 24, "h", sid, "info", 1)])
    with pytest.raises(sqlite3.IntegrityError), db.write() as tx:
        tx.add_hourly([("2026-09-20", 3, "h", 12345, "info", 1)])


def test_host_first_and_last_seen_move_the_right_way(db):
    with db.write() as tx:
        tx.touch_host("198.51.100.1", "2026-09-20T10:00:00Z", "2026-09-20T11:00:00Z", system_id="gateway")
        tx.touch_host("198.51.100.1", "2026-09-19T09:00:00Z", "2026-09-20T10:30:00Z")
        tx.touch_host("198.51.100.1", "2026-09-21T00:00:00Z", "2026-09-21T01:00:00Z")
        tx.set_expected_gap("198.51.100.1", 15)
    row = rows(db, "SELECT * FROM host")[0]
    assert (row["first_seen"], row["last_seen"]) == ("2026-09-19T09:00:00Z", "2026-09-21T01:00:00Z")
    assert row["system_id"] == "gateway" and row["expected_gap_min"] == 15


def test_only_the_newest_samples_per_signature_are_kept(db):
    with db.write() as tx:
        a = tx.signature_for(1, 1, "a", "t", T0)
        b = tx.signature_for(1, 2, "a", "u", T0)
        tx.add_samples(a, [(f"2026-09-20T10:0{i}:00Z", "h", f"line {i}") for i in range(8)], keep=3)
        tx.add_samples(b, [(T0, "h", "only one")], keep=3)
    got = [r["msg"] for r in rows(db, "SELECT msg FROM sample WHERE sig_id = ? ORDER BY ts", a)]
    assert got == ["line 5", "line 6", "line 7"]
    assert len(rows(db, "SELECT * FROM sample WHERE sig_id = ?", b)) == 1


def test_metrics_are_stored_as_a_series(db):
    with db.write() as tx:
        tx.add_metrics([(T0, "ingest_lag_s", 1.5), ("2026-09-20T10:01:00Z", "ingest_lag_s", 2.5)])
    assert [
        r["value"] for r in rows(db, "SELECT value FROM metric WHERE metric_id = 'ingest_lag_s' ORDER BY ts")
    ] == [1.5, 2.5]


# ---------- audit, alerts, feedback ----------


def test_the_audit_log_records_who_did_what(db):
    db.audit("admin", "config.save", "network.yaml")
    with db.write() as tx:
        tx.audit("admin", "login", ts=datetime(2026, 9, 20, 10, 0, tzinfo=UTC))
    got = rows(db, "SELECT user, action, detail, ts FROM audit_log ORDER BY rowid")
    assert got[0]["action"] == "config.save" and got[0]["detail"] == "network.yaml"
    assert got[1]["ts"] == "2026-09-20T10:00:00Z"


def test_alerts_change_only_the_fields_they_are_given(db):
    with db.write() as tx:
        tx.upsert_alert("f1", kind="smart", rule="smart_failing", level="crit", first_sent=T0, last_sent=T0)
    with db.write() as tx:
        tx.upsert_alert("f1", state="acked")
        tx.upsert_alert("f2")
    a = rows(db, "SELECT * FROM alert WHERE finding_id = 'f1'")[0]
    assert a["state"] == "acked" and a["kind"] == "smart" and a["first_sent"] == T0
    assert rows(db, "SELECT state FROM alert WHERE finding_id = 'f2'")[0]["state"] == "open"


def test_alert_fields_and_states_are_checked(db):
    with pytest.raises(s.StoreError, match="unknown"), db.write() as tx:
        tx.upsert_alert("f1", bogus=1)
    with pytest.raises(s.StoreError, match="state"), db.write() as tx:
        tx.upsert_alert("f1", state="exploded")


def test_feedback_is_recorded(db):
    with db.write() as tx:
        tx.add_feedback("f1", "ack", ts=T0)
    assert rows(db, "SELECT finding_id, action FROM feedback") == [{"finding_id": "f1", "action": "ack"}]


# ---------- time handling ----------


def test_times_are_normalized_to_utc_and_bad_ones_rejected():
    assert s.iso(datetime(2026, 9, 26, 13, 22, 33)) == "2026-09-26T13:22:33Z"
    four_hours_behind = datetime(2026, 9, 26, 9, 22, 33, tzinfo=timezone(timedelta(hours=-4)))
    assert s.iso(four_hours_behind) == "2026-09-26T13:22:33Z"
    assert s.iso("2026-09-26T13:22:33Z") == "2026-09-26T13:22:33Z"
    for bad in ("2026-09-26 13:22:33", "2026-09-26T13:22:33+00:00", "yesterday", 5):
        with pytest.raises(s.StoreError):
            s.iso(bad)


# ---------- writing safely ----------


def test_a_failed_block_rolls_everything_back(db):
    with pytest.raises(RuntimeError), db.write() as tx:
        sid = tx.signature_for(1, 1, "a", "t", T0)
        tx.add_hourly([("2026-09-20", 10, "h", sid, "info", 1)])
        raise RuntimeError("boom")
    assert rows(db, "SELECT COUNT(*) AS c FROM signature")[0]["c"] == 0
    assert rows(db, "SELECT COUNT(*) AS c FROM hourly")[0]["c"] == 0
    with db.write() as tx:  # and the store is still usable
        tx.set_meta("ok", "1")


def test_write_blocks_cannot_be_nested(db):
    with db.write(), pytest.raises(s.StoreError, match="nested"), db.write():
        pass
    with db.write() as tx:  # the failed attempt did not leave the store locked
        tx.set_meta("ok", "1")


def test_readers_see_committed_data_while_a_write_is_open(db):
    with db.write() as tx:
        tx.set_meta("a", "1")
    with db.write() as tx:
        tx.set_meta("a", "2")  # uncommitted
        assert rows(db, "SELECT value FROM meta WHERE key = 'a'")[0]["value"] == "1"
    assert rows(db, "SELECT value FROM meta WHERE key = 'a'")[0]["value"] == "2"


def test_threads_sharing_one_store_lose_no_updates(db):
    with db.write() as tx:
        sid = tx.signature_for(1, 1, "a", "t", T0)

    def work():
        for _ in range(100):
            with db.write() as tx:
                tx.add_hourly([("2026-09-20", 10, "h", sid, "info", 1)])

    threads = [threading.Thread(target=work) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert rows(db, "SELECT n FROM hourly")[0]["n"] == 400


def test_two_stores_on_one_file_take_turns_like_the_worker_and_the_web_server(tmp_path):
    path = tmp_path / "baseline.db"
    worker, web = s.Store(path), s.Store(path)
    with worker.write() as tx:
        sid = tx.signature_for(1, 1, "a", "t", T0)

    def count(store):
        for _ in range(60):
            with store.write() as tx:
                tx.add_hourly([("2026-09-20", 10, "h", sid, "info", 1)])

    threads = [threading.Thread(target=count, args=(x,)) for x in (worker, web)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert rows(worker, "SELECT n FROM hourly")[0]["n"] == 120
    worker.close()
    web.close()
