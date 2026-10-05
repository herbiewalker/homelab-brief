from datetime import UTC, datetime, timedelta

from brief import detectors as d

NOW = datetime(2026, 10, 4, 12, 0, 0, tzinfo=UTC)
RULES = {"gap_multiple": 3, "min_minutes": 30, "max_hours": 24}


def ago(**kw) -> str:
    return (NOW - timedelta(**kw)).strftime("%Y-%m-%dT%H:%M:%SZ")


def sig(
    sig_id=1, first=None, last=None, prev=None, returned=None, worst="warning", ignored=False, hosts=("h1",)
):
    return d.SignatureRow(
        sig_id, "app", "something happened", first or ago(days=30), last or ago(hours=1), prev,
        ignored, worst, hosts, ("sys1",), returned,
    )  # fmt: skip


# ---------------------------------------------------------------- novelty


def test_novelty_flags_a_signature_first_seen_in_the_last_day():
    f = d.novelty([sig(first=ago(hours=3))], NOW, learning=False)
    assert [(x.id, x.kind, x.story) for x in f] == [("novelty-1", "novelty", True)]
    assert f[0].detail["state"] == "new" and f[0].system == "sys1" and f[0].host == "h1"


def test_novelty_ignores_old_and_ignored_signatures():
    assert d.novelty([sig(first=ago(days=2))], NOW, learning=False) == []
    assert d.novelty([sig(first=ago(hours=1), ignored=True)], NOW, learning=False) == []


def test_info_level_novelty_is_counted_but_not_a_story():
    f = d.novelty([sig(first=ago(hours=1), worst="info")], NOW, learning=False)
    assert len(f) == 1 and f[0].story is False
    assert d.novelty([sig(first=ago(hours=1), worst="crit")], NOW, learning=False)[0].story is True


def test_a_signature_back_after_a_quiet_spell_is_flagged_once():
    back = sig(prev=ago(days=12), returned=ago(hours=2))
    f = d.novelty([back], NOW, learning=False)
    assert len(f) == 1 and f[0].detail["state"] == "returned"
    # it returned three days ago and has been active since: not reported again today
    assert d.novelty([sig(prev=ago(days=12), returned=ago(days=3))], NOW, learning=False) == []
    # a short gap is not a quiet spell
    assert d.novelty([sig(prev=ago(days=2), returned=ago(hours=2))], NOW, learning=False) == []
    # no record of when it came back: nothing to say
    assert d.novelty([sig(prev=ago(days=12), returned=None)], NOW, learning=False) == []


def test_novelty_is_silent_during_the_learning_period():
    assert d.novelty([sig(first=ago(hours=1))], NOW, learning=True) == []


def test_the_quiet_days_threshold_is_configurable():
    row = sig(prev=ago(days=4), returned=ago(hours=1))
    assert d.novelty([row], NOW, learning=False, quiet_days=7) == []
    assert len(d.novelty([row], NOW, learning=False, quiet_days=3)) == 1


# ---------------------------------------------------------------- deviation


def series(today, history, host="h1", sig_id=7):
    return d.SeriesRow(host, sig_id, "sys1", today, tuple(history), "app: noise")


def test_deviation_flags_a_count_far_above_the_median_plus_mad():
    steady = [100, 102, 98, 101, 99, 100, 103, 97, 100, 100, 99, 101, 100, 100]
    f = d.deviation([series(400, steady)], learning=False)
    assert len(f) == 1 and f[0].kind == "deviation" and f[0].value == 400
    assert f[0].detail["median"] == 100 and f[0].id == "deviation-h1-7"


def test_deviation_ignores_normal_variation_and_small_counts():
    steady = [100, 102, 98, 101, 99, 100, 103, 97, 100, 100, 99, 101, 100, 100]
    assert d.deviation([series(104, steady)], learning=False) == []
    assert d.deviation([series(19, [0] * 14)], learning=False) == []  # under min_count
    assert d.deviation([series(20, [20] * 14)], learning=False) == []  # exactly the usual amount


def test_the_mad_is_floored_at_one_so_a_flat_history_does_not_flag_tiny_changes():
    assert (
        d.deviation([series(24, [20] * 14)], learning=False, min_ratio=1) == []
    )  # 20 + 4 x 1 = 24, not above
    assert len(d.deviation([series(25, [20] * 14)], learning=False, min_ratio=1)) == 1


def test_a_rare_signature_that_suddenly_fires_a_lot_is_flagged():
    assert len(d.deviation([series(50, [0, 0, 1, 0, 0, 0, 2, 0, 0, 0, 0, 1, 0, 0])], learning=False)) == 1


def test_deviation_needs_a_few_days_of_baseline():
    assert d.deviation([series(500, [10, 10])], learning=False) == []
    assert len(d.deviation([series(500, [10, 10, 10])], learning=False)) == 1


def test_deviation_is_silent_during_the_learning_period():
    assert d.deviation([series(500, [10] * 14)], learning=True) == []


def test_mad():
    assert d.mad([1, 2, 3, 4, 100]) == 1
    assert d.mad([5, 5, 5]) == 0


# ---------------------------------------------------------------- off hours


def hours(hour, count, history, host="h1", sig_id=3, days=10, events=100):
    return d.HourRow(host, sig_id, "sys1", hour, count, frozenset(history), "app: login", days, events)


def test_off_hours_flags_an_hour_the_signature_never_appeared_in():
    f = d.off_hours([hours(3, 5, {9, 10, 11})], learning=False)
    assert len(f) == 1 and f[0].id == "off_hours-h1-3-03" and f[0].detail["hour_utc"] == 3


def test_off_hours_ignores_usual_hours_small_counts_and_new_signatures():
    assert d.off_hours([hours(10, 5, {9, 10, 11})], learning=False) == []  # a usual hour
    assert d.off_hours([hours(3, 2, {9, 10})], learning=False) == []  # under min_count
    assert d.off_hours([hours(3, 50, set())], learning=False) == []  # no history: that is novelty


def test_off_hours_is_silent_during_the_learning_period():
    assert d.off_hours([hours(3, 5, {9})], learning=True) == []


# ---------------------------------------------------------------- absence


def host(name="h1", system="sys1", known=True, last=None, gap=None, skew=None, skew_at=None):
    return d.HostRow(name, system, known, last or ago(minutes=5), gap, skew, skew_at)


def test_absence_flags_a_host_quiet_past_its_limit():
    f = d.absence([host(last=ago(minutes=45), gap=5)], NOW, RULES, {"sys1"})
    assert len(f) == 1 and f[0].id == "absence-h1" and f[0].value == 45 and "45 min" in f[0].title


def test_absence_respects_the_floor_the_ceiling_and_the_unlearned_default():
    assert (
        d.absence([host(last=ago(minutes=20), gap=5)], NOW, RULES, {"sys1"}) == []
    )  # under the 30 min floor
    assert d.absence([host(last=ago(hours=5))], NOW, RULES, {"sys1"}) == []  # no gap learned: a full day
    assert len(d.absence([host(last=ago(hours=25))], NOW, RULES, {"sys1"})) == 1
    assert d.absence([host(last=ago(hours=23), gap=3000)], NOW, RULES, {"sys1"}) == []  # capped at 24 h


def test_absence_skips_unknown_hosts_and_systems_that_are_not_expected():
    assert d.absence([host(known=False, last=ago(days=3))], NOW, RULES, {"sys1"}) == []
    assert d.absence([host(system="printer", last=ago(days=3))], NOW, RULES, {"sys1"}) == []


def test_absence_runs_during_the_learning_period_and_lists_the_oldest_first():
    # it takes no learning flag at all: it does not depend on a learned baseline
    f = d.absence([host("a", last=ago(hours=30)), host("b", last=ago(hours=50))], NOW, RULES, {"sys1"})
    assert [x.host for x in f] == ["b", "a"]


def test_durations_read_naturally():
    assert d._duration(45) == "45 min" and d._duration(300) == "5 h" and d._duration(4000) == "2 days"


# ---------------------------------------------------------------- clock skew


def test_clock_skew_flags_a_drifting_source():
    f = d.clock_skew([host(skew=600, skew_at=ago(hours=1))], NOW, max_minutes=3)
    assert len(f) == 1 and f[0].id == "clock_skew-h1" and "ahead" in f[0].title and f[0].value == 600
    behind = d.clock_skew([host(skew=-4000, skew_at=ago(hours=1))], NOW, max_minutes=3)
    assert "behind" in behind[0].title


def test_clock_skew_ignores_small_stale_and_missing_measurements():
    assert d.clock_skew([host(skew=170, skew_at=ago(hours=1))], NOW, max_minutes=3) == []  # under 3 min
    assert d.clock_skew([host(skew=900, skew_at=ago(days=3))], NOW, max_minutes=3) == []  # stale
    assert d.clock_skew([host()], NOW, max_minutes=3) == []  # never measured


def test_clock_skew_runs_during_the_learning_period_too():
    assert len(d.clock_skew([host(skew=900, skew_at=ago(minutes=5))], NOW)) == 1  # takes no learning flag


# ---------------------------------------------------------------- expected gap


def test_expected_gap_is_none_until_enough_hours_are_seen():
    assert d.expected_gap(list(range(23))) is None
    assert d.expected_gap(list(range(24))) == 60  # active every hour: 60 minutes


def test_expected_gap_uses_the_95th_percentile_of_the_quiet_stretches():
    active = [h for h in range(120) if h % 6 != 0 or h % 12 == 0]  # mostly hourly, some 2-hour gaps
    gap = d.expected_gap(active)
    assert gap in (60, 120)
    sparse = list(range(0, 24 * 40, 4))  # every fourth hour
    assert d.expected_gap(sparse) == 240


# ---------------------------------------------------------------- the refinements found on real data


def test_a_signature_is_not_news_when_its_host_was_not_logging_while_it_was_away():
    back = dict(prev=ago(days=12), returned=ago(hours=2))
    assert len(d.novelty([sig(**back)], NOW, learning=False)) == 1  # unknown: reported
    quiet = d.SignatureRow(1, "app", "t", ago(days=30), ago(hours=1), ago(days=12), False, "err", ("h1",), ("sys1",),
                           ago(hours=2), 0)  # fmt: skip
    assert d.novelty([quiet], NOW, learning=False) == []  # the host was silent: the host came back
    busy = d.SignatureRow(1, "app", "t", ago(days=30), ago(hours=1), ago(days=12), False, "err", ("h1",), ("sys1",),
                          ago(hours=2), 40)  # fmt: skip
    assert len(d.novelty([busy], NOW, learning=False)) == 1  # the host logged other things meanwhile
    assert len(d.novelty([quiet], NOW, learning=False, min_host_hours=0)) == 1


def test_deviation_wants_a_real_multiple_of_the_median_not_just_a_wide_mad():
    wide = [100, 140, 60, 180, 90, 120, 70, 160, 80, 130, 110, 100, 150, 95]  # chatty, swings a lot
    assert d.deviation([series(250, wide)], learning=False, min_ratio=1) != []  # past median + 4 x MAD
    assert d.deviation([series(250, wide)], learning=False) == []  # but only about 2.2 x the median
    assert len(d.deviation([series(420, wide)], learning=False)) == 1  # a real multiple


def test_the_ratio_does_not_apply_when_the_usual_count_is_zero():
    assert len(d.deviation([series(25, [0] * 10)], learning=False)) == 1


def test_off_hours_needs_a_real_pattern():
    assert len(d.off_hours([hours(3, 5, {9, 10})], learning=False)) == 1
    assert d.off_hours([hours(3, 5, {9, 10}, days=2)], learning=False) == []  # too few days of history
    assert d.off_hours([hours(3, 5, {9, 10}, events=9)], learning=False) == []  # too few events
    assert (
        d.off_hours([hours(3, 5, set(range(4, 16)))], learning=False) == []
    )  # seen at any hour: no unusual one
    assert (
        len(d.off_hours([hours(3, 5, set(range(4, 12)))], learning=False)) == 1
    )  # eight hours is still a pattern


def test_a_signature_that_was_never_regular_is_not_news_when_it_shows_up_again():
    def row(prior):
        return d.SignatureRow(1, "app", "t", ago(days=60), ago(hours=1), ago(days=12), False, "err", ("h1",), ("sys1",),
                              ago(hours=2), 40, prior)  # fmt: skip

    assert len(d.novelty([row(10)], NOW, learning=False)) == 1  # seen on ten days before: it was regular
    assert (
        d.novelty([row(2)], NOW, learning=False) == []
    )  # seen on two days in two months: always "returning"
    assert len(d.novelty([row(2)], NOW, learning=False, min_prior_days=2)) == 1
    assert len(d.novelty([row(None)], NOW, learning=False)) == 1  # unknown history: reported
