"""The deploy files stay valid and consistent with the program (Task 17)."""

import re
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"
SOURCE = ROOT / "brief" / "src" / "brief"
MODES = ("own-ip", "host", "bridge")
# Template variables that are read by the program (and not by Docker or the shell).
ENV_NAMES = set(
    re.findall(r'"([A-Z][A-Z_]{3,})"', "".join(p.read_text(encoding="utf-8") for p in SOURCE.glob("*.py")))
)


def template():
    return ET.parse(DEPLOY / "unraid-template.xml").getroot()


def configs(kind):
    return [c for c in template().iter("Config") if c.get("Type") == kind]


def test_template_follows_stable_or_a_pin_and_has_no_latest():
    repo = template().findtext("Repository")
    assert repo and ":" in repo and not repo.endswith(":latest")
    assert repo.endswith(":stable"), "the template follows the stable tag so Unraid can offer updates"
    assert template().findtext("Icon"), "the template carries an icon"


def test_template_runs_on_br0_with_the_web_page_and_memory_limit():
    root = template()
    assert root.findtext("Network") == "br0"
    assert (
        root.findtext("WebUI") == "http://[IP]:[PORT:80]/"
    )  # the public status page, which links to the sign-in
    assert "--memory=768m" in root.findtext("ExtraParams")


def test_template_variables_are_ones_the_program_reads():
    names = [c.get("Target") for c in configs("Variable")]
    assert names, "no variables"
    unknown = [n for n in names if n not in ENV_NAMES]
    assert not unknown, f"the template sets variables nothing reads: {unknown}"


def test_template_secrets_are_masked():
    for c in configs("Variable"):
        if any(word in c.get("Target") for word in ("PASSWORD", "URLS", "TOKEN", "KEY")):
            assert c.get("Mask") == "true", c.get("Target")


def test_template_paths_are_the_ones_the_image_uses():
    targets = {c.get("Target") for c in configs("Path")}
    assert {"/config", "/state", "/archive", "/out"} <= targets
    state = next(c for c in configs("Path") if c.get("Target") == "/state")
    assert "/mnt/user/" not in state.text  # SQLite must not live on the user share


def test_template_ports_match_the_image():
    published = {(c.get("Target"), c.get("Mode")) for c in configs("Port")}
    assert {("514", "udp"), ("514", "tcp"), ("80", "tcp"), ("443", "tcp")} <= published


def test_compose_references_exist_for_all_three_modes_and_pin_the_image():
    for mode in MODES:
        data = yaml.safe_load((DEPLOY / f"docker-compose.{mode}.yml").read_text(encoding="utf-8"))
        service = data["services"]["homelab-brief"]
        assert service["image"].startswith("ghcr.io/") and not service["image"].endswith(":latest"), mode
        assert service["mem_limit"] == "768m", mode
        targets = {v.split(":")[1] for v in service["volumes"]}
        assert {"/config", "/state", "/archive", "/out"} <= targets, mode
        env_file = service.get("env_file")
        assert env_file and "homelab-brief.env" in env_file, mode


def test_compose_network_settings_differ_the_way_the_modes_say():
    own = yaml.safe_load((DEPLOY / "docker-compose.own-ip.yml").read_text(encoding="utf-8"))["services"][
        "homelab-brief"
    ]
    host = yaml.safe_load((DEPLOY / "docker-compose.host.yml").read_text(encoding="utf-8"))["services"][
        "homelab-brief"
    ]
    bridge = yaml.safe_load((DEPLOY / "docker-compose.bridge.yml").read_text(encoding="utf-8"))["services"][
        "homelab-brief"
    ]
    assert "br0" in own["networks"] and "ports" not in own
    assert host["network_mode"] == "host" and "ports" not in host
    assert {"SYSLOG_PORT", "HTTP_PORT", "HTTPS_PORT"} <= set(host["environment"])  # away from Unraid's own
    assert "ports" in bridge and any(p.endswith("/udp") for p in bridge["ports"])
    assert "HTTPS_PUBLIC_PORT" in bridge["environment"]


def test_release_workflow_never_pushes_latest_but_moves_stable():
    text = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    commands = [ln for ln in text.splitlines() if re.search(r"docker (tag|push|build)", ln)]
    assert commands and not any("latest" in ln for ln in commands)
    assert any(":stable" in ln and "docker push" in ln for ln in commands)
    assert "uses: ./.github/workflows/checks.yml" in text


def test_changelog_has_an_entry_for_the_program_version():
    version = re.search(r'__version__ = "(.+)"', (SOURCE / "__init__.py").read_text(encoding="utf-8")).group(
        1
    )
    assert re.search(
        rf"^## {re.escape(version)}$", (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), re.MULTILINE
    )


def test_the_quickstart_compose_is_self_contained_and_consistent():
    text = (DEPLOY / "docker-compose.quickstart.yml").read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    service = data["services"]["homelab-brief"]
    assert (
        service["image"].startswith("${HB_IMAGE:-ghcr.io/herbiewalker/homelab-brief:")
        and "latest" not in service["image"]
    )
    assert service["mem_limit"] == "768m" and service["restart"] == "unless-stopped"
    targets = {v.rsplit(":", 1)[1] for v in service["volumes"]}  # the source has its own ":-" default
    assert targets == {"/config", "/state", "/archive", "/out"}
    ports = service["ports"]
    assert any(p.endswith("514/udp") for p in ports) and any(p.endswith("514/tcp") for p in ports)
    # the port a browser uses must be the one that is published, or the setup link and certificate are wrong
    https = next(p for p in ports if p.endswith(":443"))
    assert "HB_HTTPS_PORT" in https and "HB_HTTPS_PORT" in service["environment"]["HTTPS_PUBLIC_PORT"]
    for var in ("HB_SYSLOG_PORT", "HB_HTTP_PORT", "HB_HTTPS_PORT", "HB_DATA", "HB_IMAGE"):
        assert f"#   {var}" in text, f"{var} is not described in the file header"
    assert "env_file" not in service, "the quick start must work with no file other than itself"
    labels = service["labels"]
    assert labels["net.unraid.docker.webui"] == "http://[IP]:[PORT:80]/"
    assert labels["net.unraid.docker.icon"].startswith("https://raw.githubusercontent.com/")
    # every variable it passes on is one the program reads
    assert {"MQTT_HOST", "MQTT_PORT", "MQTT_USER", "MQTT_PASSWORD", "APPRISE_URLS"} <= ENV_NAMES
