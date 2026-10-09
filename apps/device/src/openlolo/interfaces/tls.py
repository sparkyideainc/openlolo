"""Self-signed certificate for the LAN MCP entry (ADR 0005).

The certificate is generated once at install (or on first simulator run) with the system
``openssl`` binary, so the API virtualenv needs no cryptography dependency. The SHA-256
fingerprint of the DER-encoded leaf is what a LAN client pins (``/app/info`` reports it);
anything else about the certificate (chain, hostname, expiry) is irrelevant to that check.
"""

import hashlib
import os
import ssl
import subprocess
from pathlib import Path


def fingerprint(cert_path: Path) -> str:
    """Lower-case hex SHA-256 over the DER encoding of the first certificate in a PEM file."""
    pem = Path(cert_path).read_text()
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()


def generate(cert_path: Path, key_path: Path, name: str, ips: tuple[str, ...] = ()) -> None:
    """Create an EC P-256 self-signed certificate valid ten years; key mode 0600, cert 0644."""
    cert_path, key_path = Path(cert_path), Path(key_path)
    cert_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    san = ",".join([f"DNS:{name}", *(f"IP:{ip}" for ip in ips)])  # IPs only when lan_extra_hosts needs them.
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "ec",
            "-pkeyopt",
            "ec_paramgen_curve:prime256v1",
            "-nodes",
            "-days",
            "3650",
            "-subj",
            f"/CN={name}",
            "-addext",
            f"subjectAltName={san}",
            "-keyout",
            str(key_path),
            "-out",
            str(cert_path),
        ],
        check=True,
        capture_output=True,
    )
    os.chmod(key_path, 0o600)
    os.chmod(cert_path, 0o644)


def ensure(cert_path: Path, key_path: Path, name: str, ips: tuple[str, ...] = ()) -> str:
    """Generate the pair when either file is missing; return the certificate fingerprint."""
    if not (Path(cert_path).exists() and Path(key_path).exists()):
        generate(cert_path, key_path, name, ips)
    return fingerprint(cert_path)
