"""The alert lifecycle: an unchanged finding pushes once per interval, a worse one escalates, a cleared
one resolves, the budget holds, quiet hours wait, and an ack stops repeats."""

from datetime import UTC, datetime, time, timedelta

from brief import lifecycle as lc
from brief.routing import Priorities, Route

PUSH = Route(80, "push_now")
URGENT = Route(100, "push_now", True)
HEADLINE = Route(60, "daily_headline")
DIGEST = Route(50, "digest_only")
POLICY = lc.Policy(
    renotify_hours=24, escalate_factor=1.5, escalate_min_minutes=60, resolve_after_quiet_runs=3,
    daily_push_budget=5, quiet_start=time(22, 0), quiet_end=time(6, 30),
)  # fmt: skip
T0 = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


def at(**kw):
    return T0 + timedelta(**kw)


def item(fid="absence-h1", route=PUSH, value=100.0, kind="absence", title=None):
    return lc.Item(
        fid, kind, title or f"{fid} is wrong", route, value, "sys", "h1", (f"{fid} body",), "hosts/h1.md"
    )


def run(book, items, when, evaluated=("absence",), policy=POLICY, pushes=0, headline=False):
    return lc.step(book, items, set(evaluated), when, policy, pushes, headline)


def states(result):
    return [(n.finding_id, n.state, n.channel) for n in result.notifications]


# ---------------------------------------------------------------- dedupe and re-notify


def test_a_new_finding_pushes_once_and_is_not_repeated_while_it_is_fresh():
    first = run({}, [item()], T0)
    assert states(first) == [("absence-h1", "new", "alert")]
    again = run(first.book, [item()], at(hours=1))
    assert again.notifications == []  # dedupe: still open, not due


def test_an_unchanged_finding_repeats_only_once_per_interval():
    book = run({}, [item()], T0).book
    assert run(book, [item()], at(hours=23, minutes=59)).notifications == []
    due = run(book, [item()], at(hours=24))
    assert states(due) == [("absence-h1", "repeat", "alert")]
    assert run(due.book, [item()], at(hours=40)).notifications == []  # the clock restarted at the repeat
    assert states(run(due.book, [item()], at(hours=48))) == [("absence-h1", "repeat", "alert")]


def test_the_renotify_interval_comes_from_the_policy():
    short = lc.Policy(renotify_hours=2)
    book = run({}, [item()], T0, policy=short).book
    assert run(book, [item()], at(hours=1), policy=short).notifications == []
    assert states(run(book, [item()], at(hours=2), policy=short)) == [("absence-h1", "repeat", "alert")]


# ---------------------------------------------------------------- escalation


def test_a_worse_finding_escalates_without_waiting_for_the_interval():
    book = run({}, [item(value=100)], T0).book
    worse = run(book, [item(value=160)], at(hours=2))
    assert states(worse) == [("absence-h1", "escalated", "alert")]
    assert worse.book["absence-h1"].last_value == 160


def test_small_growth_is_not_worse_and_escalation_measures_from_the_last_push():
    book = run({}, [item(value=100)], T0).book
    assert run(book, [item(value=120)], at(hours=2)).notifications == []  # 1.2x is under 1.5x
    esc = run(book, [item(value=160)], at(hours=2)).book
    assert run(esc, [item(value=170)], at(hours=4)).notifications == []  # measured from 160 now
    assert states(run(esc, [item(value=250)], at(hours=4))) == [("absence-h1", "escalated", "alert")]


def test_escalations_are_rate_limited():
    book = run({}, [item(value=100)], T0).book
    assert run(book, [item(value=500)], at(minutes=30)).notifications == []  # too soon after the last push
    assert states(run(book, [item(value=500)], at(minutes=61))) == [("absence-h1", "escalated", "alert")]


def test_a_higher_level_escalates_even_if_the_size_did_not_change():
    book = run({}, [item(route=PUSH)], T0).book
    assert states(run(book, [item(route=URGENT)], at(hours=2))) == [("absence-h1", "escalated", "alert")]


def test_escalation_can_be_turned_off():
    off = lc.Policy(escalate_on_worse=False)
    book = run({}, [item(value=100)], T0, policy=off).book
    assert run(book, [item(value=900)], at(hours=3), policy=off).notifications == []


def test_a_finding_that_started_at_zero_escalates_when_it_becomes_nonzero():
    book = run({}, [item(value=0)], T0).book
    assert states(run(book, [item(value=5)], at(hours=2))) == [("absence-h1", "escalated", "alert")]


# ---------------------------------------------------------------- resolve


def test_a_cleared_finding_resolves_after_enough_quiet_runs_with_one_message():
    book = run({}, [item()], T0).book
    for n in (1, 2):
        step = run(book, [], at(hours=n))
        assert step.notifications == [] and step.book["absence-h1"].state == "open"
        book = step.book
    done = run(book, [], at(hours=3))
    assert states(done) == [("absence-h1", "resolved", "alert")]
    assert done.book["absence-h1"].state == "resolved"
    assert done.notifications[0].title == "Resolved: absence-h1 is wrong"
    assert run(done.book, [], at(hours=4)).notifications == []  # said once


def test_coming_back_resets_the_count_of_quiet_runs():
    book = run({}, [item()], T0).book
    book = run(book, [], at(hours=1)).book
    book = run(book, [], at(hours=2)).book
    back = run(book, [item()], at(hours=3))
    assert back.book["absence-h1"].quiet_runs == 0
    assert run(back.book, [], at(hours=4)).notifications == []  # starts counting again from zero


def test_a_finding_that_was_never_pushed_resolves_silently():
    held = run({}, [item()], at(hours=10, minutes=30)).book  # 22:30 UTC: quiet, so held and unsent
    assert held["absence-h1"].pushed == 0
    book = held
    for n in range(1, 4):
        step = run(book, [], at(hours=10, minutes=30 + n))
        book = step.book
        assert step.notifications == []
    assert book["absence-h1"].state == "resolved"


def test_resolved_messages_can_be_turned_off():
    quiet = lc.Policy(send_resolved=False, resolve_after_quiet_runs=1)
    book = run({}, [item()], T0, policy=quiet).book
    gone = run(book, [], at(hours=1), policy=quiet)
    assert gone.notifications == [] and gone.book["absence-h1"].state == "resolved"


def test_a_run_that_did_not_look_at_a_kind_neither_confirms_nor_clears_it():
    book = run({}, [item(kind="deviation", fid="deviation-1")], T0, evaluated=("deviation",)).book
    for n in range(1, 6):  # five hourly runs that only evaluate absence
        book = run(book, [], at(hours=n), evaluated=("absence",)).book
    assert book["deviation-1"].state == "open" and book["deviation-1"].quiet_runs == 0
    assert run(book, [], at(hours=24), evaluated=("deviation",)).book["deviation-1"].quiet_runs == 1


def test_a_finding_that_returns_after_resolving_is_new_again():
    book = run({}, [item()], T0).book
    for n in (1, 2, 3):
        book = run(book, [], at(hours=n)).book
    again = run(book, [item()], at(hours=5))
    assert states(again) == [("absence-h1", "new", "alert")] and again.book["absence-h1"].pushed == 1


# ---------------------------------------------------------------- the daily budget


def test_the_budget_holds_and_the_overflow_waits_for_the_daily_headline():
    items = [item(f"absence-h{i}", value=10, route=Route(80 - i, "push_now")) for i in range(5)]
    tight = lc.Policy(daily_push_budget=3, quiet_start=None, quiet_end=None)
    r = run({}, items, T0, policy=tight)
    assert [n.finding_id for n in r.notifications] == [
        "absence-h0",
        "absence-h1",
        "absence-h2",
    ]  # most important first
    assert r.folded == ["absence-h3", "absence-h4"] and r.pushes_today == 3
    assert all(r.book[i].pending_headline for i in ("absence-h3", "absence-h4"))
    assert (
        run(r.book, items, at(hours=1), policy=tight, pushes=3).notifications == []
    )  # still over budget: nothing
    daily = run(r.book, items, at(hours=18), policy=tight, pushes=3, headline=True)
    assert states(daily) == [("absence-h3", "new", "headline"), ("absence-h4", "new", "headline")]


def test_urgent_findings_ignore_the_budget():
    tight = lc.Policy(daily_push_budget=0)
    r = run({}, [item("absence-h1", route=URGENT), item("absence-h2")], T0, policy=tight)
    assert states(r) == [("absence-h1", "new", "alert")] and r.folded == ["absence-h2"]
    assert r.pushes_today == 0  # an urgent push does not use the budget


def test_pushes_already_sent_today_count_against_the_budget():
    tight = lc.Policy(daily_push_budget=2)
    r = run({}, [item()], T0, policy=tight, pushes=2)
    assert r.notifications == [] and r.folded == ["absence-h1"]


def test_a_folded_repeat_keeps_what_it_was_in_the_headline():
    tight = lc.Policy(daily_push_budget=1, renotify_hours=1)
    first = run({}, [item()], T0, policy=tight)
    repeat = run(
        first.book, [item()], at(hours=2), policy=tight, pushes=1
    )  # a repeat, but the budget is used up
    assert repeat.notifications == [] and repeat.book["absence-h1"].pending_state == "repeat"
    daily = run(repeat.book, [item()], at(hours=20), policy=tight, pushes=1, headline=True)
    assert states(daily) == [("absence-h1", "repeat", "headline")]


def test_resolved_messages_do_not_use_the_budget():
    book = run({}, [item()], T0).book
    for n in (1, 2):
        book = run(book, [], at(hours=n)).book
    assert run(book, [], at(hours=3), pushes=5).pushes_today == 5


# ---------------------------------------------------------------- quiet hours


def test_a_non_urgent_push_waits_for_the_quiet_hours_to_end_then_goes_out():
    night = at(hours=11)  # 23:00 UTC
    r = run({}, [item()], night)
    assert r.notifications == [] and r.held == ["absence-h1"]
    still = run(r.book, [item()], at(hours=17, minutes=29))  # 05:29
    assert still.notifications == []
    morning = run(still.book, [item()], at(hours=18, minutes=30))  # 06:30: the quiet hours are over
    assert states(morning) == [("absence-h1", "new", "alert")]


def test_urgent_findings_bypass_quiet_hours():
    r = run({}, [item(route=URGENT)], at(hours=11))
    assert states(r) == [("absence-h1", "new", "alert")] and r.held == []


def test_the_resolved_message_also_waits_for_the_morning():
    book = run({}, [item()], T0).book
    for n in (1, 2):
        book = run(book, [], at(hours=n)).book
    night = run(book, [], at(hours=11))  # the third quiet run lands at 23:00
    assert night.notifications == [] and night.book["absence-h1"].state == "open"
    morning = run(night.book, [], at(hours=18, minutes=30))
    assert states(morning) == [("absence-h1", "resolved", "alert")]


def test_quiet_hours_wrap_midnight_and_have_clear_edges():
    start, end = time(22, 0), time(6, 30)
    for hh, mm, want in (
        (21, 59, False),
        (22, 0, True),
        (23, 59, True),
        (0, 0, True),
        (6, 29, True),
        (6, 30, False),
        (12, 0, False),
    ):
        assert lc.in_quiet_hours(datetime(2026, 10, 4, hh, mm, tzinfo=UTC), start, end) is want, (hh, mm)
    assert (
        lc.in_quiet_hours(T0, time(9, 0), time(17, 0)) is True
        and lc.in_quiet_hours(at(hours=6), time(9, 0), time(17, 0)) is False
    )
    assert (
        lc.in_quiet_hours(T0, None, None) is False
        and lc.in_quiet_hours(T0, time(12, 0), time(12, 0)) is False
    )


# ---------------------------------------------------------------- acknowledgements and snoozes


def test_an_ack_stops_repeats():
    book = run({}, [item()], T0).book
    assert lc.apply_feedback(book["absence-h1"], "ack", at(hours=1), POLICY) == "acked"
    for days in (1, 2, 5):
        assert run(book, [item()], at(days=days)).notifications == []  # never repeats while acked


def test_an_acked_finding_comes_back_when_it_gets_worse():
    book = run({}, [item(value=100)], T0).book
    lc.apply_feedback(book["absence-h1"], "ack", at(hours=1), POLICY)
    worse = run(book, [item(value=300)], at(hours=5))
    assert (
        states(worse) == [("absence-h1", "escalated", "alert")] and worse.book["absence-h1"].state == "open"
    )


def test_an_acked_finding_that_clears_is_still_resolved():
    book = run({}, [item()], T0).book
    lc.apply_feedback(book["absence-h1"], "ack", at(hours=1), POLICY)
    for n in (2, 3):
        book = run(book, [], at(hours=n)).book
    assert states(run(book, [], at(hours=4))) == [("absence-h1", "resolved", "alert")]


def test_a_snooze_is_quiet_until_it_ends_then_repeats():
    book = run({}, [item()], T0).book
    assert lc.apply_feedback(book["absence-h1"], "snooze_24h", T0, POLICY) == "snoozed 24h"
    assert book["absence-h1"].snooze_until == "2026-10-05T12:00:00Z"
    assert run(book, [item()], at(hours=23)).notifications == []
    over = run(book, [item()], at(hours=24))
    assert states(over) == [("absence-h1", "repeat", "alert")] and over.book["absence-h1"].state == "open"


def test_a_worse_finding_breaks_through_a_snooze():
    book = run({}, [item(value=100)], T0).book
    lc.apply_feedback(book["absence-h1"], "snooze_7d", T0, POLICY)
    assert book["absence-h1"].snooze_until == "2026-10-11T12:00:00Z"
    assert states(run(book, [item(value=400)], at(hours=3))) == [("absence-h1", "escalated", "alert")]


def test_feedback_that_means_nothing_is_ignored():
    book = run({}, [item()], T0).book
    a = book["absence-h1"]
    assert lc.apply_feedback(None, "ack", T0, POLICY) is None  # no such alert
    assert (
        lc.apply_feedback(a, "snooze_48h", T0, POLICY) is None and a.state == "open"
    )  # not an offered length
    assert lc.apply_feedback(a, "dance", T0, POLICY) is None
    gone = run(book, [], at(hours=1)).book
    for n in (2, 3):
        gone = run(gone, [], at(hours=n)).book
    assert lc.apply_feedback(gone["absence-h1"], "ack", at(hours=4), POLICY) is None  # already resolved


# ---------------------------------------------------------------- routes, headline, levels, config


def test_digest_only_stories_are_never_tracked_or_pushed():
    r = run({}, [item(route=DIGEST), item("x", route=Route(1, "suppress"))], T0)
    assert r.notifications == [] and r.book == {}


def test_a_headline_route_story_goes_out_with_the_daily_headline_only():
    story = item("clock-1", route=HEADLINE, kind="clock_skew")
    hourly = run({}, [story], T0, evaluated=("clock_skew",))
    assert hourly.notifications == [] and hourly.book["clock-1"].pending_headline
    daily = run(hourly.book, [story], at(hours=18), evaluated=("clock_skew",), headline=True)
    assert states(daily) == [("clock-1", "new", "headline")]
    assert (
        run(daily.book, [story], at(hours=19), evaluated=("clock_skew",), headline=True).notifications == []
    )


def test_levels_follow_the_route():
    assert [lc.level_for(r) for r in (URGENT, PUSH, HEADLINE, DIGEST)] == [
        "critical",
        "warning",
        "notice",
        "info",
    ]


def test_step_does_not_change_the_book_it_is_given():
    book = run({}, [item()], T0).book
    snapshot = {k: lc.Alert(**vars(v)) for k, v in book.items()}
    run(book, [], at(hours=1))
    run(book, [item(value=900)], at(hours=3))
    assert book == snapshot


def test_the_payload_has_what_home_assistant_needs():
    n = run({}, [item()], T0).notifications[0]
    p = n.payload()
    assert p["finding_id"] == "absence-h1" and p["state"] == "new" and p["level"] == "warning"
    assert (
        p["title"] == "absence-h1 is wrong"
        and p["body"] == "absence-h1 body"
        and p["detail"] == "hosts/h1.md"
    )
    assert p["actions"] == ["ack", "snooze_24h", "snooze_7d"] and p["at"] == "2026-10-04T12:00:00Z"


def test_the_policy_reads_priorities_yaml_with_sensible_defaults():
    p = lc.Policy.from_priorities(Priorities({
        "lifecycle": {"renotify_hours": 12, "daily_push_budget": 2, "escalate_factor": 2, "snooze_options_h": [1, 24]},
        "quiet_hours": {"start": "23:15", "end": "05:00"},
    }))  # fmt: skip
    assert (p.renotify_hours, p.daily_push_budget, p.escalate_factor, p.snooze_options_h) == (
        12,
        2,
        2,
        (1, 24),
    )
    assert (p.quiet_start, p.quiet_end) == (time(23, 15), time(5, 0)) and p.resolve_after_quiet_runs == 3
    empty = lc.Policy.from_priorities(Priorities({"quiet_hours": {"start": "late", "end": None}}))
    assert empty.quiet_start is None and empty.quiet_end is None and empty.renotify_hours == 24
