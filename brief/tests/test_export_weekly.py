import csv
import io
import json
from datetime import UTC, date, datetime, timedelta

import golden_day as g
import pytest

from brief import cli, export, weekly
from brief.store import Store

TODAY = date(2026, 9, 20)


@pytest.fixture
def month(tmp_path):
    store = g.build_store(tmp_path / "work")
    yield store
    store.close()


def sig_for(store, text):
    with store.read() as db:
        return db.execute("SELECT sig_id FROM signature WHERE template LIKE ?", (f"%{text}%",)).fetchone()[0]


# ---------------------------------------------------------------- export


def test_the_series_has_one_row_per_day_with_zeros_and_a_peak(month):
    sid = sig_for(month, "DROP IN")
    rows = export.series(month, g.GW, sid, TODAY, days=30)
    assert len(rows) == 30 and rows[0][0] == "2026-08-22" and rows[-1][0] == "2026-09-20"
    by_day = {r[0]: r for r in rows}
    assert by_day["2026-09-18"] == [
        "2026-09-18",
        100,
        1,
        10,
        100,
        "warning",
    ]  # a steady day, all in the 10:00 hour
    assert by_day["2026-09-19"][1] == 700 and by_day["2026-09-19"][4] == 700  # the spike day
    assert by_day["2026-09-20"] == ["2026-09-20", 0, 0, "", 0, ""]  # nothing yet that day: a visible zero row


def test_the_csv_has_a_header_and_parses_back(month):
    sid = sig_for(month, "DROP IN")
    text = export.to_csv(export.series(month, g.GW, sid, TODAY, days=3))
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0] == list(export.COLUMNS) and len(rows) == 4
    assert rows[1][1] == "100" and rows[2][1] == "700"  # a steady day, then the spike day


def test_the_busiest_hour_wins_ties_with_the_earlier_one(month):
    sid = sig_for(month, "DROP IN")
    with month.write() as tx:
        tx.add_hourly([("2026-09-17", 11, g.GW, sid, "err", 100)])  # as busy as the 10:00 hour, and worse
    row = {r[0]: r for r in export.series(month, g.GW, sid, TODAY, days=5)}["2026-09-17"]
    assert row[1:] == [200, 2, 10, 100, "err"]


def test_write_export_names_the_file_for_host_and_signature(month, tmp_path):
    sid = sig_for(month, "DROP IN")
    path = export.write_export(month, tmp_path / "out", g.GW, sid, TODAY, days=7)
    assert path == tmp_path / "out" / "export" / f"{g.GW}-{sid}.csv" and path.exists()
    assert not list(path.parent.glob("*.tmp"))


def test_top_signatures_are_the_hosts_busiest(month):
    ids = export.top_signatures(month, g.GW, TODAY, 30, 2)
    assert ids[0] == sig_for(month, "DROP IN") and len(ids) == 2


# The golden month is fixed in the past, so a window measured from today would one day stop reaching it.
WINDOW = 3650


def test_the_export_command_checks_its_arguments_and_writes(month, tmp_path, monkeypatch, capsys):
    month.close()
    state = tmp_path / "work"
    ns = cli.argparse.Namespace
    assert cli.run_export(ns(host=g.GW, sig=None, top=None, days=WINDOW), state, out_dir=tmp_path / "o") == 2
    assert cli.run_export(ns(host=g.GW, sig=1, top=2, days=WINDOW), state, out_dir=tmp_path / "o") == 2
    printed = []
    assert (
        cli.run_export(
            ns(host=g.GW, sig=None, top=2, days=WINDOW), state, out=printed.append, out_dir=tmp_path / "o"
        )
        == 0
    )
    assert len(printed) == 2 and all(p.endswith(".csv") for p in printed)
    assert (
        cli.run_export(ns(host="198.51.100.250", sig=None, top=3, days=WINDOW), state, out_dir=tmp_path / "o")
        == 1
    )
    assert "No signatures" in capsys.readouterr().err


# ---------------------------------------------------------------- weekly


def test_weeks_are_iso_weeks_monday_to_sunday():
    assert weekly.week_of(date(2026, 9, 20)) == (date(2026, 9, 14), date(2026, 9, 20), "2026-W38")
    assert weekly.week_of(date(2026, 9, 14))[0] == date(2026, 9, 14)
    assert weekly.parse_week("2026-W38") == date(2026, 9, 14) and weekly.parse_week("2026-w01") == date(
        2025, 12, 29
    )
    assert weekly.current_monday(datetime(2026, 10, 4, 12, tzinfo=UTC)) == date(2026, 9, 28)


def test_the_weekly_report_tells_the_weeks_story(month, tmp_path):
    text = "\n".join(weekly.weekly_markdown(month, g.SYSTEMS, date(2026, 9, 14), env=g.golden_env(tmp_path)))
    assert text.startswith("# Weekly report 2026-W38")
    assert "| Gateway | 1,386 | 889 | +56% |" in text  # volume against the week before
    assert "firewall: DROP IN=eth0" in text and "1,200 vs 700" in text  # the biggest mover
    assert "I/O error" in text.split("## New this week")[1].split("##")[0]  # new this week
    assert "198.51.100.3 has been silent" in text.split("## Hosts that went silent")[1]  # the camera


def test_the_report_for_a_week_with_no_data_says_so_and_fits(tmp_path):
    store = Store(tmp_path / "empty.db")
    try:
        lines = weekly.weekly_markdown(store, g.SYSTEMS, date(2026, 9, 14), env=g.golden_env(tmp_path))
    finally:
        store.close()
    assert len(lines) <= 150 and "(no data)" in "\n".join(lines) and "- nothing moved" in lines


def test_a_signature_that_went_quiet_is_listed(month, tmp_path):
    # rsyslogd's warning was regular on Aug 21-30; look at the week after its last regular day
    text = "\n".join(weekly.weekly_markdown(month, g.SYSTEMS, date(2026, 8, 31), env=g.golden_env(tmp_path)))
    quiet = text.split("## Went quiet")[1].split("##")[0]
    assert "rsyslogd" in quiet and "days last week" in quiet


def test_recurring_issues_need_three_days(month, tmp_path):
    with month.write() as tx:  # the spike returns on three more days of the week
        sid = sig_for(month, "DROP IN")
        tx.add_hourly([(f"2026-09-{d}", 10, g.GW, sid, "warning", 800) for d in (14, 15, 16)])
    text = "\n".join(weekly.weekly_markdown(month, g.SYSTEMS, date(2026, 9, 14), env=g.golden_env(tmp_path)))
    recurring = text.split("## Recurring issues")[1].split("##")[0]
    assert "firewall" in recurring and "days" in recurring


def test_write_weekly_names_the_file_for_the_week(month, tmp_path):
    path = weekly.write_weekly(
        month, g.SYSTEMS, tmp_path / "out", date(2026, 9, 14), env=g.golden_env(tmp_path)
    )
    assert path == tmp_path / "out" / "weekly" / "2026-W38.md" and len(path.read_text().splitlines()) <= 150


def test_the_weekly_command(month, tmp_path, monkeypatch):
    month.close()
    monkeypatch.setenv("BRIEF_CONFIG_DIR", str(tmp_path / "none"))
    monkeypatch.setenv("BRIEF_DEFAULTS_DIR", str(tmp_path / "none"))
    state = tmp_path / "work"
    ns = cli.argparse.Namespace
    assert cli.run_weekly(ns(week="nonsense", dry_run=False), state, out_dir=tmp_path / "o") == 2
    printed = []
    assert (
        cli.run_weekly(ns(week="2026-W38", dry_run=True), state, out=printed.append, out_dir=tmp_path / "o")
        == 0
    )
    assert printed[0] == "# Weekly report 2026-W38" and not (tmp_path / "o").exists()
    assert (
        cli.run_weekly(ns(week="2026-W38", dry_run=False), state, out=printed.append, out_dir=tmp_path / "o")
        == 0
    )
    assert (tmp_path / "o" / "weekly" / "2026-W38.md").exists()


# ---------------------------------------------------------------- the digest command


def test_the_digest_command_dry_run_and_real(month, tmp_path, monkeypatch):
    month.close()
    monkeypatch.setenv("BRIEF_CONFIG_DIR", str(tmp_path / "none"))
    monkeypatch.setenv("BRIEF_DEFAULTS_DIR", str(tmp_path / "none"))
    state = tmp_path / "work"
    ns = cli.argparse.Namespace
    now = (g.RUN_AT + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    shown = []
    assert cli.run_digest(ns(now=now, dry_run=True), state, out=shown.append, out_dir=tmp_path / "o") == 0
    assert shown[0].startswith("# Homelab brief for 2026-09-20") and not (tmp_path / "o").exists()
    shown.clear()
    code = cli.run_digest(ns(now=now, dry_run=False), state, out=shown.append, out_dir=tmp_path / "o")
    assert code == 0 and "Wrote" in shown[1]
    assert json.loads((tmp_path / "o" / "latest" / "status.json").read_text())["run_ok"] is True


def test_the_digest_command_exits_nonzero_when_the_run_failed(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIEF_CONFIG_DIR", str(tmp_path / "none"))
    monkeypatch.setenv("BRIEF_DEFAULTS_DIR", str(tmp_path / "none"))
    Store(tmp_path / "state" / "baseline.db").close()
    ns = cli.argparse.Namespace
    shown = []
    code = cli.run_digest(
        ns(now=None, dry_run=False), tmp_path / "state", out=shown.append, out_dir=tmp_path / "o"
    )
    assert code == 1 and shown[0].startswith("No data today")


def test_the_parser_knows_the_new_commands(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIEF_STATE", str(tmp_path))
    monkeypatch.setenv("BRIEF_OUT", str(tmp_path / "o"))
    Store(tmp_path / "baseline.db").close()
    assert cli.main(["digest", "--dry-run"]) == 1  # an empty database: no data today
    assert cli.main(["export", "--host", "198.51.100.1"]) == 2
    assert cli.main(["weekly", "--week", "2026-W38", "--dry-run"]) == 0
