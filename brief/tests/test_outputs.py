"""The daily run end to end: detectors, digest, files, state, status, and the failure path."""

import json
from datetime import timedelta

import golden_day as g
import pytest

from brief import detect, outputs, registry, status
from brief.store import Store


@pytest.fixture
def day(tmp_path):
    store, digest, out = g.build_daily(tmp_path)
    yield store, digest, out
    store.close()


def lines(path):
    return path.read_text(encoding="utf-8").splitlines()


def test_a_daily_run_writes_every_file_where_the_guide_says(day):
    store, digest, out = day
    assert digest.run_ok and len(digest.stories) == 5
    for path in (
        out / "latest" / "status.json",
        out / "latest" / "digest.md",
        out / "2026-09-20" / "digest.md",
        out / "state.json",
        out / "2026-09-20" / "hosts" / "198.51.100.1.md",
        out / "2026-09-20" / "hosts" / "198.51.100.2.md",
        out / "2026-09-20" / "hosts" / "198.51.100.3.md",
    ):
        assert path.exists(), path
    assert (out / "latest" / "digest.md").read_text() == (out / "2026-09-20" / "digest.md").read_text()
    assert not list(out.rglob("*.tmp"))  # every write was atomic


def test_every_markdown_file_is_within_150_lines(day):
    _, _, out = day
    files = list(out.rglob("*.md"))
    assert len(files) >= 5
    assert all(len(lines(p)) <= 150 for p in files)


def test_status_json_is_schema_1_with_the_daily_fields(day):
    _, _, out = day
    doc = json.loads((out / "latest" / "status.json").read_text())
    assert doc["schema"] == 1 and doc["run_ok"] is True and doc["learning"]["active"] is False
    assert [s["id"] for s in doc["systems"]] == ["gw", "nas", "cam"]
    assert doc["stories"][0]["kind"] == "absence" and doc["stories"][0]["rank"] == 1
    assert doc["pipeline"]["events_24h"] == 798 and doc["pipeline"]["parse_failed_24h"] == 0
    assert doc["silent_hosts"][0]["host"] == g.CAM and doc["daily_generated_at"] == "2026-09-20T06:00:00Z"
    assert doc["developing"] == [] and doc["quiet"]


def test_a_host_page_has_its_findings_busiest_signatures_and_samples(day):
    _, _, out = day
    text = (out / "2026-09-20" / "hosts" / "198.51.100.2.md").read_text()
    assert text.startswith("# 198.51.100.2 (Storage server)")
    assert "I/O error" in text and "## Busiest signatures, last 24 hours" in text
    assert "## Sample lines for the flagged signatures" in text


def test_state_json_carries_the_issues_and_a_second_day_makes_them_developing(day, tmp_path):
    store, _, out = day
    state = json.loads((out / "state.json").read_text())
    assert state["last_run"] == "2026-09-20" and len(state["issues"]) == 5
    next_day = g.RUN_AT + timedelta(days=1)
    again = outputs.run_daily(
        store, g.SYSTEMS, next_day, out, env=g.golden_env(tmp_path), state_dir=tmp_path / "none"
    )
    assert again.run_ok
    # the camera is still silent a day later, so that issue is now developing; the one-day findings are not
    assert [(d["id"], d["days"]) for d in again.developing] == [(f"absence-{g.CAM}", 2)]
    after = json.loads((out / "state.json").read_text())
    assert after["last_run"] == "2026-09-21" and after["issues"][f"absence-{g.CAM}"]["days"] == 2
    assert after["issues"]["novelty-8"]["quiet_runs"] == 1  # reported yesterday, not today


def test_running_the_same_day_again_does_not_age_the_state(day, tmp_path):
    store, _, out = day
    before = json.loads((out / "state.json").read_text())
    outputs.run_daily(
        store, g.SYSTEMS, g.RUN_AT, out, env=g.golden_env(tmp_path), state_dir=tmp_path / "none"
    )
    assert json.loads((out / "state.json").read_text()) == before


# ---------------------------------------------------------------- the failure path


def empty_store(tmp_path):
    return Store(tmp_path / "empty" / "baseline.db")


def test_a_database_with_no_events_writes_no_data_today_everywhere(tmp_path):
    store = empty_store(tmp_path)
    out = tmp_path / "out"
    try:
        digest = outputs.run_daily(store, g.SYSTEMS, g.RUN_AT, out, state_dir=tmp_path)
    finally:
        store.close()
    assert not digest.run_ok
    md = (out / "latest" / "digest.md").read_text().lower()
    assert "no data today" in md and "quiet" not in md and "nothing needs a look" not in md
    doc = json.loads((out / "latest" / "status.json").read_text())
    assert doc["run_ok"] is False and doc["headline"].startswith("No data today") and doc["stories"] == []
    assert not (out / "state.json").exists() and not (out / "2026-09-20" / "hosts").exists()


def test_a_failed_night_keeps_yesterdays_issues_instead_of_resolving_them(day, tmp_path):
    store, _, out = day
    before = (out / "state.json").read_text()
    # three nights in a row with no data would, if they counted, resolve every issue
    for n in (11, 12, 13):
        late = g.RUN_AT + timedelta(days=n)
        assert not outputs.run_daily(
            store, g.SYSTEMS, late, out, env=g.golden_env(tmp_path), state_dir=tmp_path
        ).run_ok
    assert (out / "state.json").read_text() == before


def test_a_detector_crash_becomes_a_no_data_digest_not_an_exception(day, tmp_path, monkeypatch):
    store, _, _ = day

    def boom(*a, **k):
        raise RuntimeError("detector exploded")

    monkeypatch.setattr(detect, "run", boom)
    out = tmp_path / "crash"
    digest = outputs.run_daily(store, g.SYSTEMS, g.RUN_AT, out, state_dir=tmp_path)
    assert not digest.run_ok and "the detectors failed (RuntimeError)" in digest.headline
    assert "No data today" in json.loads((out / "latest" / "status.json").read_text())["headline"]


def test_a_reported_pipeline_problem_fails_the_run(day, tmp_path):
    store, _, _ = day
    out = tmp_path / "unhealthy"
    digest = outputs.run_daily(
        store,
        g.SYSTEMS,
        g.RUN_AT,
        out,
        env=g.golden_env(tmp_path),
        state_dir=tmp_path,
        extra_problems=["receiver down"],
    )
    assert not digest.run_ok and "receiver down" in digest.headline


def test_dry_run_writes_nothing(day, tmp_path):
    store, _, _ = day
    out = tmp_path / "dry"
    d = outputs.run_daily(
        store, g.SYSTEMS, g.RUN_AT, out, env=g.golden_env(tmp_path), state_dir=tmp_path, write=False
    )
    assert d.run_ok and not out.exists()


# ---------------------------------------------------------------- the hourly status carries the daily story


def healthy(monkeypatch):
    """The synthetic month is from September, so the real worker-staleness check would fail; stub it."""
    ok = {"status": "ok", "checks": {"database": {"ok": True}}}
    monkeypatch.setattr("brief.health.check_health", lambda *a, **k: ok)


def live_status(store, previous, now, **kw):
    return status.build_status(store, g.SYSTEMS, 514, 60.0, 10, now=now, previous=previous, **kw)


def test_the_hourly_status_carries_the_daily_stories(day, monkeypatch):
    store, _, out = day
    healthy(monkeypatch)
    previous = json.loads((out / "latest" / "status.json").read_text())
    doc = live_status(store, previous, g.RUN_AT + timedelta(hours=2))
    assert doc["stories"] == previous["stories"] and doc["headline"] == previous["headline"]
    assert doc["daily_generated_at"] == previous["daily_generated_at"]


def test_a_stale_daily_run_is_a_failure_not_yesterdays_calm(day, monkeypatch):
    store, _, out = day
    healthy(monkeypatch)
    previous = json.loads((out / "latest" / "status.json").read_text())
    doc = live_status(store, previous, g.RUN_AT + timedelta(hours=40))
    assert doc["run_ok"] is False and "the daily run has not completed for 40 hours" in doc["headline"]
    assert registry.values(doc, registry.load()) == {}


def test_a_failed_daily_run_stays_failed_in_the_hourly_status(tmp_path, monkeypatch):
    healthy(monkeypatch)
    store = empty_store(tmp_path)
    try:
        outputs.run_daily(store, g.SYSTEMS, g.RUN_AT, tmp_path / "out", state_dir=tmp_path)
        previous = json.loads((tmp_path / "out" / "latest" / "status.json").read_text())
        doc = live_status(store, previous, g.RUN_AT + timedelta(hours=1))
    finally:
        store.close()
    assert doc["run_ok"] is False and doc["headline"].startswith("No data today")


def test_without_a_daily_run_yet_the_live_numbers_stand_alone(day, monkeypatch):
    store, _, _ = day
    healthy(monkeypatch)
    doc = live_status(store, None, g.RUN_AT)
    assert "stories" not in doc and doc["run_ok"] is True


def test_a_host_that_went_silent_since_the_daily_run_turns_its_system_red(day, monkeypatch):
    store, _, out = day
    healthy(monkeypatch)
    previous = json.loads((out / "latest" / "status.json").read_text())
    previous["systems"][0]["state"], previous["systems"][0]["summary"] = "green", "no findings"
    live = {
        "run_ok": True,
        "headline": "x",
        "silent_hosts": [{"host": g.GW, "system": "gw", "last_seen": "t"}],
    }
    merged = status.merge_daily(live, previous, g.RUN_AT)
    assert merged["systems"][0]["state"] == "red" and merged["systems"][0]["summary"] == "silent host"
