"""Taking names out of logs and the support bundle: placeholders for logs, stable aliases for the bundle."""

import json
import logging
import re

import golden_day as g
import pytest

from brief import jsonlog, redact

# ---------------------------------------------------------------- scrub (logs)


@pytest.mark.parametrize(
    "text, gone",
    [
        ("sender 198.51.100.7 went quiet", "198.51.100.7"),
        ("mac 00:00:5E:00:53:01 seen", "00:00:5E:00:53:01"),
        ("mac 00-00-5E-00-53-03 seen", "00-00-5E-00-53-03"),
        ("mail alice@example.com sent", "alice@example.com"),
        ("url ntfys://user:s3cret@ntfy.example/topic failed", "s3cret"),
        ("password=hunter2 rejected", "hunter2"),
        ("token: changeme1 rejected", "changeme1"),
        ("key " + "a" * 40, "a" * 40),
        ("hash " + "0123456789abcdef" * 2, "0123456789abcdef" * 2),
        ("ipv6 2001:db8::7 gone", "2001:db8::7"),
    ],
)
def test_scrub_removes_the_value(text, gone):
    assert gone not in redact.scrub(text)


@pytest.mark.parametrize(
    "text",
    [
        "version 0.0.25 started",
        "at 2026-10-07T12:30:45+00:00",
        "time 12:30:45 today",
        "file views.yaml and network.yaml",
        "port 514 and 5514",
        "a/b/c path /state/events",
        "range 1.2.3",
        "999.999.999.999 is not an address",
    ],
)
def test_scrub_leaves_ordinary_text_alone(text):
    assert redact.scrub(text) == text


def test_scrub_uses_fixed_placeholders():
    out = redact.scrub("198.51.100.7 and 198.51.100.8 and 00:00:5E:00:53:01 and a@b.example")
    assert out == "<ip> and <ip> and <mac> and <email>"


def test_log_scrubs_text_fields_and_keeps_numbers(capsys):
    jsonlog.log("thing", error="cannot reach 198.51.100.7", count=3, ok=True)
    record = json.loads(capsys.readouterr().out)
    assert record["error"] == "cannot reach <ip>" and record["count"] == 3 and record["ok"] is True


def test_log_raw_is_the_one_way_out_for_something_the_person_must_read(capsys):
    jsonlog.log("setup_token", raw=True, url="https://198.51.100.7/admin/setup", token="x" * 40)
    record = json.loads(capsys.readouterr().out)
    assert "198.51.100.7" in record["url"] and record["token"] == "x" * 40


def test_the_standard_logging_formatter_scrubs_too():
    record = logging.LogRecord("t", logging.ERROR, __file__, 1, "failed for %s", ("198.51.100.7",), None)
    assert "198.51.100.7" not in jsonlog.JsonFormatter().format(record)


def test_a_whole_daily_run_logs_no_address_and_no_headline(tmp_path, capsys):
    store, digest, out = g.build_daily(tmp_path)
    store.close()
    logged = capsys.readouterr().out
    assert "daily_written" in logged
    assert (
        not re.search(r"\d{1,3}(\.\d{1,3}){3}", logged)
        and "headline" not in logged
        and "silent" not in logged
    )


# ---------------------------------------------------------------- the redactor (support bundle)


def test_the_same_value_always_gets_the_same_alias_and_different_values_differ():
    red = redact.Redactor()
    one = red.text("from 198.51.100.7 to 198.51.100.8 then 198.51.100.7 again")
    assert one == "from <ip-1> to <ip-2> then <ip-1> again"
    assert (
        red.text("00:00:5E:00:53:01 / 00:00:5E:00:53:02 / 00:00:5E:00:53:01") == "<mac-1> / <mac-2> / <mac-1>"
    )


def test_learned_names_are_replaced_whatever_their_case_longest_first():
    red = redact.Redactor(["Kitchen Tablet", "kitchen", "Bob"])
    out = red.text("the Kitchen Tablet and KITCHEN lights, bob's phone, kitchens")
    assert "Kitchen" not in out and "KITCHEN" not in out and "bob" not in out.lower().replace("<", "")
    assert out.count("<name-1>") >= 1 and "kitchens" in out  # a longer word is not a match


def test_short_names_are_not_learned_so_ordinary_words_survive():
    red = redact.Redactor(["a", "up", ""])
    assert red.text("a router is up") == "a router is up"


def test_domains_are_replaced_but_file_names_and_versions_are_not():
    red = redact.Redactor()
    out = red.text("host nas.lan and gw.home.example and views.yaml and 0.0.25 and mail.example.com")
    assert "nas.lan" not in out and "gw.home.example" not in out and "mail.example.com" not in out
    assert "views.yaml" in out and "0.0.25" in out


def test_value_walks_nested_documents():
    red = redact.Redactor(["Gateway"])
    doc = {
        "name": "Gateway",
        "hosts": ["198.51.100.1", {"mac": "00:00:5E:00:53:01"}],
        "n": 3,
        "198.51.100.1": "key",
    }
    out = red.value(doc)
    assert (
        out["name"].startswith("<name-")
        and out["hosts"][0] == "<ip-1>"
        and out["hosts"][1]["mac"] == "<mac-1>"
    )
    assert out["n"] == 3 and "<ip-1>" in out  # a key that is an address is replaced too


def test_leaks_finds_what_is_left_and_nothing_in_clean_text():
    red = redact.Redactor(["SecretSSID"])
    assert red.leaks("fine <ip-1> text, version 0.0.25, file views.yaml") == []
    found = red.leaks("a 198.51.100.7 b 00:00:5E:00:53:01 c x@y.example d secretssid e gw.lan")
    assert len(found) >= 5 and "198.51.100.7" in found and "secretssid" in found and "gw.lan" in found


def test_secrets_never_survive_either_tool():
    red = redact.Redactor()
    for text in ("password: hunter2", "api_key=abcdef123", "ntfys://u:p@host.example/t"):
        assert (
            "hunter2" not in red.text(text)
            and "abcdef123" not in red.text(text)
            and ":p@" not in red.text(text)
        )


def test_the_alert_and_headline_logs_carry_no_name_or_title(capsys):
    from brief.alerts import LogNotifier

    notifier = LogNotifier()
    notifier.send_alert(
        {
            "finding_id": "absence-198.51.100.3",
            "state": "new",
            "level": "warning",
            "kind": "absence",
            "title": "Basement Camera 198.51.100.3 is silent",
        }  # fmt: skip
    )
    notifier.send_headline("2 things need a look; first: Basement Camera is silent")
    out = capsys.readouterr().out
    assert (
        "Basement" not in out and "198.51.100" not in out and '"kind": "absence"' in out and '"length"' in out
    )
