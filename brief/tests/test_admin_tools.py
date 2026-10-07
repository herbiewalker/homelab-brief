"""15d (first half): log search, signatures with "ignore this signature", and replay and tuning."""

import gzip
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import golden_day as g
import pytest
from test_admin import lab  # noqa: F401  (the fixture: an HTTPS app with a signed-in admin)

from brief import logsearch

D = date(2026, 10, 7)


# ---------------------------------------------------------------- the search itself


@pytest.fixture
def archive(tmp_path):
    root = tmp_path / "arch"
    for day, host, lines in (
        ("2026-10-07", "198.51.100.1", ["first ok", "Disk ERROR on sda", "second ok", "error again"]),
        ("2026-10-07", "198.51.100.2", ["other host error"]),
        ("2026-10-06", "198.51.100.1", ["old error line", "fine"]),
    ):
        (root / day).mkdir(parents=True, exist_ok=True)
        (root / day / f"{host}.log").write_text("\n".join(lines) + "\n")
    (root / "2026-10-05").mkdir()
    with gzip.open(root / "2026-10-05" / "198.51.100.1.log.gz", "wb") as f:
        f.write(b"compressed ERROR one\ncompressed fine\n")
    return root


def texts(result):
    return [x["text"] for x in result.lines]


def test_a_search_finds_matching_lines_case_insensitively_newest_first(archive):
    r = logsearch.search(archive, "198.51.100.1", D - timedelta(days=2), D, "error")
    assert texts(r) == ["error again", "Disk ERROR on sda", "old error line", "compressed ERROR one"]
    assert [x["day"] for x in r.lines] == ["2026-10-07", "2026-10-07", "2026-10-06", "2026-10-05"]
    assert r.files == 3 and r.stopped is None


def test_an_empty_sender_searches_every_sender(archive):
    r = logsearch.search(archive, "", D, D, "error")
    assert sorted(x["host"] for x in r.lines) == ["198.51.100.1", "198.51.100.1", "198.51.100.2"]


def test_a_sender_alone_shows_its_lines(archive):
    r = logsearch.search(archive, "198.51.100.2", D, D, "")
    assert texts(r) == ["other host error"]


def test_the_line_limit_keeps_the_latest_matches_and_says_so(archive):
    r = logsearch.search(archive, "198.51.100.1", D, D, "", limit=2)
    assert texts(r) == ["error again", "second ok"] and r.stopped == "2 lines"


@pytest.mark.parametrize(
    "host, start, end, text",
    [
        ("", D, D, ""),  # nothing to look for
        ("198.51.100.1", D, D - timedelta(days=1), "x"),  # backwards
        ("198.51.100.1", D - timedelta(days=20), D, "x"),  # too many days
        ("../etc", D, D, "x"),
        ("a/b", D, D, "x"),
        (".hidden", D, D, "x"),
        ("host name", D, D, "x"),
        ("198.51.100.1", D, D, "x" * 201),
    ],
)
def test_bad_searches_are_refused(archive, host, start, end, text):
    with pytest.raises(logsearch.SearchError):
        logsearch.search(archive, host, start, end, text)


def test_a_link_pointing_out_of_the_archive_is_not_followed(archive, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("password hunter2\n")
    (archive / "2026-10-07" / "198.51.100.9.log").symlink_to(secret)
    assert logsearch.search(archive, "198.51.100.9", D, D, "hunter2").lines == []
    assert logsearch.search(archive, "", D, D, "hunter2").lines == []


def test_the_byte_and_time_limits_stop_a_search_and_say_why(archive):
    r = logsearch.search(archive, "198.51.100.1", D, D, "ok", max_bytes=5)
    assert r.stopped == "the byte limit"
    ticks = iter(range(0, 1000, 20))
    r = logsearch.search(archive, "198.51.100.1", D, D, "ok", max_seconds=30, clock=lambda: next(ticks))
    assert r.stopped == "the time limit"


def test_long_lines_are_cut_and_odd_bytes_do_not_crash(archive):
    (archive / "2026-10-07" / "198.51.100.5.log").write_bytes(b"x" * 900 + b" caf\xe9 NEEDLE\n")
    r = logsearch.search(archive, "198.51.100.5", D, D, "needle")
    assert r.lines and len(r.lines[0]["text"]) == 500


def test_a_truncated_gzip_is_skipped_not_fatal(archive):
    good = gzip.compress(b"ERROR in a compressed file\n" * 50)
    (archive / "2026-10-07" / "198.51.100.6.log.gz").write_bytes(good[: len(good) // 2])
    r = logsearch.search(archive, "", D, D, "error")
    assert any(x["host"] == "198.51.100.1" for x in r.lines)  # the readable files still answer


def test_days_available(archive, tmp_path):
    assert logsearch.days_available(archive) == (date(2026, 10, 5), date(2026, 10, 7))
    assert logsearch.days_available(tmp_path / "none") is None


# ---------------------------------------------------------------- the pages


def token(client, path):
    return re.search(r'name="csrf_token" value="([^"]+)"', client.get(path).text).group(1)


def test_the_new_pages_need_a_login_and_writes_need_a_token(lab):  # noqa: F811
    for path in (
        "/admin/logs",
        "/admin/signatures",
        "/admin/replay",
        "/admin/export/signature.csv?host=a&sig=1",
    ):
        r = lab.https().get(path)
        assert r.status_code == 303 and r.headers["location"].startswith("/admin/login"), path
    for path in ("/admin/logs", "/admin/replay", "/admin/signatures/1/ignore"):
        assert lab.https().post(path, data={}).status_code == 401, path
        assert lab.client.post(path, data={}).status_code == 403, path


def test_log_search_through_the_page_escapes_lines_and_keeps_the_text_out_of_the_url(lab):  # noqa: F811
    arch = lab.settings.archive_dir
    (arch / "2026-10-07").mkdir()
    (arch / "2026-10-07" / "198.51.100.1.log").write_text("<script>alert(1)</script> secretword here\n")
    r = lab.client.post(
        "/admin/logs",
        data={
            "csrf_token": token(lab.client, "/admin/logs"),
            "host": "",
            "q": "secretword",
            "start": "2026-10-07",
            "end": "2026-10-07",
        },
    )
    assert r.status_code == 200 and "&lt;script&gt;" in r.text and "<script>alert" not in r.text
    assert "secretword" not in str(r.request.url)
    assert lab.client.get("/admin/logs?q=secretword").status_code == 200  # a GET just shows the empty form


def test_a_bad_log_search_shows_why(lab):  # noqa: F811
    r = lab.client.post(
        "/admin/logs",
        data={
            "csrf_token": token(lab.client, "/admin/logs"),
            "host": "../x",
            "q": "a",
            "start": "2026-10-07",
            "end": "2026-10-07",
        },
    )
    assert r.status_code == 422 and "A sender is an IP address" in r.text
    r = lab.client.post(
        "/admin/logs",
        data={
            "csrf_token": token(lab.client, "/admin/logs"),
            "host": "",
            "q": "a",
            "start": "yesterday",
            "end": "",
        },
    )
    assert r.status_code == 422 and "is not a date" in r.text


def seed_signatures(env):
    now = datetime.now(UTC)
    with env.store.write() as tx:
        busy = tx.signature_for(0, 1, "kernel", "disk error on <*>", now)
        quiet = tx.signature_for(0, 2, "cron", "job done", now)
        tx.add_hourly(
            [
                (f"{now:%Y-%m-%d}", now.hour, "198.51.100.1", busy, "err", 300),
                (f"{now:%Y-%m-%d}", now.hour, "198.51.100.2", quiet, "info", 4),
            ]
        )
    return busy, quiet


def test_signatures_lists_the_busiest_first_and_filters(lab):  # noqa: F811
    busy, quiet = seed_signatures(lab)
    html = lab.client.get("/admin/signatures").text
    assert html.index("disk error") < html.index("job done") and "300" in html
    assert "job done" not in lab.client.get("/admin/signatures?q=disk").text
    assert (
        "No signatures match." in lab.client.get("/admin/signatures?q=%25%27%3B--").text
    )  # hostile text is just text
    assert "No signatures match." in lab.client.get("/admin/signatures?ignored=1").text


def test_ignoring_a_signature_sets_the_flag_audits_and_is_reversible(lab):  # noqa: F811
    busy, quiet = seed_signatures(lab)
    csrf = token(lab.client, "/admin/signatures")
    r = lab.client.post(f"/admin/signatures/{busy}/ignore", data={"csrf_token": csrf, "ignored": "1"})
    assert r.status_code == 303 and r.headers["location"] == "/admin/signatures"
    with lab.store.read() as db:
        assert db.execute("SELECT ignored FROM signature WHERE sig_id = ?", (busy,)).fetchone()[0] == 1
        audit = db.execute("SELECT user, detail FROM audit_log WHERE action = 'signature.ignore'").fetchone()
    assert audit["user"] == "admin:admin" and "disk error" in audit["detail"]
    assert "ignored" in lab.client.get("/admin/signatures?ignored=1").text
    row = lab.client.post(
        f"/admin/signatures/{busy}/ignore",
        data={"csrf_token": csrf, "ignored": "0"},
        headers={"HX-Request": "true"},
    )
    assert (
        row.status_code == 200
        and row.text.lstrip().startswith("<tr>")
        and "Ignore" in row.text
        and "Stop ignoring" not in row.text
    )
    with lab.store.read() as db:
        assert db.execute("SELECT ignored FROM signature WHERE sig_id = ?", (busy,)).fetchone()[0] == 0
        assert (
            db.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'signature.unignore'").fetchone()[0]
            == 1
        )


def test_ignoring_what_is_already_ignored_changes_and_records_nothing(lab):  # noqa: F811
    busy, _ = seed_signatures(lab)
    csrf = token(lab.client, "/admin/signatures")
    for _ in range(2):
        lab.client.post(f"/admin/signatures/{busy}/ignore", data={"csrf_token": csrf, "ignored": "1"})
    with lab.store.read() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'signature.ignore'").fetchone()[0] == 1
        )


def test_an_unknown_signature_is_a_404_and_records_nothing(lab):  # noqa: F811
    csrf = token(lab.client, "/admin/signatures")
    assert (
        lab.client.post(
            "/admin/signatures/99999/ignore", data={"csrf_token": csrf, "ignored": "1"}
        ).status_code
        == 404
    )
    with lab.store.read() as db:
        assert db.execute("SELECT COUNT(*) FROM audit_log WHERE action LIKE 'signature.%'").fetchone()[0] == 0


def test_the_history_csv_is_an_attachment_with_a_header_and_refuses_bad_requests(lab):  # noqa: F811
    busy, _ = seed_signatures(lab)
    r = lab.client.get(f"/admin/export/signature.csv?host=198.51.100.1&sig={busy}&days=7")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert 'attachment; filename="198.51.100.1-' in r.headers["content-disposition"]
    lines = r.text.splitlines()
    assert lines[0] == "day,events,hours_active,peak_hour_utc,peak_events,worst_severity" and len(lines) == 8
    assert lab.client.get(f"/admin/export/signature.csv?host=../x&sig={busy}").status_code == 400
    assert lab.client.get(f"/admin/export/signature.csv?host=a&sig={busy}&days=9999").status_code == 400


# ---------------------------------------------------------------- replay


@pytest.fixture
def replay_lab(lab, tmp_path):  # noqa: F811
    """The lab with the golden month ingested into its own database, and the shipped playbook and priorities."""
    for name in ("playbook.yaml", "priorities.yaml"):
        (tmp_path / "defaults" / "brief" / name).write_bytes((g.BRIEF / name).read_bytes())
    (tmp_path / "cfg" / "brief" / "systems.yaml").write_text(
        "schema: 1\nsystems:\n"
        "  - {id: gw, label: Gateway, hosts: [198.51.100.1]}\n"
        "  - {id: nas, label: Storage server, hosts: [198.51.100.2]}\n"
        "  - {id: cam, label: Camera, hosts: [198.51.100.3]}\n"
    )
    g.build_store(lab.settings.state_dir).close()  # the lab's database is state/baseline.db
    return lab


def test_the_replay_form_has_sensible_defaults(lab):  # noqa: F811
    html = lab.client.get("/admin/replay").text
    today = datetime.now(UTC).date()
    assert (
        (today - timedelta(days=7)).isoformat() in html
        and today.isoformat() in html
        and 'value="06:00"' in html
    )
    assert "Nothing is sent or stored" in html


def test_a_replay_runs_with_the_current_config_and_reports_what_it_would_push(replay_lab):
    csrf = token(replay_lab.client, "/admin/replay")
    r = replay_lab.client.post(
        "/admin/replay",
        data={
            "csrf_token": csrf,
            "start": "2026-09-18",
            "end": "2026-09-20",
            "at": "06:00",
            "hourly": "on",
            "ignore_learning": "on",
        },
    )
    assert r.status_code == 200 and "2026-09-18 to 2026-09-20" in r.text
    assert (
        "Would have been pushed" in r.text and "198.51.100.3 has been silent" in r.text
    )  # the camera, as in the golden day
    assert "Runs with something to report" in r.text


@pytest.mark.parametrize(
    "form, reason",
    [
        ({"start": "soon", "end": "2026-09-20"}, "is not a date"),
        ({"start": "2026-09-20", "end": "2026-09-18"}, "before the start"),
        ({"start": "2026-06-01", "end": "2026-09-20"}, "at most 31 days"),
        ({"start": "2026-09-01", "end": "2026-09-20", "hourly": "on"}, "at most 14 days"),
        ({"start": "2026-09-18", "end": "2026-09-20", "at": "25:99"}, "is not a time"),
    ],
)
def test_a_bad_replay_is_refused_with_the_reason(lab, form, reason):  # noqa: F811
    csrf = token(lab.client, "/admin/replay")
    r = lab.client.post("/admin/replay", data={"csrf_token": csrf, "at": "06:00", **form})
    assert r.status_code == 422 and reason in r.text


def test_the_tool_pages_have_no_inline_script_or_style(lab):  # noqa: F811
    seed_signatures(lab)
    for path in ("/admin/logs", "/admin/signatures", "/admin/replay"):
        html = lab.client.get(path).text
        assert " style=" not in html and "<script>" not in html, path
    _ = Path
