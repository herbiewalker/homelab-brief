"""The optional SSH server: its settings, the keys it will accept, and the files it starts from.

Off unless the template variable SSH_ENABLED is true. When it is on:

- key login only (no passwords of any kind), no root login, one allowed user (`brief`), no forwarding,
  no sftp, and a login lands in a shell where the `brief` command works;
- the keys come from `ssh/authorized_keys` in the config folder, but the server reads a validated copy
  under /run/sshd that root owns (sshd refuses keys from a folder other users can write, which is how
  appdata folders usually are). A key line may carry no options (no `command=`, no `permitopen=`), only
  a key type and the key, so nothing can be smuggled in. Change keys, then restart the container;
- it listens on the container's own address in own-IP mode, on every container address in bridge mode
  (Docker maps the port), and in host mode only on the advertised address, because host mode would
  otherwise open SSH on every address of the Unraid host;
- the host key is made once and kept in /state/ssh, so clients do not see it change after an update.

`python -m brief.sshcfg` renders the files (it is run by the sshd service) and exits 1 with a message in
the log when something is wrong, so a bad setting never starts a weaker server.
"""

import os
import re
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

from brief import netinfo
from brief.config import ConfigError, config_file, load_mapping
from brief.jsonlog import log

RUN_DIR = Path("/run/sshd")
HOST_KEY = Path("/state/ssh/ssh_host_ed25519_key")
PROFILE = Path("/etc/profile.d/homelab-brief.sh")
USER = "brief"
KEY_LINE = re.compile(
    r"^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(?:256|384|521)|sk-ssh-ed25519@openssh\.com|"
    r"sk-ecdsa-sha2-nistp256@openssh\.com)\s+[A-Za-z0-9+/]+={0,3}(?:\s+[^\r\n]{0,200})?$"
)
PROFILE_VARS = (
    "BRIEF_STATE", "BRIEF_OUT", "BRIEF_ARCHIVE", "BRIEF_CONFIG_DIR", "BRIEF_DEFAULTS_DIR",
    "SSH_ENABLED", "SSH_PORT", "SSH_LISTEN", "HTTP_PORT", "HTTPS_PORT", "HTTPS_PUBLIC_PORT", "SYSLOG_PORT",
)  # fmt: skip


class SshConfigError(Exception):
    """Something that stops the SSH server from starting; the message says what to change."""


def enabled(env: Mapping[str, str]) -> bool:
    return (env.get("SSH_ENABLED") or "").strip().lower() in ("true", "1", "yes")


def keys_file(env: Mapping[str, str]) -> Path:
    return Path(env.get("BRIEF_CONFIG_DIR", "/config")) / "ssh" / "authorized_keys"


def valid_keys(text: str) -> tuple[list[str], list[str]]:
    """(accepted key lines, problems). Comments and blank lines are ignored; a line with options, or one
    that is not a public key, is refused and named by its number (never printed, to keep logs short)."""
    keys, problems = [], []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if KEY_LINE.match(line):
            keys.append(line)
        else:
            problems.append(
                f"line {number} is not a plain public key "
                "(a key type and the key, no options such as command=)"
            )
    return keys, problems


def port_for(env: Mapping[str, str]) -> int:
    raw = (env.get("SSH_PORT") or "").strip()
    if not raw:
        try:
            raw = str((load_mapping(config_file("network.yaml", env)).get("ports") or {}).get("ssh", 22))
        except (ConfigError, OSError):
            raw = "22"
    try:
        port = int(raw)
    except ValueError:
        raise SshConfigError(f"The SSH port must be a number, not {raw!r}.") from None
    if not 1 <= port <= 65535:
        raise SshConfigError(f"The SSH port {port} is out of range.")
    return port


def listen_address(env: Mapping[str, str], net: dict | None = None) -> str:
    """The address sshd binds, or SshConfigError when it would have to guess."""
    override = (env.get("SSH_LISTEN") or "").strip()
    if override:
        return override
    from brief.settings import Settings

    net = net or netinfo.network_info(Settings.from_env(env))
    if net["mode"] == "own_ip":
        if not net["address"]:
            raise SshConfigError("No container address was found to listen on (set SSH_LISTEN).")
        return net["address"]
    if net["mode"] == "bridge":
        return "0.0.0.0"  # inside the container; Docker's port mapping decides who can reach it
    advertised = (net.get("advertised") or "").strip()
    if not advertised or not _is_ip(advertised):
        raise SshConfigError(
            "Host mode shares every address of the Unraid host, so SSH needs one to listen on: set "
            "advertised_address in network.yaml to the host's IP address, or SSH_LISTEN."
        )
    return advertised


def _is_ip(text: str) -> bool:
    import ipaddress

    try:
        ipaddress.ip_address(text)
        return True
    except ValueError:
        return False


def describe(env: Mapping[str, str] | None = None) -> dict:
    """What `brief ssh show` reports: on or off, where it listens, how many keys, and what is wrong."""
    env = os.environ if env is None else env
    problems: list[str] = []
    on = enabled(env)
    listen, port = None, 22
    try:
        port = port_for(env)
        if on:
            listen = listen_address(env)
    except SshConfigError as exc:
        problems.append(str(exc))
    path = keys_file(env)
    keys: list[str] = []
    if path.is_file():
        try:
            keys, bad = valid_keys(path.read_text(encoding="utf-8"))
            problems += [f"authorized_keys: {p}" for p in bad]
        except (OSError, UnicodeDecodeError):
            problems.append(f"{path} cannot be read")
    elif on:
        problems.append(f"{path} does not exist: add your public key there")
    if on and path.is_file() and not keys:
        problems.append("authorized_keys holds no usable key, so nobody could log in")
    return {
        "enabled": on,
        "listen": listen,
        "port": port,
        "keys": len(keys),
        "keys_file": str(path),
        "problems": problems,
    }


def sshd_config(listen: str, port: int, host_key: Path, authorized: Path) -> str:
    return "\n".join(
        [
            "# Written by brief.sshcfg at start. Do not edit: change the template or network.yaml.",
            f"Port {port}",
            f"ListenAddress {listen}",
            f"HostKey {host_key}",
            "PidFile /run/sshd/sshd.pid",
            f"AllowUsers {USER}",
            "PermitRootLogin no",
            "PubkeyAuthentication yes",
            "PasswordAuthentication no",
            "KbdInteractiveAuthentication no",
            "AuthenticationMethods publickey",
            "PermitEmptyPasswords no",
            "UsePAM no",
            f"AuthorizedKeysFile {authorized}",
            "StrictModes yes",
            "MaxAuthTries 3",
            "LoginGraceTime 20",
            "MaxSessions 4",
            "ClientAliveInterval 60",
            "ClientAliveCountMax 3",
            "AllowTcpForwarding no",
            "AllowAgentForwarding no",
            "AllowStreamLocalForwarding no",
            "X11Forwarding no",
            "PermitTunnel no",
            "GatewayPorts no",
            "PermitUserEnvironment no",
            "PrintMotd no",
            "PrintLastLog no",
            "LogLevel VERBOSE",
            "",
        ]
    )


def profile_text(env: Mapping[str, str]) -> str:
    """What login shells need from the container's environment: the folder overrides, never a secret."""
    lines = [
        f'export {name}="{env[name]}"' for name in PROFILE_VARS if env.get(name) and '"' not in env[name]
    ]
    return ("# Written by brief.sshcfg.\n" + "\n".join(lines) + "\n") if lines else ""


def render(env: Mapping[str, str] | None = None, run_dir: Path = RUN_DIR, host_key: Path = HOST_KEY) -> dict:
    """Write the config, the validated authorized_keys, and the login profile. Raises SshConfigError."""
    env = os.environ if env is None else env
    info = describe(env)
    if info["problems"]:
        raise SshConfigError("; ".join(info["problems"]))
    keys, _ = valid_keys(keys_file(env).read_text(encoding="utf-8"))
    run_dir.mkdir(parents=True, exist_ok=True)
    authorized = run_dir / "authorized_keys"
    authorized.write_text("\n".join(keys) + "\n", encoding="utf-8")
    authorized.chmod(0o644)  # root owns it; sshd needs it readable by the user it logs in
    if not host_key.exists():
        host_key.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        result = subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "homelab-brief", "-f", str(host_key)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise SshConfigError(f"Could not make the host key: {result.stderr.strip()[:200]}")
    (run_dir / "sshd_config").write_text(
        sshd_config(info["listen"], info["port"], host_key, authorized), encoding="utf-8"
    )
    text = profile_text(env)
    if text and PROFILE.parent.is_dir():
        PROFILE.write_text(text, encoding="utf-8")
    return info


def main() -> int:
    env = os.environ
    if not enabled(env):
        return 0
    try:
        info = render(env)
    except SshConfigError as exc:
        log("sshd_not_started", reason=str(exc))
        return 1
    log("sshd_config_ready", listen=info["listen"], port=info["port"], keys=info["keys"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
