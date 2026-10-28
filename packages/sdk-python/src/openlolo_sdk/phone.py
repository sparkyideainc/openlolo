"""``Phone``: script the iPhone behind an OpenLolo box, one MCP tool call per method.

Every method maps onto the box's MCP tools, so a script and an AI agent go through the same
leases, journal, pause and revocation. Actions return the phone so calls chain::

    with Phone("openlolo-ab12.local") as phone:
        phone.open_app("com.apple.mobilenotes").type("hello").home()

Touch coordinates are fractions of the screen (floats in 0..1) or pixels of the last image
(ints). A touch carries the id of the frame the script last saw; the box refuses it when that
frame is no longer current (``FrameExpired``) instead of touching a screen nobody looked at.
"""

import ipaddress
import socket
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from openlolo_sdk._auth import Auth, Store
from openlolo_sdk._mcp import McpClient, ToolResult
from openlolo_sdk._tls import Pin, pinned_context
from openlolo_sdk.errors import (
    OpenLoloError,
    OperationFailed,
    OutcomeUnknown,
    from_payload,
)

Number = int | float
Button = Literal["home", "lock", "volume_up", "volume_down", "mute", "siri"]
TYPE_LIMIT = 200  # openlolo_type_text keystrokes; longer or non-ASCII text goes through the clipboard.
FAILED = {"FAILED", "CANCELLED"}


LAN_PORT = 8443  # The installer serves the LAN entry here (scripts/install.sh).


def base_url(box: str) -> str:
    """The box's https origin.

    ``openlolo-ab12`` and ``openlolo-ab12.local`` mean the LAN entry on port 8443; an address
    with a port, or a full ``https://`` URL (the remote entry), is used as given."""
    box = box.strip()
    if "://" not in box:
        host = box if "." in box or ":" in box else box + ".local"
        if host.endswith(".local"):
            host += f":{LAN_PORT}"
        box = "https://" + host
    parts = urlsplit(box)
    if parts.scheme != "https" or not parts.hostname or parts.path not in ("", "/") or parts.query:
        raise ValueError(f"not a box address: {box!r} (use https://host or the box name)")
    if parts.username or parts.password:
        raise ValueError("credentials do not belong in the box address")
    return f"https://{parts.netloc.lower()}"


def is_lan(host: str) -> bool:
    """The LAN entry (self-signed, pinned): mDNS names and private or loopback addresses."""
    if host.endswith(".local") or host == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return address.is_private or address.is_loopback or address.is_link_local


def default_name() -> str:
    script = Path(sys.argv[0]).name if sys.argv and sys.argv[0] else "python"
    return f"{script or 'python'} on {socket.gethostname().split('.')[0]}"[:64]


@dataclass
class Frame:
    """One screen image with the ids a touch must refer to."""

    id: str
    epoch: str
    width: int
    height: int
    media_type: str
    image: bytes = field(repr=False)
    metadata: dict[str, Any] = field(repr=False)
    phone: "Phone | None" = field(default=None, repr=False, compare=False)

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.write_bytes(self.image)
        return path

    def point(self, x: Number, y: Number) -> tuple[float, float]:
        """Fractions of the image for ``x, y``: floats are fractions already, ints are pixels."""
        return _axis(x, self.width, "x"), _axis(y, self.height, "y")

    def _phone(self) -> "Phone":
        if self.phone is None:
            raise ValueError("this frame is not attached to a phone")
        return self.phone

    def tap(self, x: Number, y: Number, **options) -> "Phone":
        return self._phone().tap(x, y, frame=self, **options)

    def long_press(self, x: Number, y: Number, seconds: float = 1.0, **options) -> "Phone":
        return self._phone().long_press(x, y, seconds, frame=self, **options)

    def swipe(self, x1: Number, y1: Number, x2: Number, y2: Number, **options) -> "Phone":
        return self._phone().swipe(x1, y1, x2, y2, frame=self, **options)


def _axis(value: Number, size: int, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{name} must be an int (pixels) or a float (0..1)")
    if isinstance(value, int):
        if not 0 <= value < size:
            raise ValueError(f"{name}={value} is outside the {size}-pixel image")
        return min(1.0, (value + 0.5) / size)
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"{name}={value} is not a fraction 0..1; pass an int for pixels")
    return float(value)


class Phone:
    """The phone behind one box.

    ``box`` is the box name (``openlolo-ab12``), its LAN address or its remote URL. The first
    use asks the owner to approve this script on the box page; the grant is cached in
    ``~/.config/openlolo/sdk`` and refreshed without asking again until the owner removes it.
    """

    def __init__(
        self,
        box: str,
        *,
        name: str | None = None,
        fingerprint: str | None = None,
        tls: Literal["auto", "pin", "system"] = "auto",
        cache_dir: str | Path | None = None,
        allow_approval: bool | None = None,
        approve_timeout: float = 600,
        observe: bool = True,
        timeout: float = 60,
        notify: Callable[[str], None] | None = None,
        transport: httpx.BaseTransport | None = None,
    ):
        self.url = base_url(box)
        self.observe = observe
        self.frame: Frame | None = None
        self.last: dict[str, Any] = {}
        self.acted = False  # An action may have taken control, which close() then releases.
        store = Store(self.url, Path(cache_dir) if cache_dir else None)
        host = urlsplit(self.url).hostname or ""
        pinned = tls == "pin" or (tls == "auto" and is_lan(host))
        self.pin = Pin(fingerprint or store.load().get("fingerprint")) if pinned else None
        self.http = httpx.Client(
            base_url=self.url,
            verify=pinned_context(self.pin) if self.pin else True,
            timeout=httpx.Timeout(timeout, connect=5),
            follow_redirects=False,
            trust_env=not pinned,
            transport=transport,
        )
        self.auth = Auth(
            self.http,
            store,
            client_name=(name or default_name())[:64],
            allow_approval=sys.stderr.isatty() if allow_approval is None else allow_approval,
            approve_timeout=approve_timeout,
            notify=notify or (lambda text: print(text, file=sys.stderr, flush=True)),
            pin_seen=lambda: self.pin.seen if self.pin else None,
        )
        self.mcp = McpClient(self.http, self.auth.token, timeout=timeout)

    # -- lifecycle -----------------------------------------------------------------------

    @property
    def approved(self) -> bool:
        """True when this machine holds a grant for the box (it may still have been removed there)."""
        return bool(self.auth.data.get("refresh_token"))

    @property
    def fingerprint(self) -> str | None:
        """SHA-256 of the box's LAN certificate pinned at approval; None on the remote entry."""
        return self.auth.fingerprint

    def connect(self) -> "Phone":
        """Make sure a grant exists now (approving if needed) instead of on the first call."""
        self.auth.token()
        return self

    def _close(self) -> None:
        """End the session if this script holds control, then close the connection. Leaving the
        ``with`` block does this; a script that keeps a Phone open calls ``end_session()``."""
        try:
            if self.acted:
                self.end_session()
        except (OpenLoloError, httpx.HTTPError):
            pass
        finally:
            self.http.close()

    def __enter__(self) -> "Phone":
        return self

    def __exit__(self, *exc) -> None:
        self._close()

    def __del__(self) -> None:
        # Without ``with``: free the connection when the Phone is dropped. No network call here;
        # the box ends control 90 s after the last action.
        http = self.__dict__.get("http")
        if http is not None:
            http.close()

    # -- reading -------------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        return self._read("openlolo_status")

    def device_info(self) -> dict[str, Any]:
        return self._read("openlolo_device_info")

    def capabilities(self) -> dict[str, Any]:
        return self._read("openlolo_capabilities")

    def apps(self, query: str = "", include_hidden: bool = False) -> list[dict[str, Any]]:
        return self._read("openlolo_apps", query=query, include_hidden=include_hidden).get("apps", [])

    def clipboard(self) -> str:
        return str(self._read("openlolo_clipboard").get("text") or "")

    def operation(self, operation_id: str) -> dict[str, Any]:
        return self._read("openlolo_operation_status", operation_id=operation_id)

    def screenshot(self, format: Literal["jpeg", "png"] = "jpeg", max_size: int = 1536) -> Frame:
        result = self._call("openlolo_screenshot", {"format": format, "max_size": max_size})
        frame = self._frame(result.data, result.images)
        if frame is None:
            raise OpenLoloError("NO_IMAGE", "the box returned no image")
        return frame

    def full_page(self, max_pages: int = 20, output: Literal["pages", "stitched", "both"] = "pages") -> dict:
        """Scroll a long screen top to bottom on the box; ``images`` holds the pages in order."""
        self.acted = True
        arguments = {"max_pages": max_pages, "output": output, "operation_id": str(uuid4())}
        result = self._mutate("openlolo_full_page", arguments, timeout=60 + 15 * max_pages)
        self.frame = None  # The screen scrolled; the next touch captures first.
        return {**result.data, "images": [image for image, _ in result.images]}

    # -- touch ---------------------------------------------------------------------------

    def tap(self, x: Number, y: Number, *, frame: Frame | None = None, **options) -> "Phone":
        frame = self._touch_frame(frame)
        fx, fy = frame.point(x, y)
        return self._act("openlolo_tap", {**self._ref(frame), "x": fx, "y": fy}, options)

    def long_press(
        self, x: Number, y: Number, seconds: float = 1.0, *, frame: Frame | None = None, **options
    ) -> "Phone":
        frame = self._touch_frame(frame)
        fx, fy = frame.point(x, y)
        return self._act(
            "openlolo_long_press", {**self._ref(frame), "x": fx, "y": fy, "seconds": seconds}, options
        )

    def swipe(
        self,
        x1: Number,
        y1: Number,
        x2: Number,
        y2: Number,
        seconds: float = 0.6,
        hold: float = 0,
        *,
        frame: Frame | None = None,
        **options,
    ) -> "Phone":
        frame = self._touch_frame(frame)
        (ax, ay), (bx, by) = frame.point(x1, y1), frame.point(x2, y2)
        arguments = {
            **self._ref(frame),
            "x": ax,
            "y": ay,
            "x2": bx,
            "y2": by,
            "seconds": seconds,
            "hold": hold,
        }
        return self._act("openlolo_swipe", arguments, options)

    # -- keyboard and buttons ------------------------------------------------------------

    def type(self, text: str, **options) -> "Phone":
        """Short printable ASCII is typed key by key; anything else is pasted (replaces the clipboard)."""
        if len(text) <= TYPE_LIMIT and all(" " <= c <= "~" for c in text):
            return self._act("openlolo_type_text", {"text": text}, options)
        return self.paste(text, **options)

    def paste(self, text: str, **options) -> "Phone":
        return self._act("openlolo_send_text", {"text": text}, options)

    def key(self, name: str, *modifiers: str, **options) -> "Phone":
        """``key("enter")``, ``key("n", "cmd")`` for Cmd+N."""
        return self._act("openlolo_key", {"key": name, "modifiers": list(modifiers)}, options)

    def shortcut(self, name: str, **options) -> "Phone":
        return self._act("openlolo_shortcut", {"shortcut": name}, options)

    def button(self, name: Button, **options) -> "Phone":
        return self._act("openlolo_button", {"button": name}, options)

    def home(self, **options) -> "Phone":
        return self.button("home", **options)

    def lock(self, **options) -> "Phone":
        return self.button("lock", **options)

    def volume_up(self, **options) -> "Phone":
        return self.button("volume_up", **options)

    def volume_down(self, **options) -> "Phone":
        return self.button("volume_down", **options)

    # -- apps and links ------------------------------------------------------------------

    def open_app(self, bundle_id: str, **options) -> "Phone":
        """Foreground an app by bundle id, e.g. ``"com.apple.mobilenotes"``; ``apps("notes")`` finds it."""
        if "." not in bundle_id or " " in bundle_id.strip():
            raise ValueError(f"{bundle_id!r} is not a bundle id; find it with phone.apps({bundle_id!r})")
        return self._act("openlolo_launch_app", {"bundle_id": bundle_id.strip()}, options)

    def open_url(self, url: str, bundle_id: str = "", **options) -> "Phone":
        return self._act("openlolo_open_url", {"url": url, "bundle_id": bundle_id}, options)

    def open_link(self, bundle_id: str, link: str, observe: bool | None = None, **params: str) -> "Phone":
        """A deep link from the app's profile on the box, e.g. ``open_link("net.whatsapp.WhatsApp", "chat", phone=...)``."""
        arguments = {"bundle_id": bundle_id, "link": link, "params": params}
        return self._act("openlolo_open_link", arguments, {} if observe is None else {"observe": observe})

    # -- control -------------------------------------------------------------------------

    def sleep(self, seconds: float) -> "Phone":
        time.sleep(seconds)
        return self

    def start_session(self) -> "Phone":
        """Take control (optional: the first action takes it too); also continues after an AI paused input."""
        self._mutate("openlolo_start_session", {"operation_id": str(uuid4())})
        return self

    def end_session(self) -> "Phone":
        """End control so another client can act and the phone's icon goes off; ``close()`` does this too."""
        self._mutate("openlolo_end_session", {"operation_id": str(uuid4())})
        self.acted = False
        return self

    # -- internals -----------------------------------------------------------------------

    def _read(self, tool: str, **arguments) -> dict[str, Any]:
        return self._call(tool, arguments).data

    def _call(self, tool: str, arguments: dict[str, Any], *, timeout: float | None = None) -> ToolResult:
        result = self.mcp.call(tool, arguments, timeout=timeout)
        self.last = result.data
        self._raise(result)
        return result

    def _mutate(self, tool: str, arguments: dict[str, Any], *, timeout: float | None = None) -> ToolResult:
        """A call that may change the phone, retried once with the same operation id.

        The box journals each operation id once, so the retry can never run the input twice;
        when the retry fails too, the journal says what happened instead of a blind replay."""
        try:
            return self._call(tool, arguments, timeout=timeout)
        except httpx.TransportError:
            pass
        try:
            return self._call(tool, arguments, timeout=timeout)
        except httpx.TransportError as exc:
            operation_id = arguments.get("operation_id")
            if operation_id is None:
                raise
            try:
                record = self.operation(str(operation_id))
            except (OpenLoloError, httpx.HTTPError):
                raise OutcomeUnknown(
                    "OUTCOME_UNKNOWN",
                    f"lost the box during {tool}; check the screen before acting again",
                    detail={"operation_id": operation_id},
                ) from exc
            raise OutcomeUnknown(
                "OUTCOME_UNKNOWN",
                f"lost the box during {tool}; the journal says {record.get('state')}",
                detail={"operation_id": operation_id, "operation": record},
            ) from exc

    def _act(self, tool: str, arguments: dict[str, Any], options: dict[str, Any]) -> "Phone":
        self.acted = True
        observe = options.pop("observe", self.observe)
        arguments = {**arguments, **options, "observe": observe, "operation_id": str(uuid4())}
        result = self._mutate(tool, arguments)
        observation = result.data.get("observation")
        if isinstance(observation, dict):
            self.frame = self._frame(observation, result.images)
        return self

    def _raise(self, result: ToolResult) -> None:
        if not result.is_error:
            return
        data = result.data
        if isinstance(data.get("error"), dict):
            raise from_payload(data["error"])
        nested = data.get("operation")
        operation: dict[str, Any] = nested if isinstance(nested, dict) else data
        state = operation.get("state")
        detail = {"operation_id": operation.get("id"), "operation": operation}
        if state == "OUTCOME_UNKNOWN":
            raise OutcomeUnknown("OUTCOME_UNKNOWN", "check the screen; do not replay", detail=detail)
        if state in FAILED:
            error = (operation.get("result") or {}).get("error") or {}
            code = error.get("code") if isinstance(error, dict) else None
            raise OperationFailed(str(code or state), f"operation {state.lower()}", detail=detail)
        raise OpenLoloError("TOOL_ERROR", "the box reported an error", detail=data)

    def _frame(self, observation: dict[str, Any], images: list[tuple[bytes, str]]) -> Frame | None:
        metadata = observation.get("metadata")
        if not isinstance(metadata, dict) or not images:
            return None
        image, media_type = images[0]
        frame = Frame(
            id=str(metadata["frame_id"]),
            epoch=str(metadata["geometry_epoch"]),
            width=int(metadata["width"]),
            height=int(metadata["height"]),
            media_type=str(metadata.get("media_type") or media_type),
            image=image,
            metadata=metadata,
            phone=self,
        )
        self.frame = frame
        return frame

    def _touch_frame(self, frame: Frame | None) -> Frame:
        """The frame a touch refers to: the one given, the last one seen, or a new capture."""
        if frame is not None:
            return frame
        if self.frame is None:
            self.screenshot()
        assert self.frame is not None
        return self.frame

    @staticmethod
    def _ref(frame: Frame) -> dict[str, str]:
        return {"frame_id": frame.id, "geometry_epoch": frame.epoch}
