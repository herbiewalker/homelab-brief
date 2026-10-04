import gzip
import os
from datetime import date

from brief import archive

TODAY = date(2026, 10, 4)
NOW = 1_800_000_000.0


def make(root, day, name, text, age=7200):
    folder = root / day
    folder.mkdir(parents=True, exist_ok=True)
    f = folder / name
    f.write_text(text, encoding="utf-8")
    os.utime(f, (NOW - age, NOW - age))
    return f


def run(root, **kw):
    return archive.compress(root, today=TODAY, now=NOW, **kw)


def test_dry_run_changes_nothing(tmp_path):
    f = make(tmp_path, "2026-10-03", "198.51.100.1.log", "line\n" * 100)
    report = run(tmp_path)
    assert report.compressed == 1 and f.exists()
    assert not (tmp_path / "2026-10-03" / "198.51.100.1.log.gz").exists()
    assert any("would compress" in ln for ln in report.lines)


def test_apply_compresses_a_finished_day_and_removes_the_original(tmp_path):
    text = "Sep 26 13:22:33 host-01 app: hello\n" * 500
    f = make(tmp_path, "2026-10-03", "198.51.100.1.log", text)
    report = run(tmp_path, apply=True)
    gz = tmp_path / "2026-10-03" / "198.51.100.1.log.gz"
    assert report.compressed == 1 and not f.exists() and gz.exists()
    assert gzip.open(gz, "rt", encoding="utf-8").read() == text
    assert report.bytes_after < report.bytes_before
    assert not list(tmp_path.rglob("*.tmp"))


def test_today_and_future_days_are_never_touched(tmp_path):
    a = make(tmp_path, "2026-10-04", "198.51.100.1.log", "x\n")
    b = make(tmp_path, "2026-10-05", "198.51.100.1.log", "x\n")
    report = run(tmp_path, apply=True)
    assert report.compressed == 0 and a.exists() and b.exists()


def test_files_still_being_written_are_skipped(tmp_path):
    f = make(tmp_path, "2026-10-03", "198.51.100.1.log", "x\n", age=30)
    report = run(tmp_path, apply=True, min_age_secs=600)
    assert report.skipped_young == 1 and report.compressed == 0 and f.exists()


def test_other_folders_are_left_alone(tmp_path):
    imp = make(tmp_path, "import", "198.51.100.1.log", "x\n")
    odd = make(tmp_path, "2026-13-45", "198.51.100.1.log", "x\n")
    report = run(tmp_path, apply=True)
    assert report.compressed == 0 and imp.exists() and odd.exists()


def test_a_second_run_does_nothing(tmp_path):
    make(tmp_path, "2026-10-03", "198.51.100.1.log", "x\n" * 10)
    run(tmp_path, apply=True)
    again = run(tmp_path, apply=True)
    assert again.compressed == 0 and again.finished == 0 and not again.errors


def test_a_leftover_verified_gz_just_removes_the_original(tmp_path):
    text = "x\n" * 10
    f = make(tmp_path, "2026-10-03", "198.51.100.1.log", text)
    with gzip.open(f.with_name(f.name + ".gz"), "wt", encoding="utf-8") as g:
        g.write(text)
    report = run(tmp_path, apply=True)
    assert report.finished == 1 and not f.exists()


def test_a_mismatched_gz_is_an_error_and_both_files_stay(tmp_path):
    f = make(tmp_path, "2026-10-03", "198.51.100.1.log", "x\n" * 10)
    gz = f.with_name(f.name + ".gz")
    with gzip.open(gz, "wt", encoding="utf-8") as g:
        g.write("different\n")
    report = run(tmp_path, apply=True)
    assert len(report.errors) == 1 and f.exists() and gz.exists()


def test_a_missing_archive_is_fine(tmp_path):
    assert run(tmp_path / "nope", apply=True).compressed == 0


def test_cli_defaults_to_a_dry_run(tmp_path, capsys):
    f = make(tmp_path, "2026-10-03", "198.51.100.1.log", "x\n" * 10, age=10**9)
    assert archive.main(["compress", "--archive", str(tmp_path)]) == 0
    assert f.exists() and "nothing changed" in capsys.readouterr().out
