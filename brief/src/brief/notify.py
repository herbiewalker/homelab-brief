"""Optional notification channels outside Home Assistant, through Apprise (ntfy, Gotify, email, and more).

Off by default (brief/notify.yaml). The alert lifecycle still decides what is sent and when (dedupe,
quiet hours, daily budget, escalation); this module only adds delivery channels, so nothing here can
nag. Apprise is imported only when it is enabled, so an install that does not use it pays nothing.

Apprise URLs carry credentials, so they come from the template variable named by `urls_var`, are never
written to a file or a log, and are never echoed back (errors say how many, not which).
"""

from collections.abc import Callable, Mapping

from brief.config import ConfigError, config_file, load_mapping
from brief.jsonlog import log

DEFAULT_ROUTES = ("push_now",)
ROUTES = ("push_now", "daily_headline")
HEADINGS = {"new": "", "repeat": "Still: ", "escalated": "Worse: ", "resolved": "Resolved: "}
SEVERE = ("critical", "crit", "emerg", "alert")


def notify_type(payload: dict) -> str:
    """The Apprise notification type (it picks the icon or colour on the receiving side)."""
    if payload.get("state") == "resolved":
        return "success"
    if payload.get("urgent") or payload.get("level") in SEVERE:
        return "failure"
    return "warning" if payload.get("level") == "warning" else "info"


class AppriseChannel:
    """Sends alerts and the daily headline to every configured Apprise URL."""

    def __init__(
        self,
        urls: list[str],
        routes: tuple[str, ...] = DEFAULT_ROUTES,
        include_resolved: bool = True,
        include_body: bool = False,
        factory: Callable[[], object] | None = None,
    ):
        if factory is None:
            import apprise  # imported here: only an install that turns Apprise on loads it

            factory = apprise.Apprise
        self.routes, self.include_resolved, self.include_body = routes, include_resolved, include_body
        self.client = factory()
        self.destinations = 0
        for index, url in enumerate(urls, 1):
            if self.client.add(url):
                self.destinations += 1
            else:
                log("apprise_bad_url", number=index)  # the number only: the URL has credentials in it

    @classmethod
    def from_env(cls, env: Mapping[str, str], factory=None) -> "AppriseChannel | None":
        """None when notify.yaml leaves Apprise off, or no URL is set, or no URL is usable."""
        try:
            cfg = (load_mapping(config_file("notify.yaml", env)).get("apprise")) or {}
        except (ConfigError, OSError) as exc:
            log("config_error", file="notify.yaml", error=str(exc), kept="apprise off")
            return None
        if not cfg.get("enabled"):
            return None
        var = str(cfg.get("urls_var") or "APPRISE_URLS")
        urls = [u.strip() for u in (env.get(var) or "").split(",") if u.strip()]
        if not urls:
            log("apprise_no_urls", variable=var)
            return None
        routes = tuple(r for r in (cfg.get("routes") or DEFAULT_ROUTES) if r in ROUTES) or DEFAULT_ROUTES
        channel = cls(
            urls,
            routes,
            include_resolved=bool(cfg.get("include_resolved", True)),
            include_body=bool(cfg.get("include_body", False)),
            factory=factory,
        )
        if not channel.destinations:
            log("apprise_no_usable_urls", variable=var)
            return None
        return channel

    # -- the Notifier interface used by the alert lifecycle

    def send_alert(self, payload: dict) -> bool:
        if "push_now" not in self.routes:
            return False
        if payload.get("state") == "resolved" and not self.include_resolved:
            return False
        title = f"{HEADINGS.get(payload.get('state', ''), '')}{payload.get('title', 'Alert')}"
        plain = f"{payload.get('level', 'info')}: {payload.get('system') or 'homelab'}"
        body = (payload.get("body") if self.include_body else "") or plain
        return self._send(title, body, notify_type(payload))

    def send_headline(self, text: str) -> bool:
        if "daily_headline" not in self.routes:
            return False
        return self._send("Homelab brief", text, "info")

    def test(self) -> bool:
        """The message `brief notify test` sends, to prove the destinations work."""
        return self._send("Homelab Brief test", "If you can read this, notifications work.", "info")

    def _send(self, title: str, body: str, kind: str) -> bool:
        try:
            ok = bool(self.client.notify(title=title, body=body, notify_type=kind))
        except Exception as exc:  # a dead destination must never break the run
            log("apprise_failed", error=type(exc).__name__)
            return False
        if not ok:
            log("apprise_failed", error="at least one destination did not accept the message")
        return ok


class Channels:
    """Several notifiers behind one: every one is tried, and a send counts as delivered if any took it."""

    def __init__(self, channels: list):
        self.channels = channels

    def send_alert(self, payload: dict) -> bool:
        return any([c.send_alert(payload) for c in self.channels])

    def send_headline(self, text: str) -> bool:
        return any([c.send_headline(text) for c in self.channels])
