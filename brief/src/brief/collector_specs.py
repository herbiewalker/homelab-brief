"""What each collector's settings look like as form controls (Task 33). The settings framework (settingsui) turns
these into the forms on the Collectors page and writes the values back into collectors.yaml."""

from brief.settingsui import Field, Rows, Section

INTERVALS = ("hourly", "2h", "6h", "12h", "daily", "weekly")
ID = r"^[a-z0-9][a-z0-9-]{0,30}$"
ID_HELP = "Use letters, digits, and hyphens."
URL = r"^https?://\S+$"
URL_HELP = "An address starting with https:// (or http://)."


def _interval() -> Field:
    return Field("interval", "How often", "choice", "How often the worker polls this source.", INTERVALS)


def _var(key: str, label: str, help: str, required: bool = True) -> Field:
    return Field(key, label, "var", help, required=required, placeholder=key.upper())


UNRAID = Section(
    "collector-unraid",
    "Unraid servers",
    "collectors.yaml",
    ("collectors", "unraid"),
    (
        _interval(),
        Rows(
            "servers",
            "Servers",
            (
                Field(
                    "id",
                    "Name",
                    "text",
                    "Any name you like. It is saved as a lowercase id (Storage Server 2 becomes storage-server-2) and names this server's sensors.",
                    required=True,
                    pattern=ID,
                    slug=True,
                    pattern_help=ID_HELP,
                    placeholder="server-1",
                ),  # fmt: skip
                Field(
                    "url",
                    "Address",
                    "text",
                    "The server's GraphQL address.",
                    required=True,
                    pattern=URL,
                    pattern_help=URL_HELP,
                    placeholder="https://198.51.100.10/graphql",
                ),  # fmt: skip
                Field(
                    "key_var",
                    "Key variable",
                    "var",
                    "The container variable that holds this server's read-only API key.",
                    required=True,
                    placeholder="UNRAID_SERVER_1_API_KEY",
                ),  # fmt: skip
                Field(
                    "verify_tls",
                    "Check certificate",
                    "switch",
                    "Turn off for the usual self-signed certificate on a LAN address.",
                ),  # fmt: skip
            ),
            noun="server",
            required=True,
            help="One row per Unraid server. Create a read-only API key on each one (Settings, Management Access, API Keys).",
        ),
        Field(
            "smart_source",
            "SMART data from",
            "choice",
            "api: the overall verdict and temperature. smartctl_files: also reallocated, pending, and CRC counts, from "
            "files an hourly User Script writes.",
            ("api", "smartctl_files"),
        ),  # fmt: skip
        Field(
            "smart_files_path",
            "SMART files folder",
            "text",
            "Where the container sees those files (only for smartctl_files).",
            placeholder="/import-smart",
        ),  # fmt: skip
        Field(
            "timeout_s",
            "Time limit, all servers",
            "number",
            "Stops the whole run after this long.",
            unit="seconds",
            minimum=5,
            maximum=900,
        ),  # fmt: skip
        Field(
            "request_timeout_s",
            "Time limit, each request",
            "number",
            "",
            unit="seconds",
            minimum=2,
            maximum=300,
        ),
        Field(
            "slow_request_timeout_s",
            "Time limit, disk health request",
            "number",
            "Reading disk health can wake sleeping drives, which takes a while. A server that times out here only "
            "when its drives are asleep needs a longer limit. Each run can spin drives up: set the interval to daily "
            "if that matters.",
            unit="seconds",
            minimum=2,
            maximum=300,
        ),
    ),
    blurb="Array state, disks, SMART, parity checks, containers, VMs, and notifications, read from each server's API.",
    icon="server",
)

UNIFI = Section(
    "collector-unifi",
    "UniFi",
    "collectors.yaml",
    ("collectors", "unifi"),
    (
        _interval(),
        _var(
            "url_var",
            "Console address variable",
            "The container variable that holds the console address, for example https://198.51.100.1.",
        ),  # fmt: skip
        _var(
            "key_var",
            "API key variable",
            "The container variable that holds the read-only API key (Network application, Integrations).",
        ),
        Field(
            "verify_tls",
            "Check certificate",
            "switch",
            "Turn off for the usual self-signed certificate on a LAN console.",
        ),
        Field(
            "site", "Site", "text", "Read one site only; leave empty for every site.", placeholder="Default"
        ),
        Field(
            "api_path",
            "API path",
            "text",
            "Only for a self-hosted Network application (/integration); a UniFi OS console is the default.",
            placeholder="/proxy/network/integration",
        ),  # fmt: skip
        Field(
            "clients",
            "Connected clients",
            "switch",
            "Who is connected, hourly. Each device is limited by its profile in devices.yaml.",
        ),
        Field("traffic", "Traffic per device", "switch", "Hourly byte counters (needs connected clients)."),
        Field(
            "client_logs",
            "Client lines from syslog",
            "switch",
            "Gateway DNS and access point join and leave lines.",
        ),
    ),
    blurb="Devices, firmware, and uplinks from the UniFi Network Integration API. Firmware is also read from syslog headers without a key.",
    icon="network",
)

SNMP = Section(
    "collector-snmp",
    "SNMP devices",
    "collectors.yaml",
    ("collectors", "snmp"),
    (
        _interval(),
        Field(
            "version", "Version", "choice", "v3 is preferred; v2c is fine on a trusted LAN.", ("v3", "v2c")
        ),
        _var("user_var", "User variable", "SNMPv3 user.", False),
        _var("auth_key_var", "Authentication key variable", "SNMPv3 authentication key.", False),
        _var("priv_key_var", "Encryption key variable", "SNMPv3 encryption key (authPriv).", False),
        _var(
            "community_var",
            "Community variable",
            "v2c only: the variable that holds the community string.",
            False,
        ),
        Field(
            "auth_protocol",
            "Authentication",
            "choice",
            "",
            ("MD5", "SHA", "SHA-224", "SHA-256", "SHA-384", "SHA-512"),
        ),
        Field("priv_protocol", "Encryption", "choice", "", ("DES", "AES", "AES-192", "AES-256")),
        Rows(
            "targets",
            "Devices",
            (
                Field(
                    "id",
                    "Name",
                    "text",
                    "",
                    required=True,
                    pattern=ID,
                    slug=True,
                    pattern_help=ID_HELP,
                    placeholder="switch-1",
                ),
                Field("host", "Address", "text", "", required=True, placeholder="198.51.100.2"),
                Field(
                    "profile",
                    "Kind",
                    "choice",
                    "switch: uptime, ports, errors. ups: the UPS MIB. custom: your own OIDs (edit as text).",
                    ("switch", "ups", "custom"),
                    required=True,
                ),  # fmt: skip
            ),
            noun="device",
            help="One row per device. A custom device's OID list is kept as it is; edit it as text.",
        ),
    ),
    blurb="Switch, UPS, and PDU numbers over SNMP.",
    icon="activity",
)

IDRAC = Section(
    "collector-idrac",
    "iDRAC",
    "collectors.yaml",
    ("collectors", "idrac"),
    (
        _interval(),
        Field(
            "protocol",
            "Protocol",
            "choice",
            "redfish for iDRAC 8 and 9; ipmi is all an iDRAC 7 offers.",
            ("redfish", "ipmi"),
        ),
        _var("host_var", "Address variable", "The variable that holds the iDRAC address."),
        _var("user_var", "User variable", "A read-only account."),
        _var("password_var", "Password variable", "The variable that holds its password."),
        Field(
            "verify_tls",
            "Check certificate",
            "switch",
            "Redfish only. Turn off for the usual self-signed certificate.",
        ),
        Field("privilege", "Privilege (IPMI)", "text", "", placeholder="USER"),
        Field(
            "cipher_suite",
            "Cipher suite (IPMI)",
            "number",
            "Set only if the default is refused (often 3).",
            minimum=0,
            maximum=20,
        ),
    ),
    blurb="Power draw, temperatures, fans, and the hardware event log from a Dell iDRAC.",
    icon="zap",
)

HA = Section(
    "collector-ha_statestream",
    "Home Assistant power",
    "collectors.yaml",
    ("collectors", "ha_statestream"),
    (
        Field(
            "base_topic",
            "MQTT base topic",
            "text",
            "Must match mqtt_statestream base_topic in Home Assistant.",
            required=True,
            placeholder="homelab/ha",
        ),  # fmt: skip
        Field(
            "window_s",
            "Listen time",
            "number",
            "How long each hourly run listens for retained states.",
            unit="seconds",
            minimum=1,
            maximum=60,
        ),  # fmt: skip
        Rows(
            "entities",
            "Entities",
            (
                Field("entity", "Entity", "text", "", required=True, placeholder="sensor.server_power"),
                Field(
                    "system",
                    "System",
                    "text",
                    "A system id from systems.yaml (power adds up per system).",
                    placeholder="server-1",
                ),
                Field(
                    "kind",
                    "Kind",
                    "choice",
                    "power: watts. energy: a cumulative kWh meter.",
                    ("power", "energy"),
                ),
                Field(
                    "scale",
                    "Scale",
                    "number",
                    "1000 turns kW into W; 0.001 turns Wh into kWh.",
                    integer=False,
                ),
            ),
            noun="entity",
            required=True,
            help="The Home Assistant entities to read. No Home Assistant token is needed; it uses the same MQTT broker.",
        ),
    ),
    blurb="Power and energy that Home Assistant already knows, read over MQTT.",
    icon="zap",
)

DNS = Section(
    "collector-dns_filter",
    "DNS filter",
    "collectors.yaml",
    ("collectors", "dns_filter"),
    (
        _interval(),
        Field("source", "Product", "choice", "", ("adguard_home", "pihole")),
        _var("url_var", "Address variable", "The variable that holds the filter's address."),
        _var("token_var", "Token variable", "The variable that holds its token."),
    ),
    blurb="Only if the gateway's own DNS logs are not enough.",
    icon="shield",
)

SECTIONS = {s.id: s for s in (UNRAID, UNIFI, SNMP, IDRAC, HA, DNS)}
BY_COLLECTOR = {s.root[-1]: s for s in SECTIONS.values()}
