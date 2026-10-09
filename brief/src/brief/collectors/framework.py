"""Loads collectors.yaml, schedules the enabled collectors, and keeps one failure from touching anything else.

What the framework does for every collector, so a collector module only fetches data:

  load / validate   collectors.yaml is checked like every other config file (the config guard calls
                    `validate`); a credential written into the file instead of a `*_var` name is refused
  metrics           each enabled collector adds its registry entries (and a `collector_<name>_ok` entry),
                    so switching one on or off adds or removes its sensors, `/metrics` lines and API
                    values with no other change
  run_due           called once an hour by the worker: runs the collectors whose interval has passed,
                    each cut off after its own timeout, each isolated (any exception is a failed run,
                    never a crash), and records the outcome
  state             a collector's last outcome and last good numbers live in the `meta` table
                    (`collector:<name>`); its numeric history goes to the `metric` table as
                    `collector.<metric id>`, which is what the metric_drift detector reads
  failure           a failed run keeps the previous numbers, reports `ok: false` with a safe reason
                    (never an address or a secret), and is tried again at the next interval
  test_connection   the "Test connection" button and `brief collector test`: works on a disabled
                    collector too, stores nothing
"""

import json
import os
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from brief.collectors.base import Collector, CollectorError, Context
from brief.config import ConfigError, config_file, load_mapping
from brief.jsonlog import log
from brief.store import Store, iso, parse_time

REGISTRY: dict[str, type[Collector]] = {}
PLANNED = ("unraid", "unifi", "snmp", "idrac", "ha_statestream", "dns_filter")  # named in collectors.yaml

META_PREFIX = "collector:"
METRIC_PREFIX = "collector."
SLACK = timedelta(minutes=5)  # the hourly job never starts exactly on the minute
DEFAULT_TIMEOUT_S = 30.0
MAX_TIMEOUT_S = 300.0
MAX_ERROR_CHARS = 160
SECRET_KEYS = {"key", "api_key", "apikey", "token", "password", "passphrase", "secret", "community"}
VAR_NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
INTERVAL = re.compile(r"^(\d{1,3})h$")
NAMED_INTERVALS = {"hourly": timedelta(hours=1), "daily": timedelta(days=1), "weekly": timedelta(days=7)}

_hung: dict[str, threading.Thread] = {}  # a run that outlived its timeout, until it finally ends


def register(cls: type[Collector]) -> type[Collector]:
    """Class decorator: make a collector available under its `name`."""
    if not cls.name or not re.fullmatch(r"[a-z][a-z0-9_]*", cls.name):
        raise ValueError(f"collector names are lowercase words with underscores, got {cls.name!r}")
    REGISTRY[cls.name] = cls
    return cls


@dataclass(frozen=True)
class Settings:
    name: str
    enabled: bool
    interval: timedelta
    timeout_s: float
    config: dict = field(default_factory=dict)


# ---------------------------------------------------------------- loading and validation


def parse_interval(value) -> timedelta:
    text = str(value).strip().lower()
    if text in NAMED_INTERVALS:
        return NAMED_INTERVALS[text]
    match = INTERVAL.match(text)
    if match and 1 <= int(match.group(1)) <= 168:
        return timedelta(hours=int(match.group(1)))
    raise ConfigError(f"interval must be hourly, daily, weekly, or a number of hours like 6h, got {value!r}")


def _check_keys(value, where: str) -> None:
    """No credential in the file, and every *_var names a template variable."""
    if isinstance(value, dict):
        for key, item in value.items():
            low = str(key).lower()
            if low in SECRET_KEYS:
                raise ConfigError(
                    f"collectors.yaml: {where}.{key} would hold a credential; "
                    f"name a template variable instead (for example {low}_var: MY_VARIABLE)"
                )
            if low.endswith("_var") and item not in (None, ""):
                if not isinstance(item, str) or not VAR_NAME.match(item):
                    raise ConfigError(
                        f"collectors.yaml: {where}.{key} must be a variable name like UNIFI_API_KEY"
                    )
            _check_keys(item, f"{where}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _check_keys(item, f"{where}[{index}]")


def parse(data: Mapping) -> dict[str, Settings]:
    """collectors.yaml (already loaded) -> settings per collector, or ConfigError."""
    block = data.get("collectors") or {}
    if not isinstance(block, dict):
        raise ConfigError("collectors.yaml: collectors must be a mapping of name to settings")
    known = sorted({*REGISTRY, *PLANNED})
    out: dict[str, Settings] = {}
    for name, entry in block.items():
        name = str(name)
        if name not in known:
            raise ConfigError(f"collectors.yaml: unknown collector {name!r} (known: {', '.join(known)})")
        if not isinstance(entry, dict):
            raise ConfigError(f"collectors.yaml: {name} must be a mapping")
        enabled = entry.get("enabled", False)
        if not isinstance(enabled, bool):
            raise ConfigError(f"collectors.yaml: {name}.enabled must be true or false")
        try:
            timeout = float(entry.get("timeout_s", DEFAULT_TIMEOUT_S))
        except (TypeError, ValueError):
            raise ConfigError(f"collectors.yaml: {name}.timeout_s must be a number") from None
        if not 1 <= timeout <= MAX_TIMEOUT_S:
            raise ConfigError(f"collectors.yaml: {name}.timeout_s must be between 1 and {MAX_TIMEOUT_S:g}")
        try:
            interval = parse_interval(entry.get("interval", "hourly"))
        except ConfigError as exc:
            raise ConfigError(f"collectors.yaml: {name}.{exc}") from None
        _check_keys(entry, name)
        out[name] = Settings(name, enabled, interval, timeout, dict(entry))
    for settings in out.values():
        cls = REGISTRY.get(settings.name)
        if cls is not None:
            try:
                cls().validate(settings.config)
            except ConfigError as exc:
                raise ConfigError(f"collectors.yaml: {settings.name}: {exc}") from None
    return out


def load(env: Mapping[str, str] | None = None) -> dict[str, Settings]:
    return parse(load_mapping(config_file("collectors.yaml", env)))


def validate(path) -> dict[str, Settings]:
    """For the config guard and the editor: raises ConfigError when the file cannot be used."""
    return parse(load_mapping(path))


def enabled(env: Mapping[str, str] | None = None) -> list[Settings]:
    return [s for s in load(env).values() if s.enabled]


def required_vars(config: Mapping) -> list[str]:
    """Every template variable the settings name (all *_var values, at any depth), sorted."""
    found: set[str] = set()

    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key).lower().endswith("_var") and isinstance(item, str) and item:
                    found.add(item)
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(config)
    return sorted(found)


# ---------------------------------------------------------------- metrics for the registry


def ok_metric(name: str) -> dict:
    return {
        "id": f"collector_{name}_ok",
        "name": f"{name} collector OK",
        "group": "collectors",
        "kind": "number",
        "interval": "hourly",
        "collector": name,
        "field": "ok",
    }


def metric_entries(env: Mapping[str, str] | None = None) -> list[dict]:
    """Registry entries for every enabled collector. A broken collectors.yaml means none (the config guard
    reports it), never a failure to publish the rest."""
    try:
        active = enabled(env)
    except (ConfigError, OSError):
        return []
    entries: list[dict] = []
    for settings in active:
        entries.append(ok_metric(settings.name))
        cls = REGISTRY.get(settings.name)
        if cls is None:
            continue
        try:
            declared = cls().metrics(settings.config)
        except Exception as exc:  # a collector bug must not stop the registry loading
            log("collector_metrics_failed", collector=settings.name, error=type(exc).__name__)
            continue
        for metric in declared:
            if not str(metric.get("id", "")).startswith(f"{settings.name}_"):
                log("collector_metric_skipped", collector=settings.name, metric=str(metric.get("id")))
                continue
            entries.append(
                {"kind": "number", "group": settings.name, "interval": "hourly", **metric}
                | {"collector": settings.name, "field": metric["id"]}
            )
    return entries


# ---------------------------------------------------------------- state


def read_state(store: Store) -> dict[str, dict]:
    with store.read() as db:
        rows = db.execute("SELECT key, value FROM meta WHERE key LIKE ?", (f"{META_PREFIX}%",)).fetchall()
    out = {}
    for row in rows:
        try:
            out[row["key"][len(META_PREFIX) :]] = json.loads(row["value"])
        except ValueError:
            continue
    return out


def status_block(store: Store, env: Mapping[str, str] | None = None) -> dict[str, dict]:
    """What status.json carries per enabled collector: ok, last run, why it failed, last good numbers."""
    try:
        names = {s.name for s in enabled(env)}
    except (ConfigError, OSError):
        return {}
    state = read_state(store)
    block = {}
    for name in sorted(names):
        entry = state.get(name)
        block[name] = (
            {
                "ok": bool(entry.get("ok")),
                "last_run": entry.get("last_run"),
                "last_ok": entry.get("last_ok"),
                "error": entry.get("error"),
                "metrics": entry.get("metrics") or {},
            }
            if entry
            else {"ok": False, "last_run": None, "last_ok": None, "error": "has not run yet", "metrics": {}}
        )
    return block


def is_due(settings: Settings, state: dict | None, now: datetime) -> bool:
    if not state or not state.get("last_run"):
        return True
    return now - parse_time(state["last_run"]) >= settings.interval - SLACK


# ---------------------------------------------------------------- running


def _in_thread(name: str, fn, timeout: float):
    """Run fn() in a daemon thread. Returns its value, or raises its exception, or TimeoutError."""
    box: dict = {}

    def target():
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - handed back to the caller below
            box["error"] = exc

    thread = threading.Thread(target=target, name=f"collector-{name}", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        _hung[name] = thread
        raise TimeoutError(f"timed out after {timeout:g} seconds")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _safe_reason(exc: BaseException) -> str:
    if isinstance(exc, CollectorError):
        return str(exc)[:MAX_ERROR_CHARS] or "the collector reported a problem"
    if isinstance(exc, TimeoutError):
        return str(exc)
    return f"unexpected error ({type(exc).__name__})"


def _check_vars(settings: Settings, env: Mapping[str, str]) -> None:
    missing = [v for v in required_vars(settings.config) if not (env.get(v) or "").strip()]
    if missing:
        raise CollectorError("these variables are not set: " + ", ".join(missing))


def _declared_ids(cls: type[Collector], config: dict) -> set[str]:
    return {m["id"] for m in cls().metrics(config) if m.get("id")}


def _execute(settings: Settings, store: Store, env: Mapping[str, str], now: datetime) -> dict[str, float]:
    """One run of one collector; returns the numbers it declared. Raises on any failure."""
    cls = REGISTRY.get(settings.name)
    if cls is None:
        raise CollectorError("this collector is not available in this version")
    running = _hung.get(settings.name)
    if running is not None:
        if running.is_alive():
            raise CollectorError("the previous run is still hanging")
        _hung.pop(settings.name, None)
    _check_vars(settings, env)
    collector = cls()
    ctx = Context(settings.name, settings.config, env, store, now)
    result = _in_thread(settings.name, lambda: collector.run(ctx), settings.timeout_s)
    if not isinstance(result, Mapping):
        raise CollectorError("the collector returned no numbers")
    declared = _declared_ids(cls, settings.config)
    return {
        k: v
        for k, v in result.items()
        if k in declared and isinstance(v, (int, float)) and not isinstance(v, bool)
    }


def run_due(
    store: Store,
    env: Mapping[str, str] | None = None,
    now: datetime | None = None,
    only: str | None = None,
    force: bool = False,
) -> dict[str, dict]:
    """Run the enabled collectors that are due (`only` limits it to one name; `force` ignores the interval).
    Returns {name: {"ran", "ok", "error"}}. Never raises."""
    env = os.environ if env is None else env
    now = now or datetime.now(UTC)
    outcome: dict[str, dict] = {}
    try:
        active = [s for s in enabled(env) if only in (None, s.name)]
        state = read_state(store)
    except Exception as exc:
        log("collectors_failed", error=type(exc).__name__)
        return outcome
    for settings in active:
        previous = state.get(settings.name)
        if not force and not is_due(settings, previous, now):
            outcome[settings.name] = {"ran": False}
            continue
        started = datetime.now(UTC)
        try:
            numbers = _execute(settings, store, env, now)
            error = None
        except BaseException as exc:  # noqa: BLE001 - a collector must never take the worker down
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            numbers, error = None, _safe_reason(exc)
            log(
                "collector_failed",
                collector=settings.name,
                error=error,
                detail=f"{type(exc).__name__}: {exc}",
            )
        seconds = round((datetime.now(UTC) - started).total_seconds(), 2)
        entry = {
            "last_run": iso(now),
            "ok": error is None,
            "error": error,
            "seconds": seconds,
            "last_ok": iso(now) if error is None else (previous or {}).get("last_ok"),
            "metrics": numbers if error is None else (previous or {}).get("metrics") or {},
        }
        try:
            with store.write() as tx:
                tx.set_meta(f"{META_PREFIX}{settings.name}", json.dumps(entry))
                if numbers:
                    tx.add_metrics([(now, f"{METRIC_PREFIX}{k}", float(v)) for k, v in numbers.items()])
        except Exception as exc:  # the outcome could not be saved; the next hour tries again
            log("collector_state_failed", collector=settings.name, error=type(exc).__name__)
        if error is None:
            log("collector_ok", collector=settings.name, metrics=len(numbers or {}), seconds=seconds)
        outcome[settings.name] = {"ran": True, "ok": error is None, "error": error}
    return outcome


def test_connection(
    name: str, store: Store, env: Mapping[str, str] | None = None, now: datetime | None = None
) -> tuple[bool, str]:
    """(ok, message) for one collector, enabled or not. Stores nothing and never raises."""
    env = os.environ if env is None else env
    now = now or datetime.now(UTC)
    try:
        settings = load(env).get(name)
    except (ConfigError, OSError) as exc:
        return False, f"collectors.yaml cannot be used: {exc}"
    if settings is None:
        return False, f"{name} is not in collectors.yaml"
    cls = REGISTRY.get(name)
    if cls is None:
        return False, "this collector is not available in this version"
    try:
        _check_vars(settings, env)
        ctx = Context(name, settings.config, env, store, now)
        message = _in_thread(name, lambda: cls().test(ctx), settings.timeout_s)
    except BaseException as exc:  # noqa: BLE001
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        log("collector_test_failed", collector=name, detail=f"{type(exc).__name__}: {exc}")
        return False, _safe_reason(exc)
    return True, str(message or "the connection works")
