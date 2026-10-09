import hashlib
import os
import shutil
import ssl
import subprocess
from pathlib import Path

from openlolo.interfaces import tls

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "tls" / "openlolo-test.crt"


def test_fingerprint_is_sha256_over_der():
    pem = FIXTURE.read_text()
    expected = hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()
    assert tls.fingerprint(FIXTURE) == expected
    assert len(expected) == 64 and expected == expected.lower()
    if shutil.which("openssl"):
        result = subprocess.run(
            ["openssl", "x509", "-in", str(FIXTURE), "-noout", "-fingerprint", "-sha256"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:  # LibreSSL and OpenSSL both print "SHA256 Fingerprint=AA:BB:..."
            printed = result.stdout.strip().split("=", 1)[1].replace(":", "").lower()
            assert printed == expected


def test_generate_modes_and_ensure_idempotent(tmp_path):
    cert, key = tmp_path / "tls" / "lan.crt", tmp_path / "tls" / "lan.key"
    tls.generate(cert, key, "openlolo-ab12.local", ("10.42.0.1", "127.0.0.1"))
    assert os.stat(key).st_mode & 0o777 == 0o600
    assert os.stat(cert).st_mode & 0o777 == 0o644
    assert cert.read_text().startswith("-----BEGIN CERTIFICATE-----")
    first = tls.fingerprint(cert)
    assert tls.ensure(cert, key, "openlolo-ab12.local") == first
    assert tls.fingerprint(cert) == first  # ensure() did not regenerate an existing pair.
    key.unlink()
    assert tls.ensure(cert, key, "openlolo-ab12.local") != first  # A missing half triggers a new pair.
    assert key.exists()
