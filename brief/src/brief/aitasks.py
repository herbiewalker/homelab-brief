"""The other AI tasks: explain one finding, and put the weekly report into words.

Both go through the same chain runner as the morning brief (ai.run_chain): a chain from ai.yaml that ends in
the template, a privacy scan before any cloud call, a check of the reply against what was sent, a daily
call log without content. A finding about one household device (new_device, traffic_spike, odd_hours) has no
cloud input at all. Nothing here changes a finding: a model only explains one that rules already raised.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from brief import ai, detect, playbook
from brief.aiguard import DEVICE_KINDS, known_names, private_terms
from brief.detectors import Finding
from brief.store import Store
from brief.systems import Systems

MAX_WORDS = 150
MAX_PARAGRAPHS = 4
HIDDEN_DETAIL = {"advice", "rule", "bucket"}


@dataclass
class Explanation:
    text: str
    provider: str
    model: str


@dataclass
class Summary:
    title: str
    paragraphs: list[str]
    provider: str
    model: str


# ---------------------------------------------------------------- explain a finding


def find_finding(store: Store, systems: Systems, finding_id: str, now: datetime, env=None) -> Finding | None:
    """The finding with this id as the detectors report it right now (with its playbook advice), or None."""
    found = detect.run(store, systems, now, env, ignore_learning=True)["findings"]
    for finding in playbook.apply(found, playbook.load(env)):
        if finding.id == finding_id:
            return finding
    return None


def _samples(store: Store, finding: Finding, limit: int = 3) -> list[str]:
    if finding.sig_id is None:
        return []
    with store.read() as db:
        rows = db.execute(
            "SELECT ts, host, msg FROM sample WHERE sig_id = ? ORDER BY ts DESC LIMIT ?",
            (finding.sig_id, limit),
        ).fetchall()
    return [f"{r['ts']} {r['host']}: {r['msg']}" for r in rows]


def lan_finding_text(store: Store, finding: Finding) -> str:
    lines = [f"Finding: {finding.title}", f"Kind: {finding.kind}"]
    if finding.system:
        lines.append(f"System: {finding.system}")
    if finding.detail.get("bucket"):
        lines.append(f"Priority bucket: {finding.detail['bucket']}")
    if finding.detail.get("advice"):
        lines.append(f"Recommended next step (from the playbook): {finding.detail['advice']}")
    details = {k: v for k, v in finding.detail.items() if k not in HIDDEN_DETAIL and v is not None}
    lines += [f"- {k}: {v}" for k, v in sorted(details.items())]
    samples = _samples(store, finding)
    if samples:
        lines += ["", "A few recent log lines:", *[f"  {s}" for s in samples]]
    return "\n".join(lines)


def cloud_finding_text(finding: Finding, env: Mapping[str, str] | None = None) -> str | None:
    """Only the kind, the bucket, and the playbook rule's own wording, placeholders unfilled. None for a
    finding about one household device."""
    if finding.kind in DEVICE_KINDS:
        return None
    lines = [f"Finding kind: {finding.kind}"]
    if finding.detail.get("bucket"):
        lines.append(f"Priority bucket: {finding.detail['bucket']}")
    rule = next((r for r in playbook.load(env) if r.id == finding.detail.get("rule")), None)
    if rule is not None:
        lines.append(f"Generic recommendation: {rule.advice}")
    return "\n".join(lines)


def check_explanation(reply: str, sent: str, names: set[str], mode: str) -> str:
    text = ai.clean(reply)
    if not text:
        raise ai.ProviderError("invalid", "empty reply")
    if len(text.split()) > MAX_WORDS * 2:
        raise ai.ProviderError("invalid", "far longer than asked")
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    kept = []
    for paragraph in paragraphs:
        bad = ai.unknown_terms(paragraph, sent, names)
        if bad and mode == "strict":
            raise ai.ProviderError("invalid", f"names something not in the finding: {sorted(bad)[0]}")
        if not bad:
            kept.append(paragraph)
    if not kept:
        raise ai.ProviderError("invalid", "nothing left after dropping unknown names")
    return "\n\n".join(kept)


def explain_finding(
    store: Store,
    finding: Finding,
    now: datetime | None = None,
    env: Mapping[str, str] | None = None,
    settings: ai.Settings | None = None,
    post: ai.Transport = ai._post,
) -> Explanation | None:
    """A plain-language explanation of one finding, or None (nothing is shown beyond the finding itself)."""
    task = "explain_finding"
    now = now or datetime.now(UTC)
    try:
        settings = settings or ai.load_settings(env)
        if not ai.feature_on(settings, task):
            return None
        system = ai.system_prompt(task, settings.tasks.get(task) or {}, env, max_words=str(MAX_WORDS))
        if system is None:
            return None
        names = known_names(store, env, [finding.id]) | ai.known_hosts(store)
        lan = lan_finding_text(store, finding)

        def fit(p: ai.Provider, prompt_chars: int) -> str:
            room = max((p.context_tokens - p.max_output_tokens) * ai.CHARS_PER_TOKEN - prompt_chars, 1000)
            return lan[:room]

        job = ai.Job(
            task, system, fit,
            cloud=lambda: cloud_finding_text(finding, env),
            check=lambda reply, sent: check_explanation(reply, sent, names, settings.validation),
            private=private_terms(store, env),
        )  # fmt: skip
        found = ai.run_chain(store, job, settings, now, post)
    except Exception as exc:  # an explanation is never worth an error page
        ai.log("ai_failed", task=task, error=f"{type(exc).__name__}: {exc}")
        return None
    if found is None:
        return None
    text, provider = found
    return Explanation(str(text), provider.id, provider.model)


# ---------------------------------------------------------------- the weekly summary

SECTION = re.compile(r"^## ")
CLOUD_SECTIONS = ("## Volume by system", "## Power and energy")


def cloud_weekly_text(lines: list[str]) -> str:
    """The system-level tables of the weekly report only: volume by system, and power and energy."""
    keep: list[str] = []
    inside = False
    for line in lines:
        if SECTION.match(line):
            inside = line.startswith(CLOUD_SECTIONS)
        if inside:
            keep.append(line)
    return "\n".join(keep)


def weekly_summary(
    store: Store,
    report: list[str],
    now: datetime | None = None,
    env: Mapping[str, str] | None = None,
    settings: ai.Settings | None = None,
    post: ai.Transport = ai._post,
) -> Summary | None:
    """The weekly report (its Markdown lines) as a short prose summary, or None. Never raises."""
    task = "weekly_summary"
    now = now or datetime.now(UTC)
    try:
        settings = settings or ai.load_settings(env)
        if not ai.feature_on(settings, task):
            return None
        system = ai.system_prompt(
            task, settings.tasks.get(task) or {}, env, max_paragraphs=str(MAX_PARAGRAPHS)
        )
        if system is None:
            return None
        names = known_names(store, env) | ai.known_hosts(store)
        full = "\n".join(report)

        def fit(p: ai.Provider, prompt_chars: int) -> str:
            room = max((p.context_tokens - p.max_output_tokens) * ai.CHARS_PER_TOKEN - prompt_chars, 1000)
            return full[:room]

        def check(reply: str, sent: str) -> tuple[str, list[str]]:
            return ai.parse(reply, sent, names, settings.validation)

        job = ai.Job(
            task, system, fit,
            cloud=lambda: cloud_weekly_text(report) or None,
            check=check,
            private=private_terms(store, env),
        )  # fmt: skip
        found = ai.run_chain(store, job, settings, now, post)
    except Exception as exc:
        ai.log("ai_failed", task=task, error=f"{type(exc).__name__}: {exc}")
        return None
    if found is None:
        return None
    (title, paragraphs), provider = found
    return Summary(title, paragraphs[:MAX_PARAGRAPHS], provider.id, provider.model)


def summary_markdown(summary: Summary, report_name: str) -> list[str]:
    return [
        f"# {summary.title}",
        "",
        *[line for p in summary.paragraphs for line in (p, "")],
        f"Written by {summary.provider} ({summary.model}) from {report_name}; the report is the source.",
    ]
