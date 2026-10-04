import json
import socket
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import parse_report as pr  # noqa: E402
import replay_samples as rs  # noqa: E402

SRC1, SRC2 = "198.51.100.1", "198.51.100.2"
EVENTS = [
    {"severity": 6, "severity_inferred": False, "app": "a", "source_ip": SRC1, "header_host_dup": True},
    {"severity": 3, "severity_inferred": True, "app": "a", "source_ip": SRC1, "device_mac": "00005e005301"},
    {
        "severity": 4,
        "severity_inferred": False,
        "app": "UniFi Protect",
        "source_ip": SRC2,
        "cef": {"name": "x"},
        "epoch_ms": 1,
    },
    {"severity_inferred": True, "parse_failed": True, "source_ip": SRC2, "msg": "junk"},
]


def test_summary_counts_branches_and_severity_sources():
    r = pr.summarize(EVENTS)
    c = r["counts"]
    assert c["total"] == 4 and c["parse_failed"] == 1
    assert c["severity_real"] == 2 and c["severity_inferred"] == 2
    assert c["cef"] == 1 and c["device_token"] == 1 and c["duplicated_host"] == 1 and c["epoch_time"] == 1
    assert r["severity"]["info"] == 1 and r["severity"]["err"] == 1 and r["severity"]["warning"] == 1
    assert len(r["senders"]) == 2


def test_the_report_fails_when_the_failure_rate_is_too_high(tmp_path, capsys):
    f = tmp_path / "2026-10-04.ndjson"
    f.write_text("\n".join(json.dumps(e) for e in EVENTS) + "\n", encoding="utf-8")
    assert pr.main([str(f), "--max-failure-rate", "0.5"]) == 0
    assert pr.main([str(tmp_path), "--max-failure-rate", "0.1"]) == 1
    assert "FAIL" in capsys.readouterr().err


def test_an_empty_input_is_a_failure(tmp_path):
    f = tmp_path / "empty.ndjson"
    f.write_text("", encoding="utf-8")
    assert pr.main([str(f)]) == 1


def test_replay_sends_over_udp_and_tcp_with_and_without_priority():
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.bind(("127.0.0.1", 0))
    udp.settimeout(2)
    port = udp.getsockname()[1]
    tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    tcp.bind(("127.0.0.1", port))
    tcp.listen(1)
    received_tcp = []

    def serve():
        conn, _ = tcp.accept()
        with conn:
            data = b""
            while chunk := conn.recv(4096):
                data += chunk
        received_tcp.extend(data.decode().splitlines())

    t = threading.Thread(target=serve)
    t.start()
    lines = [f"Sep 26 13:00:0{i} host-01 app: line {i}" for i in range(6)]
    sent = rs.replay(lines, "127.0.0.1", port, "both", 134, 3)
    t.join(5)
    got_udp = [udp.recvfrom(4096)[0].decode() for _ in range(sent["udp"])]
    udp.close()
    tcp.close()

    assert sent == {"udp": 3, "tcp": 3, "bare": 2}
    everything = got_udp + received_tcp
    assert len(everything) == 6
    assert sum(1 for x in everything if x.startswith("<134>")) == 4
    assert sum(1 for x in everything if x.startswith("Sep ")) == 2


def test_lines_are_read_from_every_sample_file(tmp_path):
    (tmp_path / "syslog-198.51.100.1.log").write_text("a\n\nb\n", encoding="utf-8")
    (tmp_path / "syslog-198.51.100.2.log").write_text("c\n", encoding="utf-8")
    (tmp_path / "other.log").write_text("ignored\n", encoding="utf-8")
    assert rs.lines_from(tmp_path) == ["a", "b", "c"]
