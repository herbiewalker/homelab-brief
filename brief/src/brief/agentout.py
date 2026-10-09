"""What each agent's final reply becomes (docs/guide/10-agents.md): checked in code, then stored or turned into
proposals. A model writes words; this module decides what survives.

  investigation_note, chat_answer   text: only statements that cite a real, successful tool result survive
  verdicts (triage)                 a JSON list, one verdict per signature the run was asked about; each needs a
                                    reason and a real citation, and may carry an ignore or playbook proposal
  proposals (tuner)                 a JSON list of single-value changes; the RUNTIME applies each one to the file,
                                    replays the last week with and without it, and writes the replay numbers into
                                    the proposal itself, so evidence cannot be made up by the model

Proposals from the two JSON agents are never made through the `propose_change` tool: the code below builds the
new file text itself from a small, typed change, which keeps a small model from having to rewrite a whole file.
"""

import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime

import yaml

from brief import ai, proposals, yamlscalar
from brief.jsonlog import log
from brief.store import Store, iso

OUTPUTS = ("text", "investigation_note", "chat_answer", "verdicts", "proposals")
JSON_OUTPUTS = ("verdicts", "proposals")
VERDICTS = ("noise", "watch", "concern", "unsure")
CITATION = re.compile(r"\[ref:(r\d+)\]")
SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\[])")
MAX_SIGNATURES = 5
MAX_REASON = 300
FINDING_KINDS = (
    "absence", "novelty", "deviation", "off_hours", "smart_failing", "smart_trend", "disk_temp", "crc_rising",
    "firmware_pending", "version_behind", "new_infrastructure", "new_device", "traffic_spike", "odd_hours",
    "metric_drift", "power_drift", "parity_slow", "unraid_warning", "clock_skew", "unknown_sender", "storage_low",
)  # fmt: skip
BUCKETS = ("watch", "plan", "act_now")
RULE_NAME = re.compile(r"^[a-z][a-z0-9_]{2,40}$")


# ---------------------------------------------------------------- text outputs


def check_citations(text: str, evidence: set[str]) -> tuple[str, int, int]:
    """(kept text, statements kept, statements dropped). A statement is kept only if it cites at least one
    result and every result it cites is real and succeeded. A heading survives only with something under it."""
    blocks: list[tuple[str, list[str]]] = [("", [])]  # (heading line, kept statements)
    kept = dropped = 0
    for line in ai.clean(text).splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        heading = re.match(r"^#{1,6}\s", stripped) or (
            stripped.endswith(":") and not CITATION.search(stripped) and len(stripped) < 60
        )
        if heading:
            blocks.append((stripped, []))
            continue
        prefix = re.match(r"^\s*(?:[-*]|\d+[.)])\s+", line)
        lead = prefix.group(0).strip() + " " if prefix else ""
        body = line[prefix.end() :].strip() if prefix else stripped
        first = True
        for sentence in SENTENCE.split(body):
            cited = CITATION.findall(sentence)
            if cited and all(c in evidence for c in cited):
                blocks[-1][1].append((lead if first else "") + sentence)
                first = False
                kept += 1
            else:
                dropped += 1
    lines: list[str] = []
    for heading, statements in blocks:
        if not statements:
            continue
        if heading:
            lines.append(heading)
        lines.extend(statements)
    return "\n".join(lines), kept, dropped


# ---------------------------------------------------------------- what the model is asked, per agent


def user_message(output: str, subject: str) -> str:
    if output == "verdicts":
        ids = ", ".join(signature_ids(subject)) or "none"
        return f"Review these signature ids: {ids}. Use get_signature (and get_history if needed) on each, then answer."
    if output == "proposals":
        return "Weekly review of the last 7 days. Use run_replay and list_findings, then answer with at most three changes."
    return f"Subject: {subject}" if subject else "No subject was given."


def signature_ids(subject: str) -> list[str]:
    m = re.match(r"^signatures:([0-9, ]+)$", subject.strip())
    return [i for i in re.split(r"[ ,]+", m.group(1).strip()) if i][:MAX_SIGNATURES] if m else []


def parse_list(text: str) -> list | None:
    """The first JSON list in a reply (a model may wrap it in a code fence or a sentence), or None."""
    cleaned = ai.clean(text)
    start = cleaned.find("[")
    if start < 0:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(cleaned[start:])
    except ValueError:
        return None
    return value if isinstance(value, list) else None


def _cited(item: Mapping, evidence: Mapping[str, str]) -> list[str]:
    refs = item.get("citations")
    refs = refs if isinstance(refs, list) else []
    refs = [str(r).strip("[]").removeprefix("ref:") for r in refs]
    return refs if refs and all(r in evidence for r in refs) else []


# ---------------------------------------------------------------- triage verdicts


def triage(
    store: Store, text: str, subject: str, evidence: Mapping[str, str], run_id: int, env, now: datetime
):
    """(summary lines, kept, dropped). Verdicts are stored; proposals are made by this code."""
    wanted = set(signature_ids(subject))
    items = parse_list(text) or []
    out: list[str] = []
    kept = dropped = 0
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            dropped += 1
            continue
        sig = str(item.get("sig_id", "")).strip()
        verdict = str(item.get("verdict", "")).strip().lower()
        reason = str(item.get("reason", "")).strip()[:MAX_REASON]
        refs = _cited(item, evidence)
        if sig not in wanted or sig in seen or verdict not in VERDICTS or not reason or not refs:
            dropped += 1
            continue
        seen.add(sig)
        with store.write() as tx:
            tx.db.execute(
                "INSERT INTO agent_verdict (sig_id, verdict, reason, run_id, created) VALUES (?, ?, ?, ?, ?)",
                (int(sig), verdict, reason, run_id, iso(now)),
            )
        out.append(f"signature {sig}: {verdict} - {reason} " + " ".join(f"[ref:{r}]" for r in refs))
        kept += 1
        proposal = item.get("proposal")
        if isinstance(proposal, dict):
            note = _triage_proposal(store, proposal, sig, verdict, reason, refs, evidence, run_id, env, now)
            out.append("  " + note)
    return "\n".join(out), kept, dropped


def _triage_proposal(store, proposal, sig, verdict, reason, refs, evidence, run_id, env, now) -> str:
    from brief import configedit

    kind = str(proposal.get("kind", "")).lower()
    try:
        if kind == "ignore" and verdict == "noise":
            name = f"triage_sig_{sig}"
            host = str(proposal.get("host") or "").strip()
            pattern = str(proposal.get("pattern") or "").strip()
            if not pattern or len(pattern) > 200:
                raise proposals.ProposalError("an ignore proposal needs a pattern of up to 200 characters")
            old, _ = configedit.current("ignore.yaml", env)
            entry = [f"  - name: {name}"]
            if host:
                entry.append(f"    host: {json.dumps(host)}")
            entry += [f"    match: {json.dumps(pattern)}", f"    note: {json.dumps('triage: ' + reason)}"]
            new = _append(old, "ignore", entry)
            if name not in {
                r.get("name") for r in (yaml.safe_load(new).get("ignore") or []) if isinstance(r, dict)
            }:
                raise proposals.ProposalError("the rule could not be added to ignore.yaml")
            file = "ignore.yaml"
        elif kind == "playbook" and verdict == "concern":
            advice = str(proposal.get("advice") or "").strip()
            for_kind = str(proposal.get("for_kind") or "novelty")
            bucket = str(proposal.get("bucket") or "watch")
            if for_kind not in FINDING_KINDS or bucket not in BUCKETS or not advice or len(advice) > 400:
                raise proposals.ProposalError(
                    "a playbook proposal needs a finding kind, a bucket, and short advice"
                )
            name = f"triage_sig_{sig}"
            old, _ = configedit.current("playbook.yaml", env)
            entry = [
                f"  {name}:",
                f"    kind: {for_kind}",
                f"    bucket: {bucket}",
                f"    advice: {json.dumps(advice)}",
            ]
            new = _append(old, "rules", entry)
            if name not in (yaml.safe_load(new).get("rules") or {}):
                raise proposals.ProposalError("the rule could not be added to playbook.yaml")
            file = "playbook.yaml"
        else:
            raise proposals.ProposalError(
                "an ignore proposal fits a noise verdict and a playbook proposal a concern verdict"
            )
        evidence_text = (
            f"Triage verdict for signature {sig}: {verdict}. {reason}\n"
            + "Cited: "
            + ", ".join(f"{r} ({evidence[r]})" for r in refs)
        )
        pid = proposals.create(store, "agent:triage", file, new, reason, evidence_text, run_id, env, now)
        return f"proposal #{pid} for {file} is waiting for your approval"
    except (proposals.ProposalError, yamlscalar.EditError, yaml.YAMLError) as exc:
        return f"proposal not made: {exc}"


def _append(text: str, key: str, entry: list[str]) -> str:
    """Add `entry` lines at the end of the file, which must be where the top-level `key:` block ends."""
    if not re.search(rf"^{key}:", text, re.M):
        raise proposals.ProposalError(f"the file has no {key}: block to add to")
    body = text if text.endswith("\n") else text + "\n"
    return body + "\n".join(entry) + "\n"


# ---------------------------------------------------------------- tuner proposals

TUNABLE = {
    "priorities.yaml": ("kinds", "lifecycle", "default", "grouping", "severity_boost"),
    "playbook.yaml": ("thresholds",),
}


def tuner(store: Store, systems, text: str, evidence: Mapping[str, str], run_id: int, env, now: datetime):
    """(summary lines, kept, dropped). Each valid change becomes a proposal carrying the runtime's own replay."""
    from brief import agentguards, configedit

    items = parse_list(text) or []
    out: list[str] = []
    kept = dropped = 0
    for item in items[: proposals.MAX_PER_RUN + 5]:
        if not isinstance(item, dict):
            dropped += 1
            continue
        file = str(item.get("file", ""))
        path = [p for p in str(item.get("path", "")).split(".") if p]
        reason = str(item.get("reason", "")).strip()[:MAX_REASON]
        refs = _cited(item, evidence)
        if file not in TUNABLE or not path or path[0] not in TUNABLE[file] or not reason or not refs:
            dropped += 1
            continue
        if kept >= proposals.MAX_PER_RUN:
            dropped += 1
            continue
        try:
            old, _ = configedit.current(file, env)
            new = yamlscalar.set_path(old, path, item.get("value"))
            replay = agentguards.replay_evidence(store, systems, file, old, new, env, now)
            evidence_text = (
                f"{reason}\nChange: {file} {'.'.join(path)} = {item.get('value')}\n{replay.text}\n"
                + "Cited: " + ", ".join(f"{r} ({evidence[r]})" for r in refs)
            )  # fmt: skip
            pid = proposals.create(store, "agent:tuner", file, new, reason, evidence_text, run_id, env, now)
        except (proposals.ProposalError, yamlscalar.EditError) as exc:
            dropped += 1
            out.append(f"not proposed ({file} {'.'.join(path)}): {exc}")
            continue
        kept += 1
        out.append(f"proposal #{pid}: {file} {'.'.join(path)} = {item.get('value')} - {replay.headline}")
    return "\n".join(out), kept, dropped


def finish(
    store: Store, spec, subject: str, text: str, evidence: Mapping[str, str], run_id: int, env, now: datetime,
    systems=None,
) -> tuple[str, int, int]:  # fmt: skip
    """(stored output, statements kept, statements dropped) for the reply of one run."""
    if spec.output == "verdicts":
        return triage(store, text, subject, evidence, run_id, env, now)
    if spec.output == "proposals":
        return tuner(store, systems, text, evidence, run_id, env, now)
    return check_citations(text, set(evidence))


_ = (UTC, log)
