"""Loads the inputs for the network map from the live config and database, and builds the document.

Kept apart from `mapdata` (pure merge) and `mapsvg` (drawing) so the web app, the admin page, and the CLI
all get the same map from one call. Reads only, except `save_default_layout`, which the admin page calls
after the login, CSRF, and audit steps.
"""

from datetime import UTC, datetime
from pathlib import Path

from brief import mapdata, mapsvg
from brief.config import ConfigError, config_file, load_mapping
from brief.devices import DeviceBook
from brief.store import Store

LAYOUT_FILE = "map_layout.json"


def layout_path(state_dir: Path) -> Path:
    return state_dir / LAYOUT_FILE


def map_config() -> dict:
    try:
        return load_mapping(config_file("map.yaml"))
    except (ConfigError, OSError):
        return {"enabled": False}


def topology() -> dict:
    """topology.yaml (or its last good copy); absent or broken means no manual entries."""
    try:
        return load_mapping(config_file("topology.yaml"))
    except (ConfigError, OSError):
        return {}


def current_map(
    store: Store, state_dir: Path, status: dict, systems, now: datetime | None = None
) -> dict | None:
    """The map document, or None when `map.yaml` has `enabled: false`."""
    cfg = map_config()
    if not cfg.get("enabled"):
        return None
    doc = mapdata.read_map(
        store,
        topology(),
        cfg,
        status,
        systems,
        DeviceBook.load(),
        layout_path(state_dir),
        now or datetime.now(UTC),
    )
    # The automatic placement alone (no saved positions), so the interactive page starts from the same
    # arrangement `map.svg` draws.
    auto = mapsvg.place({**doc, "layout": {}})
    doc["auto"] = {k: {"x": round(x, 1), "y": round(y, 1)} for k, (x, y) in auto.items()}
    return doc


def svg_of(doc: dict) -> str:
    size = map_config().get("static_svg") or {}
    return mapsvg.render_svg(doc, int(size.get("width") or 1600), int(size.get("height") or 1000))


def save_default_layout(state_dir: Path, layout: dict) -> dict:
    return mapdata.save_layout(layout_path(state_dir), layout)
