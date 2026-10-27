"""Connection-state reporting and error classification shared by the CoreDevice worker.

This module never imports pymobiledevice3: classification uses exception class names so the
API process and tests can reason about worker errors without the hardware environment.
"""

import asyncio
import errno
import time
from dataclasses import asdict, dataclass
from enum import Enum

from openlolo.domain.errors import OpenLoloError


class State(str, Enum):
    DISCONNECTED = "disconnected"
    WAITING_FOR_DEVICE = "waiting_for_device"
    TRUST_REQUIRED = "trust_required"
    WIFI_PAIRING_REQUIRED = "wifi_pairing_required"
    PHONE_LOCKED = "phone_locked"
    DEVELOPER_MODE_REQUIRED = "developer_mode_required"
    DDI_REQUIRED = "ddi_required"
    MOUNTING_DDI = "mounting_ddi"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    SWITCHING = "switching"


# Errors the owner must resolve on the phone or through explicit setup. The supervisor does not
# retry these on a timer: the next attempt follows a cable attach, a search window (top button,
# cable pulled) or an explicit recover.
OWNER_STATES = {
    "TRUST_REQUIRED": State.TRUST_REQUIRED,
    "WIFI_PAIRING_REQUIRED": State.WIFI_PAIRING_REQUIRED,
    "PHONE_LOCKED": State.PHONE_LOCKED,
    "DEVELOPER_MODE_REQUIRED": State.DEVELOPER_MODE_REQUIRED,
    "DDI_REQUIRED": State.DDI_REQUIRED,
    "DDI_MOUNT_FAILED": State.DDI_REQUIRED,
    "USB_TUNNEL_UNSUPPORTED": State.TRUST_REQUIRED,
    "IDENTITY_MISMATCH": State.WAITING_FOR_DEVICE,
}

STATE_MESSAGES = {
    State.DISCONNECTED: "No phone is connected",
    State.WAITING_FOR_DEVICE: "Press the top button, tap Reconnect iPhone on the box page, or plug in the phone",
    State.TRUST_REQUIRED: "Unlock the bound phone and run explicit owner pairing",
    State.WIFI_PAIRING_REQUIRED: "Run explicit owner pairing over USB once before using Wi-Fi mode",
    State.PHONE_LOCKED: "Unlock the phone: a locked phone refuses new Wi-Fi sessions",
    State.DEVELOPER_MODE_REQUIRED: "Enable Developer Mode on the phone (Settings > Privacy & Security)",
    State.DDI_REQUIRED: "The developer disk image is not mounted",
    State.MOUNTING_DDI: "Mounting the developer disk image",
    State.CONNECTING: "Establishing the CoreDevice session",
    State.CONNECTED: "Connected",
    State.RECONNECTING: "Connection lost; reconnecting",
    State.SWITCHING: "Moving the session to the USB cable",
}


@dataclass
class Connection:
    state: str = State.DISCONNECTED.value
    transport: str | None = None
    since: float = 0.0
    attempts: int = 0
    last_error: dict | None = None
    device_present: bool = False
    developer_mode: bool | None = None
    ddi_mounted: bool | None = None
    hid_active: bool = False
    product_version: str | None = None
    wifi_paired: bool | None = None
    searching: bool = False  # A search window is open.
    searched_at: float = 0.0  # Wall time the last search window ended (status LED hold-over).
    # Screen-recording indicator for the control lease: "off" (no hold), "opening", "on" (the
    # media stream is open) or "unavailable" (the last open failed; ``indication_error`` says why).
    indication: str = "off"
    indication_error: dict | None = None

    def set(self, state: State, **changes):
        if self.state != state.value:
            self.since = time.time()
        self.state = state.value
        for key, value in changes.items():
            setattr(self, key, value)

    def as_dict(self) -> dict:
        return {**asdict(self), "message": STATE_MESSAGES[State(self.state)]}


TRANSPORT_LOST_ERRNOS = {
    errno.EHOSTUNREACH,
    errno.ENETUNREACH,
    errno.ENETDOWN,
    errno.ECONNREFUSED,
    errno.ECONNABORTED,
    errno.ETIMEDOUT,
    errno.EPIPE,
    errno.ECONNRESET,
    errno.ENOTCONN,
    errno.EBADF,
}
TRANSPORT_LOST_NAMES = {
    "ConnectionTerminatedError",
    "StreamClosedError",
    "NotConnectedError",
    "ConnectionFailedError",
    "ConnectionFailedToUsbmuxdError",
    "NoDeviceConnectedError",
    "DeviceNotFoundError",
    "TunneldConnectionError",
    "UserspaceTunnelUnavailableError",
    "IncompleteReadError",
    "EOFError",
}
# RemotePairing's refusal of a new Wi-Fi session from a phone locked for more than ~30 s.
LOCKED_TEXT = "has not been unlocked recently"
TRUST_NAMES = {
    "NotPairedError",
    "NotTrustedError",
    "PairingError",
    "PasswordRequiredError",
    "FatalPairingError",
}


def transport_lost(exc: BaseException) -> bool:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (ConnectionError, asyncio.IncompleteReadError, EOFError, TimeoutError)):
            return True
        if isinstance(current, OSError) and current.errno in TRANSPORT_LOST_ERRNOS:
            return True
        if type(current).__name__ in TRANSPORT_LOST_NAMES:
            return True
        current = current.__cause__ or current.__context__
    return False


def classify(exc: BaseException) -> OpenLoloError:
    """Map a worker-side exception to a stable, payload-free error code."""
    if isinstance(exc, OpenLoloError):
        return exc
    name = type(exc).__name__
    if name in TRUST_NAMES:
        return OpenLoloError("TRUST_REQUIRED", STATE_MESSAGES[State.TRUST_REQUIRED])
    if LOCKED_TEXT in str(exc):
        return OpenLoloError("PHONE_LOCKED", STATE_MESSAGES[State.PHONE_LOCKED], 409)
    if name == "DeveloperModeIsNotEnabledError":
        return OpenLoloError("DEVELOPER_MODE_REQUIRED", STATE_MESSAGES[State.DEVELOPER_MODE_REQUIRED])
    if name in {"InvalidServiceError", "DeviceFeatureNotSupportedError"}:
        return OpenLoloError("SERVICE_UNAVAILABLE", "A required CoreDevice service is not offered")
    if name == "CoreDeviceError":
        return OpenLoloError("COREDEVICE_ERROR", "The phone rejected the CoreDevice request")
    if transport_lost(exc):
        return OpenLoloError("DEVICE_DISCONNECTED", "The phone is not reachable on the bound transport")
    return OpenLoloError("BACKEND_FAILED")
