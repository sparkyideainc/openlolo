import ipaddress
import re
import tomllib
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from openlolo.domain.models import StrictModel

# Keys from earlier releases; ignored so an existing /etc/openlolo/box.toml keeps loading.
# setup_ssid named the older SoftAP hotspot; bluetooth_adapter and setup_requires_usb belonged to
# the removed Bluetooth companion-app channel and its label-code pairing gate.
LEGACY_KEYS = {"media_python", "cleanup_deadline", "setup_ssid", "bluetooth_adapter", "setup_requires_usb"}


class Config(StrictModel):
    backend: Literal["simulated", "hardware"] = "hardware"
    state_dir: Path = Path("/var/lib/openlolo")
    runtime_dir: Path = Path("/run/openlolo")
    host: str = "127.0.0.1"
    port: int = Field(default=8080, ge=1024, le=65535)
    allowed_hosts: list[str] = ["127.0.0.1:8080", "localhost:8080"]
    allowed_origins: list[str] = ["http://127.0.0.1:8080", "http://localhost:8080"]
    lease_seconds: float = Field(default=60, ge=1, le=60)
    queue_size: int = Field(default=16, ge=1, le=64)
    frame_ttl: float = Field(default=120, ge=1, le=300)
    capture_deadline: float = Field(default=20, ge=0.1, le=60)
    # CoreDevice worker tuning. Transport (USB or Wi-Fi) is part of the owner binding, not config.
    auto_mount_ddi: bool = True
    ddi_mount_deadline: float = Field(default=240, ge=10, le=900)
    discovery_seconds: float = Field(default=3, ge=0.5, le=15)
    probe_interval: float = Field(default=10, ge=1, le=120)
    reconnect_min_seconds: float = Field(default=1, ge=0.01, le=60)
    reconnect_max_seconds: float = Field(default=15, ge=0.01, le=300)
    recover_wait_seconds: float = Field(default=8, ge=0, le=60)
    # How long a request waits for the session to come back while the worker is switching
    # transports or reconnecting right after a loss, instead of failing at once.
    switch_hold_seconds: float = Field(default=8, ge=0, le=60)
    hid_idle_seconds: float = Field(default=20, ge=1, le=600)
    hid_refresh_seconds: float = Field(default=15, ge=1, le=600)
    # Stop the HID media stream before each screenshot. Kept for iOS builds where a running
    # stream stalls the screenshot service; unset to let the stream stay up across screenshots.
    hid_stop_before_capture: bool = True
    # Screenshot source. "png": discrete full-resolution PNG from screencaptureservice (~2.8s,
    # lossless). "stream": decode the phone's mirroring H.265 on the Pi and serve the latest frame
    # (~0.1s, slightly lossy, needs PyAV). "stream" reuses the HID authentication stream.
    screenshot_source: Literal["png", "stream"] = "stream"
    stream_start_seconds: float = Field(default=3, ge=0.2, le=15)  # Wait for the first decoded frame.
    stream_frame_ttl: float = Field(default=1.0, ge=0.1, le=10)  # Refuse a decoded frame older than this.
    stream_jpeg_quality: int = Field(default=60, ge=1, le=95)  # JPEG quality for streamed frames.
    # Where the authentication video stream is sent: "device" points it back at the phone's own
    # tunnel address so no RTP crosses the tunnel; "drain" receives and discards it on the
    # userspace stack; "blackhole" advertises an unbound port so the phone's RTP is dropped.
    hid_stream_sink: Literal["device", "drain", "blackhole"] = "drain"
    # "userspace": in-process TCP/IP stack, no privileges. "kernel": a utun device handled by the
    # Linux kernel; needs CAP_NET_ADMIN and /dev/net/tun in the worker unit, but keeps the
    # media stream's packets out of the Python process.
    tunnel_mode: Literal["userspace", "kernel"] = "userspace"
    calibration_host: str | None = None
    calibration_port: int = Field(default=8081, ge=1024, le=65535)
    # Public HTTPS origin of the remote MCP entry, e.g. "https://pi.tailnet.ts.net". Unset = disabled.
    public_url: str | None = None
    mcp_port: int = Field(default=8090, ge=1024, le=65535)
    # LAN entry (ADR 0005): a second OAuth/MCP gateway instance served over TLS on the LAN for
    # LAN MCP clients, e.g. "https://openlolo-ab12.local:8443". The self-signed certificate's
    # SHA-256 fingerprint is the trust anchor. Unset = disabled.
    lan_url: str | None = None
    lan_bind: str = "0.0.0.0"
    lan_cert: Path | None = None  # Defaults to state_dir/tls/lan.crt
    lan_key: Path | None = None  # Defaults to state_dir/tls/lan.key
    # Extra Host values accepted on the LAN listener (port appended), e.g. the box's LAN IP for
    # clients that cannot resolve .local names.
    lan_extra_hosts: list[str] = []
    # OpenLolo opens a local WPA2 access point and hosts its own setup and owner page (the box
    # page) on this HTTP port. 0 disables the listener; the simulated backend defaults to 0.
    provisioning: bool = True
    portal_port: int = Field(default=80, ge=0, le=65535)
    setup_name: str | None = None  # SoftAP SSID and matching .local hostname stem; board-specific.
    setup_auto_pair: bool = True  # Trust-pair and bind the single cabled iPhone during setup mode.
    setup_ap_password: str | None = None  # Generated and persisted by Runtime; printed on setup card.
    setup_timeout_seconds: float = Field(default=600, ge=60, le=3600)
    setup_grace_seconds: float = Field(default=120, ge=10, le=3600)
    wifi_interface: str = "wlan0"

    @model_validator(mode="before")
    @classmethod
    def drop_legacy(cls, data):
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if k not in LEGACY_KEYS}
        return data

    @model_validator(mode="after")
    def local(self):
        if self.backend == "simulated" and "portal_port" not in self.model_fields_set:
            self.portal_port = 0
        taken = {self.port}
        if self.calibration_host:
            taken.add(self.calibration_port)
        if self.public_url:
            taken.add(self.mcp_port)
        if self.portal_port and self.portal_port in taken:
            raise ValueError("portal_port must differ from the control, calibration and MCP ports")
        if not ipaddress.ip_address(self.host).is_loopback:
            raise ValueError("Control API must bind to loopback; use an SSH tunnel")
        if self.calibration_host:
            address = ipaddress.ip_address(self.calibration_host)
            if address.is_unspecified or not address.is_private:
                raise ValueError("Calibration listener must bind a specific private LAN address")
        if self.reconnect_max_seconds < self.reconnect_min_seconds:
            raise ValueError("reconnect_max_seconds must be at least reconnect_min_seconds")
        if self.public_url is not None:
            self.public_url = public_origin(self.public_url)
            if self.mcp_port == self.port or self.mcp_port == self.calibration_port:
                raise ValueError("mcp_port must differ from the control and calibration ports")
        if self.lan_url is not None:
            self.lan_url = public_origin(self.lan_url)
            bind = ipaddress.ip_address(self.lan_bind)
            if not (bind.is_unspecified or bind.is_private):
                raise ValueError("lan_bind must be unspecified (0.0.0.0) or a private LAN address")
            taken = {self.port, self.calibration_port} | ({self.mcp_port} if self.public_url else set())
            if self.lan_port in taken:
                raise ValueError("lan_url port must differ from the control, calibration and MCP ports")
            if self.lan_cert is None:
                self.lan_cert = self.state_dir / "tls" / "lan.crt"
            if self.lan_key is None:
                self.lan_key = self.state_dir / "tls" / "lan.key"
            for host in self.lan_extra_hosts:
                if not host or ":" in host and not host.startswith("["):
                    raise ValueError("lan_extra_hosts entries are bare host names or IPv4 addresses")
            if self.setup_name is None:
                name = urlsplit(self.lan_url).hostname or "openlolo"
                suffix = name.split(".", 1)[0].split("-", 1)[-1]
                self.setup_name = f"OpenLolo-{suffix.upper()}" if suffix != name else "OpenLolo"
        if self.setup_name is not None and not 1 <= len(self.setup_name.encode()) <= 20:
            raise ValueError("setup_name must be 1..20 bytes (it is the setup SSID and hostname stem)")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", self.wifi_interface):
            raise ValueError("wifi_interface must be an interface name")
        return self

    @property
    def board(self) -> str | None:
        """Short box id ("ab12"): the suffix of setup_name, else of the lan_url host."""
        for name in (self.setup_name, urlsplit(self.lan_url or "").hostname):
            if name and "-" in name:
                return name.split(".", 1)[0].split("-", 1)[1].lower()
        return None

    @property
    def lan_files(self) -> tuple[Path, Path]:
        """Certificate and key paths of the LAN entry; raises when lan_url is unset."""
        if self.lan_cert is None or self.lan_key is None:
            raise ValueError("lan_url is not configured")
        return self.lan_cert, self.lan_key

    @property
    def lan_port(self) -> int:
        return urlsplit(self.lan_url or "").port or 443

    @property
    def lan_hosts(self) -> list[str]:
        """Every Host header value the LAN listener accepts."""
        if not self.lan_url:
            return []
        primary = urlsplit(self.lan_url).netloc.lower()
        suffix = f":{self.lan_port}" if self.lan_port != 443 else ""
        return [primary, *(f"{h.lower()}{suffix}" for h in self.lan_extra_hosts)]


def public_origin(value: str) -> str:
    """Accept only a bare HTTPS origin; the tunnel terminates TLS and forwards to the MCP listener."""
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("public_url must be a bare https origin such as https://pi.tailnet.ts.net")
    port = f":{parsed.port}" if parsed.port not in {None, 443} else ""
    return f"https://{parsed.hostname.lower()}{port}"


def load_config(path: str | Path = "/etc/openlolo/box.toml") -> Config:
    with open(path, "rb") as handle:
        return Config.model_validate(tomllib.load(handle))
