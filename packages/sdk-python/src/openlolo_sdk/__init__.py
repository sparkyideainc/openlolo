"""Script an iPhone through an OpenLolo box.

    from openlolo_sdk import Phone

    with Phone("openlolo-ab12") as phone:
        phone.open_app("com.apple.mobilenotes").type("hello").home()

The first run asks the owner to approve the script on the box page (Access); afterwards the
grant is cached and refreshed without asking until the owner removes it.
"""

from openlolo_sdk.errors import (
    ApprovalDenied,
    ApprovalExpired,
    ApprovalRequired,
    ControlBusy,
    FrameExpired,
    OpenLoloError,
    OperationFailed,
    OutcomeUnknown,
    Paused,
    PinMismatch,
    RateLimited,
    ResponseTooLarge,
    ScopeMissing,
)
from openlolo_sdk.phone import Frame, Phone

__version__ = "0.1.0"

__all__ = [
    "ApprovalDenied",
    "ApprovalExpired",
    "ApprovalRequired",
    "ControlBusy",
    "Frame",
    "FrameExpired",
    "OpenLoloError",
    "OperationFailed",
    "OutcomeUnknown",
    "Paused",
    "Phone",
    "PinMismatch",
    "RateLimited",
    "ResponseTooLarge",
    "ScopeMissing",
    "__version__",
]
