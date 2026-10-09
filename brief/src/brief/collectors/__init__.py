"""Phase 2 data collectors. Each lives in its own module and registers itself with the framework."""

from brief.collectors.base import Collector, CollectorError, Context
from brief.collectors.framework import register

__all__ = ["Collector", "CollectorError", "Context", "register"]

from brief.collectors import (  # noqa: E402,F401  (register the SNMP and iDRAC collectors)
    idrac,
    snmp,
    unifi,  # noqa: E402,F401  (registers the UniFi collector)
    unraid,  # noqa: E402,F401  (registers the Unraid collector)
)
