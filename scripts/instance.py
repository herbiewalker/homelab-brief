"""Where this install's private files live. Never inside the tracked repository.

The repository is the generic product. Everything about one household's network (hostnames, IPs,
real configs, raw and full sanitized logs, audit notes) lives in an instance folder next to it:

  config/brief/   real systems.yaml, devices.yaml, topology.yaml
  private/        free-form notes (audit results and similar)
  guard/          private-names.txt, sanitizer-words.txt, allow.txt
  samples/        raw/ (never committed anywhere) and sanitized/ (full set)
  .state/         sanitizer map and report (not committed)

Lookup order: the BRIEF_INSTANCE_DIR environment variable, then a sibling folder named
homelab-brief-instance, then .dev/instance inside the repository (gitignored), so a new user can
start with nothing. See docs/INSTANCE.md and instance-template/.
"""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SIBLING_NAME = "homelab-brief-instance"


def instance_dir() -> Path:
    env = os.environ.get("BRIEF_INSTANCE_DIR", "").strip()
    if env:
        return Path(env)
    sibling = ROOT.parent / SIBLING_NAME
    if sibling.is_dir():
        return sibling
    return ROOT / ".dev" / "instance"


def config_files() -> dict[str, Path]:
    base = instance_dir() / "config" / "brief"
    return {name: base / f"{name}.yaml" for name in ("systems", "devices", "topology")}


def private_text_files() -> list[Path]:
    folder = instance_dir() / "private"
    return sorted(p for p in folder.glob("*") if p.is_file()) if folder.is_dir() else []


def guard_file(name: str) -> Path:
    return instance_dir() / "guard" / name


def raw_dir() -> Path:
    return instance_dir() / "samples" / "raw"


def sanitized_dir() -> Path:
    return instance_dir() / "samples" / "sanitized"


def state_dir() -> Path:
    return instance_dir() / ".state"
