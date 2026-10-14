"""Certificate pinning for the box's LAN entry, which serves a self-signed certificate.

The pin is the SHA-256 of the DER-encoded leaf certificate, the same value the box reports in
``/app/info`` and on its page. The check runs inside the TLS handshake, before httpx writes a
single byte of the request, so a bearer token is never sent to an unpinned peer.
"""

import hashlib
import re
import ssl

from openlolo_sdk.errors import PinMismatch


def normalize(fingerprint: str) -> str:
    value = re.sub(r"[^0-9a-f]", "", fingerprint.lower())
    if len(value) != 64:
        raise ValueError("fingerprint must be 64 hex digits (SHA-256), colons allowed")
    return value


class Pin:
    """Expected fingerprint, or trust-on-first-use: the first handshake of this session decides."""

    def __init__(self, expected: str | None = None):
        self.expected = normalize(expected) if expected else None
        self.seen: str | None = None

    def check(self, der: bytes | None) -> None:
        if der is None:
            raise PinMismatch("PIN_MISMATCH", "the box presented no certificate")
        actual = hashlib.sha256(der).hexdigest()
        wanted = self.expected or self.seen
        if wanted is not None and actual != wanted:
            raise PinMismatch(
                "PIN_MISMATCH",
                f"the box certificate is {actual[:16]}…, expected {wanted[:16]}…; "
                "if the box was reset, delete its entry in the SDK cache and approve again",
            )
        self.seen = actual


def pinned_context(pin: Pin) -> ssl.SSLContext:
    """TLS client context that trusts exactly the pinned leaf (hostname and chain are moot)."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE

    class PinnedSocket(ssl.SSLSocket):
        def do_handshake(self, block: bool = False) -> None:
            super().do_handshake(block)
            try:
                pin.check(self.getpeercert(binary_form=True))
            except PinMismatch:
                self.close()
                raise

    context.sslsocket_class = PinnedSocket
    return context
