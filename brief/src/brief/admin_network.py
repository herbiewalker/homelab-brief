"""The Network page: the detected mode and its warnings, what is listening, the settings that can be
changed (through the config editor's rules), the live syslog test, repointing status, and the template
settings for each mode. Routes only; the data is in netpage.py and the file rules in netconfig.py.
"""

import re
import secrets
from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from brief import configedit, netconfig, netinfo, netpage
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
        try:
            text, _ = configedit.current("network.yaml")
        except configedit.EditError:
            text = ""
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
            base=configedit.digest(text),
            modes=("auto", *netinfo.MODES),
            **extra,
        )

    @admin.get("/network", response_class=HTMLResponse)
    def network(request: Request, done: str = ""):
        return page(request, notice=DONE.get(done))

    @admin.post("/network")
    async def network_save(request: Request):
        session = d.session(request)
        form = await request.form()
        base = str(form.get("base", ""))
        try:
            changes = {
                "mode": str(form.get("mode", "auto")).strip(),
                "advertised_address": str(form.get("advertised_address", "")).strip(),
                "unknown_senders": str(form.get("unknown_senders", "flag")).strip(),
                "ports": {
                    key: _port(form.get(key))
                    for key in netconfig.PORT_KEYS
                    if form.get(key) not in (None, "")
                },
            }
            text, _ = configedit.current("network.yaml")
            patched = netconfig.patch(text, changes)
            configedit.save(d.store, "network.yaml", patched, session.username, base, d.settings.state_dir)
        except (configedit.EditError, ConfigError, ValueError) as exc:
            status = getattr(exc, "status", 422)
            return page(request, status, error=str(exc))
        return RedirectResponse("/admin/network?done=saved", status_code=303)

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


def _port(value) -> int:
    text = str(value).strip()
    if not text.isdigit():
        raise ValueError(f"A port must be a number, not {text[:20]!r}.")
    return int(text)
