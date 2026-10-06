"""Shared helpers for loading the YAML config files under brief/.

Config conditions are structured data, never code. A missing file means "use the defaults"; a file
that exists but is invalid raises ConfigError with a message that names the file and the problem.
"""

import os
from collections.abc import Mapping
from pathlib import Path

import yaml


class ConfigError(ValueError):
    pass


def load_mapping(path: Path, expected_schema: int = 1) -> dict:
    """The top-level mapping of a YAML file, or {} when the file does not exist."""
    path = Path(path)
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path.name}: not valid YAML: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name}: expected a mapping at the top level")
    schema = data.get("schema", expected_schema)
    if schema != expected_schema:
        raise ConfigError(f"{path.name}: schema {schema!r} is not supported (expected {expected_schema})")
    return data


def is_placeholder(value: str) -> bool:
    """Example files use <angle-bracket> placeholders; they never match a real host."""
    value = value.strip()
    return value.startswith("<") and value.endswith(">")


# A user file that fails validation is replaced, for every reader at once, by its last good copy (or the
# shipped default): the config guard (configguard.py) sets these, keyed by the user file's path.
_fallbacks: dict[str, Path] = {}


def set_fallback(user_file: Path, instead: Path | None) -> None:
    """Make config_file() return `instead` for this user file (None puts the user's file back)."""
    if instead is None:
        _fallbacks.pop(str(user_file), None)
    else:
        _fallbacks[str(user_file)] = Path(instead)


def clear_fallbacks() -> None:
    _fallbacks.clear()


def user_file(name: str, env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    return Path(env.get("BRIEF_CONFIG_DIR", "/config")) / "brief" / name


def default_file(name: str, env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    return Path(env.get("BRIEF_DEFAULTS_DIR", "/app/defaults")) / "brief" / name


def config_file(name: str, env: Mapping[str, str] | None = None) -> Path:
    """Where to read a brief/ config file: the user's copy in /config/brief if there is one (or its last
    good copy while the user's file is broken), otherwise the default that ships in the image."""
    user = user_file(name, env)
    if user.exists():
        return _fallbacks.get(str(user), user)
    return default_file(name, env)
