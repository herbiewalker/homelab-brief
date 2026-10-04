#!/usr/bin/env python3
"""Turn real syslog samples (the instance folder's samples/raw) into sanitized ones (samples/sanitized).

Everything private is replaced with a stable placeholder, and every line keeps its structure
(timestamp, host field, program, CEF fields, JSON) because the parsers are built against it.

  hostnames, device, client, SSID, network, camera, and user names -> host-01, client-02, ...
  IPv4 / IPv6 addresses        -> RFC 5737 / RFC 3849 documentation ranges
  MAC addresses (any style)    -> RFC 7042 documentation range
  DNS domains in coredns lines -> domain-01.example
  e-mail addresses             -> email-01@example.com
  tokens, passwords, serials   -> [REDACTED], and the line is listed for manual review
  your private words           -> SITE  (the instance's guard/private-names.txt, sanitizer-words.txt)

The same input always maps to the same placeholder; the mapping lives in the instance's
.state/sanitizer-map.json. Before anything is written, a self-check confirms that no replaced
value survives anywhere in the output and that the commit guard finds nothing; if either fails,
nothing is written. The check cannot find a private name it was never taught, so read the
output before committing.

Usage (from the repo root, in WSL or Linux):  python3 scripts/sanitize_samples.py
"""

import argparse
import ipaddress
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import instance  # noqa: E402
import private_name_guard as guard  # noqa: E402

ROOT = guard.ROOT  # the repository; private files live in the instance folder
RAW_DIR = instance.raw_dir()
OUT_DIR = instance.sanitized_dir()
MAP_FILE = instance.state_dir() / "sanitizer-map.json"
REPORT_FILE = instance.state_dir() / "sanitize-report.txt"
WORD_FILES = [instance.guard_file("private-names.txt"), instance.guard_file("sanitizer-words.txt")]

SITE = "SITE"
REDACTED = "[REDACTED]"
KEEP_USERS = {"root", "nobody", "daemon", "sshd", "messagebus", "www-data"}
MIN_NAME = 4  # shorter learned names are replaced only in the header, never in free text

TS = r"[A-Z][a-z]{2} [ \d]\d \d\d:\d\d:\d\d"
HEADER = re.compile(rf"^({TS}) (.*)$")
CEF_HOST = re.compile(r"^(\S+(?: \| \S+)*) CEF:")
KEY_MATCH = re.compile(r"(?<![\w\"])([A-Za-z][A-Za-z0-9_]*)=")
SECRET_KV = re.compile(
    r"(?i)\b(password|passwd|secret|token|api[_-]?key|apikey|authorization|psk|passphrase"
    r"|serial(?:_?number)?)\b(\s*[=:]\s*)(\"[^\"]*\"|\S+)"
)
BEARER = re.compile(r"(?i)\b(Bearer\s+)(\S+)")
LONG_HEX = re.compile(r"(?<![A-Za-z0-9])[0-9A-Fa-f]{32,}(?![A-Za-z0-9])")
LONG_B64 = re.compile(r"(?<![A-Za-z0-9+/_=-])[A-Za-z0-9+/_-]{40,}={0,2}(?![A-Za-z0-9+/_=-])")
EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
MAC = re.compile(
    r"(?<![0-9A-Fa-f])(?<![0-9A-Fa-f][:-])(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}"
    r"(?![0-9A-Fa-f])(?![:-][0-9A-Fa-f]{2}(?![0-9A-Fa-f]))"
)
IPTABLES_MAC = re.compile(r"(\bMAC=)((?:[0-9A-Fa-f]{2}:){11}[0-9A-Fa-f]{2})((?::[0-9A-Fa-f]{2})*)")
MAC_NOSEP = re.compile(r"(?<![0-9A-Za-z])([0-9A-Fa-f]{12}),(?=[A-Za-z0-9])")
UUID = re.compile(
    r"(?<![0-9A-Fa-f-])[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}(?![0-9A-Fa-f-])"
)
EVENT_ID = re.compile(r"(eventId=)([0-9A-Fa-f]{24})(?![0-9A-Fa-f])")
IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?!\d|\.\d)")
IPV6 = re.compile(r"(?<![0-9A-Za-z:])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![0-9A-Za-z:])")
DNS_DOMAIN = re.compile(r'("domain":")([^"]+)(")')
CAMERA = re.compile(r'(msg="' + SITE + r'\s+)([^"(]+?)(\s+\()')  # the model's ")" is sometimes cut off
USER_PATTERNS = [
    re.compile(
        r"(?i)(\b(?:successful|unsuccessful|failed|invalid)\s+login\s+(?:user\s+)?)([A-Za-z0-9._@-]+)"
    ),
    re.compile(r"(?i)(\bsession\s+(?:opened|closed)\s+for\s+user\s+)([^\s(]+)"),
    re.compile(r"(?i)((?:Accepted|Failed)\s+\w+\s+for\s+(?:invalid user\s+)?)(\S+)"),
    re.compile(r"(?i)(\bInvalid user\s+)(\S+)"),
    re.compile(r"(?i)(\b(?:password|pubkey|public key)\s+auth\s+\w+\s+for\s+')([^']+)(?=')"),
    re.compile(r"(?i)(\bdropbear\[\d+\]:\s+Exit\s+\()([^)\s]+)(?=\))"),
]
FW_RULE = re.compile(r'(DESCR="[^"\[]*\[)([^\]"]+)')
DHCP_NAME = re.compile(r"(?i)(DHCP(?:ACK|REQUEST|OFFER|DISCOVER|INFORM)\([^)]*\)\s+\S+\s+\S+\s+)(\S+)")
DOMAIN_LIKE = re.compile(
    r"\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|net|org|io|local|lan|home|arpa|us|co|dev|cloud)\b", re.I
)

PLACEHOLDER_NAME = re.compile(r"(?:host|client|device|ssid|network|admin|user|camera|rule)-\d+")
UNIFI_NAME_KEYS = {
    "UNIFIclientAlias": "client",
    "UNIFIclientHostname": "client",
    "UNIFIwifiName": "ssid",
    "UNIFInetworkName": "network",
    "UNIFIadmin": "admin",
}
UNIFI_DEVICE_KEY = re.compile(r"^UNIFI\w*DeviceName$")


class SanitizeError(Exception):
    pass


class Mapper:
    """Hands out stable placeholders; state is a plain dict so it can be saved as JSON."""

    def __init__(self, state=None):
        self.state = state if state is not None else {}

    def _get(self, cat, key, make):
        table = self.state.setdefault(cat, {})
        if key not in table:
            table[key] = make(len(table) + 1)
        return table[key]

    def name(self, cat, value):
        return self._get(cat, value.lower(), lambda n: f"{cat}-{n:02d}")

    def ip4(self, value):
        ip = ipaddress.IPv4Address(value)
        cat = "ip4_private" if (ip.is_private or ip.is_link_local) else "ip4_public"
        base = "198.51.100" if cat == "ip4_private" else "203.0.113"

        def make(n):
            if n > 254:
                raise SanitizeError(f"more than 254 distinct {cat} addresses; widen the placeholder pool")
            return f"{base}.{n}"

        return self._get(cat, str(ip), make)

    def ip6(self, value):
        key = str(ipaddress.IPv6Address(value))
        return self._get("ip6", key, lambda n: f"2001:db8::{n:x}")

    def mac_digits(self, digits):
        def make(n):
            if n > 255:
                raise SanitizeError("more than 255 distinct MAC addresses; widen the placeholder pool")
            return f"{guard.DOC_MAC_PREFIX}{n:02x}"

        return self._get("mac", digits.lower(), make)

    def uuid(self, value):
        return self._get("uuid", value.lower(), lambda n: f"00000000-0000-4000-8000-{n:012x}")

    def object_id(self, value):
        return self._get("object_id", value.lower(), lambda n: f"{n:024x}")

    def domain(self, value):
        return self._get("domain", value.lower().rstrip("."), lambda n: f"domain-{n:02d}.example")

    def email(self, value):
        return self._get("email", value.lower(), lambda n: f"email-{n:02d}@example.com")


def format_mac(digits12, original):
    sep = ":" if ":" in original else "-" if "-" in original else ""
    text = sep.join(digits12[i : i + 2] for i in range(0, 12, 2))
    return text.upper() if original.upper() == original and re.search("[A-F]", original.upper()) else text


def iter_unifi_values(line):
    """Yield (key, start, end) for each UNIFI* key=value in a CEF line."""
    matches = list(KEY_MATCH.finditer(line))
    for i, m in enumerate(matches):
        key = m.group(1)
        if not key.startswith("UNIFI"):
            continue
        end = matches[i + 1].start() - 1 if i + 1 < len(matches) else len(line)
        yield key, m.end(), max(m.end(), end)


def read_words():
    ci, cs = set(), set()
    for path in WORD_FILES:
        if not path.exists():
            continue
        for raw in path.read_text(encoding="utf-8").splitlines():
            word = raw.strip()
            if not word or word.startswith("#"):
                continue
            (ci if len(word) >= MIN_NAME else cs).add(word)
    return ci, cs


class Sanitizer:
    def __init__(self, mapper=None, words_ci=(), words_cs=(), extra_hosts=()):
        self.mapper = mapper if mapper is not None else Mapper()
        self.words_ci = {w.lower() for w in words_ci}
        self.words_cs = set(words_cs)
        self.names = {}  # lower name -> category, for replacement inside free text
        self.raw_values = set()  # every raw value replaced, for the self-check
        self.counts = Counter()
        self.flags = []  # (line index, kind); never the value
        self.domains_seen = set()
        for host in extra_hosts:
            self._learn_name("host", host)
        self._name_re = None

    def _learn_name(self, cat, value, pipes=False):
        value = value.strip().strip('"')
        if len(value) >= MIN_NAME and (pipes or " | " not in value):
            self.names.setdefault(value.lower(), cat)
            self.raw_values.add(value)
            self._name_re = None

    def learn(self, lines):
        for line in lines:
            m = HEADER.match(line)
            if not m:
                continue
            rest = m.group(2)
            cef = CEF_HOST.match(rest)
            if cef:
                if " | " not in cef.group(1):
                    self._learn_name("host", cef.group(1))
            else:
                self._learn_name("host", rest.split(" ", 1)[0])
            for rx in USER_PATTERNS:
                for um in rx.finditer(line):
                    if um.group(2).lower() not in KEEP_USERS:
                        self._learn_name("user", um.group(2))
            for key, s, e in iter_unifi_values(line):
                if key in UNIFI_NAME_KEYS:
                    self._learn_name(UNIFI_NAME_KEYS[key], line[s:e], pipes=True)
                elif UNIFI_DEVICE_KEY.match(key):
                    self._learn_name("device", line[s:e], pipes=True)

    def _rebuild(self):
        alts = sorted(self.names, key=len, reverse=True) + sorted(self.words_ci, key=len, reverse=True)
        if not alts:
            self._name_re = re.compile(r"(?!x)x")
        else:
            body = "|".join(re.escape(a) for a in alts)
            self._name_re = re.compile(rf"(?<![A-Za-z0-9])(?:{body})(?![A-Za-z0-9])", re.IGNORECASE)

    def _replace_names(self, line):
        if self._name_re is None:
            self._rebuild()

        def sub(m):
            text = m.group(0)
            key = text.lower()
            if m.start() > 0 and m.string[m.start() - 1] == ",":
                return text  # model in the "<mac>,<model>-<firmware>" header token; product names are public
            self.raw_values.add(text)
            if key in self.names:
                self.counts["names"] += 1
                return self.mapper.name(self.names[key], text)
            self.counts["site words"] += 1
            return SITE

        line = self._name_re.sub(sub, line)
        for word in self.words_cs:
            line = re.sub(rf"(?<![A-Za-z0-9]){re.escape(word)}(?![A-Za-z0-9])", self._count_site(word), line)
        return line

    def _count_site(self, word):
        def sub(_):
            self.raw_values.add(word)
            self.counts["site words"] += 1
            return SITE

        return sub

    def _flag(self, index, kind, raw):
        self.flags.append((index, kind))
        self.raw_values.add(raw)
        self.counts["redacted"] += 1

    def line(self, text, index=0):
        line = text

        def redact_kv(m):
            self._flag(index, m.group(1).lower(), m.group(3).strip('"'))
            return f"{m.group(1)}{m.group(2)}{REDACTED}"

        line = SECRET_KV.sub(redact_kv, line)
        line = BEARER.sub(
            lambda m: (self._flag(index, "bearer", m.group(2)), f"{m.group(1)}{REDACTED}")[1], line
        )
        line = LONG_HEX.sub(lambda m: (self._flag(index, "long hex", m.group(0)), REDACTED)[1], line)

        def redact_token(m):
            text = m.group(0)
            # Real tokens mix upper case, lower case, and digits; plain hyphenated words do not.
            if not (re.search("[A-Z]", text) and re.search("[a-z]", text) and re.search(r"\d", text)):
                return text
            self._flag(index, "long token", text)
            return REDACTED

        line = LONG_B64.sub(redact_token, line)

        line = self._unifi_values(line)
        line = self._header_host(line)
        line = self._replace_names(line)
        line = CAMERA.sub(self._camera, line)
        line = FW_RULE.sub(lambda m: m.group(1) + self._named("rule", m.group(2)), line)
        line = EMAIL.sub(self._email, line)
        for rx in USER_PATTERNS:
            line = rx.sub(self._user, line)
        line = DHCP_NAME.sub(self._dhcp, line)
        line = DNS_DOMAIN.sub(self._dns_domain, line)
        for dom in DOMAIN_LIKE.findall(line):
            if not re.match(r"(host|client|device|ssid|network|admin|user|camera)-\d+\.", dom.lower()):
                self.domains_seen.add(dom.lower())
        line = MAC_NOSEP.sub(lambda m: self._mac(m.group(1)) + ",", line)
        line = IPTABLES_MAC.sub(self._iptables_mac, line)
        line = MAC.sub(lambda m: self._mac(m.group(0)), line)
        line = EVENT_ID.sub(lambda m: m.group(1) + self._shape("object_id", m.group(2)), line)
        line = UUID.sub(lambda m: self._shape("uuid", m.group(0)), line)
        line = IPV6.sub(self._ip6, line)
        line = IPV4.sub(self._ip4, line)
        return line

    def lines(self, lines):
        return [self.line(text, i) for i, text in enumerate(lines)]

    def _named(self, cat, value):
        self.raw_values.add(value)
        self.counts["names"] += 1
        return self.mapper.name(cat, value)

    def _unifi_values(self, line):
        edits = []
        for key, s, e in iter_unifi_values(line):
            value = line[s:e]
            if not value.strip():
                continue
            if key in UNIFI_NAME_KEYS:
                if key == "UNIFIadmin" and value.strip().lower() in KEEP_USERS:
                    continue
                edits.append((s, e, self._named(UNIFI_NAME_KEYS[key], value.strip())))
            elif UNIFI_DEVICE_KEY.match(key):
                edits.append((s, e, self._named("device", value.strip())))
        for s, e, repl in reversed(edits):
            line = line[:s] + repl + line[e:]
        return line

    def _header_host(self, line):
        m = HEADER.match(line)
        if not m:
            return line
        ts, rest = m.group(1), m.group(2)
        cef = CEF_HOST.match(rest)
        if cef:
            host = cef.group(1)
            if " | " in host:
                return line
            return f"{ts} {self._named('host', host)}{rest[len(host) :]}"
        first, sep, tail = rest.partition(" ")
        if not sep:
            return line
        placeholder = self._named("host", first)
        if tail.startswith(first + " "):
            self.counts["names"] += 1
            tail = placeholder + tail[len(first) :]
        return f"{ts} {placeholder} {tail}"

    def _dhcp(self, m):
        name = m.group(2)
        if PLACEHOLDER_NAME.fullmatch(name):
            return m.group(0)
        return m.group(1) + self._named("client", name)

    def _camera(self, m):
        return m.group(1) + self._named("camera", m.group(2)) + m.group(3)

    def _email(self, m):
        self.raw_values.add(m.group(0))
        self.counts["emails"] += 1
        return self.mapper.email(m.group(0))

    def _user(self, m):
        name = m.group(2)
        if name.lower() in KEEP_USERS or re.fullmatch(r"user-\d+", name) or name.startswith("["):
            return m.group(0)
        return m.group(1) + self._named("user", name)

    def _dns_domain(self, m):
        self.raw_values.add(m.group(2))
        self.counts["dns domains"] += 1
        return m.group(1) + self.mapper.domain(m.group(2)) + m.group(3)

    def _shape(self, kind, value):
        self.raw_values.add(value)
        self.counts["ids"] += 1
        return self.mapper.uuid(value) if kind == "uuid" else self.mapper.object_id(value)

    def _iptables_mac(self, m):
        raw = m.group(2).split(":")
        dst, src = ":".join(raw[:6]), ":".join(raw[6:])
        tail = ":".join(m.group(3).lstrip(":").split(":")[:2])  # keep the ethertype, drop header bytes
        return f"{m.group(1)}{self._mac(dst)}:{self._mac(src)}" + (f":{tail}" if tail else "")

    def _mac(self, text):
        digits = re.sub(r"[^0-9A-Fa-f]", "", text)
        if digits.lower().startswith(guard.DOC_MAC_PREFIX):
            return text
        self.raw_values.add(text)
        self.counts["macs"] += 1
        return format_mac(self.mapper.mac_digits(digits), text)

    def _ip4(self, m):
        text = m.group(1)
        try:
            ip = ipaddress.IPv4Address(text)
        except ValueError:
            return text
        if ip.is_loopback or ip.is_unspecified or text.startswith("255.") or ip.is_multicast:
            return text
        if any(part != str(int(part)) for part in text.split(".")):
            return text
        self.raw_values.add(text)
        self.counts["ips"] += 1
        return self.mapper.ip4(text)

    def _ip6(self, m):
        text = m.group(0)
        groups = [g for g in text.split(":") if g]
        if not ("::" in text or text.count(":") == 7) or len(groups) < 2:
            return text
        try:
            ip = ipaddress.IPv6Address(text)
        except ValueError:
            return text
        if ip.is_loopback or ip.is_unspecified:
            return text
        self.raw_values.add(text)
        self.counts["ips"] += 1
        return self.mapper.ip6(text)


def survivors(sanitizer, out_lines):
    """Raw values that were replaced somewhere but still appear in the output."""
    text = "\n".join(out_lines)
    low = text.lower()
    found = []
    for raw in sorted(sanitizer.raw_values):
        if raw in ("", SITE, REDACTED) or len(raw) < 2:
            continue
        # A preceding comma marks the public model name in "<mac>,<model>-<firmware>", which is kept.
        pattern = re.compile(rf"(?<![A-Za-z0-9,]){re.escape(raw.lower())}(?![A-Za-z0-9])")
        if pattern.search(low):
            found.append(raw[:2] + "***")
    return found


def guard_findings(out_lines, private=None):
    allow = guard.load_allow()
    private = private if private is not None else guard._compile(guard.load_private_tokens())
    problems = []
    for i, line in enumerate(out_lines, 1):
        problems += [f"line {i}: {hit}" for hit in guard.scan_line(line, private, allow)]
    return problems


def load_state():
    if MAP_FILE.exists():
        return json.loads(MAP_FILE.read_text(encoding="utf-8"))
    return {}


def run(raw_dir, out_dir, write_state=True):
    files = sorted(raw_dir.glob("syslog-*.log"))
    if not files:
        print(f"no syslog-*.log files in {raw_dir}; run scripts/pull-samples.ps1 first", file=sys.stderr)
        return 1
    words_ci, words_cs = read_words()
    macish = re.compile(r"[0-9A-Fa-f]{2}(?:[:-]?[0-9A-Fa-f]{2}){5}")
    skip = {w.lower() for w in words_ci}
    private_hosts = [
        t
        for t in guard.load_private_tokens()
        if t.lower() not in skip and not guard.IPV4.fullmatch(t) and not macish.fullmatch(t)
    ]
    san = Sanitizer(Mapper(load_state()), words_ci, words_cs, private_hosts)
    contents = {f: f.read_text(encoding="utf-8", errors="replace").splitlines() for f in files}
    for lines in contents.values():
        san.learn(lines)

    outputs = {}
    for f, lines in contents.items():
        m = re.fullmatch(r"syslog-(.+)\.log", f.name)
        src = san.mapper.ip4(m.group(1)) if guard.IPV4.fullmatch(m.group(1)) else m.group(1)
        san.raw_values.add(m.group(1))
        outputs[f"syslog-{src}.log"] = san.lines(lines)

    all_out = [line for lines in outputs.values() for line in lines]
    problems = [f"survivor {s}" for s in survivors(san, all_out)] + guard_findings(all_out)
    if problems:
        print("sanitizer self-check FAILED; nothing written:", file=sys.stderr)
        print("\n".join(problems[:40]), file=sys.stderr)
        return 1

    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("syslog-*.log"):
        old.unlink()
    for name, lines in outputs.items():
        (out_dir / name).write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    if write_state:
        MAP_FILE.parent.mkdir(parents=True, exist_ok=True)
        MAP_FILE.write_text(json.dumps(san.mapper.state, indent=1, sort_keys=True), encoding="utf-8")
        write_report(san, outputs)
    print(f"sanitized {len(outputs)} files, {len(all_out)} lines -> {out_dir}")
    print("replacements: " + ", ".join(f"{k}={v}" for k, v in sorted(san.counts.items())))
    print(
        f"flagged for review: {len(san.flags)} line(s); public-looking domains kept: {len(san.domains_seen)}"
    )
    print(f"details (no raw values): {REPORT_FILE}")
    return 0


def write_report(san, outputs):
    lines = ["Sanitizer report (contains no raw values)", ""]
    lines += [f"{k}: {v}" for k, v in sorted(san.counts.items())]
    lines += ["", "Lines flagged for manual review (output line number, kind):"]
    lines += [f"  {i + 1}\t{kind}" for i, kind in san.flags] or ["  none"]
    lines += ["", "Domain-like names kept in the output (public infrastructure? check each):"]
    lines += [f"  {d}" for d in sorted(san.domains_seen)] or ["  none"]
    lines += ["", "Review the output by eye before committing. The self-check cannot find a private"]
    lines += [
        "name it was never taught; add such words to the instance's guard/private-names.txt and re-run."
    ]
    REPORT_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--raw", type=Path, default=RAW_DIR)
    ap.add_argument("--out", type=Path, default=OUT_DIR)
    args = ap.parse_args(argv)
    try:
        return run(args.raw, args.out)
    except SanitizeError as exc:
        print(f"sanitizer error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
