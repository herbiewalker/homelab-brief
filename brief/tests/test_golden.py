"""The golden day: a synthetic month through the real pipeline, replayed, against a committed snapshot.

If this fails, the detectors, the thresholds in playbook.yaml, the routing in priorities.yaml, the
signature mining, or the replay changed what a known set of logs produces. Read the diff. If the new
output is what you want, regenerate the snapshot and commit it together with the change:

    GOLDEN_UPDATE=1 python -m pytest tests/test_golden.py

(in the repository: from the brief/ folder, in the test image or your dev venv).
"""

import difflib
import json
import os
from pathlib import Path

import golden_day

SNAPSHOT = Path(__file__).parent / "golden" / "expected_replay.json"


def render(doc: dict) -> list[str]:
    return json.dumps(doc, indent=2, sort_keys=True).splitlines()


def test_the_golden_day_matches_its_snapshot(tmp_path):
    actual = golden_day.snapshot(tmp_path)
    if os.environ.get("GOLDEN_UPDATE") == "1":
        SNAPSHOT.parent.mkdir(exist_ok=True)
        SNAPSHOT.write_text("\n".join(render(actual)) + "\n", encoding="utf-8")
        return
    expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    if actual != expected:
        diff = "\n".join(
            difflib.unified_diff(render(expected), render(actual), "expected", "actual", lineterm="", n=2)
        )
        raise AssertionError(
            "The golden day's replay output changed.\n\n" + diff[:6000] + "\n\nIf this is intended, "
            "regenerate the snapshot (GOLDEN_UPDATE=1 python -m pytest tests/test_golden.py) and commit it."
        )


def test_the_golden_day_tells_the_story_it_was_built_to_tell(tmp_path):
    """The snapshot could be regenerated wrongly, so the story is also checked in plain terms."""
    doc = golden_day.snapshot(tmp_path)
    seen = {(run["at"][5:16], n["kind"]) for run in doc["runs"] for n in run["new"]}
    assert seen == {
        (
            "09-18T02:00",
            "absence",
        ),  # the camera stopped after 22:00 on the 17th; three hours on it is overdue
        ("09-20T06:00", "deviation"),
        ("09-20T06:00", "off_hours"),
        ("09-20T06:00", "novelty"),
    }
    runs = {r["at"]: r for r in doc["runs"]}
    day = runs["2026-09-20T06:00:00Z"]["new"]
    by_kind = {}
    for n in day:
        by_kind.setdefault(n["kind"], []).append(n)
    assert len(by_kind["novelty"]) == 2  # the new disk error and the returned warning
    states = {n["detail"]["state"] for n in by_kind["novelty"]}
    assert states == {"new", "returned"}
    assert by_kind["deviation"][0]["system"] == "gw" and by_kind["deviation"][0]["detail"]["today"] == 700
    assert by_kind["off_hours"][0]["system"] == "nas" and by_kind["off_hours"][0]["detail"]["hour_utc"] == 14
    push = doc["summary"]["pushes"]
    assert [p["id"] for p in push] == ["absence-198.51.100.3"]  # only the camera would have been pushed
    assert not any(run["learning"] for run in doc["runs"])  # a month of live data: learning is over


def _compare_text(name: str, actual: str) -> None:
    path = Path(__file__).parent / "golden" / name
    if os.environ.get("GOLDEN_UPDATE") == "1":
        path.parent.mkdir(exist_ok=True)
        path.write_text(actual, encoding="utf-8")
        return
    expected = path.read_text(encoding="utf-8")
    if actual != expected:
        diff = "\n".join(
            difflib.unified_diff(
                expected.splitlines(), actual.splitlines(), "expected", "actual", lineterm="", n=2
            )
        )
        raise AssertionError(
            f"The golden {name} changed.\n\n{diff[:6000]}\n\nIf this is intended, regenerate it "
            "(GOLDEN_UPDATE=1 python -m pytest tests/test_golden.py) and commit it."
        )


def test_the_golden_digest_matches_its_snapshot(tmp_path):
    text = golden_day.digest_snapshot(tmp_path)
    assert len(text.splitlines()) <= 150
    _compare_text("expected_digest.md", text)


def test_the_golden_weekly_report_matches_its_snapshot(tmp_path):
    text = golden_day.weekly_snapshot(tmp_path)
    assert len(text.splitlines()) <= 150
    _compare_text("expected_weekly.md", text)
