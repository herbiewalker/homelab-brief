"""Proposals: changes an agent or an outside client suggests, which do nothing until a person approves them.

The locked rules (docs/guide/10-agents.md), enforced here and not by any prompt or setting:

  - a proposal is a record (file, new text, reason, evidence). Creating one changes no config file;
  - only the files in PROPOSABLE can be proposed (never ai.yaml, agents.yaml, network.yaml, notify.yaml,
    or anything about who may sign in), and the new text must pass the same validator the pipeline uses;
  - approval is the config editor's own save: validated again, refused if the file changed since the
    proposal was made (so a person never approves a diff they did not see), the old text kept as a version
    for rollback, and written to the audit log; rejection is logged too;
  - a proposal whose text is no longer valid cannot be approved, and an old pending one expires.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from brief import configedit
from brief.store import Store, iso

PROPOSABLE = ("ignore.yaml", "priorities.yaml", "playbook.yaml")
MAX_PENDING = 20
MAX_PER_RUN = 3
MAX_CHANGED_LINES = 80
MAX_REASON = 600
MAX_EVIDENCE = 4000
EXPIRE_AFTER = timedelta(days=14)


@dataclass
class Draft:
    """A proposal about to be recorded, as the safety rules see it."""

    store: Store
    file: str
    old: str
    new: str
    source: str
    evidence: str
    env: Mapping[str, str] | None
    now: datetime


# Extra rules a proposal must pass: (Draft) -> a reason to refuse, or None. The locked safety rules for the
# triage and tuner agents live in agentguards.RULES and always run; GUARDS is for anything added beyond them.
Guard = Callable[[Draft], str | None]
GUARDS: list[Guard] = []


class ProposalError(ValueError):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Proposal:
    proposal_id: int
    created: str
    source: str
    run_id: int | None
    file: str
    base_digest: str
    new_text: str
    reason: str
    evidence: str
    status: str
    decided: str | None
    decided_by: str | None
    note: str | None


def _row(r) -> Proposal:
    return Proposal(**{k: r[k] for k in Proposal.__dataclass_fields__})


def create(
    store: Store,
    source: str,
    file: str,
    new_text: str,
    reason: str,
    evidence: str,
    run_id: int | None = None,
    env: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> int:
    """Record a proposal. Raises ProposalError if it is not allowed or not valid. Changes no config file."""
    now = now or datetime.now(UTC)
    if file not in PROPOSABLE:
        raise ProposalError(f"A proposal can only change {', '.join(PROPOSABLE)}.")
    reason, evidence = reason.strip(), evidence.strip()
    if not reason or len(reason) > MAX_REASON:
        raise ProposalError(f"Give a reason of up to {MAX_REASON} characters.")
    if not evidence or len(evidence) > MAX_EVIDENCE:
        raise ProposalError(f"Give the evidence (up to {MAX_EVIDENCE} characters).")
    new_text = configedit.normalize(new_text)
    old, _ = configedit.current(file, env)
    if new_text == old:
        raise ProposalError("That is the file as it already is: nothing to propose.")
    problem = configedit.validate_text(file, new_text)
    if problem:
        raise ProposalError(f"The new {file} is not valid: {problem}")
    added, removed = configedit.changed_lines(old, new_text)
    if added + removed > MAX_CHANGED_LINES:
        raise ProposalError(
            f"A proposal may change at most {MAX_CHANGED_LINES} lines; propose a smaller step."
        )
    from brief import agentguards

    draft = Draft(store, file, old, new_text, source, evidence, env, now)
    for guard in [*agentguards.RULES, *GUARDS]:
        refusal = guard(draft)
        if refusal:
            raise ProposalError(refusal)
    base = configedit.digest(old)
    with store.write() as tx:
        waiting = tx.db.execute(
            "SELECT file, new_text, run_id FROM proposal WHERE status = 'pending'"
        ).fetchall()
        if len(waiting) >= MAX_PENDING:
            raise ProposalError(f"There are already {MAX_PENDING} proposals waiting; decide some first.")
        if any(w["file"] == file and w["new_text"] == new_text for w in waiting):
            raise ProposalError("That exact proposal is already waiting.")
        if run_id is not None and sum(1 for w in waiting if w["run_id"] == run_id) >= MAX_PER_RUN:
            raise ProposalError(f"One run may make at most {MAX_PER_RUN} proposals.")
        cur = tx.db.execute(
            "INSERT INTO proposal (created, source, run_id, file, base_digest, new_text, reason, evidence) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (iso(now), source, run_id, file, base, new_text, reason, evidence),
        )
        pid = int(cur.lastrowid)
        tx.audit(source, "proposal.create", f"#{pid} {file}: +{added} -{removed} lines")
    return pid


def get(store: Store, proposal_id: int) -> Proposal | None:
    with store.read() as db:
        row = db.execute("SELECT * FROM proposal WHERE proposal_id = ?", (proposal_id,)).fetchone()
    return _row(row) if row else None


def listing(store: Store, status: str | None = "pending", limit: int = 50) -> list[Proposal]:
    where = "WHERE status = ? " if status else ""
    args = (status, limit) if status else (limit,)
    with store.read() as db:
        rows = db.execute(f"SELECT * FROM proposal {where}ORDER BY proposal_id DESC LIMIT ?", args).fetchall()  # noqa: S608
    return [_row(r) for r in rows]


def sweep(store: Store, now: datetime | None = None) -> int:
    """Expire pending proposals that nobody decided in two weeks. Returns how many."""
    now = now or datetime.now(UTC)
    with store.write() as tx:
        cur = tx.db.execute(
            "UPDATE proposal SET status = 'expired', decided = ? WHERE status = 'pending' AND created < ?",
            (iso(now), iso(now - EXPIRE_AFTER)),
        )
        return cur.rowcount


def view(p: Proposal, env: Mapping[str, str] | None = None) -> dict:
    """What the page shows: the proposal, the diff against the file as it is now, and whether it can still be
    approved."""
    old, _ = configedit.current(p.file, env)
    problem = configedit.validate_text(p.file, p.new_text) if p.status == "pending" else None
    stale = p.status == "pending" and configedit.digest(old) != p.base_digest
    added, removed = configedit.changed_lines(old, p.new_text)
    return {
        "p": p,
        "diff": configedit.diff_lines(old, p.new_text),
        "added": added,
        "removed": removed,
        "stale": stale,
        "problem": problem,
        "approvable": p.status == "pending" and not stale and problem is None,
    }


def _decide(store: Store, p: Proposal, status: str, user: str, note: str, now: datetime) -> None:
    with store.write() as tx:
        tx.db.execute(
            "UPDATE proposal SET status = ?, decided = ?, decided_by = ?, note = ? "
            "WHERE proposal_id = ? AND status = 'pending'",
            (status, iso(now), user, note[:300] or None, p.proposal_id),
        )
        tx.audit(user, f"proposal.{status}", f"#{p.proposal_id} {p.file} from {p.source}")


def approve(
    store: Store,
    proposal_id: int,
    user: str,
    state_dir: Path | str,
    env: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> str:
    """Apply a pending proposal through the config editor. Raises ProposalError if it cannot be approved."""
    now = now or datetime.now(UTC)
    p = get(store, proposal_id)
    if p is None:
        raise ProposalError("There is no such proposal.", 404)
    if p.status != "pending":
        raise ProposalError(f"That proposal is already {p.status}.", 409)
    info = view(p, env)
    if info["problem"]:
        _decide(store, p, "invalid", user, info["problem"], now)
        raise ProposalError(f"It is no longer valid and cannot be approved: {info['problem']}", 409)
    if info["stale"]:
        _decide(store, p, "stale", user, "the file changed after the proposal was made", now)
        raise ProposalError(
            "The file changed after this was proposed, so it was closed. Ask for a new one.", 409
        )
    try:
        message = configedit.save(
            store, p.file, p.new_text, user, p.base_digest, state_dir, env, action="config.proposal", now=now
        )
    except configedit.EditError as exc:
        _decide(store, p, "invalid", user, str(exc), now)
        raise ProposalError(str(exc), exc.status) from None
    _decide(store, p, "approved", user, "", now)
    return message


def reject(store: Store, proposal_id: int, user: str, note: str = "", now: datetime | None = None) -> None:
    p = get(store, proposal_id)
    if p is None:
        raise ProposalError("There is no such proposal.", 404)
    if p.status != "pending":
        raise ProposalError(f"That proposal is already {p.status}.", 409)
    _decide(store, p, "rejected", user, note, now or datetime.now(UTC))


def counts(store: Store) -> dict[str, int]:
    with store.read() as db:
        return {
            r["status"]: r["n"] for r in db.execute("SELECT status, COUNT(*) n FROM proposal GROUP BY status")
        }
