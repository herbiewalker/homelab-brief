"""What the setup pages' settings look like as form controls (Task 33): schedule and retention, notifications,
network, and systems. The framework (settingsui) turns these into cards and writes the values back into the files."""

from dataclasses import dataclass

from brief.settingsui import Field, Rows, Section

TIME = r"^([01]\d|2[0-3]):[0-5]\d$"
TIME_HELP = "A time of day as HH:MM, 24-hour, in UTC (for example 06:30)."
DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
ID = r"^[a-z0-9][a-z0-9_-]{0,30}$"
ID_HELP = "Use letters, digits, hyphens, and underscores."
HOST = r"^[A-Za-z0-9.:_-]{1,80}$"
HOST_HELP = "an address or name (letters, digits, dots, colons, hyphens)."
SENDER = r"^[0-9A-Fa-f.:/]{2,50}$"
SENDER_HELP = "an IP address or a range like 198.51.100.0/24."
NAME = r"^[A-Za-z0-9.*_-]{1,100}$"
NAME_HELP = "a host name or IP (a leading *. is allowed)."


def _days(key: str, label: str, help: str, low: int = 1, high: int = 3650, unit: str = "days") -> Field:
    return Field(key, label, "number", help, unit=unit, minimum=low, maximum=high)


SCHEDULE = Section(
    "schedule",
    "Schedule",
    "schedule.yaml",
    (),
    (
        Field("daily_run_utc", "Daily run", "text", "The full daily run: detectors, brief, host pages, views.", pattern=TIME, pattern_help=TIME_HELP, placeholder="06:00"),
        Field("weekly_day", "Weekly report day", "choice", "The weekly trend report covers the last complete week.", DAYS),
        Field("weekly_run_utc", "Weekly report time", "text", "When the weekly report is written on that day.", pattern=TIME, pattern_help=TIME_HELP, placeholder="06:30"),
    ),
    blurb="When the scheduled jobs run. All times are UTC. The hourly job (status, sensors, absence alerts) runs every hour the worker is up.",
    icon="clock",
    switch="",
)  # fmt: skip

RETENTION = Section(
    "retention",
    "Retention",
    "retention.yaml",
    (),
    (
        _days("archive_days", "Raw syslog archive", "How long the compressed daily syslog files are kept."),
        _days("events_days", "Normalized events", "These can be rebuilt from the archive, so keep them short."),
        _days("exports_days", "History exports", "Files written for download in the export folder."),
        _days("rollup_after_days", "Roll up after", "Client, DNS, and power hourly rows are summed into daily rows after this long.", 7),
        Field("samples_per_signature", "Samples per pattern", "number", "Example lines kept for each log pattern.", unit="lines", minimum=1, maximum=50),
        Field("config_history_versions", "Config versions", "number", "Saved versions of each config file, for Undo and rollback.", unit="versions", minimum=5, maximum=500),
        _days("audit_log_days", "Audit log", "How long who-changed-what is kept."),
    ),
    blurb="How long each kind of data is kept. Shorter saves disk; longer gives the baselines and the replay more to work with.",
    icon="database",
    switch="",
)  # fmt: skip

APPRISE = Section(
    "notify-apprise",
    "Phone and chat notifications",
    "notify.yaml",
    ("apprise",),
    (
        Field("urls_var", "Destinations variable", "var", "The container variable holding your Apprise destination URLs, separated by commas. The URLs carry credentials, so they live only in the container's environment.", placeholder="APPRISE_URLS"),
        Field("routes", "Send these", "multi", "push_now: urgent alerts. daily_headline: the one-line morning summary.", ("push_now", "daily_headline")),
        Field("include_resolved", "Also say when something clears", "switch"),
        Field("include_body", "Include details (hosts and counts)", "switch", "Off sends only a title and a level, which is safer for a public chat."),
    ),
    blurb="Optional delivery through Apprise (ntfy, Gotify, email, and many more), on top of Home Assistant. What is sent still follows the alert rules, so nothing here causes extra noise.",
    icon="bell",
    switch="enabled",
    switch_label="Send notifications through Apprise",
    test_label="Send a test",
)  # fmt: skip

MQTT = Section(
    "notify-mqtt",
    "Home Assistant (MQTT)",
    "notify.yaml",
    ("mqtt",),
    (),
    blurb="Publishes sensors, alerts, and the daily headline to your MQTT broker so Home Assistant can show them. The broker address itself is set on the container (MQTT_HOST).",
    icon="radio",
    switch="enabled",
    switch_label="Publish to Home Assistant over MQTT",
)

NETWORK = Section(
    "network",
    "Network settings",
    "network.yaml",
    (),
    (
        Field("mode", "Mode", "choice", "auto detects it from inside the container. Set it only if detection is wrong. Docker decides the real mode; this cannot change it.", ("auto", "own_ip", "host", "bridge")),
        Field("advertised_address", "Advertised address", "text", "The address devices send syslog to and links use. Leave empty to use the detected one.", clearable=True, pattern=HOST, pattern_help=HOST_HELP, placeholder="detected automatically"),
        Field("unknown_senders", "Senders not on the list", "choice", "flag keeps their lines and raises a finding. drop discards them.", ("flag", "drop")),
        Field("allowed_senders", "Allowed senders", "list", "One address or range per line. Anything else is flagged (or dropped). Empty allows every sender.", lines=4, pattern=SENDER, pattern_help=SENDER_HELP, placeholder="198.51.100.0/24"),
        Field("tls_extra_names", "Extra certificate names", "list", "Names or addresses to include in the self-signed certificate, one per line.", lines=3, pattern=NAME, pattern_help=NAME_HELP),
    ),
    blurb="Checked before it is saved, kept in the history, and written to the audit log. Ports and the advertised address apply after the container restarts.",
    icon="network",
    switch="",
)  # fmt: skip

PORTS = Section(
    "network-ports",
    "Ports",
    "network.yaml",
    ("ports",),
    tuple(
        Field(key, label, "number", help, unit="port", minimum=1, maximum=65535)
        for key, label, help in (
            ("syslog", "Syslog", "Where devices send logs (UDP and TCP)."),
            ("http", "Web (read-only)", "The status pages and the API."),
            ("https", "Web (admin)", "This admin interface."),
            ("ssh", "SSH", "Only used when SSH is switched on."),
        )
    ),
    blurb="The ports the container listens on. In host mode they must not clash with Unraid's own (80, 443); 8480, 8443, and 5514 are the usual alternatives. In bridge mode the Docker template must publish the same ports.",
    icon="plug",
    switch="",
)

SYSTEMS = Section(
    "systems",
    "Systems",
    "systems.yaml",
    (),
    (
        Rows(
            "systems",
            "Your systems",
            (
                Field("id", "Id", "text", "A short name used in sensors and files. Letters are saved lowercase.", required=True, pattern=ID, pattern_help=ID_HELP, slug=True, placeholder="gateway"),
                Field("label", "Name on the tile", "text", "What the board and the brief call it. Empty uses the id.", placeholder="Gateway"),
                Field("kind", "Kind", "text", "gateway, server, switch, camera... free text."),
                Field("source_tz", "Time zone of its log lines", "text", "Most devices log in UTC or local time. Use a zone name like America/Chicago.", initial="UTC"),
                Field("hosts", "Hosts", "list", "Every address or name this system sends syslog as, separated by commas.", pattern=NAME, pattern_help=NAME_HELP, placeholder="198.51.100.1, gateway.local"),
                Field("expected", "Expected to keep sending (alert if it goes quiet)", "switch", initial=True),
            ),
            noun="system",
            help="A system is one tile on the board and one status sensor in Home Assistant.",
        ),
        Field("default_system", "Where unlisted hosts go", "text", "The id of the system that collects senders that match nothing.", slug=True, pattern=ID, pattern_help=ID_HELP, placeholder="other"),
    ),
    blurb="Group the devices that send you logs. Hosts that match no system roll up into the default system.",
    icon="server",
    switch="",
)  # fmt: skip


@dataclass(frozen=True)
class Page:
    key: str
    title: str
    lead: str
    sections: tuple[Section, ...]
    nav: str
    config_file: str


PAGES = {
    p.key: p
    for p in (
        Page(
            "schedule",
            "Schedule and retention",
            "When the jobs run and how long each kind of data is kept.",
            (SCHEDULE, RETENTION),
            "schedule",
            "schedule.yaml",
        ),
        Page(
            "notifications",
            "Notifications",
            "Where alerts and the daily headline are delivered, besides the pages and the API.",
            (APPRISE, MQTT),
            "notifications",
            "notify.yaml",
        ),
    )
}


# ---------------------------------------------------------------- detection and alerting (Task 34)

ROUTES = ("push_now", "daily_headline", "digest_only", "suppress")
ROUTE_HELP = "push_now: notify now. daily_headline: in the morning summary. digest_only: only in the brief. suppress: never."


def _regex_check(text: str):
    from brief import ignoretest

    return ignoretest.regex_problem(text)


IGNORE = Section(
    "ignore",
    "Ignore rules",
    "ignore.yaml",
    (),
    (
        Rows(
            "ignore",
            "Rules",
            (
                Field("name", "Name", "text", "Names the rule and its count of ignored lines.", required=True, pattern=ID, pattern_help=ID_HELP, slug=True, placeholder="port-flaps"),
                Field("host", "Only for this host", "text", "Optional: a sender address, a host name, or a system id. Empty means every host.", pattern=HOST, pattern_help=HOST_HELP, placeholder="any host"),
                Field("match", "Pattern", "text", "A regular expression searched in \"app: message\". Start it with (?i) to ignore capital letters.", required=True, check=_regex_check, placeholder="(?i)link (up|down)"),
                Field("note", "Why it is safe to ignore", "text", "A reminder for later."),
            ),
            noun="rule",
            help="Ignored lines are still counted per rule, so a sudden change in how often they happen stays visible. The first matching rule wins.",
            test_label="Test against recent lines",
        ),
    ),
    blurb="Known noise that is filtered out before the detectors look at it.",
    icon="list",
    switch="",
)  # fmt: skip

RANKING = Section(
    "priorities-ranking",
    "Ranking and grouping",
    "priorities.yaml",
    (),
    (
        Field("default.rank", "Rank", "number", "How high a finding of a kind that is not listed below ranks (higher is first).", minimum=0, maximum=1000, group="Findings of an unlisted kind"),
        Field("default.route", "Route", "choice", ROUTE_HELP, ROUTES),
        Field("grouping.min_members", "Merge at", "number", "Findings of one kind on one host become one story once there are this many.", unit="findings", minimum=2, maximum=100, group="Grouping"),
        Field("grouping.show", "Names shown", "number", "How many of the biggest are named in the story.", unit="names", minimum=1, maximum=20),
        Field("severity_boost.emerg", "Emergency", "number", "Added to a story's rank when its worst log level is this one, so a brand-new error outranks a routine swing in volume.", minimum=0, maximum=200, group="Rank boost by log level"),
        Field("severity_boost.alert", "Alert", "number", minimum=0, maximum=200),
        Field("severity_boost.crit", "Critical", "number", minimum=0, maximum=200),
        Field("severity_boost.err", "Error", "number", minimum=0, maximum=200),
    ),
    blurb="How findings are ordered in the brief. Change a number and use the preview to see what it would have done to last week.",
    icon="sliders",
    switch="",
    preview=True,
)  # fmt: skip

ALERTING = Section(
    "priorities-alerts",
    "Alert behaviour",
    "priorities.yaml",
    (),
    (
        Field("quiet_hours.start", "Quiet from", "text", "Pushes that are not urgent wait until quiet hours end.", pattern=TIME, pattern_help=TIME_HELP, utc_time=True, group="Quiet hours (UTC)"),
        Field("quiet_hours.end", "Quiet until", "text", pattern=TIME, pattern_help=TIME_HELP, utc_time=True),
        Field("lifecycle.renotify_hours", "Repeat a pushed finding at most every", "number", unit="hours", minimum=1, maximum=720, group="Repeats and escalation"),
        Field("lifecycle.escalate_on_worse", "Push again early when it gets worse", "switch"),
        Field("lifecycle.escalate_factor", "Worse means this many times bigger", "number", integer=False, minimum=1.1, maximum=100),
        Field("lifecycle.escalate_min_minutes", "Never escalate the same finding more often than", "number", unit="minutes", minimum=1, maximum=10080),
        Field("lifecycle.send_resolved", "Say when a pushed finding clears", "switch", group="Resolving and budget"),
        Field("lifecycle.resolve_after_quiet_runs", "Resolved after this many quiet runs", "number", unit="runs", minimum=1, maximum=30),
        Field("lifecycle.daily_push_budget", "Extra pushes per day", "number", "Beyond this, non-urgent pushes fold into the next daily headline.", unit="pushes", minimum=0, maximum=100),
        Field("lifecycle.snooze_options_h", "Snooze choices", "list", "The buttons on an alert, in hours, separated by commas.", item_type="int", pattern=r"^[0-9]{1,4}$", pattern_help="a whole number of hours."),
    ),
    blurb="When a finding becomes a push, how often it repeats, and what waits for the morning.",
    icon="bell",
    switch="",
    preview=True,
)  # fmt: skip

KIND_NAMES = (
    "absence", "pipeline_error", "config_error", "novelty", "deviation", "off_hours", "smart_failing", "smart_trend",
    "disk_temp", "crc_rising", "firmware_pending", "version_behind", "new_infrastructure", "new_device", "traffic_spike",
    "odd_hours", "metric_drift", "power_drift", "parity_slow", "unraid_warning", "clock_skew", "unknown_sender", "storage_low",
)  # fmt: skip


def _kind_fields() -> tuple[Field, ...]:
    out = []
    for kind in KIND_NAMES:
        group = kind.replace("_", " ")
        out += [
            Field(f"kinds.{kind}.rank", "Rank", "number", minimum=0, maximum=1000, group=group),
            Field(f"kinds.{kind}.route", "Route", "choice", choices=ROUTES, group=group),
            Field(f"kinds.{kind}.urgent", "Urgent", "switch", group=group),
        ]
    return tuple(out)


KINDS = Section(
    "priorities-kinds",
    "Kinds of finding",
    "priorities.yaml",
    (),
    _kind_fields(),
    blurb="Rank orders the brief (higher first). Route decides how it is delivered: " + ROUTE_HELP + " Urgent lets a push_now finding skip quiet hours and the daily budget.",
    icon="alert",
    switch="",
    grid=("rank", "route", "urgent"),
    grid_title="Kind of finding",
    preview=True,
)  # fmt: skip

PAGES.update(
    {
        p.key: p
        for p in (
            Page(
                "ignore",
                "Ignore rules",
                "Log noise you have decided is safe, filtered out before detection but still counted.",
                (IGNORE,),
                "ignore",
                "ignore.yaml",
            ),
            Page(
                "priorities",
                "Priorities and alerts",
                "How findings are ranked and routed, and when an alert is sent.",
                (RANKING, ALERTING, KINDS),
                "priorities",
                "priorities.yaml",
            ),
        )
    }
)
SECTIONS = {s.id: s for p in PAGES.values() for s in p.sections}
SECTIONS.update({s.id: s for s in (NETWORK, PORTS, SYSTEMS)})


# ---------------------------------------------------------------- playbook thresholds and advice (Task 34)
# (group, heading, [(parameter, label, unit, help, whole number?, minimum, maximum)])
THRESHOLD_GROUPS = (
    ("learning", "Learning period", [
        ("days", "Learn for", "days", "Live days of baseline collection before novelty and deviation findings start.", True, 0, 90),
    ]),
    ("novelty", "New or returning log patterns", [
        ("quiet_days_before_return", "Quiet for", "days", "A pattern that was silent this long and appears again counts as returned.", True, 1, 90),
        ("host_active_hours_in_gap", "Host logged other things for", "hours", "Only counts as a return if the host was otherwise active this long meanwhile; if the host was down, the host returned instead.", True, 0, 168),
        ("min_prior_days", "Used to be regular", "days", "The pattern must have been seen on at least this many days before it went quiet.", True, 1, 90),
    ]),
    ("deviation", "Unusual volume", [
        ("window_days", "Baseline window", "days", "How far back the normal volume is measured.", True, 3, 90),
        ("min_count", "At least", "lines", "Smaller counts are never flagged.", True, 1, 100000),
        ("mad_multiple", "Spread multiple", "x", "Flag above the median plus this many typical spreads (MAD).", False, 1, 50),
        ("min_ratio", "At least this many times the median", "x", "Also required, when the median is not zero.", False, 1, 1000),
        ("min_history_days", "Needs history of", "days", "Days of the pattern's own history before it can be flagged.", True, 1, 90),
    ]),
    ("off_hours", "Lines at an unusual hour", [
        ("window_days", "Baseline window", "days", "", True, 3, 90),
        ("min_count", "At least", "lines", "In the hour being judged.", True, 1, 100000),
        ("min_history_days", "Needs history of", "days", "", True, 1, 90),
        ("min_history_events", "Needs at least", "events", "Events in the history before a usual pattern is believed.", True, 1, 100000),
        ("max_usual_hours", "Usually within", "hours of the day", "A pattern concentrated in at most this many hours of the day has a usual time.", True, 1, 24),
    ]),
    ("absence", "A source gone quiet", [
        ("gap_multiple", "Silent for", "x its usual gap", "A host that has not sent for this many times its learned gap is silent.", False, 1, 100),
        ("min_minutes", "Never sooner than", "minutes", "", True, 1, 10080),
        ("max_hours", "Never later than", "hours", "The limit used before a gap has been learned.", True, 1, 720),
    ]),
    ("metric_drift", "Collector numbers drifting", [
        ("recent_days", "Recent window", "days", "", True, 1, 90),
        ("baseline_days", "Earlier window", "days", "", True, 2, 365),
        ("min_change_pct", "Flag a change of", "%", "The recent mean must differ from the earlier mean by at least this much.", False, 1, 1000),
        ("min_samples", "Needs at least", "samples", "In both windows.", True, 2, 100000),
    ]),
    ("traffic_spike", "Client traffic spike", [
        ("multiple", "More than", "x its normal day", "", False, 1, 1000),
        ("floor_gb", "and more than", "GB", "So a tiny device doubling is not news.", False, 0, 10000),
        ("window_days", "Baseline window", "days", "", True, 3, 90),
        ("min_history_days", "Needs history of", "days", "", True, 1, 90),
    ]),
    ("odd_hours", "Device online at odd hours", [
        ("min_online_min", "Online at least", "minutes", "In an hour of the device's quiet hours (devices.yaml).", True, 1, 60),
    ]),
    ("new_device", "New client devices", [
        ("settle_hours", "Starting population ends after", "hours", "Devices first seen within this time of the first one are the starting list, not news.", True, 1, 720),
        ("show_days", "Report for", "days", "", True, 1, 60),
    ]),
    ("power_drift", "Power draw drifting up", [
        ("recent_hours", "Recent window", "hours", "", True, 1, 168),
        ("baseline_days", "Earlier window", "days", "", True, 2, 365),
        ("min_increase_pct", "Flag an increase of", "%", "", False, 1, 1000),
        ("min_increase_w", "and at least", "watts", "So a 3 W device doubling is not news.", False, 0, 100000),
        ("min_samples", "Needs at least", "hourly readings", "", True, 2, 100000),
    ]),
    ("disk_temp_hdd_c", "Spinning disk temperature", [
        ("warn", "Warn at", "degrees C", "A disk whose coolest sample over the days stays at or above this is flagged.", True, 30, 90),
        ("days", "Over", "days", "", True, 1, 30),
    ]),
    ("version_behind", "Software out of date", [
        ("days", "Behind for", "days", "", True, 1, 365),
    ]),
    ("new_infrastructure", "New network equipment", [
        ("settle_hours", "Starting inventory ends after", "hours", "", True, 1, 720),
        ("show_days", "Report for", "days", "", True, 1, 60),
    ]),
    ("parity_slow", "Slow parity check", [
        ("floor_mb_s", "Below", "MB/s", "", False, 1, 10000),
        ("drop_vs_previous_pct", "or down by", "% from the previous check", "", False, 1, 100),
        ("max_age_days", "Look back", "days", "", True, 1, 365),
        ("history", "Compare against the last", "checks", "", True, 1, 50),
    ]),
    ("smart_trend", "Disk SMART trends", [
        ("window_days", "Window for a rise", "days", "Reallocated sectors, CRC errors and the like.", True, 1, 90),
    ]),
    ("clock_skew", "Clock skew", [
        ("max_minutes", "Flag a clock off by more than", "minutes", "", True, 1, 1440),
    ]),
    ("storage_low", "Archive storage", [
        ("min_free_pct", "Flag below", "% free", "", True, 1, 90),
        ("min_days_until_full", "or fewer than", "days until full", "", True, 1, 365),
    ]),
)  # fmt: skip


def _threshold_fields() -> tuple[Field, ...]:
    out = []
    for group, heading, params in THRESHOLD_GROUPS:
        for param, label, unit, help_, whole, low, high in params:
            out.append(
                Field(
                    f"thresholds.{group}.{param}",
                    label,
                    "number",
                    help_,
                    unit=unit,
                    integer=whole,
                    minimum=low,
                    maximum=high,
                    group=heading,
                )
            )
    return tuple(out)


THRESHOLDS = Section(
    "playbook-thresholds",
    "Thresholds",
    "playbook.yaml",
    (),
    _threshold_fields(),
    blurb="When each detector decides something is worth a finding. These are starting values to tune against your own data; use the preview to see what a change would have done to last week.",
    icon="sliders",
    switch="",
    preview=True,
)

RULE_IDS = (
    ("smart_failing", "A disk reports a failed SMART status"),
    ("smart_pending_nonzero", "A disk has unreadable sectors"),
    ("smart_realloc_rising", "A disk is remapping sectors"),
    ("crc_rising", "CRC errors are rising"),
    ("firmware_pending", "A firmware update is available"),
    ("new_infrastructure", "New network equipment appeared"),
    ("parity_slow", "A parity check was slow"),
    ("clock_skew", "A clock is off"),
    ("storage_low", "Archive storage is low"),
    ("traffic_spike_generic", "A client moved far more data than usual"),
    ("new_device_generic", "A new device joined"),
    ("odd_hours_device", "A device was online in its quiet hours"),
    ("power_drift_system", "A system's power draw rose"),
)
BUCKETS = ("watch", "plan", "act_now")


def _advice_fields() -> tuple[Field, ...]:
    out = []
    for rule, heading in RULE_IDS:
        out += [
            Field(
                f"rules.{rule}.advice",
                "What to tell you",
                "paragraph",
                "Words in {braces} are filled in with the finding's details.",
                lines=5,
                group=heading,
            ),
            Field(
                f"rules.{rule}.bucket",
                "How soon",
                "choice",
                "watch: recheck in a week. plan: replace within roughly 4 to 8 weeks. act_now: do it today.",
                BUCKETS,
            ),
        ]
    return tuple(out)


ADVICE = Section(
    "playbook-advice",
    "Advice wording",
    "playbook.yaml",
    (),
    _advice_fields(),
    blurb="The recommendation that goes with each kind of finding, in plain words, no model needed. The conditions that pick a rule, and adding a new rule, are edited as text.",
    icon="file-text",
    switch="",
)  # fmt: skip

PAGES["thresholds"] = Page(
    "thresholds",
    "Thresholds and advice",
    "When each detector fires, and what the brief tells you to do about it.",
    (THRESHOLDS, ADVICE),
    "thresholds",
    "playbook.yaml",
)
SECTIONS.update({THRESHOLDS.id: THRESHOLDS, ADVICE.id: ADVICE})


# ---------------------------------------------------------------- devices, topology, map, views, sensors (Task 34)

MAC_RE = r"^(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$"
NODE_KINDS = (
    "internet", "gateway", "switch", "ap", "server", "vm", "container", "service", "camera",
    "iot_group", "client_group", "ups", "display", "other",
)  # fmt: skip
EDGE_KINDS = ("uplink", "hosts", "depends", "power")
EXAMPLE_ONLY = ("systems.yaml", "devices.yaml", "topology.yaml")  # these ship only as an example: start blank

DEVICES = Section(
    "devices",
    "Devices",
    "devices.yaml",
    (),
    (
        Rows(
            "devices",
            "Devices you want to treat specially",
            (
                Field("mac", "MAC address", "text", "Like 00:00:5E:00:53:01.", required=True, pattern=MAC_RE, pattern_help="a MAC address like 00:00:5E:00:53:01.", placeholder="00:00:5E:00:53:01"),
                Field("label", "Name", "text", "What the brief calls it."),
                Field("owner", "Owner", "text", "Optional."),
                Field("profile", "What is kept about it", "choice", "full: presence, DNS detail, odd-hours checks, traffic. security_only: that it exists and its traffic volume (the default for any unlisted device). ignore: nothing is stored about it.", ("full", "security_only", "ignore"), initial="security_only"),
                Field("quiet_hours", "Quiet hours (UTC)", "hours", "Optional, like 23:00-06:00. A full-profile device online during them is reported.", placeholder="23:00-06:00"),
            ),
            noun="device",
            help="Only devices that need a name or a different privacy level have to be listed. Anything else a collector sees is kept at the security_only level.",
        ),
    ),
    blurb="This file holds real MAC addresses and names, so it exists only on your server and is left out of support bundles. Client and DNS detail never leaves the LAN.",
    icon="box",
    switch="",
)  # fmt: skip

TOPOLOGY = Section(
    "topology",
    "Topology",
    "topology.yaml",
    (),
    (
        Rows(
            "nodes",
            "Extra things on the map",
            (
                Field("id", "Id", "text", "A short name; links refer to it.", required=True, pattern=r"^[A-Za-z0-9._-]{1,64}$", pattern_help="letters, digits, dots, hyphens, underscores.", placeholder="isp"),
                Field("kind", "Kind", "choice", "", NODE_KINDS, initial="other"),
                Field("label", "Label", "text"),
            ),
            noun="node",
            help="Things the collectors cannot discover: the ISP modem, a UPS, Home Assistant, a tablet.",
        ),
        Rows(
            "edges",
            "Links",
            (
                Field("from", "From", "text", "A node id, discovered or listed above.", required=True, placeholder="isp"),
                Field("to", "To", "text", required=True, placeholder="udm"),
                Field("kind", "Kind", "choice", "uplink: network. hosts: runs on. depends: needs. power: power feed.", EDGE_KINDS, initial="uplink"),
                Field("label", "Label", "text"),
            ),
            noun="link",
        ),
        Rows(
            "overrides",
            "Changes to discovered things",
            (
                Field("id", "Discovered id", "text", required=True),
                Field("label", "Show as", "text"),
                Field("hide", "Hide it", "switch"),
            ),
            noun="change",
            help="Rename or hide something a collector found. Your entries win over discovery.",
        ),
    ),
    blurb="Extra nodes and links for the network map, and changes to what was discovered.",
    icon="map",
    switch="",
)  # fmt: skip

MAP = Section(
    "map",
    "Network map",
    "map.yaml",
    (),
    (
        Field("refresh_s", "Refresh every", "number", "How often the interactive map re-reads its data.", unit="seconds", minimum=30, maximum=86400),
        Field("layers", "Layers", "multi", "What the map draws.", ("network", "services", "power")),
        Field("group_clients", "Client devices", "choice", "by_vlan: one node per network. none: hide them. each: one node per client (noisy, capped at 200).", ("by_vlan", "none", "each")),
        Field("show_containers", "Show containers", "switch"),
        Field("show_vms", "Show virtual machines", "switch"),
        Field("auto_layout", "Automatic layout", "choice", "Used only for nodes without a saved position.", ("cose", "grid", "circle", "breadthfirst")),
        Field("static_svg.enabled", "Also write a static picture (map.svg)", "switch", group="Static picture"),
        Field("static_svg.width", "Width", "number", unit="pixels", minimum=200, maximum=8000),
        Field("static_svg.height", "Height", "number", unit="pixels", minimum=200, maximum=8000),
        Field("drawio_export", "Also write a draw.io file (map.drawio)", "switch", "For editing in diagrams.net.", group="Export"),
    ),
    blurb="Positions you drag on the map are saved separately; these are the defaults.",
    icon="map",
    switch="enabled",
    switch_label="Show the network map",
)  # fmt: skip

VIEWS = Section(
    "views",
    "Tablet and picture board",
    "views.yaml",
    (),
    (
        Field("png.width", "Picture width", "number", "Match the tablet's screen so the board is not scaled.", unit="pixels", minimum=200, maximum=4000, group="Picture board (status.png)"),
        Field("png.height", "Picture height", "number", unit="pixels", minimum=200, maximum=4000),
        Field("png.font", "Font file", "text", "A .ttf in your config folder. Empty uses the image's built-in font.", clearable=True, placeholder="built-in"),
        Field("board.refresh_s", "Reload every", "number", "The page reloads itself (a meta tag, no script). 0 turns it off.", unit="seconds", minimum=0, maximum=86400, group="Tablet board (board.html)"),
        Field("board.stories", "Stories listed", "number", "How many top stories the board shows.", unit="stories", minimum=1, maximum=20),
    ),
    blurb="The static pages for tablets and e-readers. They use no scripts and no outside files.",
    icon="monitor",
    switch="",
)  # fmt: skip

REGISTRY = Section(
    "registry",
    "Sensors",
    "registry.yaml",
    (),
    (
        Rows(
            "metrics",
            "Sensors sent to Home Assistant",
            (
                Field("id", "Id", "text", "Changing an id makes a new sensor and leaves the old one behind.", required=True, pattern=r"^[a-z][a-z0-9_]{0,63}$", pattern_help="lowercase letters, digits, underscores."),
                Field("name", "Name", "text", "Shown in Home Assistant."),
                Field("group", "Group", "text"),
                Field("kind", "Kind", "choice", "number, state (green, yellow, red), or text.", ("number", "state", "text"), initial="number"),
                Field("unit", "Unit", "text"),
                Field("interval", "How often", "choice", "", ("hourly", "daily"), initial="hourly"),
            ),
            noun="sensor",
            help="Warning and critical levels on a sensor are edited as text.",
        ),
    ),
    blurb="One sensor per entry. A new collector metric needs a line here to appear in Home Assistant.",
    icon="radio",
    switch="",
)  # fmt: skip

PAGES["devices"] = Page(
    "devices",
    "Devices and map",
    "Devices with their own privacy level, extra things on the network map, and how the map is drawn.",
    (DEVICES, TOPOLOGY, MAP),
    "devices",
    "",
)
PAGES["display"] = Page(
    "display",
    "Pages and sensors",
    "The tablet and picture board, and the sensors published to Home Assistant.",
    (VIEWS, REGISTRY),
    "display",
    "",
)
SECTIONS.update({s.id: s for s in (DEVICES, TOPOLOGY, MAP, VIEWS, REGISTRY)})


# ---------------------------------------------------------------- AI, MCP, and agents (Task 36)

MODEL_RE = r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,120}$"
PROVIDER_ID = r"^[a-z][a-z0-9_-]{0,30}$"

AI_PROVIDERS = Section(
    "ai-providers",
    "Model providers",
    "ai.yaml",
    (),
    (
        Rows(
            "providers",
            "Providers",
            (
                Field("id", "Id", "text", "A short name that the fallback chains use.", required=True, pattern=PROVIDER_ID, pattern_help="lowercase letters, digits, hyphens, underscores.", slug=True, placeholder="local"),
                Field("type", "Kind", "choice", "openai_compatible works with Ollama, llama.cpp, vLLM, LM Studio, a LiteLLM gateway, and hosted services.", ("openai_compatible", "anthropic"), initial="openai_compatible"),
                Field("base_url", "Address", "text", "For an OpenAI-compatible server, its /v1 address. Not needed for the Anthropic kind.", pattern=r"^https?://\S+$", pattern_help="an address starting with http:// or https://.", placeholder="http://198.51.100.20:11434/v1"),
                Field("api_key_var", "Key variable", "var", "The container variable holding this provider's key, if it needs one. Never type the key itself.", placeholder="AI_LOCAL_API_KEY"),
                Field("model", "Model", "text", "Use List models on the card above to see what the server offers.", pattern=MODEL_RE, pattern_help="a model name such as llama3.1:8b."),
                Field("locality", "Where it runs", "choice", "lan: on your network. cloud: a hosted service, which only ever receives system-level summaries and only for features you allow.", ("lan", "cloud"), initial="lan"),
                Field("timeout_s", "Time limit", "number", unit="seconds", minimum=1, maximum=3600, initial=120),
                Field("max_output_tokens", "Longest answer", "number", unit="tokens", minimum=1, maximum=100000, initial=1200),
                Field("context_tokens", "Context size", "number", "Match the model server's own setting.", unit="tokens", minimum=256, maximum=2000000, initial=8192),
                Field("disable_thinking", "Turn off the model's separate reasoning mode", "switch"),
                Field("supports_tools", "Can call tools (needed for agents)", "switch", initial=True),
                Field("enabled", "Switched on", "switch"),
            ),
            noun="provider",
            help="Each provider is written as a block, one setting per line.",
            block=True,
            keep_examples=True,
        ),
    ),
    blurb="The model servers the product may talk to. Switch one on only when its address is set. A test is a real, tiny model call and counts toward the daily budget.",
    icon="sparkles",
    switch="",
)  # fmt: skip

AI_OTHER = Section(
    "ai-other",
    "Outside clients and logging",
    "ai.yaml",
    (),
    (
        Field("mcp.default_scope", "What a new client token may read by default", "multi", "logs, devices, and dns reveal more about your household: leave them off unless a client needs them.", ("status", "findings", "history", "systems", "map", "logs", "devices", "dns", "propose"), group="Outside AI clients (MCP)"),
        Field("logging.record_calls", "Record every model call", "switch", "Provider, model, feature, time, tokens, and outcome. Never the content.", group="Logging"),
    ),
    blurb="Defaults for tokens you create on the MCP tab, and the call log.",
    icon="shield",
    switch="",
)  # fmt: skip

AGENT_LIMITS = {
    "max_steps": ("Steps per run", "steps", 1, 30),
    "max_tool_calls": ("Tool calls per run", "calls", 1, 60),
    "max_tokens_per_run": ("Tokens per run", "tokens", 500, 200000),
    "timeout_s": ("Time limit per run", "seconds", 10, 3600),
    "max_runs_per_day": ("Runs per day", "runs", 1, 1000),
}

AGENT_DEFAULTS = Section(
    "agents-defaults",
    "Limits for every agent",
    "agents.yaml",
    ("defaults",),
    (
        *[
            Field(key, label, "number", unit=unit, minimum=low, maximum=high, group="Hard limits" if key == "max_steps" else "")
            for key, (label, unit, low, high) in AGENT_LIMITS.items()
        ],
        Field("chain", "Providers to try", "list", "Providers that can call tools, in order. Agents have no template fallback.", pattern=PROVIDER_ID, pattern_help="a provider id.", group="Model"),
    ),
    blurb="An agent that sets its own limit below overrides these. Every statement an agent writes must cite a tool result; that rule is locked on.",
    icon="bot",
    switch="",
)  # fmt: skip


def _agent_section(
    name: str, title: str, blurb: str, triggers: tuple[Field, ...], tools: tuple[str, ...]
) -> Section:
    limits = tuple(
        Field(
            key,
            label,
            "number",
            "Empty uses the limit for every agent.",
            unit=unit,
            minimum=low,
            maximum=high,
            group="Own limits" if key == "max_steps" else "",
        )
        for key, (label, unit, low, high) in AGENT_LIMITS.items()
    )
    return Section(
        f"agent-{name}",
        title,
        "agents.yaml",
        ("agents", name),
        (
            *triggers,
            *limits,
            Field(
                "tools",
                "Tools it may use",
                "multi",
                "All read-only. A tool that reads device detail or raw lines is never offered to a cloud model.",
                tools,
                group="Tools",
            ),
        ),
        blurb=blurb,
        icon="bot",
        switch="enabled",
        switch_label=f"Let {title.lower()} run",
    )


def _agent_sections() -> tuple[Section, ...]:
    from brief.agenttools import TOOLS

    names = tuple(sorted(TOOLS))
    return (
        _agent_section(
            "investigator", "Investigator", "Looks into a high-ranking finding and attaches a cited note to it.",
            (
                Field("trigger.on_finding_rank_at_least", "Investigate findings ranked at least", "number", "A finding at or above this rank is investigated in the background.", minimum=0, maximum=1000, group="When it runs"),
                Field("trigger.on_demand", "Also allow an on-demand run", "switch"),
            ),
            names,
        ),
        _agent_section(
            "triage", "Triage", "Gives a verdict on each new log pattern. It only advises; it never ignores or routes anything itself.",
            (
                Field("trigger.on_new_signatures", "Look at new log patterns", "switch", group="When it runs"),
                Field("trigger.after_learning_period", "Wait for the learning period to end", "switch"),
            ),
            names,
        ),
        _agent_section(
            "tuner", "Tuner", "Suggests single-value changes to thresholds and priorities. Each is replayed over the last week by the product, and nothing changes until you approve it.",
            (Field("trigger.schedule", "Run", "choice", "weekly, or never to run it only by hand.", ("weekly", "never"), group="When it runs"),),
            names,
        ),
        _agent_section(
            "ask_the_lab", "Ask the lab", "Answers questions typed on the Agents tab, with a citation for every fact.",
            (Field("trigger.on_demand", "Answer questions typed in the admin interface", "switch", group="When it runs"),),
            names,
        ),
    )  # fmt: skip


AGENT_SECTIONS = _agent_sections()
SECTIONS.update({s.id: s for s in (AI_PROVIDERS, AI_OTHER, AGENT_DEFAULTS, *AGENT_SECTIONS)})
