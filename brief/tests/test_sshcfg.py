"""Task 16: the optional SSH server's settings: key-only, nothing weaker, and nothing guessed."""

import subprocess
from pathlib import Path

import pytest

from brief import sshcfg

ED = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGq1Tk6uP0lH9y3wXk2m4bJq0dQmVv7r5cE8nHkZtWab you@laptop"
RSA = "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQDexample= other@host"


def env_for(tmp_path, **extra):
    cfg = tmp_path / "cfg"
    (cfg / "ssh").mkdir(parents=True, exist_ok=True)
    return {
        "BRIEF_CONFIG_DIR": str(cfg),
        "BRIEF_DEFAULTS_DIR": str(tmp_path / "defaults"),
        "SSH_ENABLED": "true",
        **extra,
    }


def write_keys(tmp_path, text):
    (tmp_path / "cfg" / "ssh").mkdir(parents=True, exist_ok=True)
    (tmp_path / "cfg" / "ssh" / "authorized_keys").write_text(text)


# ---------------------------------------------------------------- which keys are accepted


def test_plain_public_keys_are_accepted_and_comments_ignored():
    keys, problems = sshcfg.valid_keys(f"# my keys\n\n{ED}\n  {RSA}  \n")
    assert keys == [ED, RSA] and problems == []


@pytest.mark.parametrize(
    "line",
    [
        'command="rm -rf /" ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGq1Tk6u you',  # an option
        'permitopen="198.51.100.1:22" ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGq1Tk6u',
        'from="1.2.3.4" ssh-rsa AAAAB3NzaC1yc2E',
        "ssh-dss AAAAB3NzaC1kc3MAAACB you",  # a key type nobody should use
        "not a key at all",
        "ssh-ed25519",  # no key
        "ssh-ed25519 AAAA\x00BBBB",
    ],
)
def test_a_line_with_options_or_an_odd_type_is_refused_never_trusted(line):
    keys, problems = sshcfg.valid_keys(line + "\n")
    assert keys == [] and len(problems) == 1 and "line 1" in problems[0]


def test_one_bad_line_does_not_hide_the_good_ones():
    keys, problems = sshcfg.valid_keys(f"{ED}\ncommand=x {RSA}\n")
    assert keys == [ED] and "line 2" in problems[0]


# ---------------------------------------------------------------- off by default, and what is checked


def test_it_is_off_unless_asked_for(tmp_path):
    assert sshcfg.enabled({}) is False and sshcfg.enabled({"SSH_ENABLED": "false"}) is False
    assert all(sshcfg.enabled({"SSH_ENABLED": v}) for v in ("true", "TRUE", "1", "yes"))
    assert sshcfg.main.__module__  # (main exits 0 without touching anything when it is off)


def test_main_does_nothing_when_disabled(monkeypatch, tmp_path):
    monkeypatch.delenv("SSH_ENABLED", raising=False)
    monkeypatch.setattr(sshcfg, "render", lambda *a, **k: pytest.fail("rendered while disabled"))
    assert sshcfg.main() == 0


def test_enabled_without_keys_is_a_problem_not_an_open_door(tmp_path):
    env = env_for(tmp_path, SSH_LISTEN="198.51.100.50")
    info = sshcfg.describe(env)
    assert info["enabled"] and info["keys"] == 0 and any("does not exist" in p for p in info["problems"])
    write_keys(tmp_path, "# nothing here\n")
    assert any("no usable key" in p for p in sshcfg.describe(env)["problems"])
    with pytest.raises(sshcfg.SshConfigError):
        sshcfg.render(env, run_dir=tmp_path / "run", host_key=tmp_path / "hostkey")


def test_describe_counts_keys_and_reports_bad_lines(tmp_path):
    write_keys(tmp_path, f"{ED}\ncommand=x {RSA}\n")
    info = sshcfg.describe(env_for(tmp_path, SSH_LISTEN="198.51.100.50", SSH_PORT="2222"))
    assert info["keys"] == 1 and info["port"] == 2222 and info["listen"] == "198.51.100.50"
    assert any("authorized_keys: line 2" in p for p in info["problems"])


def test_a_bad_port_is_refused():
    for bad in ("0", "70000", "abc"):
        with pytest.raises(sshcfg.SshConfigError):
            sshcfg.port_for({"SSH_PORT": bad})


def test_the_port_comes_from_network_yaml_when_the_variable_is_not_set(tmp_path):
    folder = tmp_path / "cfg" / "brief"
    folder.mkdir(parents=True)
    (folder / "network.yaml").write_text("schema: 1\nports: {ssh: 2200}\n")
    assert (
        sshcfg.port_for(
            {"BRIEF_CONFIG_DIR": str(tmp_path / "cfg"), "BRIEF_DEFAULTS_DIR": str(tmp_path / "d")}
        )
        == 2200
    )
    assert (
        sshcfg.port_for(
            {"BRIEF_CONFIG_DIR": str(tmp_path / "none"), "BRIEF_DEFAULTS_DIR": str(tmp_path / "d")}
        )
        == 22
    )


# ---------------------------------------------------------------- where it listens


def net(mode, address="198.51.100.50", advertised=None):
    return {"mode": mode, "address": address, "advertised": advertised}


def test_own_ip_listens_on_the_container_address():
    assert sshcfg.listen_address({}, net("own_ip")) == "198.51.100.50"
    with pytest.raises(sshcfg.SshConfigError):
        sshcfg.listen_address({}, net("own_ip", address=None))


def test_bridge_listens_inside_the_container_where_docker_maps_the_port():
    assert sshcfg.listen_address({}, net("bridge", "172.18.0.5")) == "0.0.0.0"


def test_host_mode_will_not_open_ssh_on_every_host_address():
    with pytest.raises(sshcfg.SshConfigError, match="advertised_address"):
        sshcfg.listen_address({}, net("host"))
    with pytest.raises(sshcfg.SshConfigError):
        sshcfg.listen_address({}, net("host", advertised="lab.example"))  # a name is not an address to bind
    assert sshcfg.listen_address({}, net("host", advertised="198.51.100.60")) == "198.51.100.60"


def test_an_explicit_address_wins():
    assert sshcfg.listen_address({"SSH_LISTEN": "198.51.100.99"}, net("host")) == "198.51.100.99"


# ---------------------------------------------------------------- what is written


def test_the_config_allows_keys_only_and_nothing_else():
    text = sshcfg.sshd_config(
        "198.51.100.50", 2222, Path("/state/ssh/key"), Path("/run/sshd/authorized_keys")
    )
    lines = set(text.splitlines())
    for required in (
        "Port 2222", "ListenAddress 198.51.100.50", "AllowUsers brief", "PermitRootLogin no",
        "PasswordAuthentication no", "KbdInteractiveAuthentication no", "AuthenticationMethods publickey",
        "PermitEmptyPasswords no", "UsePAM no", "AllowTcpForwarding no", "AllowAgentForwarding no",
        "X11Forwarding no", "PermitTunnel no", "StrictModes yes", "PermitUserEnvironment no", "MaxAuthTries 3",
    ):  # fmt: skip
        assert required in lines, required
    assert (
        "Subsystem" not in text
        and "PasswordAuthentication yes" not in text
        and "PermitRootLogin yes" not in text
    )


def test_render_writes_a_root_owned_copy_of_the_keys_and_the_config(tmp_path, monkeypatch):
    write_keys(tmp_path, f"{ED}\n# comment\n")
    made = []

    def fake_keygen(cmd, **kwargs):
        Path(cmd[-1]).write_text("KEY")
        made.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_keygen)
    run_dir, host_key = tmp_path / "run", tmp_path / "state" / "ssh" / "ssh_host_ed25519_key"
    info = sshcfg.render(env_for(tmp_path, SSH_LISTEN="198.51.100.50"), run_dir=run_dir, host_key=host_key)
    assert info["keys"] == 1 and (run_dir / "authorized_keys").read_text() == ED + "\n"
    assert "ListenAddress 198.51.100.50" in (run_dir / "sshd_config").read_text()
    assert made and "ed25519" in made[0] and host_key.exists()
    made.clear()
    sshcfg.render(env_for(tmp_path, SSH_LISTEN="198.51.100.50"), run_dir=run_dir, host_key=host_key)
    assert made == []  # the host key is made once, so clients never see it change


def test_a_host_key_that_cannot_be_made_stops_the_server(tmp_path, monkeypatch):
    write_keys(tmp_path, ED + "\n")
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 1, "", "no ssh-keygen")
    )
    with pytest.raises(sshcfg.SshConfigError, match="host key"):
        sshcfg.render(
            env_for(tmp_path, SSH_LISTEN="198.51.100.50"),
            run_dir=tmp_path / "r",
            host_key=tmp_path / "k" / "key",
        )


def test_main_logs_and_exits_1_on_a_bad_setup(tmp_path, monkeypatch, capsys):
    for key, value in env_for(tmp_path).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("SSH_LISTEN", "198.51.100.50")
    assert sshcfg.main() == 1
    assert "sshd_not_started" in capsys.readouterr().out


def test_login_shells_get_folder_settings_never_secrets():
    text = sshcfg.profile_text(
        {
            "BRIEF_STATE": "/state",
            "BRIEF_OUT": "/out",
            "APPRISE_URLS": "ntfy://token@x",
            "MQTT_PASSWORD": "pw",
            "BRIEF_X": "y",
        }
    )
    assert (
        'export BRIEF_STATE="/state"' in text
        and "APPRISE" not in text
        and "MQTT" not in text
        and "BRIEF_X" not in text
    )
    assert sshcfg.profile_text({}) == ""
