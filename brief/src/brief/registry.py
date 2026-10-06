"""The metric registry: what the product measures, and the values of one run.

brief/registry.yaml lists the metrics (numbers); a state sensor for every system in systems.yaml is
added automatically, so adding a system adds its sensor with no change here. The MQTT publisher, the
`/metrics` endpoint, and `/api/v1/metrics` all read their values from `values()`, which takes the one
status document, so the three can never disagree. A failed run has no values: sensors then expire and
a scraper sees `run_ok 0` and nothing else, instead of a calm network.
"""

from collections.abc import Mapping
from pathlib import Path

from brief.config import ConfigError, config_file, load_mapping
from brief.systems import Systems

STATES = ("green", "yellow", "red", "unknown")
STATE_NUMBER = {"green": 0, "yellow": 1, "red": 2}  # unknown has no number: it is simply not exported
SYSTEM_PREFIX = "system_"


def read_file(env: Mapping[str, str] | None = None) -> list[dict]:
    """The entries of registry.yaml (every kind), checked for the basics."""
    path: Path = config_file("registry.yaml", env)
    metrics = load_mapping(path).get("metrics") or []
    if not isinstance(metrics, list):
        raise ConfigError(f"{path.name}: metrics must be a list")
    for item in metrics:
        if not isinstance(item, dict) or not item.get("id"):
            raise ConfigError(f"{path.name}: every metric needs an id")
    return metrics


def system_metrics(systems: Systems) -> list[dict]:
    return [
        {
            "id": f"{SYSTEM_PREFIX}{s.id}",
            "name": f"{s.label} status",
            "group": "systems",
            "kind": "state",
            "interval": "hourly",
            "system": s.id,
        }
        for s in systems.systems
    ]


def load(env: Mapping[str, str] | None = None, systems: Systems | None = None) -> list[dict]:
    """Number and state metrics from the file, plus one state sensor per system."""
    entries = [m for m in read_file(env) if m.get("kind", "number") in ("number", "state")]
    known = {m["id"] for m in entries}
    return entries + [m for m in system_metrics(systems or Systems()) if m["id"] not in known]


def values(status: Mapping, metrics: list[dict]) -> dict[str, int | float | str]:
    """metric id -> value for this run. Empty when the run failed. A metric with no value is left out."""
    if not status.get("run_ok"):
        return {}
    pipeline = status.get("pipeline") or {}
    states = {s["id"]: s["state"] for s in status.get("systems") or []}
    out: dict[str, int | float | str] = {}
    for metric in metrics:
        if metric.get("kind", "number") == "state":
            state = states.get(metric.get("system", ""))
            if state in STATES:
                out[metric["id"]] = state
        else:
            value = pipeline.get(metric["id"])
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                out[metric["id"]] = value
    return out
