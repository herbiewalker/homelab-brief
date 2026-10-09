"""Config reload every run, with validation and a last-good fallback.

Each cycle the worker asks the guard to check every watched file in /config/brief. A file that loads is
copied to /state/config-last-good/ (so there is always a known-good copy). A file that does not load is
replaced, for every part of the product at once, by its last good copy (or the shipped default when there
never was one), the pipeline keeps running, and the problem becomes a `config_error` finding until the
file is fixed. The errors are also written to /state/config_errors.json, so the daily run, the hourly
alerts, the CLI, and the web server all see the same list.
"""

import json
import os
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path

from brief import aiconfig, detect, netconfig, playbook, privateconf, selfmon
from brief.collectors import framework
from brief.config import ConfigError, default_file, load_mapping, set_fallback, user_file
from brief.detectors import Finding
from brief.ignore import IgnoreRules
from brief.jsonlog import log
from brief.routing import Priorities
from brief.schedule import Schedule
from brief.systems import Systems

KIND = "config_error"
ERRORS_FILE = "config_errors.json"
LAST_GOOD = "config-last-good"
MAX_ERROR_CHARS = 240


def _mapping(path: Path) -> dict:
    return load_mapping(path)


def _registry(path: Path) -> None:
    metrics = load_mapping(path).get("metrics") or []
    if not isinstance(metrics, list) or not all(isinstance(m, dict) and m.get("id") for m in metrics):
        raise ConfigError(f"{path.name}: metrics must be a list and every metric needs an id")


def _priorities(path: Path) -> None:
    Priorities(load_mapping(path))


def _playbook(path: Path) -> None:
    detect.validate_thresholds(load_mapping(path).get("thresholds"))
    playbook.validate(path)


def _map(path: Path) -> None:
    data = load_mapping(path)
    for key, kind in (("enabled", bool), ("refresh_s", int), ("show_containers", bool), ("show_vms", bool)):
        value = data.get(key)
        if key in data and not (isinstance(value, kind) and (kind is bool or not isinstance(value, bool))):
            raise ConfigError(
                f"{path.name}: {key} must be {'true or false' if kind is bool else 'a whole number'}"
            )
    layers = data.get("layers", [])
    if not isinstance(layers, list) or not all(isinstance(x, str) for x in layers):
        raise ConfigError(f"{path.name}: layers must be a list of names")


def _agents(path: Path) -> None:
    data = load_mapping(path)
    agents = data.get("agents") or {}
    if not isinstance(agents, dict) or not all(isinstance(a, dict) for a in agents.values()):
        raise ConfigError(f"{path.name}: agents must be a mapping of name to settings")
    for name, agent in agents.items():
        if "enabled" in agent and not isinstance(agent["enabled"], bool):
            raise ConfigError(f"{path.name}: agents.{name}.enabled must be true or false")
    from brief import agentrun

    agentrun.parse(data)  # tool names, limits, and the locked citation rule
    if not isinstance(data.get("defaults") or {}, dict):
        raise ConfigError(f"{path.name}: defaults must be a mapping")


# file name -> validator(path) that raises on a problem. Anything not listed here is not watched.
VALIDATORS: dict[str, Callable[[Path], object]] = {
    "systems.yaml": Systems.load,
    "ignore.yaml": IgnoreRules.load,
    "priorities.yaml": _priorities,
    "playbook.yaml": _playbook,
    "schedule.yaml": lambda p: Schedule.from_mapping(load_mapping(p)),
    "registry.yaml": _registry,
    "notify.yaml": _mapping,
    "views.yaml": _mapping,
    "ai.yaml": aiconfig.validate,
    "network.yaml": lambda p: netconfig.validate(load_mapping(p)),
    "retention.yaml": _mapping,
    "devices.yaml": lambda p: privateconf.validate_devices(load_mapping(p)),
    "topology.yaml": lambda p: privateconf.validate_topology(load_mapping(p)),
    "collectors.yaml": framework.validate,
    "map.yaml": _map,
    "agents.yaml": _agents,
}


class ConfigGuard:
    def __init__(self, state_dir: Path | str, env: Mapping[str, str] | None = None):
        self.state = Path(state_dir)
        self.env = os.environ if env is None else env
        self.errors: dict[str, dict] | None = None  # None until the first check: then what the file says

    def check(self) -> dict[str, dict]:
        """Validate every watched file once. Returns {file: {"error", "using"}} for the broken ones."""
        found: dict[str, dict] = {}
        # a guard made fresh (the web server after an edit) still knows what an earlier check found
        before = self.errors if self.errors is not None else read_errors(self.state)
        for name, validate in VALIDATORS.items():
            user = user_file(name, self.env)
            if not user.exists():
                set_fallback(user, None)
                continue
            try:
                validate(user)
            except Exception as exc:  # any failure to load is a config error, never a crash
                found[name] = self._fall_back(name, user, exc)
            else:
                set_fallback(user, None)
                self._remember(name, user)
        for name, issue in found.items():
            if before.get(name, {}).get("error") != issue["error"]:  # log a change, not every cycle
                log("config_error", file=name, error=issue["error"], kept=issue["using"])
        for name in set(before) - set(found):
            log("config_recovered", file=name)
        if found != before or not (self.state / ERRORS_FILE).exists():
            self._write(found)
        self.errors = found
        return found

    def _fall_back(self, name: str, user: Path, exc: Exception) -> dict:
        good = self.state / LAST_GOOD / name
        if good.exists():
            set_fallback(user, good)
            using = "the last good copy"
        else:
            set_fallback(user, default_file(name, self.env))
            using = "the built-in default"
        text = " ".join(str(exc).split()) or type(exc).__name__
        return {"error": text[:MAX_ERROR_CHARS], "using": using}

    def _remember(self, name: str, user: Path) -> None:
        good = self.state / LAST_GOOD / name
        try:
            if not good.exists() or good.read_bytes() != user.read_bytes():
                good.parent.mkdir(parents=True, exist_ok=True)
                tmp = good.with_name(good.name + ".tmp")
                shutil.copyfile(user, tmp)
                os.replace(tmp, good)
        except OSError as exc:  # a full disk must not turn into a config error
            log("config_backup_failed", file=name, error=type(exc).__name__)

    def _write(self, found: dict[str, dict]) -> None:
        try:
            self.state.mkdir(parents=True, exist_ok=True)
            tmp = self.state / (ERRORS_FILE + ".tmp")
            tmp.write_text(json.dumps({"errors": found}, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, self.state / ERRORS_FILE)
        except OSError as exc:
            log("config_errors_unwritable", error=type(exc).__name__)


def read_errors(state_dir: Path | str) -> dict[str, dict]:
    """The errors the worker last found (empty when there are none or the file is missing)."""
    try:
        data = json.loads((Path(state_dir) / ERRORS_FILE).read_text(encoding="utf-8"))
        errors = data.get("errors")
        return errors if isinstance(errors, dict) else {}
    except (OSError, ValueError):
        return {}


def findings(state_dir: Path | str) -> list[Finding]:
    """One config_error finding per broken file. The id is stable, so the alert lifecycle tracks it
    and resolves it when the file is fixed."""
    return [
        Finding(
            id=f"{KIND}-{name}",
            kind=KIND,
            system=selfmon.SYSTEM,
            title=f"{name} is not valid ({issue['error']}); using {issue['using']}",
            detail={"file": name, "error": issue["error"], "using": issue["using"]},
            escalates=False,
        )
        for name, issue in sorted(read_errors(state_dir).items())
    ]
