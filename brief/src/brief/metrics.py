"""`/metrics`: Prometheus text format (read by Grafana Agent, VictoriaMetrics, and any scraper).

Built from the same status document and registry as the MQTT sensors, so a scrape and a sensor never
disagree. A failed run exports `homelab_brief_run_ok 0` and no values, which a scraper sees as a gap
rather than a calm network. Read-only; nothing here touches the database except counting open alerts.
"""

import re

from brief import registry
from brief.store import parse_time

PREFIX = "homelab_brief"
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
_BAD = re.compile(r"[^a-zA-Z0-9_:]")


def _name(metric_id: str) -> str:
    return f"{PREFIX}_{_BAD.sub('_', metric_id)}"


def _label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _number(value: int | float) -> str:
    return repr(float(value)) if isinstance(value, float) else str(value)


def _timestamp(text: str | None) -> float | None:
    try:
        return parse_time(text).timestamp() if text else None
    except (ValueError, TypeError):
        return None


def render(status: dict, metrics: list[dict], version: str, open_alerts: int | None = None) -> str:
    """The exposition text for one status document."""
    lines: list[str] = []

    def family(
        name: str, help_text: str, samples: list[tuple[str, int | float]], kind: str = "gauge"
    ) -> None:
        if not samples:
            return
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {kind}")
        lines.extend(f"{name}{labels} {_number(value)}" for labels, value in samples)

    run_ok = bool(status.get("run_ok"))
    family(f"{PREFIX}_info", "Build information.", [(f'{{version="{_label(version)}"}}', 1)])
    family(
        f"{PREFIX}_run_ok",
        "1 when the last run had enough data to say anything, else 0.",
        [("", int(run_ok))],
    )
    generated = _timestamp(status.get("generated_at"))
    if generated is not None:
        family(
            f"{PREFIX}_status_generated_timestamp_seconds", "When the status was computed.", [("", generated)]
        )
    daily = _timestamp(status.get("daily_generated_at"))
    if daily is not None:
        family(f"{PREFIX}_daily_run_timestamp_seconds", "When the last daily run finished.", [("", daily)])
    if open_alerts is not None:
        family(
            f"{PREFIX}_alerts_open", "Alerts that are open, acknowledged, or snoozed.", [("", open_alerts)]
        )
    values = registry.values(status, metrics)
    systems = []
    for metric in metrics:
        if metric["id"] not in values:
            continue
        value = values[metric["id"]]
        if metric.get("kind") == "state":
            number = registry.STATE_NUMBER.get(str(value))
            if number is not None:  # unknown has no number: it is left out, not reported as healthy
                systems.append((f'{{system="{_label(str(metric.get("system", "")))}"}}', number))
            continue
        unit = f" ({metric['unit']})" if metric.get("unit") else ""
        family(_name(metric["id"]), f"{metric.get('name', metric['id'])}{unit}.", [("", value)])
    family(
        f"{PREFIX}_system_status",
        "System state: 0 green, 1 yellow, 2 red. Unknown systems are left out.",
        systems,
    )
    return "\n".join(lines) + "\n"
