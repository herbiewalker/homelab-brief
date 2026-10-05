import pytest

from brief import routing
from brief.config import ConfigError
from brief.detectors import Finding

DATA = {
    "default": {"rank": 10, "route": "digest_only"},
    "kinds": {
        "absence": {"rank": 80, "route": "push_now"},
        "deviation": {"rank": 50, "route": "digest_only"},
        "smart_failing": {"rank": 100, "route": "push_now", "urgent": True},
    },
    "rules": {"smart_pending_nonzero": {"rank": 95, "route": "push_now", "urgent": True}},
}


def f(kind, fid=None, story=True, value=None):
    return Finding(id=fid or f"{kind}-1", kind=kind, title=kind, story=story, value=value)


def test_a_finding_routes_by_kind_then_falls_back_to_the_default():
    p = routing.Priorities(DATA)
    assert p.route_for(f("absence")) == routing.Route(80, "push_now", False)
    assert p.route_for(f("smart_failing")).urgent is True
    assert p.route_for(f("something_new")) == routing.Route(10, "digest_only", False)


def test_a_rule_id_wins_over_the_kind():
    p = routing.Priorities(DATA)
    assert p.route_for(f("smart_failing"), rule="smart_pending_nonzero").rank == 95
    assert (
        p.route_for(f("absence"), rule="not_a_rule").rank == 80
    )  # an unknown rule falls through to the kind


def test_a_kind_inherits_what_it_does_not_set():
    p = routing.Priorities({"default": {"rank": 20, "route": "daily_headline"}, "kinds": {"x": {"rank": 70}}})
    assert p.route_for(f("x")) == routing.Route(70, "daily_headline", False)


def test_ranking_puts_stories_first_then_rank_then_size_then_id():
    p = routing.Priorities(DATA)
    found = [
        f("deviation", "deviation-a", value=5),
        f("absence", "absence-b", story=False),  # count only: after every story
        f("deviation", "deviation-b", value=9),
        f("absence", "absence-a"),
    ]
    assert [x.id for x, _ in p.ranked(found)] == ["absence-a", "deviation-b", "deviation-a", "absence-b"]


def test_only_stories_routed_push_now_would_push():
    p = routing.Priorities(DATA)
    found = [f("absence", "absence-a"), f("absence", "absence-b", story=False), f("deviation")]
    assert [x.id for x, _ in p.pushes(found)] == ["absence-a"]


def test_an_unknown_route_is_refused():
    with pytest.raises(ConfigError, match="unknown route"):
        routing.Priorities({"kinds": {"x": {"route": "shout"}}})


def test_the_shipped_priorities_file_loads_and_routes_every_detector(tmp_path):
    from pathlib import Path

    shipped = Path(__file__).resolve().parents[1] / "priorities.yaml"  # brief/ in the repo, /app in the image
    folder = tmp_path / "brief"
    folder.mkdir()
    (folder / "priorities.yaml").write_bytes(shipped.read_bytes())
    p = routing.Priorities.load(
        {"BRIEF_DEFAULTS_DIR": str(tmp_path), "BRIEF_CONFIG_DIR": str(tmp_path / "none")}
    )
    for kind in ("absence", "novelty", "deviation", "off_hours", "clock_skew"):
        assert kind in p.kinds
    assert p.route_for(f("absence")).route == "push_now"
    assert p.lifecycle["daily_push_budget"] == 5


def test_a_broken_priorities_file_falls_back_to_the_default_instead_of_hiding_findings(tmp_path):
    folder = tmp_path / "c" / "brief"
    folder.mkdir(parents=True)
    (folder / "priorities.yaml").write_text("kinds: [broken", encoding="utf-8")
    p = routing.Priorities.load(
        {"BRIEF_CONFIG_DIR": str(tmp_path / "c"), "BRIEF_DEFAULTS_DIR": str(tmp_path / "d")}
    )
    assert p.route_for(f("absence")) == routing.Route(10, "digest_only", False)
    (folder / "priorities.yaml").write_text("kinds:\n  x: { route: shout }\n", encoding="utf-8")
    assert (
        routing.Priorities.load(
            {"BRIEF_CONFIG_DIR": str(tmp_path / "c"), "BRIEF_DEFAULTS_DIR": str(tmp_path / "d")}
        ).kinds
        == {}
    )
