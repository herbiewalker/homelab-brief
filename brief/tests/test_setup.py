"""Task 16a: first-run setup (`brief init` and the wizard), and the devices and topology files."""

import builtins
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_admin import lab  # noqa: F401  (the fixture: an HTTPS app with a signed-in admin)

from brief import cli, configedit, configguard, health, privateconf, setup
from brief.config import ConfigError, load_mapping
from brief.store import Store
from brief.systems import Systems

BRIEF = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


@pytest.fixture
def home(tmp_path, monkeypatch):
    user = tmp_path / "cfg" / "brief"
    defaults = tmp_path / "defaults" / "brief"
    user.mkdir(parents=True)
    defaults.mkdir(parents=True)
    for name in configedit.EDITABLE:
        shipped = BRIEF / name if (BRIEF / name).exists() else BRIEF / name.replace(".yaml", ".example.yaml")
        (defaults / shipped.name).write_bytes(shipped.read_bytes())
    state = tmp_path / "state"
    state.mkdir()
    for key, value in {
        "BRIEF_STATE": state, "BRIEF_CONFIG_DIR": tmp_path / "cfg", "BRIEF_DEFAULTS_DIR": tmp_path / "defaults",
        "BRIEF_OUT": tmp_path / "out", "BRIEF_ARCHIVE": tmp_path / "archive",
    }.items():  # fmt: skip
        monkeypatch.setenv(key, str(value))
    monkeypatch.setattr(health, "receiver_ok", lambda port: True)
    monkeypatch.setattr("getpass.getuser", lambda: "tester")
    store = Store(state / "baseline.db")
    yield tmp_path, user, state, store
    store.close()


def seed_senders(store, now=None):
    now = now or datetime.now(UTC)
    with store.write() as tx:
        for host in ("198.51.100.10", "198.51.100.11", "198.51.100.12"):
            tx.touch_host(host, now - timedelta(days=2), now, system_id="other", known=False)
        wifi = tx.signature_for(0, 1, "hostapd", "STA assoc", now)
        nas = tx.signature_for(0, 2, "smbd", "connect", now)
        misc = tx.signature_for(0, 3, "cron", "job", now)
        day, hour = f"{now:%Y-%m-%d}", now.hour
        tx.add_hourly([(day, hour, "198.51.100.10", wifi, "info", 90), (day, hour, "198.51.100.11", nas, "info", 40),
                       (day, hour, "198.51.100.12", misc, "info", 5)])  # fmt: skip


# ---------------------------------------------------------------- the helpers


@pytest.mark.parametrize(
    "apps, kind",
    [
        (["hostapd", "kernel"], "ap"),
        (["smbd"], "server"),
        (["unifi-protect"], "camera"),
        (["HOSTAPD"], "ap"),
        (["cron"], ""),
        ([], ""),
    ],
)
def test_the_kind_is_only_a_suggestion_from_the_busiest_programs(apps, kind):
    assert setup.guess_kind(apps) == kind


@pytest.mark.parametrize(
    "text, out", [("Main Gateway", "main-gateway"), ("  NAS #2 ", "nas-2"), ("a_b-c", "a_b-c"), ("!!!", "")]
)
def test_slug_makes_an_id_from_a_name(text, out):
    assert setup.slug(text) == out


def test_senders_lists_each_with_its_busiest_programs_and_a_suggestion(home):
    tmp_path, user, state, store = home
    seed_senders(store)
    rows = {r["host"]: r for r in setup.senders(store, {}, datetime.now(UTC))}
    assert rows["198.51.100.10"]["apps"] == ["hostapd"] and rows["198.51.100.10"]["kind_guess"] == "ap"
    assert rows["198.51.100.11"]["kind_guess"] == "server" and rows["198.51.100.12"]["kind_guess"] == ""
    assert setup.needs_setup(store) == 3


def test_apply_makes_systems_adds_senders_and_validates(home):
    tmp_path, user, state, store = home
    msg = setup.apply(store, [setup.Assignment("198.51.100.10", "Wi-Fi", kind="ap"), setup.Assignment("198.51.100.11", "Wi-Fi"),
                              setup.Assignment("198.51.100.12", "Storage Server", "Storage", "server")], "tester", state)  # fmt: skip
    assert "3 sender(s) assigned, 2 new system(s) made" in msg
    systems = Systems.load(user / "systems.yaml")
    assert {s.id: s.hosts for s in systems.systems} == {
        "wi-fi": ("198.51.100.10", "198.51.100.11"),
        "storage-server": ("198.51.100.12",),
    }
    assert {s.id: s.kind for s in systems.systems} == {"wi-fi": "ap", "storage-server": "server"}
    with store.read() as db:
        assert db.execute("SELECT user FROM audit_log WHERE action = 'config.save'").fetchone()[0] == "tester"
    assert configedit.versions(state, "systems.yaml")


def test_apply_adds_to_an_existing_system_without_duplicating(home):
    tmp_path, user, state, store = home
    setup.apply(store, [setup.Assignment("198.51.100.10", "gw")], "tester", state)
    setup.apply(
        store,
        [setup.Assignment("198.51.100.11", "gw"), setup.Assignment("198.51.100.10", "gw")],
        "tester",
        state,
    )
    (system,) = Systems.load(user / "systems.yaml").systems
    assert system.hosts == ("198.51.100.10", "198.51.100.11")


def test_apply_refuses_nothing_and_nonsense(home):
    tmp_path, user, state, store = home
    with pytest.raises(configedit.EditError, match="at least one"):
        setup.apply(store, [setup.Assignment("198.51.100.10", "  ")], "tester", state)
    with pytest.raises(ConfigError, match="system id"):
        setup.apply(store, [setup.Assignment("198.51.100.10", "!!!")], "tester", state)
    assert not (user / "systems.yaml").exists()


# ---------------------------------------------------------------- brief init


PROMPTS: list[str] = []


def answers(monkeypatch, *given):
    queue = list(given)
    PROMPTS.clear()

    def fake(prompt=""):
        PROMPTS.append(prompt)
        if not queue:
            raise EOFError
        return queue.pop(0)

    monkeypatch.setattr(builtins, "input", fake)


def test_init_with_no_senders_says_how_to_start(home, capsys):
    assert cli.main(["init"]) == 0
    out = capsys.readouterr().out
    assert (
        "should send syslog to" in out
        and "No sender has reached the receiver yet" in out
        and "brief network test" in out
    )


def test_init_asks_for_each_unknown_sender_and_saves_after_confirming(home, capsys, monkeypatch):
    tmp_path, user, state, store = home
    seed_senders(store)
    answers(monkeypatch, "Wi-Fi", "", "Storage", "y")
    assert cli.main(["init"]) == 0
    out = capsys.readouterr().out
    assert (
        "3 sender(s) seen, 3 not in a system yet." in out
        and "looks like: ap" in "".join(PROMPTS)
        and "talks about: hostapd" in "".join(PROMPTS)
    )
    assert (
        "2 sender(s) assigned, 2 new system(s) made." in out
        and "Next steps:" in out
        and "brief config validate" in out
    )
    assert [s.id for s in Systems.load(user / "systems.yaml").systems] == ["wi-fi", "storage"]


def test_init_does_nothing_when_you_say_no_or_stop(home, capsys, monkeypatch):
    tmp_path, user, state, store = home
    seed_senders(store)
    answers(monkeypatch, "Wi-Fi", "", "", "n")
    assert (
        cli.main(["init"]) == 0
        and "Nothing was saved." in capsys.readouterr().out
        and not (user / "systems.yaml").exists()
    )
    answers(monkeypatch, "q")
    assert cli.main(["init"]) == 0 and "Nothing assigned." in capsys.readouterr().out
    answers(monkeypatch)  # no terminal at all: end of input
    assert cli.main(["init"]) == 0 and not (user / "systems.yaml").exists()


def test_init_assign_is_for_scripts_and_needs_no_prompt(home, capsys, monkeypatch):
    tmp_path, user, state, store = home
    seed_senders(store)
    monkeypatch.setattr(builtins, "input", lambda p="": pytest.fail("asked a question"))
    assert (
        cli.main(["init", "--assign", "198.51.100.10=Wi-Fi:Wi-Fi network", "--assign", "198.51.100.11=wi-fi"])
        == 0
    )
    (system,) = Systems.load(user / "systems.yaml").systems
    assert system.id == "wi-fi" and system.label == "Wi-Fi network" and len(system.hosts) == 2
    assert cli.main(["init", "--assign", "nonsense"]) == 2


# ---------------------------------------------------------------- the wizard page


def token(client, path="/admin/wizard"):
    return re.search(r'name="csrf_token" value="([^"]+)"', client.get(path).text).group(1)


def test_the_wizard_needs_a_login_and_does_not_take_over_the_account_setup_page(lab):  # noqa: F811
    assert lab.https().get("/admin/wizard").status_code == 303
    assert lab.https().post("/admin/wizard", data={}).status_code == 401
    assert lab.client.post("/admin/wizard", data={}).status_code == 403
    page = lab.https().get("/admin/setup")  # the first-account page: 404 once an admin exists, as before
    assert page.status_code == 404


def test_the_wizard_lists_senders_with_suggestions(lab):  # noqa: F811
    seed_senders(lab.store)
    html = lab.client.get("/admin/wizard").text
    assert "3 senders seen, 3 not in a system yet" in html and "hostapd" in html and "ap (suggested)" in html
    assert "Send syslog" in html and "all valid" in html and 'href="/admin/network"' in html


def test_the_wizard_with_nothing_received_says_so(lab):  # noqa: F811
    assert "No sender has reached the receiver yet" in lab.client.get("/admin/wizard").text


def test_naming_senders_in_the_wizard_saves_validated_systems(lab, tmp_path):  # noqa: F811
    seed_senders(lab.store)
    html = lab.client.get("/admin/wizard").text
    hosts = re.findall(r'name="host-(\d+)" value="([^"]+)"', html)
    data = {"csrf_token": token(lab.client), "count": str(len(hosts))}
    for i, host in hosts:
        data[f"host-{i}"] = host
        data[f"system-{i}"] = "Wi-Fi" if host != "198.51.100.12" else ""
        data[f"kind-{i}"] = "ap"
    r = lab.client.post("/admin/wizard", data=data)
    assert r.status_code == 303 and r.headers["location"] == "/admin/wizard?done=saved"
    (system,) = Systems.load(tmp_path / "cfg" / "brief" / "systems.yaml").systems
    assert system.id == "wi-fi" and set(system.hosts) == {"198.51.100.10", "198.51.100.11"}
    after = lab.client.get(r.headers["location"]).text
    assert "Saved. The pipeline picks the new systems up" in after and '<option value="wi-fi">' in after
    assert (
        "in wi-fi" in after and "1 not in a system yet" in after
    )  # shown at once, before the worker's next cycle
    with lab.store.read() as db:
        assert db.execute("SELECT user FROM audit_log WHERE action = 'config.save'").fetchone()[0] == "admin"


def test_the_wizard_refuses_an_empty_form_and_a_bad_name(lab, tmp_path):  # noqa: F811
    seed_senders(lab.store)
    csrf = token(lab.client)
    r = lab.client.post(
        "/admin/wizard", data={"csrf_token": csrf, "count": "1", "host-0": "198.51.100.10", "system-0": ""}
    )
    assert r.status_code == 422 and "at least one system" in r.text
    r = lab.client.post(
        "/admin/wizard", data={"csrf_token": csrf, "count": "1", "host-0": "198.51.100.10", "system-0": "###"}
    )
    assert r.status_code == 422 and "Not saved." in r.text
    assert not (tmp_path / "cfg" / "brief" / "systems.yaml").exists()


def test_the_overview_points_at_the_wizard_while_senders_are_unmapped(lab):  # noqa: F811
    assert "not in a system yet" not in lab.client.get("/admin").text
    seed_senders(lab.store)
    html = lab.client.get("/admin").text
    assert "3 senders not in a system yet." in html and "/admin/wizard" in html


def test_the_wizard_has_no_inline_script_or_style(lab):  # noqa: F811
    seed_senders(lab.store)
    html = lab.client.get("/admin/wizard").text
    assert " style=" not in html and "<script>" not in html


# ---------------------------------------------------------------- devices.yaml and topology.yaml


def parse(tmp_path, text):
    path = tmp_path / "f.yaml"
    path.write_text(text)
    return load_mapping(path)


def test_the_shipped_examples_are_valid(tmp_path):
    privateconf.validate_devices(load_mapping(BRIEF / "devices.example.yaml"))
    privateconf.validate_topology(load_mapping(BRIEF / "topology.example.yaml"))


@pytest.mark.parametrize(
    "text",
    [
        "schema: 1\ndevices: {a: b}\n",
        "schema: 1\ndevices:\n  - label: no mac\n",
        'schema: 1\ndevices:\n  - mac: "00:00:5E"\n',
        'schema: 1\ndevices:\n  - {mac: "00:00:5E:00:53:01", profile: spy}\n',
        'schema: 1\ndevices:\n  - {mac: "00:00:5E:00:53:01"}\n  - {mac: "00-00-5E-00-53-01"}\n',
        'schema: 1\ndevices:\n  - {mac: "00:00:5E:00:53:01", quiet_hours: {start: "25:00", end: "06:00"}}\n',
        'schema: 1\ndevices:\n  - {mac: "00:00:5E:00:53:01", label: [1]}\n',
    ],
)
def test_a_bad_devices_file_is_refused(tmp_path, text):
    with pytest.raises(ConfigError):
        privateconf.validate_devices(parse(tmp_path, text))


@pytest.mark.parametrize(
    "text",
    [
        "schema: 1\nnodes:\n  - {kind: switch}\n",
        "schema: 1\nnodes:\n  - {id: a, kind: toaster}\n",
        "schema: 1\nnodes:\n  - {id: a}\n  - {id: a}\n",
        "schema: 1\nedges:\n  - {from: a}\n",
        "schema: 1\nedges:\n  - {from: a, to: b, kind: wireless}\n",
        "schema: 1\noverrides:\n  - {label: x}\n",
        "schema: 1\noverrides:\n  - {id: a, hide: maybe}\n",
        "schema: 1\nnodes: nope\n",
    ],
)
def test_a_bad_topology_file_is_refused(tmp_path, text):
    with pytest.raises(ConfigError):
        privateconf.validate_topology(parse(tmp_path, text))


def test_the_config_editor_and_guard_cover_both_files(home):
    tmp_path, user, state, store = home
    assert {"devices.yaml", "topology.yaml"} <= set(
        configedit.EDITABLE
    ) and "private" in configedit.DESCRIPTIONS["devices.yaml"]
    text = configedit.starting_text("devices.yaml")
    assert "Example laptop" in text  # starts from the example, which never holds real values
    configedit.save(store, "devices.yaml", 'schema: 1\ndevices:\n  - {mac: "00:00:5E:00:53:01", label: Phone}\n', "tester",
                    configedit.digest(text), state)  # fmt: skip
    (user / "topology.yaml").write_text("schema: 1\nnodes:\n  - {id: a, kind: toaster}\n")
    guard = configguard.ConfigGuard(state)
    assert "topology.yaml" in guard.check() and "devices.yaml" not in guard.check()
