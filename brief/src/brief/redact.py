"""Taking an install's names out of text: for logs and for the support bundle.

Two tools, for two jobs:

`scrub(text)` is for logs. It replaces anything shaped like an IP address, a MAC address, an e-mail
address, a URL with credentials, or a long secret with a fixed placeholder. It needs no setup and keeps no
state, so it can sit in every log call.

`Redactor` is for the support bundle. It also knows the private words of this install (system labels,
host names, device labels and owners) and gives each distinct value a stable alias (`<ip-3>`, `<name-1>`),
so the structure stays readable ("the same sender appears in three places") while the values are gone.
The aliases are never written next to the bundle unless the user asks for the map.

Neither can find a private name it was never told about. That is why the bundle holds counts and
structure instead of log lines or message text, and why it checks its own output before it is written.
"""

import ipaddress
import re
from collections.abc import Iterable

IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d]|\.\d)")
IPV6 = re.compile(r"(?<![0-9A-Za-z:])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![0-9A-Za-z:])")
MAC = re.compile(
    r"(?<![0-9A-Fa-f])(?<![0-9A-Fa-f][:-])(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}"
    r"(?![0-9A-Fa-f])(?![:-][0-9A-Fa-f]{2}(?![0-9A-Fa-f]))"
)
EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
URL_CREDENTIALS = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/@:]+(?::[^\s/@]*)?@")
SECRET_KV = re.compile(
    r"(?i)\b(password|passwd|secret|token|api[_-]?key|apikey|authorization|psk|passphrase)\b(\s*[=:]\s*)(\"[^\"]*\"|\S+)"
)
LONG_HEX = re.compile(r"(?<![A-Za-z0-9])[0-9A-Fa-f]{32,}(?![A-Za-z0-9])")
LONG_B64 = re.compile(r"(?<![A-Za-z0-9+/_=-])[A-Za-z0-9+/_-]{40,}={0,2}(?![A-Za-z0-9+/_=-])")
FILE_SUFFIXES = frozenset(
    "yaml yml md py json db log gz tar csv html png txt ini bin vrl sql tgz xml js css sh conf pem key pub".split()
)
DOMAIN = re.compile(r"\b(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,24}\b")
KNOWN_TLDS = frozenset(
    "com net org io lan local home internal example arpa app dev cloud co uk de nl us eu info biz me tv".split()
)
MIN_NAME = 3
SECRET = "[REDACTED]"


def _valid_ip(text: str) -> bool:
    try:
        ipaddress.ip_address(text)
        return True
    except ValueError:
        return False


def _looks_like_domain(match: str) -> bool:
    labels = match.lower().split(".")
    if labels[-1] in FILE_SUFFIXES:
        return False
    return len(labels) >= 3 or labels[-1] in KNOWN_TLDS


def scrub(text: str) -> str:
    """For logs: IPs, MACs, e-mail addresses, credentials, and long secrets become fixed placeholders."""
    text = URL_CREDENTIALS.sub(lambda m: m.group(1) + "<credentials>@", text)
    text = SECRET_KV.sub(lambda m: m.group(1) + m.group(2) + SECRET, text)
    text = EMAIL.sub("<email>", text)
    text = MAC.sub("<mac>", text)
    text = IPV4.sub(lambda m: "<ip>" if _valid_ip(m.group(1)) else m.group(0), text)
    text = IPV6.sub(lambda m: "<ip6>" if _valid_ip(m.group(0)) else m.group(0), text)
    text = LONG_HEX.sub(SECRET, text)
    return LONG_B64.sub(SECRET, text)


class Redactor:
    """Stable aliases for one bundle: the same value always becomes the same alias."""

    def __init__(self, names: Iterable[str] = ()):
        self.map: dict[str, str] = {}
        self._counts: dict[str, int] = {}
        learned = {n.strip() for n in names if n and len(n.strip()) >= MIN_NAME}
        # longest first, so "kitchen tablet" is replaced before "kitchen"
        self._names = sorted(learned, key=len, reverse=True)
        self._name_rx = (
            re.compile(
                "|".join(rf"(?<![A-Za-z0-9]){re.escape(n)}(?![A-Za-z0-9])" for n in self._names),
                re.IGNORECASE,
            )
            if self._names
            else None
        )

    def alias(self, kind: str, value: str) -> str:
        key = f"{kind}:{value.lower()}"
        if key not in self.map:
            self._counts[kind] = self._counts.get(kind, 0) + 1
            self.map[key] = f"<{kind}-{self._counts[kind]}>"
        return self.map[key]

    def text(self, text: str) -> str:
        text = URL_CREDENTIALS.sub(lambda m: m.group(1) + "<credentials>@", text)
        text = SECRET_KV.sub(lambda m: m.group(1) + m.group(2) + SECRET, text)
        text = EMAIL.sub(lambda m: self.alias("email", m.group(0)), text)
        text = MAC.sub(lambda m: self.alias("mac", m.group(0)), text)
        text = IPV4.sub(lambda m: self.alias("ip", m.group(1)) if _valid_ip(m.group(1)) else m.group(0), text)
        text = IPV6.sub(
            lambda m: self.alias("ip6", m.group(0)) if _valid_ip(m.group(0)) else m.group(0), text
        )
        text = DOMAIN.sub(
            lambda m: self.alias("domain", m.group(0)) if _looks_like_domain(m.group(0)) else m.group(0), text
        )
        if self._name_rx:
            text = self._name_rx.sub(lambda m: self.alias("name", m.group(0)), text)
        text = LONG_HEX.sub(SECRET, text)
        return LONG_B64.sub(SECRET, text)

    def value(self, obj):
        """The same, through nested lists and dicts (JSON and YAML documents)."""
        if isinstance(obj, str):
            return self.text(obj)
        if isinstance(obj, list):
            return [self.value(x) for x in obj]
        if isinstance(obj, dict):
            return {self.text(str(k)) if isinstance(k, str) else k: self.value(v) for k, v in obj.items()}
        return obj

    def leaks(self, text: str) -> list[str]:
        """What is still in `text` that should not be: an IP, a MAC, an e-mail, a domain, or a learned name."""
        found = [m.group(1) for m in IPV4.finditer(text) if _valid_ip(m.group(1))]
        found += [m.group(0) for m in IPV6.finditer(text) if _valid_ip(m.group(0))]
        found += [m.group(0) for m in MAC.finditer(text)]
        found += [m.group(0) for m in EMAIL.finditer(text)]
        found += [m.group(0) for m in DOMAIN.finditer(text) if _looks_like_domain(m.group(0))]
        if self._name_rx:
            found += [m.group(0) for m in self._name_rx.finditer(text)]
        return found
