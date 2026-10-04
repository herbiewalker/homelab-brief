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


def config_file(name: str, env: Mapping[str, str] | None = None) -> Path:
    """Where to read a brief/ config file: the user's copy in /config/brief if there is one,
    otherwise the default that ships in the image (/app/defaults/brief)."""
    env = os.environ if env is None else env
    user = Path(env.get("BRIEF_CONFIG_DIR", "/config")) / "brief" / name
    if user.exists():
        return user
    return Path(env.get("BRIEF_DEFAULTS_DIR", "/app/defaults")) / "brief" / name
