"""The locked safety rules for what the triage and tuner agents may propose (docs/guide/10-agents.md).

They run inside `proposals.create`, so they apply to every route a proposal can take, and they look at the
actual change and the actual data, not at what the model says about it:

  triage   may only add an ignore rule (for noise) or a playbook rule (advice for a concern), never ignore disk,
           authentication, or security events: a new ignore rule is refused if its pattern names such a thing, if it
           would also match any recent log line that does, or if it is so broad that it matches most lines
  tuner    may only change single values in priorities.yaml and playbook.yaml thresholds; never anything for disk,
           parity, SMART, storage, or absence findings; and the change is replayed over the last week with and
           without it: it is refused if any acknowledged, snoozed, urgent, or protected finding would no longer
           be found or pushed. The replay's numbers are written into the proposal by this code, not by the model.
"""

import hashlib
import os
import re
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

from brief import configedit, livestatus, proposals, replay
from brief.config import config_file
from brief.ignore import IgnoreRules
from brief.store import Store

DENY = re.compile(
    r"(?i)(?<![\w])(disk|disks|smart|sd[a-z]\d*|nvme\d*|ata\d+|i/o error|blk_\w*|parity|raid|zfs|btrfs|"
    r"auth\w*|login|logon|password|passwd|sshd|ssh|sudo|pam_\w*|denied|forbidden|unauthori[sz]ed|"
    r"security|intrusion|attack\w*|malware|exploit\w*|fail2ban|brute\w*)(?![\w])"
)
PROTECTED_KINDS = (
    "absence", "smart_failing", "smart_trend", "disk_temp", "crc_rising", "parity_slow", "storage_low",
    "unraid_warning", "pipeline_error", "config_error",
)  # fmt: skip
PROTECTED_THRESHOLDS = ("absence", "disk_temp_hdd_c", "smart_trend", "parity_slow", "storage_low")
BROAD_SHARE = 0.4  # a new ignore rule matching more than this share of recent lines is refused
BROAD_MIN_SAMPLES = 20
SAMPLE_LIMIT = 3000
REPLAY_DAYS = 7


# ---------------------------------------------------------------- reading a change


def _load(text: str) -> dict:
    data = yaml.safe_load(text) or {}
    return data if isinstance(data, dict) else {}


def changed_paths(old, new, prefix: tuple = ()) -> list[tuple]:
    """Every leaf path whose value differs between two YAML documents (added, removed, or changed)."""
    if isinstance(old, dict) and isinstance(new, dict):
        out: list[tuple] = []
        for key in sorted(set(old) | set(new), key=str):
            out += changed_paths(old.get(key), new.get(key), (*prefix, key))
        return out
    return [] if old == new else [prefix]


# ---------------------------------------------------------------- triage


def _new_ignore_rules(old: str, new: str):
    before = {r.name: (r.pattern.pattern, r.host) for r in IgnoreRules.from_mapping(_load(old)).rules}
    return [
        r
        for r in IgnoreRules.from_mapping(_load(new)).rules
        if before.get(r.name) != (r.pattern.pattern, r.host)
    ]


def _recent_lines(store: Store) -> list[str]:
    with store.read() as db:
        rows = db.execute(
            "SELECT sig.app AS app, s.msg AS msg FROM sample s JOIN signature sig ON sig.sig_id = s.sig_id "
            "ORDER BY s.ts DESC LIMIT ?",
            (SAMPLE_LIMIT,),
        ).fetchall()
    return [f"{r['app']}: {r['msg']}" if r["app"] else str(r["msg"]) for r in rows]


def triage_rule(d: "proposals.Draft") -> str | None:
    if d.source != "agent:triage":
        return None
    if d.file == "playbook.yaml":
        extra = [p for p in changed_paths(_load(d.old), _load(d.new)) if p[:1] != ("rules",)]
        if extra or configedit.changed_lines(d.old, d.new)[1]:
            return "Triage may only add playbook rules, not change or remove anything else."
        return None
    if d.file != "ignore.yaml":
        return "Triage may only propose an ignore rule or a playbook rule."
    added = _new_ignore_rules(d.old, d.new)
    if not added:
        return "Triage may only add ignore rules."
    lines = _recent_lines(d.store)
    risky = [ln for ln in lines if DENY.search(ln)]
    for rule in added:
        if DENY.search(rule.pattern.pattern) or DENY.search(rule.name) or DENY.search(rule.host or ""):
            return "Triage never ignores events about disks, authentication, or security."
        if any(rule.pattern.search(ln) for ln in risky):
            return "That rule would also ignore recent lines about disks, authentication, or security."
        if len(lines) >= BROAD_MIN_SAMPLES and sum(
            1 for ln in lines if rule.pattern.search(ln)
        ) > BROAD_SHARE * len(lines):
            return "That rule is too broad: it matches most recent lines."
    return None


# ---------------------------------------------------------------- tuner


@dataclass
class Replayed:
    headline: str
    text: str
    hidden: list[str] = field(default_factory=list)  # protected findings that would be lost, by id


_cache: dict[tuple, Replayed] = {}


def _scenario_env(file: str, text: str, env: Mapping[str, str] | None, folder: Path) -> dict:
    (folder / "brief").mkdir(parents=True, exist_ok=True)
    for name in configedit.EDITABLE:
        src = config_file(name, env)
        if src.exists():
            shutil.copyfile(src, folder / "brief" / name)
    (folder / "brief" / file).write_text(text, encoding="utf-8")
    return {
        **(os.environ if env is None else env),
        "BRIEF_CONFIG_DIR": str(folder),
        "BRIEF_DEFAULTS_DIR": str(folder),
    }


def _run(store: Store, systems, start, end, env) -> tuple[replay.Replay, set[str], dict[str, str], set[str]]:
    rep = replay.replay(store, systems, start, end, hourly=False, env=env, ignore_learning=True)
    kinds: dict[str, str] = {}
    for run in rep.runs:
        for f in run.findings:
            kinds[f.id] = f.kind
    pushed = {n.finding_id for run in rep.runs for n in rep.pushes(run)}
    return rep, set(kinds), kinds, pushed


def _protected_ids(store: Store, kinds: Mapping[str, str]) -> set[str]:
    with store.read() as db:
        held = {
            r["finding_id"]
            for r in db.execute("SELECT finding_id FROM alert WHERE state IN ('acked', 'snoozed')")
        }
        urgent = {
            r["finding_id"] for r in db.execute("SELECT DISTINCT finding_id FROM push WHERE urgent = 1")
        }
    return {i for i in kinds if kinds[i] in PROTECTED_KINDS} | (held | urgent)


def replay_evidence(
    store: Store, systems, file: str, old: str, new: str, env, now: datetime | None = None
) -> Replayed:
    """Replay the last week with the file as it is and with the change; report pushes and what would be lost."""
    now = now or datetime.now(UTC)
    key = (
        file,
        hashlib.sha256(old.encode()).hexdigest(),
        hashlib.sha256(new.encode()).hexdigest(),
        now.date(),
    )
    if key in _cache:
        return _cache[key]
    systems = systems if systems is not None else livestatus.load_systems()
    end = now.date()
    start = end - timedelta(days=REPLAY_DAYS)
    with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
        before, ids_before, kinds, pushed_before = _run(
            store, systems, start, end, _scenario_env(file, old, env, Path(a))
        )
        after, ids_after, _, pushed_after = _run(
            store, systems, start, end, _scenario_env(file, new, env, Path(b))
        )
    protected = _protected_ids(store, kinds)
    hidden = sorted(
        i
        for i in protected & ids_before
        if i not in ids_after or (i in pushed_before and i not in pushed_after)
    )
    lost = sorted(ids_before - ids_after)
    pushes_before, pushes_after = replay.summary(before)["would_push"], replay.summary(after)["would_push"]
    headline = f"pushes {pushes_before} -> {pushes_after}; {len(lost)} finding(s) no longer found; {len(hidden)} protected one(s) hidden"
    text = (
        f"Replay of the last {REPLAY_DAYS} days, run by the product with and without this change: "
        f"pushes before {pushes_before}, after {pushes_after}; findings that would be missed: {len(lost)}"
        + (f" ({', '.join(lost[:5])})" if lost else "")
        + f"; acknowledged, snoozed, urgent, or protected findings that would be hidden: {len(hidden)}."
    )
    result = Replayed(headline, text, hidden)
    if len(_cache) > 8:
        _cache.clear()
    _cache[key] = result
    return result


def tuner_rule(d: "proposals.Draft") -> str | None:
    if d.source != "agent:tuner":
        return None
    if d.file not in ("priorities.yaml", "playbook.yaml"):
        return "The tuner may only change priorities.yaml and playbook.yaml."
    for path in changed_paths(_load(d.old), _load(d.new)):
        if d.file == "playbook.yaml" and path[:1] != ("thresholds",):
            return "The tuner may only change playbook thresholds, not its advice or rules."
        if path[:2] in {("kinds", k) for k in PROTECTED_KINDS} | {
            ("thresholds", t) for t in PROTECTED_THRESHOLDS
        }:
            return "The tuner never changes anything about disk, parity, SMART, storage, or absence alerts."
        if (
            len(path) >= 3
            and path[0] == "kinds"
            and path[2] == "route"
            and _load(d.new)["kinds"][path[1]].get("route") == "suppress"
        ):
            return "The tuner never suppresses a kind of finding outright."
    result = replay_evidence(d.store, None, d.file, d.old, d.new, d.env, d.now)
    if result.hidden:
        return f"That change would have hidden an acknowledged, snoozed, urgent, or protected finding ({result.hidden[0]})."
    if "Replay of the last" not in d.evidence:
        return "A tuner proposal must carry the replay of the last week, written by the product."
    return None


RULES = [triage_rule, tuner_rule]
