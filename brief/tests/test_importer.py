from pathlib import Path

import pytest

from brief import cli, importer


class FakeProc:
    """Stands in for Vector: writes the output files, then idles until terminated."""

    def __init__(self, state: Path, archive: Path, lines: int, alive=True):
        events = state / "events"
        events.mkdir(parents=True, exist_ok=True)
        (events / "import.ndjson.partial").write_text("{}\n" * lines, encoding="utf-8")
        (archive / "import").mkdir(parents=True, exist_ok=True)
        (archive / "import" / "198.51.100.1.log").write_text("x\n" * lines, encoding="utf-8")
        self.alive, self.terminated, self.returncode = alive, False, 3

    def poll(self):
        return None if self.alive else self.returncode

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


def sources(tmp_path, n=4):
    src = tmp_path / "import"
    src.mkdir()
    (src / "syslog-198.51.100.1.log").write_text("line\n" * n, encoding="utf-8")
    (src / "notes.txt").write_text("ignored\n", encoding="utf-8")
    return src


def run(tmp_path, src, spawn, say=None):
    config = tmp_path / "backfill.yaml"
    config.write_text('include: ["/import/syslog-*.log"]\n', encoding="utf-8")
    return importer.run_import(
        src, tmp_path / "state", tmp_path / "archive", config=config, idle_seconds=0.02, poll_seconds=0.01,
        say=say or (lambda m: None), spawn=spawn)  # fmt: skip


def fake(tmp_path, lines=4, alive=True):
    return lambda cmd, **kw: FakeProc(tmp_path / "state", tmp_path / "archive", lines, alive)


def test_a_good_import_waits_for_output_to_settle_then_stops_vector(tmp_path):
    src = sources(tmp_path)
    said, procs = [], []

    def spawn(cmd, **kw):
        assert cmd[0] == "vector" and cmd[1] == "--config"
        procs.append(FakeProc(tmp_path / "state", tmp_path / "archive", 4))
        return procs[0]

    assert run(tmp_path, src, spawn, said.append) == 0
    assert procs[0].terminated
    assert any("4 of 4" in m for m in said) and any("Importing 4 lines from 1 files" in m for m in said)
    events = tmp_path / "state" / "events"
    assert (events / "import.ndjson").exists() and not (events / "import.ndjson.partial").exists()


def test_the_worker_never_sees_the_partial_file():
    # the ingest reads *.ndjson; the partial file must not match that pattern
    assert not "import.ndjson.partial".endswith(".ndjson")


def test_a_killed_vector_leaves_nothing_behind_and_the_hint_mentions_memory(tmp_path):
    src = sources(tmp_path)

    class Dies(FakeProc):
        polls = 0

        def poll(self):
            Dies.polls += 1
            if Dies.polls > 1:
                self.returncode = -9  # what Popen reports for a process killed by SIGKILL
                return -9
            return None

    def spawn_dies(cmd, **kw):
        return Dies(tmp_path / "state", tmp_path / "archive", 3)

    with pytest.raises(importer.ImportFailed, match="memory"):
        run(tmp_path, src, spawn_dies)
    events = tmp_path / "state" / "events"
    assert not (events / "import.ndjson").exists() and not (events / "import.ndjson.partial").exists()
    assert not list((tmp_path / "archive" / "import").glob("*.log"))  # the raw copies are removed too


def test_a_cut_off_import_fails_and_counts_nothing(tmp_path):
    src = sources(tmp_path, n=1000)
    with pytest.raises(importer.ImportFailed, match="cut off"):
        run(tmp_path, src, fake(tmp_path, lines=400))  # far fewer lines than the source
    assert not (tmp_path / "state" / "events" / "import.ndjson").exists()


def test_a_few_missing_lines_are_tolerated(tmp_path):
    src = sources(tmp_path, n=2000)
    assert run(tmp_path, src, fake(tmp_path, lines=1999)) == 0  # blank or oversize lines are skipped


def test_leftovers_of_an_unfinished_import_are_removed_before_a_new_run(tmp_path):
    src = sources(tmp_path)
    events = tmp_path / "state" / "events"
    events.mkdir(parents=True)
    (events / "import.ndjson.partial").write_text("stale\n" * 50, encoding="utf-8")
    (tmp_path / "archive" / "import").mkdir(parents=True)
    (tmp_path / "archive" / "import" / "198.51.100.9.log").write_text("stale\n", encoding="utf-8")
    said = []
    assert run(tmp_path, src, fake(tmp_path), said.append) == 0
    assert any("did not finish" in m for m in said)
    assert not (tmp_path / "archive" / "import" / "198.51.100.9.log").exists()
    assert (events / "import.ndjson").read_text(encoding="utf-8").count("\n") == 4  # fresh, not the stale 50


def test_the_source_folder_is_never_written(tmp_path):
    src = sources(tmp_path)
    before = sorted((p.name, p.stat().st_size) for p in src.iterdir())
    run(tmp_path, src, fake(tmp_path))
    assert sorted((p.name, p.stat().st_size) for p in src.iterdir()) == before


def test_no_source_files_is_a_clear_error(tmp_path):
    empty = tmp_path / "import"
    empty.mkdir()
    with pytest.raises(importer.ImportFailed, match="No syslog-"):
        run(tmp_path, empty, lambda *a, **k: pytest.fail("must not start Vector"))
    with pytest.raises(importer.ImportFailed):
        run(tmp_path, tmp_path / "missing", lambda *a, **k: pytest.fail("must not start Vector"))


def test_a_second_import_is_refused_so_nothing_is_counted_twice(tmp_path):
    src = sources(tmp_path)
    (tmp_path / "state" / "events").mkdir(parents=True)
    (tmp_path / "state" / "events" / "import.ndjson").write_text("{}\n", encoding="utf-8")
    with pytest.raises(importer.ImportFailed, match="count every"):
        run(tmp_path, src, lambda *a, **k: pytest.fail("must not start Vector"))


def test_vector_dying_early_is_reported(tmp_path):
    src = sources(tmp_path)
    with pytest.raises(importer.ImportFailed, match="stopped early"):
        run(tmp_path, src, fake(tmp_path, lines=0, alive=False))


def test_vector_writing_nothing_gives_up_with_a_hint(tmp_path, monkeypatch):
    src = sources(tmp_path)

    class Silent:
        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(importer.time, "sleep", lambda s: None)
    ticks = iter(range(0, 100_000, 30))
    monkeypatch.setattr(importer.time, "monotonic", lambda: next(ticks))
    with pytest.raises(importer.ImportFailed, match="wrote nothing"):
        run(tmp_path, src, lambda cmd, **kw: Silent())


def test_another_source_folder_gets_a_temporary_config_that_is_removed_afterwards(tmp_path):
    src = tmp_path / "elsewhere"
    src.mkdir()
    (src / "syslog-198.51.100.1.log").write_text("a\nb\n", encoding="utf-8")
    seen = {}

    def spawn(cmd, **kw):
        seen["config"] = Path(cmd[2])
        seen["text"] = Path(cmd[2]).read_text(encoding="utf-8")
        return FakeProc(tmp_path / "state", tmp_path / "archive", 2)

    run(tmp_path, src, spawn)
    assert f"{src.as_posix()}/syslog-*.log" in seen["text"] and "/import/" not in seen["text"]
    assert not seen["config"].exists()


def test_the_cli_routes_import(tmp_path, monkeypatch):
    got = {}
    monkeypatch.setattr(importer, "main", lambda source, state, archive: got.update(source=source) or 0)
    monkeypatch.setenv("BRIEF_STATE", str(tmp_path))
    assert cli.main(["import", "--from", "/somewhere"]) == 0 and got["source"] == "/somewhere"
