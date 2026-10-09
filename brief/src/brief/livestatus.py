"""The status document computed now, with its systems and sensors: what the web app, the MCP server, and the
agents all read the same way. `provider(...)` gives the zero-argument callable the tools expect."""

import time
from collections.abc import Callable
from pathlib import Path

from brief import netinfo, registry
from brief.config import ConfigError, config_file
from brief.outputs import read_json
from brief.settings import Settings
from brief.status import build_status
from brief.store import Store
from brief.systems import Systems


def load_systems() -> Systems:
    try:
        return Systems.load(config_file("systems.yaml"))
    except (ConfigError, OSError):
        return Systems()


def provider(store: Store, settings: Settings, state_dir: Path) -> Callable[[], tuple[dict, Systems, list]]:
    """For a process with no web uptime (the worker, the command line): it is never reported as starting."""

    def current() -> tuple[dict, Systems, list]:
        systems = load_systems()
        doc = build_status(
            store, systems, settings.syslog_port, settings.interval, time.monotonic() + 3600,
            network=netinfo.network_info(settings),
            previous=read_json(settings.out_dir / "latest" / "status.json"),
            state_dir=state_dir,
        )  # fmt: skip
        try:
            metrics = registry.load(None, systems)
        except (ConfigError, OSError):
            metrics = []
        return doc, systems, metrics

    return current
