"""Factory reset: back to first-time setup. Started from the box page or the top button."""

from openlolo.adapters.system.network import run as run_command
from openlolo.domain.errors import OpenLoloError

RESET_UNIT = "openlolo-reset.service"  # Root unit: deploy/reset/openlolo-reset.sh


async def start_factory_reset(config, bindings, provisioning) -> dict:
    """Start the reset; returns ``{"state": "reset"}`` when done in process (simulated backend)
    or ``{"state": "resetting"}`` once the root unit is queued."""
    if config.backend == "simulated":
        bindings.delete()
        if provisioning is not None and not provisioning.active:
            await provisioning.enter("reset")
        return {"state": "reset"}
    # The unit stops the API service, so it must run on its own: --no-block returns at once.
    code, output = await run_command("systemctl", "start", "--no-block", RESET_UNIT, timeout=10, quiet=False)
    if code != 0:
        raise OpenLoloError("RESET_FAILED", output or "Could not start the reset", 502)
    return {"state": "resetting"}
