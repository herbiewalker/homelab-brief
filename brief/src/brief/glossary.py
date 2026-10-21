"""The words of the product, each explained once in plain language.

The "?" tips on pages, the tooltips on tiles and headings, and the Help page all read this one table, so
they cannot disagree. A key is a short id used in templates (`{{ tip('absence') }}`); `kpi:<label>` entries
are the tooltips of the Overview tiles, which are found by their label.
"""

# key -> (term as shown, one plain sentence)
TERMS: dict[str, tuple[str, str]] = {
    "system": (
        "System",
        "A named group of devices that belong together, such as all access points. It gets one status row.",
    ),
    "signature": (
        "Signature",
        "The shape of a log message with the changing parts (numbers, names, addresses) blanked out. "
        "Thousands of lines that differ only in those parts count as one signature.",
    ),
    "baseline": (
        "Baseline",
        "What normal looks like for a host or a signature, learned from its own recent history. "
        "Findings compare the present with the baseline.",
    ),
    "learning": (
        "Learning period",
        "The first days after install, while baselines are being built. Deviations are not reported yet "
        "because nothing is known to be normal.",
    ),
    "novelty": (
        "New signature",
        "A kind of message this host has never sent before, or one that came back after a long silence.",
    ),
    "deviation": (
        "Deviation",
        "A signature that is far more frequent than its baseline, for example 3 times the usual rate.",
    ),
    "absence": (
        "Silent host",
        "A host that normally sends logs every day and has stopped. A dead camera or a crashed server "
        "looks like this.",
    ),
    "off_hours": (
        "Off-hours activity",
        "Activity at an hour when this host is usually quiet.",
    ),
    "finding": (
        "Finding",
        "Something the detectors noticed. Findings are grouped into stories; important ones become alerts.",
    ),
    "story": (
        "Story",
        "Related findings on one host, gathered into one item so a single problem is not listed fifty times.",
    ),
    "alert": (
        "Alert",
        "A finding important enough to notify you. It is sent once, repeated only if it gets worse, "
        "and closed when it clears.",
    ),
    "ack": (
        "Acknowledge",
        "Tell the system you have seen it. No reminders until the problem gets worse.",
    ),
    "snooze": ("Snooze", "Silence an alert for a while. It comes back sooner if it gets worse."),
    "quiet_hours": (
        "Quiet hours",
        "A window (in UTC) when non-urgent notifications are held back and sent afterwards.",
    ),
    "brief": (
        "Brief",
        "The daily summary: a headline, the most important stories, and what was checked and fine. "
        "The facts come from the detectors; an AI model may only reword them.",
    ),
    "collector": (
        "Collector",
        "A small task that asks a device or service (Unraid, UniFi, SNMP, Home Assistant) for its state "
        "on a schedule, instead of waiting for it to send logs.",
    ),
    "ingest_lag": (
        "Ingest lag",
        "How far behind real time the newest processed log line is. A few seconds is normal.",
    ),
    "parse_failed": (
        "Parse failures",
        "Lines that arrived but could not be read as log messages. They are counted, never dropped silently.",
    ),
    "sender": ("Sender", "A device that sends logs to this container, identified by its address."),
    "ai_chain": (
        "Fallback chain",
        "The AI providers tried in order for a task. The last one is always the plain template, so the "
        "brief never waits on a model.",
    ),
    # Tooltips of the Overview tiles, found by their label.
    "kpi:Pipeline": (
        "Pipeline",
        "Log events received in the last 24 hours, and how far behind real time processing is.",
    ),
    "kpi:Silent hosts": (
        "Silent hosts",
        "Hosts that normally send logs and have stopped, out of all the hosts being watched.",
    ),
    "kpi:Storage": (
        "Storage",
        "How long the log archive can keep growing before the disk is full, from its growth so far.",
    ),
    "kpi:Memory": ("Memory", "What the container is using now, against the limit it was given."),
    "kpi:Collectors": (
        "Collectors",
        "Data sources that are polled (not log senders) and answered on their last run.",
    ),
    "kpi:AI and agents": (
        "AI and agents",
        "Whether the optional AI layer is on, and how many model calls it made in the last 24 hours.",
    ),
    "kpi:Alerts": (
        "Alerts",
        "Open, acknowledged, or snoozed alerts. Resolved ones are not counted.",
    ),
}

# What each page is for, one line. Shown under its name on the Settings page and as the menu tooltip.
PAGES: dict[str, str] = {
    "map": "The network drawn as a map, with live status colours.",
    "wizard": "Name the devices that are sending logs and group them into systems.",
    "systems": "Define the systems: which devices belong to which group.",
    "collectors": "Servers and services the product polls: Unraid, UniFi, SNMP, Home Assistant.",
    "network": "The address and ports the container listens on, and a live test that syslog arrives.",
    "schedule": "When the daily and weekly runs happen, and how long history is kept.",
    "notifications": "Where alerts are sent besides Home Assistant.",
    "devices": "Device profiles, topology, and what the network map shows.",
    "display": "The pages and sensors published for tablets and Home Assistant.",
    "config": "Every configuration file as text, with history and undo.",
    "ignore": "Rules that hide noise you do not want to see, with a tester.",
    "priorities": "How findings are ranked and which ones notify you.",
    "thresholds": "How far from normal counts as a deviation, and the advice rules.",
    "ai": "The AI layer: providers, features, agents, proposals, and the call log.",
    "backups": "Download or restore a backup, and export reports.",
    "signatures": "The kinds of message seen, busiest first, with the option to ignore one.",
    "logs": "Search the raw log archive.",
    "replay": "Run the detectors over past days to see what would have fired.",
    "audit": "Every change made through the console or the command line.",
    "account": "Your admin account and password.",
    "help": "What each word in the product means.",
}


def term(key: str) -> tuple[str, str]:
    return TERMS[key]


def kpi_tip(label: str) -> str:
    """The tooltip of an Overview tile, or an empty string when the tile has none."""
    entry = TERMS.get(f"kpi:{label}")
    return entry[1] if entry else ""


def help_terms() -> list[tuple[str, str]]:
    """The entries of the Help page: the words, not the tile tooltips."""
    words = [(t, s) for k, (t, s) in TERMS.items() if not k.startswith("kpi:")]
    return sorted(words, key=lambda x: x[0].lower())
