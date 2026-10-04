from pathlib import Path

import pytest

from brief.config import ConfigError, load_mapping
from brief.ignore import IgnoreRules
from brief.systems import Systems

BRIEF = Path(__file__).resolve().parents[1]  # the brief/ folder (or /app in the container)

SYSTEMS = {
    "default_system": "other",
    "systems": [
        {"id": "gateway", "label": "Gateway", "kind": "gateway", "hosts": ["198.51.100.1", "GW-Main"],
         "source_tz": "America/New_York"},
        {"id": "server-1", "hosts": ["198.51.100.20", "<placeholder-host>"], "expected": False},
    ],
}  # fmt: skip


# ---------- systems ----------


def test_a_sender_is_matched_by_ip_or_header_name_in_any_case():
    s = Systems.from_mapping(SYSTEMS)
    by_ip = s.resolve("198.51.100.1", "whatever")
    by_name = s.resolve("203.0.113.9", "gw-main")
    assert by_ip.system_id == by_name.system_id == "gateway"
    assert by_ip.known and by_name.known and by_ip.source_tz == "America/New_York"


def test_an_unknown_sender_rolls_up_to_the_default_system_and_is_flagged():
    r = Systems.from_mapping(SYSTEMS).resolve("203.0.113.77", "stranger")
    assert (r.system_id, r.known, r.source_tz) == ("other", False, "UTC")


def test_placeholders_in_the_example_file_match_nothing():
    s = Systems.from_mapping(SYSTEMS)
    assert not s.resolve("1.2.3.4", "<placeholder-host>").known


def test_labels_and_expected_have_defaults():
    s = Systems.from_mapping(SYSTEMS)
    server = next(x for x in s.systems if x.id == "server-1")
    assert server.label == "server-1" and server.expected is False and server.kind == "other"


@pytest.mark.parametrize(
    "bad, message",
    [
        ({"systems": [{"id": "a", "hosts": []}, {"id": "a", "hosts": []}]}, "duplicate"),
        ({"systems": [{"id": "a", "hosts": ["h"]}, {"id": "b", "hosts": ["H"]}]}, "both"),
        ({"systems": [{"id": "Bad Id", "hosts": []}]}, "id"),
        ({"systems": [{"id": "a", "hosts": "not-a-list"}]}, "hosts"),
        ({"systems": [{"id": "a", "hosts": [], "source_tz": "Mars/Base"}]}, "timezone"),
        ({"systems": "nope"}, "list"),
        ({"default_system": "Not Valid"}, "default_system"),
    ],
)
def test_bad_systems_files_are_rejected_with_a_reason(bad, message):
    with pytest.raises(ConfigError, match=message):
        Systems.from_mapping(bad)


def test_a_missing_file_means_everything_is_unknown(tmp_path):
    s = Systems.load(tmp_path / "systems.yaml")
    assert not s.resolve("198.51.100.1").known and s.default_system == "other"


def test_a_file_is_loaded_and_a_wrong_schema_is_refused(tmp_path):
    f = tmp_path / "systems.yaml"
    f.write_text("schema: 1\nsystems:\n  - id: a\n    hosts: [198.51.100.1]\n", encoding="utf-8")
    assert Systems.load(f).resolve("198.51.100.1").system_id == "a"
    f.write_text("schema: 2\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="schema"):
        Systems.load(f)
    f.write_text("a: [unclosed", encoding="utf-8")
    with pytest.raises(ConfigError, match="YAML"):
        load_mapping(f)


def test_the_shipped_example_loads():
    s = Systems.load(BRIEF / "systems.example.yaml")
    assert s.systems and not s.resolve("198.51.100.1").known


# ---------- ignore rules ----------

RULES = {
    "ignore": [
        {"name": "flaps", "host": "Switch-1", "match": "(?i)link (up|down)"},
        {"name": "anywhere", "match": r"ubnt_ai_daemon.*(fail|error)"},
        {"name": "by-system", "host": "gateway", "match": "noisy"},
    ]
}


def hit(rules, msg, app="kernel", ip="198.51.100.5", host="switch-1", system="switches"):
    r = rules.match(source_ip=ip, header_host=host, system_id=system, app=app, msg=msg)
    return r.name if r else None


def test_a_rule_applies_to_its_host_by_header_name_ip_or_system():
    rules = IgnoreRules.from_mapping(RULES)
    assert hit(rules, "port 3 Link Down") == "flaps"
    assert hit(rules, "port 3 link up", host="other-switch") is None
    assert hit(rules, "noisy thing", system="gateway", host="x") == "by-system"
    named_by_ip = IgnoreRules.from_mapping({"ignore": [{"name": "r", "host": "198.51.100.5", "match": "x"}]})
    assert hit(named_by_ip, "x", host="anything") == "r"


def test_a_rule_without_a_host_matches_any_host_and_sees_the_app_name():
    rules = IgnoreRules.from_mapping(RULES)
    assert hit(rules, "connect failed", app="ubnt_ai_daemon", host="whatever") == "anywhere"
    assert hit(rules, "connect failed", app="other_daemon") is None


def test_the_first_matching_rule_wins():
    rules = IgnoreRules.from_mapping({"ignore": [{"name": "a", "match": "x"}, {"name": "b", "match": "x"}]})
    assert hit(rules, "x") == "a"


@pytest.mark.parametrize(
    "bad, message",
    [
        ({"ignore": [{"name": "a", "match": "("}]}, "bad pattern"),
        ({"ignore": [{"match": "x"}]}, "name"),
        ({"ignore": [{"name": "a"}]}, "match"),
        ({"ignore": [{"name": "a", "match": "x" * 501}]}, "longer"),
        ({"ignore": [{"name": "a", "match": "x"}, {"name": "a", "match": "y"}]}, "unique"),
        ({"ignore": "nope"}, "list"),
        ({"ignore": ["nope"]}, "mapping"),
    ],
)
def test_bad_ignore_files_are_rejected_with_a_reason(bad, message):
    with pytest.raises(ConfigError, match=message):
        IgnoreRules.from_mapping(bad)


def test_the_shipped_ignore_file_loads_and_ignores_nothing_real():
    rules = IgnoreRules.load(BRIEF / "ignore.yaml")
    assert rules.rules
    for msg in ("Link up on port 3", "Low memory warning", "some_daemon failed"):
        assert hit(rules, msg, host="a-real-host", system="a-real-system") is None
