"""Task 18: the playbook engine. Conditions are structured data; nothing in the file is executed."""

from pathlib import Path

import pytest
import yaml

from brief import playbook
from brief.config import ConfigError
from brief.detectors import Finding

BRIEF = Path(__file__).resolve().parents[1]


def rules(text: str):
    return playbook.parse(yaml.safe_load("rules:\n" + text))


def finding(kind="smart_trend", **detail):
    return Finding("f1", kind, "A finding", system="s1", host="198.51.100.9", value=3.0, detail=detail)


PENDING = """
  pending:
    kind: smart_trend
    match:
      - { field: attr, op: in, value: ["197:Current_Pending_Sector", "198:Offline_Uncorrectable"] }
      - { field: raw, op: gt, value: 0 }
    bucket: act_now
    advice: "{disk} reports {raw} unreadable sectors."
  realloc:
    kind: smart_trend
    match:
      - { field: attr, op: eq, value: "5:Reallocated_Sector_Ct" }
    bucket: plan
    advice: "{disk} remapped sectors."
"""


def test_the_first_matching_rule_wins_and_fills_the_advice():
    f = finding(attr="197:Current_Pending_Sector", raw=8, disk="disk-3")
    (out,) = playbook.apply([f], rules(PENDING))
    assert out.detail["rule"] == "pending" and out.detail["bucket"] == "act_now"
    assert out.detail["advice"] == "disk-3 reports 8 unreadable sectors."
    assert f.detail == {
        "attr": "197:Current_Pending_Sector",
        "raw": 8,
        "disk": "disk-3",
    }  # the input is untouched


def test_conditions_must_all_hold_and_a_missing_field_is_false():
    r = rules(PENDING)
    assert playbook.advise(finding(attr="197:Current_Pending_Sector", raw=0, disk="d"), r) is None
    assert playbook.advise(finding(attr="197:Current_Pending_Sector", disk="d"), r) is None
    assert playbook.advise(finding(attr="5:Reallocated_Sector_Ct", disk="d"), r)["rule"] == "realloc"


def test_another_kind_is_never_matched():
    assert playbook.advise(finding("crc_rising", attr="5:Reallocated_Sector_Ct"), rules(PENDING)) is None


def test_a_rule_without_match_applies_to_every_finding_of_its_kind():
    r = rules('  any:\n    kind: crc_rising\n    advice: "Check the cable on {disk}."\n')
    assert playbook.advise(finding("crc_rising", disk="d1"), r)["advice"] == "Check the cable on d1."


@pytest.mark.parametrize(
    "op, value, have, expected",
    [
        ("eq", 5, 5.0, True), ("eq", "5", 5, True), ("eq", "a", "a", True), ("eq", "a", "b", False),
        ("ne", "a", "b", True), ("ne", 5, "5", False),
        ("gt", 1, 2, True), ("gt", 2, 2, False), ("gte", 2, 2, True), ("lt", 3, 2, True), ("lte", 2, 3, False),
        ("gt", 1, "many", False), ("gt", 1, True, False),
        ("in", ["a", "b"], "b", True), ("in", [1, 2], "2", True), ("in", ["a"], "c", False),
        ("contains", "err", "an error here", True), ("contains", "x", ["x", "y"], True), ("contains", "z", "abc", False),
        ("regex", r"^disk\d+$", "disk12", True), ("regex", r"^disk\d+$", "xdisk12", False),
    ],
)  # fmt: skip
def test_every_operator(op, value, have, expected):
    cond = playbook._condition("r", 0, {"field": "f", "op": op, "value": value})
    assert playbook.holds(cond, {"f": have}) is expected


def test_the_finding_itself_is_a_source_of_facts():
    r = rules(
        "  r:\n    kind: smart_trend\n    match:\n      - { field: kind, op: eq, value: smart_trend }\n"
        "      - { field: system, op: eq, value: s1 }\n      - { field: value, op: gte, value: 3 }\n"
        '    advice: "{host} on {system}"\n'
    )
    assert playbook.advise(finding(), r)["advice"] == "198.51.100.9 on s1"


def test_a_missing_placeholder_reads_unknown_and_numbers_are_tidy():
    r = rules('  r:\n    kind: smart_trend\n    advice: "{disk} {n} {ratio}"\n')
    assert playbook.advise(finding(n=4.0, ratio=0.25), r)["advice"] == "unknown 4 0.25"


@pytest.mark.parametrize(
    "text, message",
    [
        ("  r:\n    advice: hello\n", "kind is missing"),
        ("  r:\n    kind: k\n", "advice must be text"),
        ("  r:\n    kind: k\n    advice: '{a.b}'\n", "plain {name}"),
        ("  r:\n    kind: k\n    advice: '{a!r}'\n", "plain {name}"),
        ("  r:\n    kind: k\n    advice: '{0}{'\n", "plain {name}"),
        ("  r:\n    kind: k\n    advice: x\n    bucket: soon\n", "bucket"),
        ("  r:\n    kind: k\n    advice: x\n    run: rm\n", "unknown keys"),
        ("  r:\n    kind: k\n    advice: x\n    match: {a: b}\n", "must be a list"),
        (
            "  r:\n    kind: k\n    advice: x\n    match: [{ field: a, op: exec, value: 1 }]\n",
            "op must be one of",
        ),
        (
            "  r:\n    kind: k\n    advice: x\n    match: [{ field: a, op: gt, value: lots }]\n",
            "must be a number",
        ),
        ("  r:\n    kind: k\n    advice: x\n    match: [{ field: a, op: in, value: x }]\n", "non-empty list"),
        (
            "  r:\n    kind: k\n    advice: x\n    match: [{ field: a, op: regex, value: '(' }]\n",
            "not a valid pattern",
        ),
        (
            "  r:\n    kind: k\n    advice: x\n    match: [{ field: a, op: regex, value: '(a+)+$' }]\n",
            "nested repetition",
        ),
        (
            "  r:\n    kind: k\n    advice: x\n    match: [{ field: a, op: regex, value: '(x*){2,}' }]\n",
            "nested repetition",
        ),
        ("  r:\n    kind: k\n    advice: x\n    match: [{ field: 'a b', op: eq, value: 1 }]\n", "plain name"),
        ("  r:\n    kind: k\n    advice: x\n    match: [{ field: a, op: eq }]\n", "value is missing"),
        ("  'bad id!':\n    kind: k\n    advice: x\n", "rule id"),
    ],
)
def test_a_bad_rule_is_refused_with_a_message(text, message):
    with pytest.raises(ConfigError, match=message):
        rules(text)


def test_the_shipped_playbook_is_valid_and_covers_the_phase_2_kinds():
    parsed = playbook.validate(BRIEF / "playbook.yaml")
    assert {r.kind for r in parsed} >= {"smart_failing", "smart_trend", "parity_slow", "firmware_pending"}


def test_load_returns_nothing_for_a_broken_file(tmp_path):
    user = tmp_path / "config" / "brief"
    user.mkdir(parents=True)
    (user / "playbook.yaml").write_text(
        "schema: 1\nrules:\n  r: {kind: k, advice: '{a.b}'}\n", encoding="utf-8"
    )
    env = {"BRIEF_CONFIG_DIR": str(tmp_path / "config"), "BRIEF_DEFAULTS_DIR": str(tmp_path / "none")}
    assert playbook.load(env) == []


def test_advice_reaches_the_digest_and_the_rule_routes_the_story(tmp_path):
    from brief import grouping
    from brief.render_md import _story
    from brief.routing import Priorities

    f = playbook.apply(
        [finding("parity_slow", server="server-1", duration="9 h", speed=12, previous_speed=95)],
        playbook.validate(BRIEF / "playbook.yaml"),
    )
    assert f[0].detail["rule"] == "parity_slow" and "server-1" in f[0].detail["advice"]
    priorities = Priorities(
        {"kinds": {"parity_slow": {"rank": 10}}, "rules": {"parity_slow": {"rank": 99, "route": "push_now"}}}
    )
    (story,) = grouping.build_stories(f, priorities)
    assert story.rank == 99 and story.route == "push_now"
    text = "\n".join(_story(1, story))
    assert "advice (watch): server-1's parity check took 9 h at 12 MB/s" in text
