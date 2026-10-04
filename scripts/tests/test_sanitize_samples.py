import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import private_name_guard as guard  # noqa: E402
import sanitize_samples as s  # noqa: E402

# Every value below is invented. Addresses are built from parts so this file passes the commit guard.
IP1 = ".".join(["172", "16", "9", "3"])
IP2 = ".".join(["192", "168", "88", "21"])
IP3 = ".".join(["10", "77", "6", "2"])
PUBLIC_IP = ".".join(["93", "184", "216", "34"])
MAC1 = ":".join(["3c", "5a", "b4", "11", "22", "33"])
MAC2 = ":".join(["f4", "e2", "c6", "aa", "bb", "cc"]).upper()
MAC3 = "-".join(["94", "2a", "6f", "01", "02", "03"])
MAC_NOSEP = "9c05d6f21863"
TAIL = ":".join(["08", "00", "45", "00", "00", "3c"])  # ethertype plus IP header bytes, as iptables logs them
GW = "ZZQ7K-QX-Fakeville-GW"
UUID_TEXT = "0428d7c5-ae97-42f5-92bf-85435d3bbef3"
EVENT_ID = "6ab7fc7601d00e03e49a89cb"
LONG_HEX = "009D8FD74650513DDF5C83F334FA89F2"
IPV6 = "fd12:3456:789a::1"

WORDS_CI = {"ZZQ7K", "ZZQ7KY", "Fakeville"}
WORDS_CS = {"QX"}

RAW = [
    f"Sep 26 13:22:33 {GW} {GW} earlyoom[1593]: mem avail:  1464 of  3946 MiB (37.12%)",
    (
        f"Sep 26 13:17:32 {GW} CEF: 0|Ubiquiti|UniFi Network|10.6.101|403|Wired Client Connected|1|"
        f"UNIFIcategory=Client Devices UNIFIhost=ZZQ7K QX | Fakeville | GW "
        f"UNIFIconnectedToDeviceName=Kitchen Switch Ten UNIFIconnectedToDeviceIp={IP1} "
        f"UNIFIclientMac={MAC1} UNIFIclientIp={IP2} UNIFIclientAlias=Pat's Phone "
        f"UNIFIclientHostname=Pats-iPhone UNIFIwifiName=FakeNet-5G UNIFInetworkName=Kids "
        f"UNIFIadmin=pat.admin UNIFIlinkSpeed=1000"
    ),
    (
        "Sep 26 13:10:39 ZZQ7KY | Fakeville | NVR "
        "CEF:0|Ubiquiti|UniFi Protect|7.2.105|2161|smartAudioDetect|7|"
        f"UNIFIcategory=detection eventId={EVENT_ID} eventType=smartAudioDetect category=detection "
        'severity=7 timestamp=1790442597425 msg="ZZQ7K Back Porch (AI 360) detected someone speaking"'
    ),
    (
        f'Sep 26 13:15:39 {GW} {GW} coredns[4650]: {{"timestamp":"2026-09-26T13:15:39-04:00",'
        f'"type":"dnsAdBlock","domain":"tracker.visited-site.test","ip":"{IP2}","mac":"{MAC1}",'
        f'"src_ip":"{IP2}","dst_ip":"127.0.0.1","dst_port":1053,"protocol":"udp"}}'
    ),
    f"Sep  9 03:14:10 SwitchRoomOne {MAC_NOSEP},USW-Flex-7.5.15+17146: mcad: stun_url: stun://{IP3}/",
    (
        f"Sep 26 13:15:37 ApHallway {MAC_NOSEP},U7-Pro-Max-8.7.11+19419: "
        f"hostapd: STA {MAC2} roamed to peer AP {MAC3}"
    ),
    f"Sep 26 13:21:23 SomeNasBox webgui: Successful login user root from {IP2}",
    f"Sep 26 13:21:24 SomeNasBox sshd[88]: Accepted publickey for pat from {IP2} port 2222 ssh2",
    "Sep 26 13:21:25 SomeNasBox kernel: author <Jane@fakecorp.test> password=hunter2 api_key: abcDEF123",
    f"Sep 26 13:21:26 SomeNasBox app: blob {LONG_HEX} and id {UUID_TEXT}",
    f"Sep 26 13:35:42 SomeNasBox net: ok 127.0.0.1 mask 255.255.255.0 gw {PUBLIC_IP} v6 {IPV6} loop ::1",
    f"Sep 26 13:35:43 SomeNasBox dnsmasq-dhcp[1]: DHCPACK(br0) {IP2} {MAC1} Pats-iPhone",
    "Sep 26 13:35:44 SomeNasBox emhttpd: spinning down /dev/sdc",
]

MUST_NOT_SURVIVE = [
    GW, "ZZQ7K", "ZZQ7KY", "Fakeville", "SwitchRoomOne", "ApHallway", "SomeNasBox", "Kitchen Switch Ten",
    "Pat's Phone", "Pats-iPhone", "FakeNet-5G", "pat.admin", "Back Porch", "tracker.visited-site.test",
    IP1, IP2, IP3, PUBLIC_IP, MAC1, MAC2, MAC3, MAC_NOSEP, "Jane@fakecorp.test", "hunter2", "abcDEF123",
    LONG_HEX, EVENT_ID, UUID_TEXT, IPV6,
]  # fmt: skip


def make():
    san = s.Sanitizer(s.Mapper(), WORDS_CI, WORDS_CS)
    san.learn(RAW)
    return san


@pytest.fixture
def out():
    san = make()
    return san, san.lines(RAW)


def test_no_raw_value_survives_in_the_output(out):
    san, lines = out
    text = "\n".join(lines).lower()
    for value in MUST_NOT_SURVIVE:
        assert value.lower() not in text, f"{value[:3]}*** survived"
    assert s.survivors(san, lines) == []


def test_line_count_and_timestamps_are_preserved(out):
    _, lines = out
    assert len(lines) == len(RAW)
    for raw, clean in zip(RAW, lines, strict=True):
        assert raw[:15] == clean[:15]


def test_structure_the_parsers_rely_on_is_kept(out):
    _, lines = out
    gateway, cef, nvr, dns = lines[0], lines[1], lines[2], lines[3]
    host = re.match(rf"{s.TS} (host-\d+) (host-\d+) earlyoom", gateway)
    assert host and host.group(1) == host.group(2)
    assert (
        "CEF: 0|Ubiquiti|UniFi Network|10.6.101|403|Wired Client Connected|1|UNIFIcategory=Client Devices"
        in cef
    )
    assert "UNIFIhost=SITE SITE | SITE | GW" in cef
    assert "UNIFIlinkSpeed=1000" in cef
    assert "SITE | SITE | NVR CEF:0|Ubiquiti|UniFi Protect|7.2.105|2161|smartAudioDetect|7|" in nvr
    assert re.search(r'msg="SITE camera-\d+ \(AI 360\) detected someone speaking"', nvr)
    payload = json.loads(dns.split("coredns[4650]: ", 1)[1])
    assert payload["domain"].endswith(".example") and payload["dst_ip"] == "127.0.0.1"
    assert payload["dst_port"] == 1053


def test_header_token_with_mac_keeps_model_and_firmware(out):
    _, lines = out
    assert re.search(r"host-\d+ 00005e0053[0-9a-f]{2},USW-Flex-7\.5\.15\+17146: mcad", lines[4])


def test_mac_style_and_case_are_kept(out):
    _, lines = out
    assert re.search(r"STA 00:00:5E:00:53:[0-9A-F]{2} roamed to peer AP 00-00-5e-00-53-[0-9a-f]{2}", lines[5])
    assert re.search(r'"mac":"00:00:5e:00:53:[0-9a-f]{2}"', lines[3])


def test_addresses_move_to_documentation_ranges_and_special_ones_stay(out):
    _, lines = out
    for line in lines:
        for ip in s.IPV4.findall(line):
            if ip in {"127.0.0.1", "255.255.255.0", "10.6.101"}:
                continue
            assert ip.startswith(("198.51.100.", "203.0.113.")), ip
    assert "ok 127.0.0.1 mask 255.255.255.0 gw 203.0.113." in lines[10]
    assert "v6 2001:db8::" in lines[10] and "loop ::1" in lines[10]


def test_users_emails_and_ids(out):
    _, lines = out
    assert "login user root from" in lines[6]
    assert re.search(r"Accepted publickey for user-\d+ from", lines[7])
    assert re.search(r"<email-\d+@example\.com>", lines[8])
    assert re.search(r"id 00000000-0000-4000-8000-[0-9a-f]{12}$", lines[9])
    assert re.search(r"eventId=[0-9a-f]{24} eventType", lines[2])
    assert re.search(r"DHCPACK\(br0\) 198\.51\.100\.\d+ 00:00:5e:00:53:[0-9a-f]{2} client-\d+$", lines[11])


def test_secrets_are_redacted_and_flagged_without_the_value(out):
    san, lines = out
    assert "password=[REDACTED]" in lines[8] and "api_key: [REDACTED]" in lines[8]
    assert "blob [REDACTED] and" in lines[9]
    kinds = {kind for _, kind in san.flags}
    assert {"password", "api_key", "long hex"} <= kinds
    assert all(isinstance(i, int) and isinstance(k, str) for i, k in san.flags)
    assert "hunter2" not in repr(san.flags)


def test_unaffected_lines_pass_through():
    assert (
        make()
        .line("Sep 26 13:35:44 SomeNasBox emhttpd: spinning down /dev/sdc")
        .endswith("emhttpd: spinning down /dev/sdc")
    )


def test_mapping_is_stable_across_runs_and_after_saving_state(out):
    san, first = out
    again = san.lines(RAW)
    assert again == first
    restored = s.Sanitizer(s.Mapper(json.loads(json.dumps(san.mapper.state))), WORDS_CI, WORDS_CS)
    restored.learn(RAW)
    assert restored.lines(RAW) == first
    gateway_hosts = {re.match(rf"{s.TS} (host-\d+)", first[i]).group(1) for i in (0, 3)}
    assert len(gateway_hosts) == 1


def test_same_value_in_different_lines_gets_the_same_placeholder(out):
    _, lines = out
    assert re.search(r"UNIFIclientIp=(198\.51\.100\.\d+)", lines[1]).group(1) == re.search(
        r'"ip":"(198\.51\.100\.\d+)"', lines[3]
    ).group(1)


def test_output_passes_the_commit_guard(out):
    _, lines = out
    assert s.guard_findings(lines, private=[]) == []


def test_self_check_reports_a_survivor():
    san = make()
    san.line(RAW[1])
    assert s.survivors(san, ["something about fakeville again"]) == ["Fa***"]
    assert s.survivors(san, ["clean line"]) == []


def test_run_writes_sanitized_files_and_refuses_when_the_self_check_fails(tmp_path, monkeypatch):
    raw, out_dir = tmp_path / "raw", tmp_path / "out"
    raw.mkdir()
    (raw / f"syslog-{IP2}.log").write_text("\n".join(RAW[:2] + RAW[6:8]) + "\n", encoding="utf-8")
    monkeypatch.setattr(guard, "load_private_tokens", lambda *a, **k: set())
    monkeypatch.setattr(s, "read_words", lambda: (WORDS_CI, WORDS_CS))
    monkeypatch.setattr(s, "MAP_FILE", tmp_path / "map.json")
    monkeypatch.setattr(s, "REPORT_FILE", tmp_path / "report.txt")

    assert s.run(raw, out_dir) == 0
    (written,) = list(out_dir.glob("syslog-*.log"))
    assert re.fullmatch(r"syslog-198\.51\.100\.\d+\.log", written.name)
    text = written.read_text(encoding="utf-8")
    assert IP2 not in text and "Fakeville" not in text
    assert "Pat's Phone" not in (tmp_path / "report.txt").read_text(encoding="utf-8")

    original = s.Sanitizer.lines
    monkeypatch.setattr(
        s.Sanitizer, "lines", lambda self, lines: original(self, lines) + ["leftover Fakeville"]
    )
    for old in out_dir.glob("*"):
        old.unlink()
    assert s.run(raw, out_dir) == 1
    assert list(out_dir.glob("syslog-*.log")) == []


def test_pool_exhaustion_is_an_error_not_a_collision():
    m = s.Mapper()
    for n in range(254):
        m.ip4(f"172.16.{n // 250}.{n % 250 + 1}")
    with pytest.raises(s.SanitizeError):
        m.ip4(".".join(["172", "30", "0", "1"]))


def test_a_working_directory_is_not_a_secret_but_an_auth_key_is():
    san = make()
    assert san.line("Sep 26 13:00:00 SomeNasBox sudo[1]: u : PWD=/root ; USER=root").endswith(
        "PWD=/root ; USER=root"
    )
    key = "Ab3dE6gH9jK2mN5pQ8sT1vW4yZ7bC0eF3hJ6kM9nP2qR5tU8"  # invented value  # gitleaks:allow
    out = san.line(f"Sep 26 13:00:01 SomeNasBox mcad[2]: authkey: {key}")
    assert out.endswith("authkey: [REDACTED]") and key not in out
    assert ("long token" in {kind for _, kind in san.flags}) or ("authkey" in {kind for _, kind in san.flags})


def test_long_hyphenated_state_strings_are_not_mistaken_for_tokens():
    san = make()
    state = "r-10-a-43199880-r-20-a-43200620-r-20-a-43199430-r-20-a-43200730"
    out = san.line(f"Sep 26 12:01:58 SomeNasBox kernel: [DHCP-SM] {MAC1}: {state}")
    assert state in out and san.flags == []


def test_quoted_ssh_auth_user_is_replaced_and_the_model_in_the_header_is_kept():
    san = s.Sanitizer()
    raw = (
        f"Sep  2 20:44:03 USP-PDU-Pro {MAC_NOSEP},USP-PDU-Pro-7.2.123+16565: dropbear[9940]: "
        f"Password auth succeeded for 'oLQJe3u4HXri' from {IP2}:52410"
    )
    san.learn([raw])
    out = san.line(raw)
    assert "oLQJe3u4HXri" not in out and re.search(r"for 'user-\d+' from 198\.51\.100\.\d+:52410", out)
    assert re.search(
        r"^Sep  2 20:44:03 host-\d+ 00005e0053[0-9a-f]{2},USP-PDU-Pro-7\.2\.123\+16565: dropbear", out
    )
    assert s.survivors(san, [out]) == []


def test_a_user_seen_once_is_replaced_everywhere_in_the_batch():
    san = s.Sanitizer()
    first = f"Sep  2 20:44:03 DeviceOne dropbear[1]: Password auth succeeded for 'oLQJe3u4HXri' from {IP2}:5"
    second = f"Sep  2 20:44:09 DeviceOne dropbear[1]: Exit (oLQJe3u4HXri) from <{IP2}:5>: closed"
    other = "Sep  2 20:45:00 DeviceOne app: handing oLQJe3u4HXri a session"
    san.learn([first, second, other])
    out = san.lines([first, second, other])
    assert all("oLQJe3u4HXri" not in line for line in out)
    assert s.survivors(san, out) == []


def test_iptables_mac_field_keeps_ethertype_but_not_header_bytes():
    out = make().line(
        f"Sep 26 13:17:05 SomeNasBox kernel: IN=eth7 OUT= MAC={MAC1}:{MAC2.lower()}:{TAIL} SRC={IP2}"
    )
    assert re.search(r"MAC=00:00:5e:00:53:[0-9a-f]{2}:00:00:5e:00:53:[0-9a-f]{2}:08:00 SRC=", out)
    assert guard.scan_line(out, [], set()) == []


def test_a_camera_name_is_replaced_even_when_the_model_text_is_cut_off():
    san = make()
    out = san.line(
        "Sep 26 13:10:39 ZZQ7KY | Fakeville | NVR CEF:0|Ubiquiti|UniFi Protect|7|1|motion|3|eventType=motion "
        'msg="ZZQ7K Rear Mudroom (G6_Ins has recorded motion."'
    )
    assert 'msg="SITE camera-01 (G6_Ins has recorded motion."' in out and "Mudroom" not in out
    assert s.survivors(san, [out]) == []


def test_firewall_rule_names_are_replaced_but_the_rule_text_stays():
    out = make().line(
        'Sep 26 13:17:05 SomeNasBox kernel: [PREROUTING-DNAT-6] DESCR="PortForward DNAT [mediabox]" IN=eth7'
    )
    assert 'DESCR="PortForward DNAT [rule-01]" IN=eth7' in out and "mediabox" not in out


def test_a_colon_after_a_mac_does_not_hide_it():
    out = make().line(f"Sep 26 13:28:34 SomeNasBox dpi: 1 trace files added for {MAC1}: 5.1 KB")
    assert MAC1 not in out and re.search(r"for 00:00:5e:00:53:[0-9a-f]{2}: 5\.1 KB", out)
