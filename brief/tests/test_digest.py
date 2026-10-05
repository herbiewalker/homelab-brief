from datetime import UTC, date, datetime

from brief import digest as dg
from brief import grouping, issues, render_md
from brief.detectors import Finding
from brief.routing import Priorities
from brief.systems import System, Systems

NOW = datetime(2026, 10, 4, 6, 0, tzinfo=UTC)
PRIORITIES_DATA = {
    "default": {"rank": 10, "route": "digest_only"},
    "kinds": {
        "absence": {"rank": 80, "route": "push_now"},
        "deviation": {"rank": 50, "route": "digest_only"},
        "novelty": {"rank": 45, "route": "digest_only"},
        "noise": {"rank": 5, "route": "suppress"},
    },
}
PRIORITIES = Priorities(PRIORITIES_DATA)
SYSTEMS = Systems(
    [
        System("gw", "Gateway", "gateway", ("198.51.100.1",), "UTC", True),
        System("nas", "Storage server", "server", ("198.51.100.2",), "UTC", True),
    ]  # fmt: skip
)
LEARNED = {"active": False, "day": 9, "of": 7}
PIPE = {"events_24h": 5000, "new_signatures_24h": 2, "hosts_silent": 0, "hosts_checked": 2}


def f(kind="deviation", n=1, host="198.51.100.1", system="gw", story=True, value=10, **detail):
    return Finding(
        f"{kind}-{host}-{n}", kind, f"{kind} number {n}", system, host, n, story, float(value), detail
    )


# ---------------------------------------------------------------- grouping


def test_findings_of_one_kind_on_one_host_merge_once_there_are_enough():
    found = [f(n=i, value=100 - i) for i in range(5)]
    stories = grouping.build_stories(found, PRIORITIES, {"gw": "Gateway"})
    assert len(stories) == 1 and stories[0].grouped and len(stories[0].members) == 5
    assert stories[0].title == "5 signatures far above normal on Gateway"
    assert stories[0].id == "group-deviation-198.51.100.1"
    assert stories[0].top == (
        "deviation number 0",
        "deviation number 1",
        "deviation number 2",
    )  # the biggest first


def test_a_few_findings_stay_separate_and_hosts_and_kinds_are_not_mixed():
    found = [f(n=1), f(n=2), f(n=3, host="198.51.100.2", system="nas"), f("novelty", n=4)]
    stories = grouping.build_stories(found, PRIORITIES, min_members=3)
    assert len(stories) == 4 and not any(s.grouped for s in stories)
    assert len(grouping.build_stories(found, PRIORITIES, min_members=2)) == 3  # the two on one host merge


def test_count_only_and_suppressed_findings_are_not_stories():
    quiet = f("novelty", n=1, story=False)
    hidden = f("noise", n=2)
    seen = []
    stories = grouping.build_stories([quiet, hidden, f(n=3)], PRIORITIES, on_suppress=seen.append)
    assert [s.kind for s in stories] == ["deviation"] and seen == [hidden]
    assert grouping.counted_only([quiet, hidden, f(n=3)]) == [quiet]


def test_stories_are_ranked_by_route_rank_then_size():
    found = [
        f("novelty", n=1),
        f("absence", n=2),
        f("deviation", n=3, value=5),
        f("deviation", n=4, host="x", value=50),
    ]
    ranked = grouping.build_stories(found, PRIORITIES)
    assert [s.kind for s in ranked] == ["absence", "deviation", "deviation", "novelty"]
    assert ranked[1].value == 50  # the bigger deviation first


# ---------------------------------------------------------------- developing issues


def run_issue(prev, findings, day):
    return issues.update(prev, findings, date.fromisoformat(day))


def test_an_issue_is_new_then_developing_with_a_trend():
    one = run_issue(None, [f(value=100)], "2026-10-01")
    assert one["issues"]["deviation-198.51.100.1-1"]["days"] == 1 and issues.developing(one) == []
    two = run_issue(one, [f(value=200)], "2026-10-02")
    d = issues.developing(two)
    assert d[0]["days"] == 2 and d[0]["trend"] == "growing"
    assert (
        run_issue(two, [f(value=205)], "2026-10-03")["issues"]["deviation-198.51.100.1-1"]["trend"] == "same"
    )
    assert (
        run_issue(two, [f(value=50)], "2026-10-03")["issues"]["deviation-198.51.100.1-1"]["trend"]
        == "shrinking"
    )


def test_an_issue_is_resolved_after_three_quiet_runs_and_a_return_starts_over():
    state = run_issue(None, [f()], "2026-10-01")
    state = run_issue(state, [f()], "2026-10-02")
    for day in ("2026-10-03", "2026-10-04"):
        state = run_issue(state, [], day)
        assert "deviation-198.51.100.1-1" in state["issues"]  # still remembered after one or two quiet days
    assert run_issue(state, [], "2026-10-05")["issues"] == {}  # gone after the third
    back = run_issue(state, [f()], "2026-10-05")["issues"]["deviation-198.51.100.1-1"]
    assert back["days"] == 1 and back["first_seen"] == "2026-10-05"  # it went away and came back: a fresh run


def test_running_the_same_day_twice_does_not_count_it_twice():
    state = run_issue(None, [f()], "2026-10-01")
    state = run_issue(state, [f()], "2026-10-02")
    again = run_issue(state, [f()], "2026-10-02")
    assert again["issues"]["deviation-198.51.100.1-1"]["days"] == 2
    gone = run_issue(run_issue(state, [], "2026-10-03"), [], "2026-10-03")
    assert (
        gone["issues"]["deviation-198.51.100.1-1"]["quiet_runs"] == 1
    )  # one quiet day, however often it ran


def test_a_damaged_state_is_replaced_not_trusted():
    assert run_issue({"schema": 99, "issues": {"x": {}}}, [f()], "2026-10-01")["issues"].keys() == {
        "deviation-198.51.100.1-1"
    }
    assert run_issue("garbage", [], "2026-10-01")["issues"] == {}


# ---------------------------------------------------------------- composing the digest


def compose(findings, pipeline=None, learning=LEARNED, **kw):
    return dg.compose(findings, SYSTEMS, PRIORITIES, pipeline or PIPE, learning, NOW, **kw)


def test_a_quiet_day_says_so_with_green_systems():
    d = compose([])
    assert d.run_ok and d.headline == "Quiet: 2 hosts checked, nothing needs a look"
    assert [(s.name, s.state) for s in d.systems] == [("Gateway", "green"), ("Storage server", "green")]
    assert "2 hosts checked, 2 clean" in d.quiet


def test_system_states_follow_the_stories():
    d = compose([f("absence", host="198.51.100.2", system="nas"), f("deviation", n=2)])
    states = {s.id: s for s in d.systems}
    assert states["nas"].state == "red" and states["gw"].state == "yellow"  # a push_now story is red
    assert states["gw"].summary == "1 deviation"
    assert d.headline.startswith("2 things need a look; first: absence number 1")


def test_findings_for_a_system_not_in_systems_yaml_still_show_up():
    d = compose([f(system="other", host="198.51.100.9")])
    assert [s.id for s in d.systems][-1] == "other" and d.systems[-1].state == "yellow"


def test_info_level_findings_are_counted_not_listed_and_learning_is_said():
    d = compose(
        [f("novelty", story=False), f("novelty", n=2, story=False)],
        learning={"active": True, "day": 2, "of": 7},
    )
    assert d.counted == 2 and not d.stories
    assert "2 info-level findings counted, not listed" in d.quiet
    assert any(q.startswith("Learning period, day 2 of 7") for q in d.quiet)
    assert d.headline.endswith("(learning, day 2 of 7)")


def test_developing_issues_come_from_the_state():
    state = run_issue(run_issue(None, [f()], "2026-10-01"), [f(value=300)], "2026-10-02")
    d = compose([f(value=300)], state=state)
    assert [x["days"] for x in d.developing] == [2]


# ---------------------------------------------------------------- the failure path


def test_no_events_means_no_data_today_never_all_quiet():
    d = compose([], pipeline={"events_24h": 0, "hosts_checked": 2})
    assert (
        not d.run_ok and d.headline.startswith("No data today:") and "no events were received" in d.headline
    )
    assert not d.stories and d.quiet == [] and {s.state for s in d.systems} == {"unknown"}
    text = "\n".join(render_md.digest_markdown(d, "2026-10-04")).lower()
    assert "quiet" not in text and "nothing needs a look" not in text and "run failed" in text


def test_a_named_problem_is_in_the_headline_and_markdown():
    d = compose([f()], problems=["receiver down"])
    assert not d.run_ok and "receiver down" in d.headline
    md = "\n".join(render_md.digest_markdown(d, "2026-10-04"))
    assert "receiver down" in md and "not enough data to say" in md


def test_status_fields_for_a_good_and_a_failed_run():
    good = compose([f("absence")]).to_status()
    assert good["stories"][0]["rank"] == 1 and good["stories"][0]["detail"] == "hosts/198.51.100.1.md"
    assert good["systems"][0]["id"] == "gw" and set(good) == {"systems", "stories", "developing", "quiet"}
    from brief import status

    bad = status.status_document(compose([], pipeline={"events_24h": 0}))
    assert bad["run_ok"] is False and bad["stories"] == [] and bad["headline"].startswith("No data today")


# ---------------------------------------------------------------- the 150-line limit


def test_clip_cuts_and_says_so():
    assert render_md.clip(["a"] * 150) == ["a"] * 150
    out = render_md.clip([f"line {i}" for i in range(400)])
    assert len(out) == 150 and out[-1].startswith("... 252 more lines left out")


def test_a_huge_digest_still_fits_in_150_lines():
    findings = []
    for h in range(60):
        for n in range(6):
            findings.append(
                f("deviation", n=n, host=f"198.51.100.{h}", system=f"s{h}", value=n, state="x" * 400)
            )
    d = compose(findings, pipeline={"events_24h": 9, "hosts_checked": 60})
    assert len(d.stories) == 60  # one grouped story per host
    assert len(render_md.digest_markdown(d, "2026-10-04")) <= 150
    many_systems = Systems(
        [System(f"s{i}", f"System {i}", "x", (f"203.0.113.{i}",), "UTC", True) for i in range(200)]
    )
    wide = dg.compose(findings, many_systems, PRIORITIES, PIPE, LEARNED, NOW)
    assert len(render_md.digest_markdown(wide, "2026-10-04")) <= 150


def test_a_host_page_with_hundreds_of_findings_fits_in_150_lines():
    findings = [
        Finding(f"x-{i}", "deviation", f"finding {i}", "gw", "h", i, True, 1.0, {"k": "v" * 50})
        for i in range(400)
    ]
    facts = render_md.HostFacts(
        host="h", system="gw", system_name="Gateway", last_seen="2026-10-04T05:59:00Z", events_24h=12345,
        top=[{"sig_id": i, "label": "l" * 200, "count": 1000 - i, "worst": "err"} for i in range(10)],
        samples={i: [("2026-10-04T05:00:00Z", "m" * 500)] * 3 for i in range(400)},
    )  # fmt: skip
    lines = render_md.host_markdown(facts, findings)
    assert len(lines) == 150 and "more lines left out" in lines[-1]
    assert max(len(x) for x in lines) < 400  # long samples are cut


def test_a_host_page_for_a_failed_run_makes_no_assessment():
    facts = render_md.HostFacts(host="h", events_24h=0)
    text = "\n".join(render_md.host_markdown(facts, [], run_ok=False))
    assert "no assessment" in text and "None." not in text


def test_host_file_names_are_safe():
    assert dg.host_file("198.51.100.1") == "198.51.100.1.md"
    assert dg.host_file("2001:db8::1") == "2001-db8--1.md" and dg.host_file(None) == "unknown.md"
    assert dg.host_file("a/b") == "a-b.md"


def test_the_grouping_threshold_comes_from_priorities_yaml():
    found = [f(n=1), f(n=2)]
    loose = Priorities({**{"kinds": {"deviation": {"rank": 50}}}, "grouping": {"min_members": 2, "show": 1}})
    d = dg.compose(found, SYSTEMS, loose, PIPE, LEARNED, NOW)
    assert len(d.stories) == 1 and d.stories[0].grouped and len(d.stories[0].top) == 1
    assert len(dg.compose(found, SYSTEMS, PRIORITIES, PIPE, LEARNED, NOW).stories) == 2  # the default is 3


def test_a_new_error_outranks_a_routine_swing_in_volume():
    quiet = f("deviation", n=1, value=500)
    error = f("novelty", n=2, worst_severity="err")
    mild = f("novelty", n=3, worst_severity="info")
    boosted = Priorities({**PRIORITIES_DATA, "severity_boost": {"err": 10, "crit": 20}})
    ranked = grouping.build_stories([quiet, error, mild], boosted)
    assert [(s.kind, s.rank) for s in ranked] == [("novelty", 55), ("deviation", 50), ("novelty", 45)]
    flat = grouping.build_stories([quiet, error], PRIORITIES)
    assert [s.kind for s in flat] == ["deviation", "novelty"]  # no boost configured: the plain ranks


def test_a_group_takes_the_boost_of_its_worst_member():
    found = [f("novelty", n=i, worst_severity=("crit" if i == 2 else "info")) for i in range(1, 5)]
    boosted = Priorities({**PRIORITIES_DATA, "severity_boost": {"crit": 20}})
    (story,) = grouping.build_stories(found, boosted)
    assert story.grouped and story.rank == 65


def test_two_groups_on_one_system_say_which_host():
    a = [f(n=i, host="198.51.100.11", system="gw") for i in range(4)]
    b = [f(n=i + 10, host="198.51.100.12", system="gw") for i in range(4)]
    titles = [s.title for s in grouping.build_stories(a + b, PRIORITIES, {"gw": "Gateway"})]
    assert sorted(titles) == [
        "4 signatures far above normal on Gateway (198.51.100.11)",
        "4 signatures far above normal on Gateway (198.51.100.12)",
    ]
    (only,) = grouping.build_stories(a, PRIORITIES, {"gw": "Gateway"})
    assert only.title == "4 signatures far above normal on Gateway"  # one host: no need to say
