"""Owner-validated keyboard shortcuts: a shortcut is offered to clients only after the owner has
watched it work on this phone and recorded what it did (capabilities lists only these)."""

import time

from openlolo.adapters.storage.binding import device_tag
from openlolo.domain.capabilities import SHORTCUTS
from openlolo.domain.errors import OpenLoloError


def validate_shortcut(journal, binding, payload: dict) -> dict:
    name = payload.get("name")
    if name not in SHORTCUTS or payload.get("owner_confirmed") is not True:
        raise OpenLoloError("SHORTCUT_VALIDATION_REQUIRED")
    evidence = payload.get("evidence", "")
    prerequisites = payload.get("prerequisites", "")
    if not 10 <= len(evidence) <= 300 or not 5 <= len(prerequisites) <= 300:
        raise OpenLoloError(
            "VALIDATION_EVIDENCE_REQUIRED", "Record visible behavior and device prerequisites"
        )
    profile = journal.profile(device_tag(binding))
    profile.setdefault("shortcuts", {})[name] = {
        "evidence": evidence,
        "prerequisites": prerequisites,
        "recorded_at": time.time(),
    }
    journal.save_profile(device_tag(binding), profile)
    return {"validated": name}
