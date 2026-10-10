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
SECTIONS = {s.id: s for s in (SCHEDULE, RETENTION, APPRISE, MQTT, NETWORK, PORTS, SYSTEMS)}
