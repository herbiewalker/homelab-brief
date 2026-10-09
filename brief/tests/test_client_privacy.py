"""Task 21: what each device profile lets the product keep, checked through the real worker and ingest.

The same gateway DNS line and access point join line is sent for three devices, one per profile, plus an
unlisted device (which must be treated as security_only). The assertions are about what is, and is not,
in the tables and in the generic signature counts.
"""

import json

import pytest
from test_worker import make_env

from brief import clients, devices
from brief import worker as w
from brief.devices import ClientPolicy, DeviceBook, norm_mac

FULL, SECURITY, IGNORED, UNLISTED = (
    "00:00:5e:00:53:11",
    "00:00:5e:00:53:12",
    "00:00:5e:00:53:13",
    "00:00:5e:00:53:14",
)
DEVICES = f"""schema: 1
devices:
  - {{ mac: "{FULL}", label: "Full laptop", owner: "Person A", profile: full }}
  - {{ mac: "{SECURITY}", label: "Security console", owner: "Household", profile: security_only }}
  - {{ mac: "{IGNORED}", label: "Ignored plug", owner: "Household", profile: ignore }}
"""
UNIFI_ON = "schema: 1\ncollectors:\n  unifi:\n    enabled: true\n    url_var: UNIFI_URL\n    key_var: UNIFI_API_KEY\n    client_logs: {value}\n"


def dns_line(mac, domain):
    msg = json.dumps(
        {"timestamp": "2026-09-26T13:15:39-04:00", "type": "dnsAdBlock", "category": "ADVERTISEMENT",
         "domain": domain, "ip": "198.51.100.19", "mac": mac, "src_ip": "198.51.100.19"}
    )  # fmt: skip
    return json.dumps(
        {"source_ip": "198.51.100.1", "app": "coredns", "msg": msg, "severity": 6, "ts": "Sep 26 13:22:33",
         "timestamp": "2026-09-26T17:23:00Z"}
    )  # fmt: skip


def join_line(mac):
    msg = f"wevent.ubnt_custom_event(): EVENT_STA_JOIN wifi2ap12: {mac} / 1"
    return json.dumps(
        {"source_ip": "198.51.100.5", "app": "wevent", "msg": msg, "severity": 6, "ts": "Sep 26 13:22:33",
         "timestamp": "2026-09-26T17:23:00Z"}
    )  # fmt: skip


def run(tmp_path, client_logs=True):
    env, user = make_env(tmp_path)
    (user / "devices.yaml").write_text(DEVICES, encoding="utf-8")
    (user / "collectors.yaml").write_text(UNIFI_ON.format(value=str(client_logs).lower()), encoding="utf-8")
    state = tmp_path / "state"
    (state / "events").mkdir(parents=True)
    lines = []
    for mac in (FULL, SECURITY, IGNORED, UNLISTED):
        lines += [
            dns_line(mac, f"ads-{mac[-2:]}.example"),
            dns_line(mac, f"ads-{mac[-2:]}.example"),
            join_line(mac),
        ]
    lines.append(json.dumps({"source_ip": "198.51.100.1", "app": "app", "msg": "ordinary line", "severity": 6,
                             "ts": "Sep 26 13:22:33", "timestamp": "2026-09-26T17:23:00Z"}))  # fmt: skip
    (state / "events" / "2026-09-26.ndjson").write_text("\n".join(lines) + "\n", encoding="utf-8")
    worker = w.Worker(state, env)
    result = worker.run_once()
    return worker, result


def rows(worker, sql):
    with worker.store.read() as db:
        return [dict(r) for r in db.execute(sql)]


def test_each_profile_keeps_only_what_it_allows(tmp_path):
    worker, _ = run(tmp_path)
    seen = {r["mac"] for r in rows(worker, "SELECT mac FROM client")}
    assert seen == {FULL, SECURITY, UNLISTED}  # the ignored device is not stored at all
    dns = rows(worker, "SELECT client, queries, blocked, top_domains FROM dns_hourly")
    assert [d["client"] for d in dns] == [FULL]  # DNS detail for the full profile only
    assert dns[0]["queries"] == 2 and dns[0]["blocked"] == 2
    top = json.loads(dns[0]["top_domains"])
    assert top["domains"] == {"ads-11.example": 2} and top["categories"] == {"ADVERTISEMENT": 2}
    assert IGNORED not in json.dumps(rows(worker, "SELECT * FROM client_hourly"))


def test_non_full_devices_stay_out_of_the_signature_counts_and_samples(tmp_path):
    worker, result = run(tmp_path)
    # 3 lines per device: only the full device's two DNS lines and its join line, plus the ordinary line, are counted
    assert result.counted == 4
    assert result.suppressed == 9  # security_only, ignore, and unlisted devices: 3 lines each
    kept = json.dumps(rows(worker, "SELECT msg FROM sample"))
    for mac in (SECURITY, IGNORED, UNLISTED):
        assert mac not in kept and mac[-2:] + ".example" not in kept
    suppressed = {
        r["rule"]: r["n"] for r in rows(worker, "SELECT rule, SUM(n) n FROM suppressed GROUP BY rule")
    }
    assert suppressed == {"device:security_only": 6, "device:ignore": 3}


def test_with_client_logs_off_nothing_changes_for_anyone(tmp_path):
    worker, result = run(tmp_path, client_logs=False)
    assert rows(worker, "SELECT * FROM client") == [] and rows(worker, "SELECT * FROM dns_hourly") == []
    assert result.suppressed == 0 and result.counted == 13  # every line is an ordinary log line, as before


def test_without_a_devices_file_every_device_is_security_only(tmp_path):
    env, user = make_env(tmp_path)
    (user / "collectors.yaml").write_text(UNIFI_ON.format(value="true"), encoding="utf-8")
    state = tmp_path / "state"
    (state / "events").mkdir(parents=True)
    (state / "events" / "2026-09-26.ndjson").write_text(
        dns_line(FULL, "ads.example") + "\n", encoding="utf-8"
    )
    worker = w.Worker(state, env)
    worker.run_once()
    assert rows(worker, "SELECT mac FROM client") == [{"mac": FULL}]
    assert rows(worker, "SELECT * FROM dns_hourly") == []


def test_a_broken_devices_file_falls_back_to_the_safe_default(tmp_path):
    env, user = make_env(tmp_path)
    (user / "devices.yaml").write_text("schema: 1\ndevices: [not, valid, {{\n", encoding="utf-8")
    assert DeviceBook.load(env).profile_of(FULL) == "security_only"
    assert ClientPolicy.load(env).logs is False


# ---------------------------------------------------------------- the pieces


def test_log_lines_are_recognised_exactly():
    event = clients.parse(
        "coredns",
        json.dumps(
            {"type": "dnsAdBlock", "category": "MALWARE", "domain": "A.Example", "mac": "00:00:5E:00:53:11"}
        ),
    )
    assert event.kind == "dns" and event.blocked and event.domain == "a.example" and event.mac == FULL
    assert clients.parse("coredns", "{not json") is None
    assert clients.parse("coredns", json.dumps({"domain": "x"})) is None  # no MAC: cannot be attributed
    assert clients.parse("wevent", f"EVENT_STA_LEAVE wifi1ap6: {FULL.upper()} / 6").kind == "leave"
    assert clients.parse("wevent", "wevent.sta_join(): unset vport_down") is None
    assert clients.parse("sshd", "EVENT_STA_JOIN wifi1ap6: " + FULL) is None  # the program matters


def test_mac_normalisation_and_quiet_hours():
    assert (
        norm_mac("00-00-5E-00-53-11") == FULL and norm_mac("00005e005311") == FULL and norm_mac("x") is None
    )
    book = DeviceBook.from_mapping(
        {"devices": [{"mac": FULL, "profile": "full", "quiet_hours": {"start": "23:00", "end": "06:00"}}]}
    )
    device = book.get(FULL)
    assert [h for h in range(24) if device.in_quiet_hour(h)] == [0, 1, 2, 3, 4, 5, 23]
    day = devices.Device(FULL, "", "", "full", 9 * 60, 17 * 60)
    assert [h for h in range(24) if day.in_quiet_hour(h)] == list(range(9, 17))


def test_labels_do_not_give_away_more_than_they_must():
    book = DeviceBook.from_mapping({"devices": [{"mac": FULL, "label": "Full laptop", "profile": "full"}]})
    assert book.label(FULL) == "Full laptop"
    assert book.label(UNLISTED, hostname="phone-1") == "phone-1"
    assert book.label(UNLISTED, vendor="Acme") == "Acme device …53:14"
    assert book.label(UNLISTED) == "unknown device …53:14"


def test_rollup_turns_old_hours_into_one_daily_row_and_can_be_repeated(tmp_path):
    from brief.store import Store

    store = Store(tmp_path / "baseline.db")
    with store.write() as tx:
        for hour in (1, 2, 3):
            clients.add_hourly(tx.db, "2026-06-01", hour, FULL, up=10, down=20, online_min=60)
        clients.add_hourly(tx.db, "2026-10-01", 5, FULL, up=1, down=1, online_min=60)  # recent: stays hourly
        for hour in (1, 2):
            tx.db.execute(
                "INSERT INTO dns_hourly VALUES ('2026-06-01', ?, ?, 5, 2, ?)",
                (hour, FULL, json.dumps({"domains": {"a.example": 5}, "categories": {"ADVERTISEMENT": 2}})),
            )
        assert clients.rollup(tx.db, "2026-09-01") == 5
        assert clients.rollup(tx.db, "2026-09-01") == 0
    got = rows_of(store, "SELECT day, hour, bytes_up, bytes_down, online_min FROM client_hourly ORDER BY day")
    assert got == [
        {"day": "2026-06-01", "hour": 24, "bytes_up": 30, "bytes_down": 60, "online_min": 180},
        {"day": "2026-10-01", "hour": 5, "bytes_up": 1, "bytes_down": 1, "online_min": 60},
    ]
    (dns,) = rows_of(store, "SELECT hour, queries, blocked, top_domains FROM dns_hourly")
    assert (dns["hour"], dns["queries"], dns["blocked"]) == (24, 10, 4)
    assert json.loads(dns["top_domains"])["domains"] == {"a.example": 10}


def rows_of(store, sql):
    with store.read() as db:
        return [dict(r) for r in db.execute(sql)]


@pytest.mark.parametrize("profile, stored", [("ignore", False), ("security_only", True), ("full", True)])
def test_activity_filters_by_profile_before_anything_is_written(profile, stored):
    activity = clients.Activity()
    event = clients.ClientEvent("dns", FULL, "x.example", "ADS", True)
    activity.record(event, profile, "2026-09-26T13:00:00Z", "2026-09-26", 13)
    assert bool(activity.presence) is stored
    assert bool(activity.dns) is (profile == "full")
