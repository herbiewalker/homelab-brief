"""The collector contract.

A collector is a small class that fetches data from one interface (an API, SNMP, MQTT), writes its rows
into the baseline database, and returns numbers for the metric registry. It is switched on in
brief/collectors.yaml and needs no change anywhere else: the scheduler, the publisher, `/metrics`, the
brief, and the admin GUI find it through `brief.collectors.framework`.

    class Example(Collector):
        name = "example"

        def metrics(self, config):          # what it publishes (registry entries; ids start with the name)
            return [{"id": "example_things", "name": "Things", "group": "example", "kind": "number"}]

        def test(self, ctx):                # "Test connection": raise CollectorError with a clear reason
            ...
            return "reached the service"

        def run(self, ctx):                 # write rows with ctx.store.write(), return {metric id: number}
            ...
            return {"example_things": 3}

Rules the framework enforces: a failing run reports an error (an `<name>_ok` metric of 0) and keeps the last
good data; a slow run is cut off after `timeout_s`; credentials come from template variables named in the
config (`key_var: UNIFI_API_KEY`), never from the file; and a collector never raises into the worker.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from brief.store import Store


class CollectorError(Exception):
    """A failure the user can act on. Its message is shown in the GUI and in status.json, so it must not
    contain an address, a name, or a secret: say what is wrong ("the API key was rejected"), not where."""


@dataclass
class Context:
    name: str
    config: dict  # this collector's block from collectors.yaml
    env: Mapping[str, str]
    store: Store
    now: datetime

    def var(self, key: str) -> str:
        """The value of the template variable named by config[key] (for example key_var -> UNIFI_API_KEY)."""
        variable = self.config.get(key)
        if not variable:
            raise CollectorError(f"{key} is not set in collectors.yaml")
        value = (self.env.get(str(variable)) or "").strip()
        if not value:
            raise CollectorError(f"the variable {variable} is not set")
        return value


class Collector:
    name: str = ""

    def validate(self, config: dict) -> None:
        """Raise brief.config.ConfigError for a setting this collector cannot use. Optional."""

    def metrics(self, config: dict) -> list[dict]:
        """Registry entries for what this collector publishes; every id starts with `<name>_`."""
        return []

    def test(self, ctx: Context) -> str:
        """Check the connection without storing anything. Return a short success message."""
        raise CollectorError("this collector has no connection test")

    def run(self, ctx: Context) -> dict[str, float | str]:
        raise NotImplementedError

    def required_vars(self, config: dict) -> list[str] | None:
        """Template variables that must be set before a run. None (the default) means every `*_var` the
        settings name. A collector that serves several independent targets (one key each) returns [] and
        reports a missing key per target, so one forgotten key does not stop the others."""
        return None
