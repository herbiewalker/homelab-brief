"""Self-monitoring: memory, disk space, the days-until-full projection, and the storage_low finding."""

from datetime import UTC, datetime, timedelta

import pytest

from brief import selfmon
from brief.store import Store

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
GB = selfmon.GB


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "baseline.db")
    yield s
    s.close()


def samples(store, rows):
    """rows: (days ago, archive GB, free GB, total GB)."""
    with store.write() as tx:
        for ago, archive, free, total in rows:
            at = NOW - timedelta(days=ago)
            tx.add_metrics(
                [
                    (at, "storage.archive_bytes", archive * GB),
                    (at, "storage.free_bytes", free * GB),
                    (at, "storage.total_bytes", total * GB),
                ]
            )


# ---------------------------------------------------------------- the projection


def test_days_until_full_comes_from_the_growth_of_the_archive(store):
    samples(store, [(6, 100, 500, 1000), (3, 130, 470, 1000), (0, 160, 440, 1000)])  # 10 GB a day
    nums = selfmon.numbers(store, NOW)
    assert nums["archive_mb"] == 160_000 and nums["archive_free_gb"] == 440 and nums["archive_free_pct"] == 44
    assert nums["storage_days_left"] == 44.0  # 440 GB free at 10 GB a day


def test_no_projection_from_less_than_two_days_of_history_or_a_flat_archive(store):
    samples(store, [(1, 100, 500, 1000), (0, 150, 450, 1000)])  # a 50 GB burst in one day is not a trend
    assert selfmon.numbers(store, NOW)["storage_days_left"] is None
    samples(store, [(9, 160, 440, 1000), (0, 160, 440, 1000)])  # nothing grew over nine days
    flat = Store(store.path.with_name("flat.db"))
    try:
        with flat.write() as tx:
            for ago in (9, 0):
                tx.add_metrics(
                    [
                        (NOW - timedelta(days=ago), "storage.archive_bytes", 160 * GB),
                        (NOW - timedelta(days=ago), "storage.free_bytes", 440 * GB),
                    ]
                )
        assert selfmon.numbers(flat, NOW)["storage_days_left"] is None
    finally:
        flat.close()


def test_everything_is_none_before_the_first_sample(store):
    nums = selfmon.numbers(store, NOW)
    assert (
        nums["archive_mb"] is None and nums["archive_free_gb"] is None and nums["storage_days_left"] is None
    )
    assert nums["events_last_hour"] == 0


# ---------------------------------------------------------------- the finding


@pytest.mark.parametrize(
    "nums, expect",
    [
        ({"archive_free_pct": 40, "archive_free_gb": 400, "storage_days_left": 90}, False),
        ({"archive_free_pct": 40, "archive_free_gb": 400, "storage_days_left": None}, False),
        ({"archive_free_pct": 9.5, "archive_free_gb": 95, "storage_days_left": None}, True),
        ({"archive_free_pct": 40, "archive_free_gb": 400, "storage_days_left": 6}, True),
        ({"archive_free_pct": None, "archive_free_gb": None, "storage_days_left": None}, False),
    ],
)
def test_storage_low_fires_on_little_space_or_a_near_full_projection(nums, expect):
    found = selfmon.storage_findings(nums)
    assert bool(found) is expect
    if found:
        f = found[0]
        assert f.kind == "storage_low" and f.id == "storage_low-archive" and f.escalates is False
        assert f.system == selfmon.SYSTEM


def test_the_thresholds_come_from_playbook_yaml(tmp_path):
    folder = tmp_path / "cfg" / "brief"
    folder.mkdir(parents=True)
    (folder / "playbook.yaml").write_text("schema: 1\nthresholds:\n  storage_low: { min_free_pct: 60 }\n")
    env = {"BRIEF_CONFIG_DIR": str(tmp_path / "cfg"), "BRIEF_DEFAULTS_DIR": str(tmp_path / "none")}
    nums = {"archive_free_pct": 40, "archive_free_gb": 400, "storage_days_left": None}
    assert selfmon.storage_findings(nums, env) and not selfmon.storage_findings(nums)


# ---------------------------------------------------------------- sampling


def test_record_samples_the_archive_and_the_disk(store, tmp_path):
    archive = tmp_path / "archive"
    (archive / "a").mkdir(parents=True)
    (archive / "a" / "x.log").write_bytes(b"x" * 1500)
    (archive / "y.log").write_bytes(b"y" * 500)
    sample = selfmon.record(store, NOW, archive, tmp_path)
    assert sample["storage.archive_bytes"] == 2000 and sample["storage.free_bytes"] > 0
    nums = selfmon.numbers(store, NOW)
    assert nums["archive_mb"] == 0.0 and nums["archive_free_gb"] is not None


def test_a_missing_archive_is_not_an_error(store, tmp_path):
    sample = selfmon.record(store, NOW, tmp_path / "nope", tmp_path)
    assert "storage.archive_bytes" not in sample


def test_dir_bytes_ignores_what_it_cannot_read(tmp_path):
    (tmp_path / "f").write_bytes(b"12345")
    (tmp_path / "link").symlink_to(tmp_path / "f")  # not followed, not counted twice
    assert selfmon.dir_bytes(tmp_path) == 5 and selfmon.dir_bytes(tmp_path / "missing") == 0


# ---------------------------------------------------------------- memory


def test_memory_is_the_working_set_of_the_container_with_its_limit(tmp_path):
    (tmp_path / "memory.current").write_text(str(500 * selfmon.MB))
    (tmp_path / "memory.max").write_text(str(768 * selfmon.MB))
    (tmp_path / "memory.stat").write_text(f"anon 1\ninactive_file {100 * selfmon.MB}\n")
    assert selfmon.memory_mb(tmp_path) == (400.0, 768.0)


def test_memory_has_no_limit_when_the_container_is_unlimited_and_falls_back_to_the_process(tmp_path):
    (tmp_path / "memory.current").write_text(str(200 * selfmon.MB))
    (tmp_path / "memory.max").write_text("max\n")
    assert selfmon.memory_mb(tmp_path) == (200.0, None)
    status = tmp_path / "status"
    status.write_text("Name: x\nVmRSS:\t 51200 kB\n")
    assert selfmon.memory_mb(tmp_path / "no-cgroup", status) == (52.4, None)
    assert selfmon.memory_mb(tmp_path / "no-cgroup", tmp_path / "no-proc") == (None, None)


def test_a_cgroup_v1_container_with_no_limit_reports_none(tmp_path):
    folder = tmp_path / "memory"
    folder.mkdir()
    (folder / "memory.usage_in_bytes").write_text(str(300 * selfmon.MB))
    (folder / "memory.limit_in_bytes").write_text(str(1 << 62))
    assert selfmon.memory_mb(tmp_path) == (300.0, None)


# ---------------------------------------------------------------- per-source rate


def test_source_rates_count_the_last_hour_per_sender(store):
    day, hour = f"{NOW:%Y-%m-%d}", NOW.hour
    with store.write() as tx:
        sid = tx.signature_for(0, 1, "app", "a template", NOW)
        tx.add_hourly(
            [
                (day, hour, "198.51.100.1", sid, "info", 40),  # this hour
                (day, hour - 1, "198.51.100.1", sid, "info", 10),  # the hour before: still in the window
                (day, hour - 3, "198.51.100.1", sid, "info", 999),  # too old
                (day, hour, "198.51.100.2", sid, "info", 7),
            ]
        )
        tx.add_suppressed([(day, hour, "198.51.100.2", "rule", 3)])
    assert selfmon.source_rates(store, NOW) == {"198.51.100.1": 50, "198.51.100.2": 10}
    assert selfmon.numbers(store, NOW)["events_last_hour"] == 50 + 7  # counted, not the suppressed ones
