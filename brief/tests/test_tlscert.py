import ssl
import stat
from datetime import UTC, datetime, timedelta

from cryptography import x509

from brief import tlscert as t
from brief.settings import Settings

NAMES = ["nas.example", "198.51.100.20", "localhost", "127.0.0.1"]


def paths(tmp_path):
    return tmp_path / "tls" / "cert.pem", tmp_path / "tls" / "key.pem"


def test_a_certificate_is_generated_with_dns_and_ip_names(tmp_path):
    cert_path, key_path = paths(tmp_path)
    assert t.ensure_certificate(cert_path, key_path, NAMES) is True
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert "nas.example" in san.get_values_for_type(x509.DNSName)
    assert {str(ip) for ip in san.get_values_for_type(x509.IPAddress)} == {"198.51.100.20", "127.0.0.1"}
    assert cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is False
    assert t.certificate_names(cert_path) == set(NAMES)


def test_the_private_key_is_readable_only_by_its_owner(tmp_path):
    cert_path, key_path = paths(tmp_path)
    t.ensure_certificate(cert_path, key_path, NAMES)
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    assert not list(cert_path.parent.glob("*.tmp"))


def test_the_pair_works_for_a_real_tls_server(tmp_path):
    cert_path, key_path = paths(tmp_path)
    t.ensure_certificate(cert_path, key_path, NAMES)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)  # raises if the key does not match the certificate


def test_an_unchanged_certificate_is_reused(tmp_path):
    cert_path, key_path = paths(tmp_path)
    t.ensure_certificate(cert_path, key_path, NAMES)
    before = cert_path.read_bytes()
    assert t.ensure_certificate(cert_path, key_path, list(reversed(NAMES))) is False
    assert cert_path.read_bytes() == before


def test_it_is_regenerated_when_the_names_change(tmp_path):
    cert_path, key_path = paths(tmp_path)
    t.ensure_certificate(cert_path, key_path, NAMES)
    assert t.ensure_certificate(cert_path, key_path, [*NAMES, "new.example"]) is True
    assert "new.example" in t.certificate_names(cert_path)


def test_it_is_regenerated_before_it_expires(tmp_path):
    cert_path, key_path = paths(tmp_path)
    t.ensure_certificate(cert_path, key_path, NAMES)
    soon = datetime.now(UTC) + timedelta(days=t.VALID_DAYS - 10)
    assert t.ensure_certificate(cert_path, key_path, NAMES, now=soon) is True


def test_an_unreadable_certificate_is_replaced(tmp_path):
    cert_path, key_path = paths(tmp_path)
    t.ensure_certificate(cert_path, key_path, NAMES)
    cert_path.write_text("not a certificate")
    assert t.certificate_names(cert_path) is None
    assert t.ensure_certificate(cert_path, key_path, NAMES) is True


def test_the_wanted_names_include_the_advertised_address_this_host_and_localhost():
    names = t.wanted_names("nas.example", ("alias.example", "nas.example"), hostname="box")
    assert names == ["nas.example", "alias.example", "box", "localhost", "127.0.0.1"]
    assert t.wanted_names("", (), hostname="box") == ["box", "localhost", "127.0.0.1"]


def test_a_certificate_you_supply_is_used_as_it_is_and_never_overwritten(tmp_path):
    user = tmp_path / "config" / "tls"
    user.mkdir(parents=True)
    (user / "cert.pem").write_text("my certificate")
    (user / "key.pem").write_text("my key")
    settings = Settings(state_dir=tmp_path / "state")
    cert, key, generated = t.resolve_certificate(settings, {"BRIEF_CONFIG_DIR": str(tmp_path / "config")})
    assert (cert, key, generated) == (user / "cert.pem", user / "key.pem", False)
    assert (user / "cert.pem").read_text() == "my certificate"
    assert not (tmp_path / "state" / "tls").exists()


def test_without_your_own_certificate_a_self_signed_one_is_made_in_the_state_folder(tmp_path):
    settings = Settings(state_dir=tmp_path / "state", advertised="nas.example")
    cert, key, generated = t.resolve_certificate(settings, {"BRIEF_CONFIG_DIR": str(tmp_path / "none")})
    assert generated and cert.parent == tmp_path / "state" / "tls"
    assert "nas.example" in t.certificate_names(cert)
    _, _, again = t.resolve_certificate(settings, {"BRIEF_CONFIG_DIR": str(tmp_path / "none")})
    assert again is False
