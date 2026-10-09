"""Phase 2 data collectors. Each lives in its own module and registers itself with the framework."""

from brief.collectors.base import Collector, CollectorError, Context
from brief.collectors.framework import register

__all__ = ["Collector", "CollectorError", "Context", "register"]

from brief.collectors import unraid  # noqa: E402,F401  (registers the Unraid collector)
