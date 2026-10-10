"""The Network page: the detected mode and its warnings, what is listening, the settings that can be
changed (through the config editor's rules), the live syslog test, repointing status, and the template
settings for each mode. Routes only; the data is in netpage.py and the file rules in netconfig.py.
"""

import re
import secrets
from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from brief import admin_settings, netinfo, netpage
from brief.admin import Deps
from brief.config import ConfigError
from brief.systems import Systems

TOKEN = re.compile(r"^[a-f0-9]{6,32}$")
DONE = {"saved": "Saved. Ports and the advertised address apply after the container restarts."}


def register(admin: APIRouter, d: Deps) -> None:
    def load_systems() -> Systems:
        from brief.config import config_file

        try:
            return Systems.load(config_file("systems.yaml"))
        except (ConfigError, OSError):
            return Systems()

    def running_ports() -> dict[str, int]:
        s = d.settings
        return {"syslog": s.syslog_port, "http": s.http_port, "https": s.https_port}

    def page(request: Request, status: int = 200, **extra) -> HTMLResponse:
        now = datetime.now(UTC)
        net = netinfo.network_info(d.settings)
        want = netpage.configured()
        ports = running_ports()
        warnings = [
            *net["warnings"],
            *netpage.port_warnings(net["mode"], ports),
        ]
        lost = netpage.lost_sender_warning(d.store, now)
        if lost:
            warnings.append(lost)
        token = secrets.token_hex(4)
        target = net["send_syslog_to"]
        return d.render(
            request,
            "admin_network.html",
            status,
            csrf=d.session(request).csrf,
            active="network",
            net=net,
            warnings=warnings,
            notes=netpage.notes(net["mode"]),
            listening=netpage.listening(ports),
            pending=netpage.pending_restart(ports, d.settings.advertised, want),
            want=want,
            ports=ports,
            repointing=netpage.repointing(load_systems(), d.store, now),
            templates=netpage.template_settings(net["mode"], target or net["address"], ports),
            token=token,
            commands=netpage.test_commands(target, ports["syslog"], token),
            cards=admin_settings.cards_html(d, request, ("network", "network-ports")),
            **extra,
        )

    @admin.get("/network", response_class=HTMLResponse)
    def network(request: Request, done: str = ""):
        return page(request, notice=DONE.get(done))

    @admin.get("/network/test/{token}", response_class=HTMLResponse)
    def network_test(request: Request, token: str):
        if not TOKEN.match(token):
            return Response("Not a test token.", status_code=404)
        found = netpage.find_test_line(d.settings.state_dir, f"homelab-brief-test-{token}")
        net = netinfo.network_info(d.settings)
        sender_note = None
        if found and netinfo.in_docker_range(found["source_ip"]) and found["source_ip"].endswith(".1"):
            sender_note = (
                f"The line arrived from {found['source_ip']}, a Docker gateway address, not the real sender: "
                "the bridge is hiding sender addresses."
            )
        return d.render(
            request,
            "_net_test.html",
            csrf=d.session(request).csrf,
            token=token,
            found=found,
            sender_note=sender_note,
            mode=net["mode"],
        )
