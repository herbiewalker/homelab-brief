"""The admin GUI's TLS certificate.

A certificate you supply (cert.pem and key.pem in /config/tls) is always used as it is. Otherwise a
self-signed one is generated on first start, names the address devices use, and is regenerated when
those names change or it is about to expire. The key never leaves the state folder.
"""

import ipaddress
import os
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

VALID_DAYS = 730
RENEW_BEFORE_DAYS = 30


def wanted_names(advertised: str = "", extra: tuple[str, ...] = (), hostname: str | None = None) -> list[str]:
    """The names the certificate should cover: the advertised address, extras, this host, and localhost."""
    names: list[str] = []
    for name in (
        advertised,
        *extra,
        hostname if hostname is not None else socket.gethostname(),
        "localhost",
        "127.0.0.1",
    ):
        name = (name or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def _san(names: list[str]) -> x509.SubjectAlternativeName:
    entries: list[x509.GeneralName] = []
    for name in names:
        try:
            entries.append(x509.IPAddress(ipaddress.ip_address(name)))
        except ValueError:
            entries.append(x509.DNSName(name))
    return x509.SubjectAlternativeName(entries)


def certificate_names(cert_path: Path) -> set[str] | None:
    """The names an existing certificate covers, or None if it cannot be read."""
    try:
        cert = x509.load_pem_x509_certificate(Path(cert_path).read_bytes())
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except (OSError, ValueError, x509.ExtensionNotFound):
        return None
    return {str(v) for v in san.get_values_for_type(x509.DNSName)} | {
        str(v) for v in san.get_values_for_type(x509.IPAddress)
    }


def _expires_soon(cert_path: Path, now: datetime) -> bool:
    cert = x509.load_pem_x509_certificate(Path(cert_path).read_bytes())
    return cert.not_valid_after_utc - now < timedelta(days=RENEW_BEFORE_DAYS)


def _write(path: Path, data: bytes, mode: int) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def ensure_certificate(
    cert_path: Path, key_path: Path, names: list[str], now: datetime | None = None
) -> bool:
    """Make sure a self-signed certificate for these names exists. Returns True if one was generated."""
    cert_path, key_path = Path(cert_path), Path(key_path)
    now = now or datetime.now(UTC)
    if cert_path.exists() and key_path.exists():
        have = certificate_names(cert_path)
        if have is not None and have == set(names) and not _expires_soon(cert_path, now):
            return False

    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "homelab-brief")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(hours=1))
        .not_valid_after(now + timedelta(days=VALID_DAYS))
        .add_extension(_san(names), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(True, False, False, False, False, False, False, False, False), critical=True
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    _write(
        key_path,
        key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ),
        0o600,
    )
    _write(cert_path, cert.public_bytes(serialization.Encoding.PEM), 0o644)
    return True


def resolve_certificate(settings, env=None) -> tuple[Path, Path, bool]:
    """(cert, key, generated): the user's pair in /config/tls if present, else the self-signed one."""
    env = os.environ if env is None else env
    user = Path(env.get("BRIEF_CONFIG_DIR", "/config")) / "tls"
    if (user / "cert.pem").exists() and (user / "key.pem").exists():
        return user / "cert.pem", user / "key.pem", False
    state = Path(settings.state_dir) / "tls"
    cert, key = state / "self-signed.pem", state / "self-signed.key"
    generated = ensure_certificate(cert, key, wanted_names(settings.advertised, settings.extra_names))
    return cert, key, generated
