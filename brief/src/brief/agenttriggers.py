"""What starts an agent without a person asking (docs/guide/10-agents.md): only a queue entry, never a call.

After the daily run the worker asks `after_daily` what is worth looking into; after the weekly report it asks
`after_weekly`. Each puts at most a few jobs on the agent queue (`agentrun.enqueue`), which the worker works
through one at a time when the rules allow. Nothing here runs a model, and nothing is queued unless AI and the
agents feature are on and the agent itself is enabled, so a switched-off install never builds up a backlog.
"""

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

from brief import agentrun, ai
from brief.routing import Priorities
from brief.store import Store, iso

MAX_INVESTIGATIONS = 3  # per daily run
RECENT = timedelta(days=3)  # a story already investigated this recently is not looked at again


def _agent(env, name: str):
    settings = ai.load_settings(env)
    spec = agentrun.load(env).get(name)
    return spec if spec is not None and spec.enabled and ai.feature_on(settings, "agents") else None


def _investigated(store: Store, subject: str, now: datetime) -> bool:
    with store.read() as db:
        return (
            db.execute(
                "SELECT 1 FROM agent_run WHERE agent = 'investigator' AND subject = ? AND started >= ? "
                "AND outcome NOT IN ('off', 'skipped') LIMIT 1",
                (subject, iso(now - RECENT)),
            ).fetchone()
            is not None
        )


def after_daily(store: Store, status: Mapping, now: datetime | None = None, env=None) -> list[str]:
    """Queue investigations for the stories ranked high enough, and triage for new signatures."""
    now = now or datetime.now(UTC)
    queued: list[str] = []
    if not status.get("run_ok"):
        return queued
    spec = _agent(env, "investigator")
    threshold = (spec.trigger or {}).get("on_finding_rank_at_least") if spec else None
    if spec and isinstance(threshold, int):
        priorities = Priorities.load(env)
        for story in status.get("stories") or []:
            kind = story.get("kind") or ""
            rank = priorities.kinds.get(kind, priorities.default).rank
            sid = story.get("id")
            if len(queued) >= MAX_INVESTIGATIONS:
                break
            if isinstance(sid, str) and rank >= threshold and not _investigated(store, sid, now):
                if agentrun.enqueue(store, spec.name, sid, "finding"):
                    queued.append(f"investigator:{sid}")
    triage = _agent(env, "triage")
    learning = (status.get("learning") or {}).get("active")
    if (
        triage
        and (triage.trigger or {}).get("on_new_signatures")
        and not (learning and (triage.trigger or {}).get("after_learning_period", True))
    ):
        ids = new_signatures(store, now)
        if ids and agentrun.enqueue(
            store, triage.name, "signatures:" + ",".join(map(str, ids)), "new_signatures"
        ):
            queued.append(f"triage:{len(ids)}")
    return queued


def new_signatures(store: Store, now: datetime, limit: int = 5) -> list[int]:
    """The busiest signatures that first appeared in the last day, are not ignored, and have no verdict yet."""
    with store.read() as db:
        rows = db.execute(
            "SELECT s.sig_id FROM signature s LEFT JOIN hourly h ON h.sig_id = s.sig_id AND h.day >= ? "
            "WHERE s.first_seen >= ? AND s.ignored = 0 "
            "AND s.sig_id NOT IN (SELECT sig_id FROM agent_verdict) "
            "GROUP BY s.sig_id ORDER BY COALESCE(SUM(h.n), 0) DESC, s.sig_id LIMIT ?",
            ((now - timedelta(days=1)).date().isoformat(), iso(now - timedelta(days=1)), limit),
        ).fetchall()
    return [r["sig_id"] for r in rows]


def after_weekly(store: Store, now: datetime | None = None, env=None) -> list[str]:
    """Queue the weekly tuner run."""
    spec = _agent(env, "tuner")
    if spec and (spec.trigger or {}).get("schedule") == "weekly":
        if agentrun.enqueue(store, spec.name, "weekly review", "schedule"):
            return ["tuner"]
    return []
