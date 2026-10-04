from pathlib import Path

import pytest

from brief import vectorcfg as v

TEMPLATE = """\
sources:
  syslog_udp:
    address: "0.0.0.0:514"
  syslog_tcp:
    address: "0.0.0.0:514"
transforms:
  gate:
    source: |
      # BEGIN rendered:senders
      allowed_senders = []
      unknown_policy = "flag"
      # END rendered:senders
      if length(allowed_senders) > 0 { .x = 1 }
"""
REAL_TEMPLATE = Path(__file__).resolve().parents[2] / "vector" / "vector.yaml"


def test_defaults_render_port_514_and_an_empty_allowlist():
    out = v.render(TEMPLATE, 514, [], "flag")
    assert out.count('address: "0.0.0.0:514"') == 2
    assert "allowed_senders = []" in out and 'unknown_policy = "flag"' in out


def test_port_and_allowlist_are_rendered_with_the_original_indentation():
    out = v.render(TEMPLATE, 5514, ["198.51.100.0/24", "203.0.113.9/32"], "drop")
    assert out.count('address: "0.0.0.0:5514"') == 2
    assert '      allowed_senders = ["198.51.100.0/24", "203.0.113.9/32"]' in out
    assert '      unknown_policy = "drop"' in out
    assert out.count("# BEGIN rendered:senders") == 1 and out.count("# END rendered:senders") == 1
    assert "if length(allowed_senders) > 0" in out


def test_bare_addresses_become_host_networks_and_ipv6_is_accepted():
    cidrs = v.allowed_cidrs({"allowed_senders": ["203.0.113.9", " 198.51.100.0/24 ", "2001:db8::1"]})
    assert cidrs == ["203.0.113.9/32", "198.51.100.0/24", "2001:db8::1/128"]


@pytest.mark.parametrize("bad", ["<management-subnet>/24", "not-an-ip", "300.1.1.1", "198.51.100.0/99"])
def test_a_bad_allowlist_entry_is_an_error_not_a_silent_skip(bad):
    with pytest.raises(v.ConfigError):
        v.allowed_cidrs({"allowed_senders": [bad]})


def test_allowlist_must_be_a_list():
    with pytest.raises(v.ConfigError):
        v.allowed_cidrs({"allowed_senders": "198.51.100.0/24"})


def test_unknown_policy_defaults_to_flag_and_rejects_other_values():
    assert v.unknown_policy({}) == "flag"
    assert v.unknown_policy({"unknown_senders": "DROP"}) == "drop"
    with pytest.raises(v.ConfigError):
        v.unknown_policy({"unknown_senders": "ignore"})


def test_port_precedence_is_env_then_network_yaml_then_514():
    assert v.syslog_port({}, {}) == 514
    assert v.syslog_port({"ports": {"syslog": 5140}}, {}) == 5140
    assert v.syslog_port({"ports": {"syslog": 5140}}, {"SYSLOG_PORT": "5514"}) == 5514
    assert v.syslog_port({"ports": {"syslog": 5140}}, {"SYSLOG_PORT": ""}) == 5140


@pytest.mark.parametrize("bad", ["0", "70000", "abc", "-1"])
def test_a_bad_port_is_an_error(bad):
    with pytest.raises(v.ConfigError):
        v.syslog_port({}, {"SYSLOG_PORT": bad})


def test_a_template_without_the_senders_block_is_refused():
    with pytest.raises(v.ConfigError, match="rendered:senders"):
        v.render(TEMPLATE.replace("# BEGIN rendered:senders", "# nothing"), 514, [], "flag")


def test_a_template_without_both_address_lines_is_refused():
    with pytest.raises(v.ConfigError, match="address"):
        v.render(TEMPLATE.replace('address: "0.0.0.0:514"', "address: x", 1), 514, [], "flag")


def test_main_renders_a_file_from_config_and_env(tmp_path):
    (tmp_path / "vector").mkdir()
    (tmp_path / "brief").mkdir()
    (tmp_path / "vector" / "vector.yaml").write_text(TEMPLATE, encoding="utf-8")
    (tmp_path / "brief" / "network.yaml").write_text(
        "ports: { syslog: 5140 }\nallowed_senders: [198.51.100.0/24]\nunknown_senders: drop\n",
        encoding="utf-8",
    )
    out = tmp_path / "run" / "vector.yaml"
    env = {
        "BRIEF_CONFIG_DIR": str(tmp_path),
        "BRIEF_VECTOR_DIR": str(tmp_path / "vector"),
        "VECTOR_RENDERED": str(out),
        "SYSLOG_PORT": "5514",
    }
    assert v.main(env=env) == 0
    text = out.read_text(encoding="utf-8")
    assert "0.0.0.0:5514" in text and '["198.51.100.0/24"]' in text and '"drop"' in text


def test_main_fails_loudly_on_a_bad_config(tmp_path, capsys):
    (tmp_path / "vector").mkdir()
    (tmp_path / "brief").mkdir()
    (tmp_path / "vector" / "vector.yaml").write_text(TEMPLATE, encoding="utf-8")
    (tmp_path / "brief" / "network.yaml").write_text("allowed_senders: [oops]\n", encoding="utf-8")
    env = {
        "BRIEF_CONFIG_DIR": str(tmp_path),
        "BRIEF_VECTOR_DIR": str(tmp_path / "vector"),
        "VECTOR_RENDERED": str(tmp_path / "out.yaml"),
    }
    assert v.main(env=env) == 1
    assert "oops" in capsys.readouterr().err
    assert not (tmp_path / "out.yaml").exists()


def test_main_works_without_a_network_yaml(tmp_path):
    (tmp_path / "vector").mkdir()
    (tmp_path / "vector" / "vector.yaml").write_text(TEMPLATE, encoding="utf-8")
    env = {
        "BRIEF_CONFIG_DIR": str(tmp_path),
        "BRIEF_VECTOR_DIR": str(tmp_path / "vector"),
        "VECTOR_RENDERED": str(tmp_path / "out.yaml"),
    }
    assert v.main(env=env) == 0
    assert "0.0.0.0:514" in (tmp_path / "out.yaml").read_text(encoding="utf-8")


@pytest.mark.skipif(not REAL_TEMPLATE.exists(), reason="vector/vector.yaml is not in this build context")
def test_the_real_template_renders():
    out = v.render(REAL_TEMPLATE.read_text(encoding="utf-8"), 5514, ["198.51.100.0/24"], "flag")
    assert out.count("0.0.0.0:5514") == 2 and '["198.51.100.0/24"]' in out
