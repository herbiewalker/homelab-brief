import json
from datetime import UTC, date, datetime, time, timedelta

import pytest

from brief import cli, detect, replay
from brief.store import Store
from brief.systems import System, Systems

H1, H2 = "198.51.100.1", "198.51.100.2"
SYSTEMS = Systems(
    [
        System("sys1", "one", "other", (H1,), "UTC", True),
        System("sys2", "two", "other", (H2,), "UTC", True),
    ]
)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "baseline.db")
    yield s
    s.close()


def at(day, hour=0, minute=0):
    return datetime(*map(int, day.split("-")), hour, minute, tzinfo=UTC)


def stamp(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def hours_between(start, end):
    """Every hour bucket (day, hour) from start to end inclusive."""
    t = start.replace(minute=0, second=0)
    while t <= end:
        yield t.strftime("%Y-%m-%d"), t.hour
        t += timedelta(hours=1)


def build(store, h1_until=None, h2_until=None, spike=True):
    h1_until = h1_until or at("2026-10-03", 21)
    h2_until = h2_until or at("2026-10-04", 12)
    """Two hosts logging hourly since Sep 1 (h1 stops at h1_until); a steady firewall signature on h2
    (100 a day at 10:00 UTC) that spikes on Oct 3 at 20:00."""
    with store.write() as tx:
        first = stamp(at("2026-09-01"))
        tx.touch_host(H1, first, stamp(h1_until), system_id="sys1")
        tx.touch_host(H2, first, stamp(h2_until), system_id="sys2")
        beat = tx.signature_for(1, 1, "heartbeat", "alive", first)
        fw = tx.signature_for(1, 2, "firewall", "drop packets", first)
        tx.touch_signature(beat, first, stamp(h2_until))
        tx.touch_signature(fw, first, stamp(at("2026-10-03", 10)))
        tx.add_hourly([(d, h, H1, beat, "info", 1) for d, h in hours_between(at("2026-09-01"), h1_until)])
        tx.add_hourly([(d, h, H2, beat, "info", 1) for d, h in hours_between(at("2026-09-01"), h2_until)])
        tx.add_hourly([(f"2026-09-{day:02d}", 10, H2, fw, "warning", 100) for day in range(1, 31)])
        tx.add_hourly([(f"2026-10-{day:02d}", 10, H2, fw, "warning", 100) for day in range(1, 4)])
        if spike:
            tx.add_hourly([("2026-10-03", 20, H2, fw, "warning", 700)])
        tx.note_live_data(first)
    return beat, fw


def kinds_new(rep):
    return [(run.at.strftime("%m-%d %H:%M"), f.kind) for run in rep.runs for f in run.new]


# ---------------------------------------------------------------- the schedule


def test_the_schedule_has_a_daily_run_and_hourly_absence_runs():
    plan = replay.schedule(date(2026, 10, 1), date(2026, 10, 2))
    assert len(plan) == 48 and plan == sorted(plan)
    daily = [(m, k) for m, k in plan if "novelty" in k]
    assert [m.strftime("%m-%d %H:%M") for m, _ in daily] == ["10-01 06:00", "10-02 06:00"]
    assert all("clock_skew" not in k for _, k in plan)  # it is only measured live
    assert all(k == ("absence",) for m, k in plan if m.hour != 6)


def test_without_hourly_absence_there_is_one_run_a_day():
    plan = replay.schedule(date(2026, 10, 1), date(2026, 10, 3), time(5, 45), hourly=False)
    assert [m.strftime("%H:%M") for m, _ in plan] == ["05:45"] * 3
    assert "absence" in plan[0][1] and "deviation" in plan[0][1]


# ---------------------------------------------------------------- new and resolved


def test_a_finding_is_new_once_and_resolved_when_it_clears(store):
    build(store, h2_until=at("2026-10-04", 23))
    with store.write() as tx:  # h1 comes back at 23:00 on Oct 4
        tx.add_hourly([("2026-10-04", 23, H1, 1, "info", 1)])
    rep = replay.replay(store, SYSTEMS, date(2026, 10, 3), date(2026, 10, 4))
    absences = [(run.at.strftime("%m-%d %H:%M"), [f.id for f in run.new], run.resolved) for run in rep.runs
                if any(f.kind == "absence" for f in run.new) or any(r.startswith("absence") for r in run.resolved)]  # fmt: skip
    # silent from the end of Oct 3 21:00; the limit is a day when no gap is learned, so it fires at 22:00 on Oct 4
    assert absences == [
        ("10-04 22:00", [f"absence-{H1}"], []),
        ("10-04 23:00", [], [f"absence-{H1}"]),
    ]


def test_daily_detectors_report_at_the_daily_run_not_every_hour(store):
    build(store)
    rep = replay.replay(store, SYSTEMS, date(2026, 10, 4), date(2026, 10, 4))
    found = [(m, k) for m, k in kinds_new(rep) if k == "deviation"]
    assert found == [("10-04 06:00", "deviation")]  # the Oct 3 20:00 spike is in the window at 06:00
    run = next(r for r in rep.runs if r.at.hour == 6)
    spike = next(f for f in run.new if f.kind == "deviation")
    assert spike.detail["today"] == 800 and spike.system == "sys2"  # 100 usual + 700 spike


def test_the_spike_leaves_the_window_a_day_later(store):
    build(store)
    rep = replay.replay(store, SYSTEMS, date(2026, 10, 4), date(2026, 10, 5))
    resolved = [(r.at.strftime("%m-%d %H:%M"), r.resolved) for r in rep.runs if r.resolved]
    assert resolved and resolved[0][0] == "10-05 06:00" and resolved[0][1][0].startswith("deviation-")


def shipped_env(tmp_path):
    """An environment whose defaults hold the shipped priorities.yaml (where absence routes to a push)."""
    from pathlib import Path

    folder = tmp_path / "defaults" / "brief"
    folder.mkdir(parents=True)
    shipped = Path(__file__).resolve().parents[1]  # brief/ in the repo, /app in the image
    for name in ("priorities.yaml", "playbook.yaml"):
        (folder / name).write_bytes((shipped / name).read_bytes())
    return {"BRIEF_DEFAULTS_DIR": str(tmp_path / "defaults"), "BRIEF_CONFIG_DIR": str(tmp_path / "none")}


def test_only_new_story_findings_routed_push_now_would_push(store, tmp_path):
    build(store)
    rep = replay.replay(store, SYSTEMS, date(2026, 10, 3), date(2026, 10, 5), env=shipped_env(tmp_path))
    pushed = [
        (run.at.strftime("%m-%d %H:%M"), n.state, n.finding_id) for run in rep.runs for n in rep.pushes(run)
    ]
    # h1 went silent at 22:00 on Oct 4, inside quiet hours, so its push waits until 07:00 on Oct 5;
    # h2 stops a day after h1 and goes out at once in the afternoon. A deviation routes to the digest.
    assert pushed == [
        ("10-05 07:00", "new", f"absence-{H1}"),
        ("10-05 13:00", "new", f"absence-{H2}"),
    ]
    s = replay.summary(rep)
    assert s["would_push"] == 2 and s["new_by_kind"]["absence"] == 2 and s["new_by_kind"]["deviation"] == 1
    assert s["held_runs"] >= 1  # the nine hours of quiet hours after 22:00


def test_the_learning_period_silences_the_daily_detectors_in_a_replay(store):
    build(store)
    with store.write() as tx:
        tx.db.execute("DELETE FROM meta WHERE key = 'live_since'")
        tx.note_live_data(stamp(at("2026-10-02")))  # live data only two days before the replayed day
    quiet = replay.replay(store, SYSTEMS, date(2026, 10, 4), date(2026, 10, 4))
    assert "deviation" not in [k for _, k in kinds_new(quiet)]
    assert all(run.learning for run in quiet.runs if "deviation" in run.kinds)
    forced = replay.replay(store, SYSTEMS, date(2026, 10, 4), date(2026, 10, 4), ignore_learning=True)
    assert "deviation" in [k for _, k in kinds_new(forced)]


def test_a_replay_never_writes(store, monkeypatch):
    build(store)

    def refuse(*a, **k):
        raise AssertionError("a replay must not write")

    monkeypatch.setattr(Store, "write", refuse)
    replay.replay(store, SYSTEMS, date(2026, 10, 3), date(2026, 10, 4))


def test_a_range_that_ends_before_it_starts_is_refused(store):
    with pytest.raises(ValueError, match="before"):
        replay.replay(store, SYSTEMS, date(2026, 10, 4), date(2026, 10, 3))


# ---------------------------------------------------------------- as of the replayed moment


def comparable(result):
    # absence is only as precise as the hour in a replay (host activity is kept in hour buckets), so
    # compare it by id; everything else must match exactly, wording included
    return sorted(
        (f.id, "" if f.kind == "absence" else f.title, f.story)
        for f in result["findings"]
        if f.kind != "clock_skew"
    )


def test_a_replay_gives_what_a_live_run_said_even_after_later_data_arrives(store):
    t0 = at("2026-10-04", 6)
    build(store, h1_until=at("2026-10-02", 21), h2_until=t0)  # h1 has been silent for 33 hours at t0
    with store.write() as tx:  # something brand new happened in the last 24 hours
        new = tx.signature_for(1, 3, "kernel", "disk error", stamp(at("2026-10-04", 2)))
        tx.add_hourly([("2026-10-04", 2, H2, new, "err", 5)])
    live = detect.run(store, SYSTEMS, t0, ignore_learning=True)
    assert {f.kind for f in live["findings"]} >= {"absence", "deviation", "novelty"}  # the check has teeth

    # days later: the host came back, the signature kept going, and the columns that only hold the
    # latest value (host.last_seen, signature.last_seen) have all moved on
    with store.write() as tx:
        tx.add_hourly([("2026-10-06", h, H1, 1, "info", 1) for h in range(24)])
        tx.add_hourly([("2026-10-06", 9, H2, new, "err", 50)])
        tx.touch_host(H1, stamp(at("2026-09-01")), stamp(at("2026-10-06", 23)), system_id="sys1")
        tx.touch_signature(new, stamp(at("2026-10-06", 9)), stamp(at("2026-10-06", 9)))
    again = detect.run(store, SYSTEMS, t0, ignore_learning=True, activity=detect.HostActivity.load(store))
    assert comparable(again) == comparable(live)
    # without the as-of host activity the absence would read today's host.last_seen and differ
    stale = detect.run(store, SYSTEMS, t0, ignore_learning=True)
    assert comparable(stale) != comparable(live)


def test_host_activity_answers_for_any_past_moment(store):
    build(store)
    act = detect.HostActivity.load(store)
    rows = {r.host: r for r in act.host_rows(at("2026-10-03", 12, 30))}
    assert rows[H1].last_seen == "2026-10-03T12:30:00Z"  # logging through the current hour, never after "now"
    assert rows[H2].known and rows[H2].system == "sys2"
    assert act.host_rows(at("2026-08-01")) == []  # nothing had been heard from yet
    after = {r.host: r for r in act.host_rows(at("2026-10-04", 12))}
    assert after[H1].last_seen == "2026-10-03T21:59:59Z"  # silent since the end of 21:00


# ---------------------------------------------------------------- the command


def run_cli(tmp_path, monkeypatch, **overrides):
    monkeypatch.setenv("BRIEF_STATE", str(tmp_path))
    monkeypatch.setenv("BRIEF_CONFIG_DIR", str(tmp_path / "none"))
    monkeypatch.setenv("BRIEF_DEFAULTS_DIR", str(tmp_path / "none"))
    lines = []
    args = cli.argparse.Namespace(
        start="2026-10-03", end="2026-10-04", at="06:00", no_hourly=False, ignore_learning=False,
        json=False, verbose=False, dry_run=True)  # fmt: skip
    for k, v in overrides.items():
        setattr(args, k, v)
    return cli.run_replay(args, tmp_path, out=lines.append), lines


def test_the_command_prints_a_readable_replay(tmp_path, monkeypatch):
    s = Store(tmp_path / "baseline.db")
    build(s)
    s.close()
    code, lines = run_cli(tmp_path, monkeypatch)
    text = "\n".join(lines)
    assert code == 0 and "Read-only: nothing was sent or stored" in text
    assert "-- 2026-10-04 --" in text and "NEW" in text and "deviation" in text
    assert "Acknowledgements and snoozes cannot be replayed" in text


def test_the_command_can_print_json(tmp_path, monkeypatch):
    s = Store(tmp_path / "baseline.db")
    build(s)
    s.close()
    code, lines = run_cli(tmp_path, monkeypatch, json=True)
    doc = json.loads(lines[0])
    assert code == 0 and doc["from"] == "2026-10-03" and doc["summary"]["runs"] == 48
    assert any(n["kind"] == "deviation" and "rank" in n for r in doc["runs"] for n in r["new"])


def test_the_command_rejects_bad_dates_and_a_backwards_range(tmp_path, monkeypatch, capsys):
    s = Store(tmp_path / "baseline.db")
    s.close()
    assert run_cli(tmp_path, monkeypatch, start="yesterday")[0] == 2
    assert run_cli(tmp_path, monkeypatch, at="six")[0] == 2
    assert run_cli(tmp_path, monkeypatch, start="2026-10-05", end="2026-10-01")[0] == 2
    assert "before" in capsys.readouterr().err


def test_the_argument_parser_accepts_the_documented_flags(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIEF_STATE", str(tmp_path))
    Store(tmp_path / "baseline.db").close()
    code = cli.main(["replay", "--from", "2026-10-01", "--to", "2026-10-01", "--dry-run", "--no-hourly"])
    assert code == 0


# ---------------------------------------------------------------- the alert lifecycle inside a replay


def test_replay_holds_a_push_through_quiet_hours_and_then_repeats_it_daily(store, tmp_path):
    build(store, h2_until=at("2026-10-07", 23))
    rep = replay.replay(store, SYSTEMS, date(2026, 10, 4), date(2026, 10, 6), env=shipped_env(tmp_path))
    sent = [
        (run.at.strftime("%m-%d %H:%M"), n.state, n.finding_id) for run in rep.runs for n in run.notifications
    ]
    # h1 is overdue from 22:00 on Oct 4 (quiet hours), so the push waits for 07:00, then once a day
    assert sent == [
        ("10-05 07:00", "new", f"absence-{H1}"),
        ("10-06 07:00", "repeat", f"absence-{H1}"),
    ]
    held = [run.at.strftime("%m-%d %H:%M") for run in rep.runs if run.held]
    assert held[0] == "10-04 22:00" and held[-1] == "10-05 06:00" and len(held) == 9


def test_replay_can_leave_the_lifecycle_out(store, tmp_path):
    build(store)
    rep = replay.replay(
        store, SYSTEMS, date(2026, 10, 3), date(2026, 10, 5), env=shipped_env(tmp_path), with_lifecycle=False
    )
    assert (
        all(not run.notifications and not run.held for run in rep.runs)
        and replay.summary(rep)["would_push"] == 0
    )


def test_replay_says_resolved_when_a_host_comes_back(store, tmp_path):
    build(store, h2_until=at("2026-10-09", 23))
    with store.write() as tx:
        tx.add_hourly(
            [("2026-10-05", h, H1, 1, "info", 1) for h in range(10, 24)]
        )  # h1 returns on Oct 5 at 10:00
        tx.add_hourly([("2026-10-06", h, H1, 1, "info", 1) for h in range(24)])
        tx.add_hourly([("2026-10-07", h, H1, 1, "info", 1) for h in range(24)])
    rep = replay.replay(store, SYSTEMS, date(2026, 10, 4), date(2026, 10, 6), env=shipped_env(tmp_path))
    sent = [(n.state, n.channel) for run in rep.runs for n in run.notifications]
    assert sent[0] == ("new", "alert") and ("resolved", "alert") in sent
    text = "\n".join(replay.format_text(rep))
    assert "PUSH     new" in text and "PUSH     resolved" in text and "sent alert:resolved: 1" in text


def test_replay_output_lists_what_was_held(store, tmp_path):
    build(store, h2_until=at("2026-10-07", 23))
    rep = replay.replay(store, SYSTEMS, date(2026, 10, 4), date(2026, 10, 5), env=shipped_env(tmp_path))
    text = "\n".join(replay.format_text(rep))
    assert "held for the end of quiet hours: 1" in text and "held by quiet hours in 9 runs" in text
    doc = replay.as_dict(rep)
    assert any(r["held"] for r in doc["runs"]) and doc["summary"]["held_runs"] == 9
