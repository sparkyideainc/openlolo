"""Exceptions raised by the SDK, one class per thing a script may want to handle."""

from typing import Any


class OpenLoloError(Exception):
    """An error reported by the box or the SDK; ``code`` is the box's stable error code."""

    def __init__(self, code: str, message: str = "", *, detail: dict[str, Any] | None = None):
        super().__init__(f"{code}: {message}" if message else code)
        self.code, self.message, self.detail = code, message, detail or {}

    @property
    def dispatched(self) -> bool | None:
        """False when the box refused the request before anything reached the phone."""
        value = self.detail.get("dispatched")
        return value if isinstance(value, bool) else None

    @property
    def operation_id(self) -> str | None:
        return self.detail.get("operation_id")


class ApprovalRequired(OpenLoloError):
    """No valid grant and approval is not allowed here (no terminal, or allow_approval=False)."""


class ApprovalDenied(OpenLoloError):
    """The owner denied the request on the box page."""


class ApprovalExpired(OpenLoloError):
    """The request lapsed before the owner decided, or the approval wait timed out."""


class PinMismatch(OpenLoloError):
    """The box's TLS certificate is not the one pinned at first approval."""


class ControlBusy(OpenLoloError):
    """Another client controls the phone right now."""


class Paused(OpenLoloError):
    """Input is paused on the box; call ``start_session()`` only to continue on purpose."""


class RateLimited(OpenLoloError):
    """Too many calls per minute; the SDK already backed off and retried."""


class FrameExpired(OpenLoloError):
    """The frame a touch refers to is no longer the box's current one; take a screenshot."""


class OutcomeUnknown(OpenLoloError):
    """The box cannot say whether the input reached the phone; check the screen, do not replay."""


class OperationFailed(OpenLoloError):
    """The box ran the operation and it failed or was cancelled."""


class ScopeMissing(OpenLoloError):
    """The grant does not include what this call needs."""


class ResponseTooLarge(OpenLoloError):
    """The box answered with more bytes than the SDK accepts."""


BY_CODE: dict[str, type[OpenLoloError]] = {
    "CONTROL_BUSY": ControlBusy,
    "PAUSED": Paused,
    "RATE_LIMITED": RateLimited,
    "FRAME_EXPIRED": FrameExpired,
    "OUTCOME_UNKNOWN": OutcomeUnknown,
    "SCOPE_REQUIRED": ScopeMissing,
}


def from_payload(error: dict[str, Any]) -> OpenLoloError:
    code = str(error.get("code") or "UNKNOWN")
    message = str(error.get("message") or error.get("instruction") or "")
    return BY_CODE.get(code, OpenLoloError)(code, message, detail=error)
