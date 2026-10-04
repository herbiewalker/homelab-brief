#!/usr/bin/env python3
"""Block commits that contain private hostnames, IPs, MACs, or device names.

Two layers, both on the lines being added:
  1. Private tokens read from the instance folder (see scripts/instance.py): systems.yaml,
     devices.yaml, topology.yaml, any IPv4 or MAC in private/*, and guard/private-names.txt.
     The old repo-local gitignored copies are still read if present.
  2. Generic patterns that never belong in the repo: RFC 1918 / link-local IPv4 addresses and
     real-looking MAC addresses.
Values in scripts/guard-allow.txt (generic ones, committed) or the instance's guard/allow.txt
(yours) are exempt, as is any line containing "guard:allow". Findings print file, line, and a
masked token, never the full value, so logs stay clean. Without an instance (for example in CI)
only layer 2 runs.

Usage: private_name_guard.py            check staged changes (pre-commit)
       private_name_guard.py --all      check every tracked file (CI, manual)
"""

import ipaddress
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import instance  # noqa: E402

ROOT = instance.ROOT
ALLOW_FILE = ROOT / "scripts" / "guard-allow.txt"
HOST_KEYS = {"hosts", "host", "hostname", "hostnames", "alias", "aliases", "ssid"}
# Labels and owners can be real device or person names in devices.yaml and topology.yaml. In
# systems.yaml a label is only a generic tile title, so it is not treated as private there.
LABEL_KEYS = HOST_KEYS | {"name", "label", "owner"}
YAML_KEYS = {"systems": HOST_KEYS, "devices": LABEL_KEYS, "topology": LABEL_KEYS}
MIN_TOKEN = 4
DOC_MAC_PREFIX = "00005e0053"
# Only the real private ranges are sensitive: RFC 1918 plus link-local. Netmasks, loopback, and the
# RFC 5737 documentation ranges used by the sample sanitizer are not.
PRIVATE_NETS = [  # guard:allow
    ipaddress.ip_network(n)  # guard:allow
    for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16")  # guard:allow
]

IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.]*\d)")
MAC = re.compile(r"(?<![0-9A-Fa-f])((?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2})(?![0-9A-Fa-f])")
KEY_LINE = re.compile(r"^\s*(?:-\s*)?([A-Za-z_]+):\s*(.*?)\s*$")
LIST_ITEM = re.compile(r"^\s*-\s+(.*?)\s*$")
QUOTED = re.compile(r"""["']([^"']+)["']""")


def mask(token: str) -> str:
    return token[:2] + "***"


def is_sensitive_ip(text: str) -> bool:
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return False
    return ip.version == 4 and any(ip in net for net in PRIVATE_NETS)


def is_real_mac(text: str) -> bool:
    digits = re.sub(r"[^0-9a-f]", "", text.lower())
    if digits.startswith(DOC_MAC_PREFIX):  # RFC 7042 documentation range, used by the sample sanitizer
        return False
    return len(digits) == 12 and len(set(digits)) > 2 and digits != "ffffffffffff"


def mac_variants(mac: str) -> set[str]:
    digits = re.sub(r"[^0-9a-f]", "", mac.lower())
    pairs = [digits[i : i + 2] for i in range(0, 12, 2)]
    return {":".join(pairs), "-".join(pairs), digits}


def load_allow(*paths: Path) -> set[str]:
    out = set()
    for path in paths or (ALLOW_FILE, instance.guard_file("allow.txt")):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                out.add(line.lower())
    return out


def _clean(value: str) -> str:
    return value.split(" #")[0].strip().strip("\"'")


def tokens_from_yaml(text: str, keys: set[str]) -> set[str]:
    tokens: set[str] = set()
    in_name_list = False
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = KEY_LINE.match(line)
        if m:
            key, value = m.group(1).lower(), m.group(2)
            in_name_list = key in keys and value == ""
            if key in keys and value:
                quoted = QUOTED.findall(value)
                tokens.update(quoted if quoted else [_clean(value)])
            continue
        if in_name_list:
            item = LIST_ITEM.match(line)
            if item:
                tokens.add(_clean(item.group(1)))
            elif line.strip():
                in_name_list = False
    return {t for t in tokens if len(t) >= MIN_TOKEN and not t.startswith("<")}


def tokens_from_any(text: str) -> set[str]:
    found = set(IPV4.findall(text))
    for mac in MAC.findall(text):
        found |= mac_variants(mac)
    return found


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def load_private_tokens() -> set[str]:
    tokens: set[str] = set()
    yaml_files = dict(instance.config_files())
    legacy = {name: ROOT / "brief" / f"{name}.yaml" for name in YAML_KEYS}  # gitignored copies
    for name, keys in YAML_KEYS.items():
        for path in (yaml_files[name], legacy[name]):
            text = _read(path)
            if text is not None:
                tokens |= tokens_from_yaml(text, keys) | tokens_from_any(text)
    for path in instance.private_text_files():
        text = _read(path)
        if text is not None:
            tokens |= tokens_from_any(text)
    for path in (instance.guard_file("private-names.txt"), ROOT / ".dev" / "private-names.txt"):
        text = _read(path)
        if text is not None:
            tokens |= {
                ln.strip()
                for ln in text.splitlines()
                if len(ln.strip()) >= MIN_TOKEN and not ln.startswith("#")
            }
    return {t for t in tokens if len(t) >= MIN_TOKEN}


def _compile(tokens: set[str]) -> list[tuple[str, re.Pattern]]:
    return [
        (t, re.compile(rf"(?<![A-Za-z0-9]){re.escape(t)}(?![A-Za-z0-9])", re.IGNORECASE))
        for t in sorted(tokens, key=len, reverse=True)
    ]


def scan_line(line: str, private, allow: set[str]) -> list[str]:
    """Return masked descriptions of every violation in one line."""
    if "guard:allow" in line:
        return []
    hits = []
    for token, pattern in private:
        if token.lower() in allow:
            continue
        if pattern.search(line):
            hits.append(f"private token {mask(token)}")
    for ip in IPV4.findall(line):
        if ip.lower() not in allow and is_sensitive_ip(ip):
            hits.append(f"private IPv4 {mask(ip)}")
    for mac in MAC.findall(line):
        if is_real_mac(mac) and not mac_variants(mac) & allow:
            hits.append(f"MAC address {mask(mac)}")
    return hits


def scan_text(path: str, text: str, private, allow: set[str]) -> list[str]:
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        for hit in scan_line(line, private, allow):
            out.append(f"{path}:{n}: {hit}")
    return out


def parse_added_lines(diff: str):
    """Yield (path, line_number, text) for each added line in a -U0 unified diff."""
    path, lineno = None, 0
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            path = raw[6:] if raw.startswith("+++ b/") else None
        elif raw.startswith("@@"):
            m = re.search(r"\+(\d+)", raw)
            lineno = int(m.group(1)) if m else 0
        elif raw.startswith("+") and not raw.startswith("+++") and path:
            yield path, lineno, raw[1:]
            lineno += 1


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
        encoding="utf-8",
        errors="replace",
    ).stdout


def main(argv: list[str]) -> int:
    allow = load_allow()
    private = _compile(load_private_tokens())
    problems: list[str] = []
    if "--all" in argv:
        for rel in _git("ls-files", "-z").split("\0"):
            if not rel:
                continue
            p = ROOT / rel
            try:
                text = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            problems += scan_text(rel, text, private, allow)
    else:
        diff = _git("diff", "--cached", "-U0", "--no-color", "--diff-filter=ACMR")
        for path, lineno, text in parse_added_lines(diff):
            for hit in scan_line(text, private, allow):
                problems.append(f"{path}:{lineno}: {hit}")
    if problems:
        print(
            "private-name guard: blocked. Remove these, or add a harmless value to scripts/guard-allow.txt:"
        )
        print("\n".join(problems))
        return 1
    layers = "private tokens + generic" if private else "generic only (no private files found)"
    print(f"private-name guard: ok ({layers})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
