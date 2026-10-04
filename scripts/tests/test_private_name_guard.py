import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import private_name_guard as g  # noqa: E402

# Built from parts so this file does not trip the guard itself.
LAN_IP = ".".join(["192", "168", "77", "5"])
MAC_A = ":".join(["3c", "5a", "b4", "11", "22", "33"])
SYSTEMS = """
systems:
  - id: cam
    label: "Basement Camera"
    hosts: ["fortknox-nvr", "<placeholder-host>"]
  - id: two
    label: "Short"
    hosts:
      - nas-upstairs
      - ab
"""


def private_from(text):
    return g._compile(g.tokens_from_yaml(text, g.LABEL_KEYS) | g.tokens_from_any(text))


def scan(line, private=(), allow=frozenset()):
    return g.scan_line(line, list(private), set(allow))


def test_yaml_tokens_cover_inline_and_block_lists_but_skip_placeholders_and_short_values():
    tokens = g.tokens_from_yaml(SYSTEMS, g.LABEL_KEYS)
    assert {"fortknox-nvr", "Basement Camera", "nas-upstairs"} <= tokens
    assert "<placeholder-host>" not in tokens
    assert "ab" not in tokens and "Short" in tokens


def test_private_token_is_blocked_case_insensitively_on_word_boundaries():
    private = private_from(SYSTEMS)
    assert scan("see FortKnox-NVR for details", private)
    assert not scan("fortknox-nvrs are not a thing", private)


def test_findings_are_masked():
    private = private_from(SYSTEMS)
    (hit,) = scan("fortknox-nvr", private)
    assert "fortknox-nvr" not in hit and hit.endswith("fo***")


def test_generic_private_ip_and_real_mac_are_blocked():
    assert scan(f"gateway {LAN_IP}")
    assert scan(f"device {MAC_A}")


def test_documentation_values_pass():
    assert not scan("listen on 0.0.0.0 or 127.0.0.1")
    assert not scan("version 3.2.3.2 of s6")
    assert not scan("mac 00:00:00:00:00:01")
    assert not scan("public ip 8.8.8.8 is not a private address")


def test_allow_list_and_inline_marker_exempt():
    assert not scan(f"gateway {LAN_IP}", allow={LAN_IP})
    assert not scan(f"gateway {LAN_IP}  guard:allow")
    private = private_from(SYSTEMS)
    assert not scan("fortknox-nvr", private, allow={"fortknox-nvr"})


def test_mac_in_private_file_is_caught_in_any_separator_style():
    private = g._compile(g.tokens_from_any(f"mac: {MAC_A}"))
    assert scan(MAC_A.replace(":", "-"), private)
    assert scan(MAC_A.replace(":", ""), private)


def test_parse_added_lines_reports_new_file_line_numbers():
    diff = (
        "diff --git a/x.md b/x.md\n--- a/x.md\n+++ b/x.md\n@@ -3,0 +4,2 @@\n+first\n+second\n"
        "@@ -9 +11 @@\n-old\n+third\n"
    )
    assert list(g.parse_added_lines(diff)) == [
        ("x.md", 4, "first"),
        ("x.md", 5, "second"),
        ("x.md", 11, "third"),
    ]


def test_systems_labels_are_not_private_but_hosts_are():
    tokens = g.tokens_from_yaml(SYSTEMS, g.YAML_KEYS["systems"])
    assert "fortknox-nvr" in tokens and "nas-upstairs" in tokens
    assert "Basement Camera" not in tokens


def test_rfc7042_documentation_macs_pass():
    assert not scan("sender 00:00:5e:00:53:2a")
    assert not scan("sender 00-00-5E-00-53-FF")


def test_rfc5737_documentation_addresses_pass():
    for ip in ("192.0.2.9", "198.51.100.77", "203.0.113.200"):
        assert not scan(f"gateway {ip}")
