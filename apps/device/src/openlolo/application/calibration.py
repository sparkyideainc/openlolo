"""Session-bound Safari feedback. Viewport offsets are validation data only."""

import math
import secrets
import statistics
import time
from typing import Any

from pydantic import Field

from openlolo.adapters.storage.binding import device_tag
from openlolo.domain.capabilities import SHORTCUTS
from openlolo.domain.errors import OpenLoloError
from openlolo.domain.models import StrictModel


class CalibrationStart(StrictModel):
    native_width: int = Field(ge=100, le=5000)
    native_height: int = Field(ge=100, le=5000)
    viewport_top: float = Field(ge=0, le=2000)
    duration: int = Field(default=900, ge=60, le=7200)
    grid_trials: int = Field(default=0, ge=0, le=1000)


class Telemetry(StrictModel):
    type: str = Field(pattern=r"^(geometry|down)$")
    width: float = Field(gt=0, le=5000)
    height: float = Field(gt=0, le=5000)
    scale: float = Field(gt=0, le=10)
    x: float | None = Field(default=None, ge=0, le=5000)
    y: float | None = Field(default=None, ge=0, le=5000)
    trial: int | None = Field(default=None, ge=0, le=2000)


class Calibration:
    def __init__(self, config, journal):
        self.config, self.journal = config, journal
        self.session: dict[str, Any] | None = None

    def start(self, binding, payload):
        if not self.config.calibration_host:
            raise OpenLoloError("CALIBRATION_LISTENER_DISABLED")
        options = CalibrationStart.model_validate(payload)
        if options.native_width >= options.native_height:
            raise OpenLoloError("GEOMETRY_INCOMPATIBLE")
        token = secrets.token_urlsafe(24)
        train = [(x, y) for y in (0.25, 0.5, 0.75) for x in (0.15, 0.5, 0.85)]
        held_out = [(0.3, 0.35), (0.7, 0.65), (0.25, 0.7), (0.75, 0.3), (0.5, 0.6)]
        self.session = {
            "token": token,
            "device": device_tag(binding),
            "expires": time.monotonic() + options.duration,
            "options": options.model_dump(),
            "geometry": None,
            "targets": train + held_out + [train[(i * 7) % 9] for i in range(options.grid_trials)],
            "samples": [],
            "pending": None,
            "next": 0,
        }
        return {
            "url": f"http://{self.config.calibration_host}:{self.config.calibration_port}/{token}/",
            "session": token,
            "training_targets": 9,
            "held_out_targets": 5,
            "grid_trials": options.grid_trials,
            "expires_in": options.duration,
        }

    def current(self, token=None):
        session = self.session
        if not session or time.monotonic() > session["expires"]:
            raise OpenLoloError("CALIBRATION_EXPIRED", status=404)
        if token is not None and not secrets.compare_digest(token, session["token"]):
            raise OpenLoloError("CALIBRATION_NOT_FOUND", status=404)
        return session

    def public(self, token):
        session = self.current(token)
        i = session["next"]
        return {
            "trial": i,
            "target": session["targets"][i] if i < len(session["targets"]) else None,
            "samples": len(session["samples"]),
            "total": len(session["targets"]),
            "pending": session["pending"] is not None,
        }

    def target(self, binding):
        session = self.current()
        if session["device"] != device_tag(binding):
            raise OpenLoloError("IDENTITY_MISMATCH")
        if session["pending"]:
            raise OpenLoloError("CALIBRATION_FEEDBACK_PENDING")
        g = session["geometry"]
        if not g:
            raise OpenLoloError("CALIBRATION_PAGE_REQUIRED")
        i = session["next"]
        if i >= len(session["targets"]):
            raise OpenLoloError("CALIBRATION_COMPLETE")
        fx, fy = session["targets"][i]
        options = session["options"]
        scale = options["native_width"] / g["width"]
        x, y = fx, (options["viewport_top"] + fy * g["height"] * scale) / options["native_height"]
        if not 0 <= y <= 1:
            raise OpenLoloError("CALIBRATION_VIEWPORT_INVALID")
        session["pending"] = {
            "trial": i,
            "x": x,
            "y": y,
            "expected_x": fx * g["width"],
            "expected_y": fy * g["height"],
            "sent_at": time.monotonic(),
        }
        return x, y

    def telemetry(self, token, payload):
        event = Telemetry.model_validate(payload)
        session = self.current(token)
        geometry = {"width": event.width, "height": event.height, "scale": event.scale}
        if session["geometry"] and session["geometry"] != geometry:
            self.session = None
            raise OpenLoloError("CALIBRATION_GEOMETRY_CHANGED")
        session["geometry"] = geometry
        if event.type == "geometry":
            return
        pending = session["pending"]
        if (
            not pending
            or event.trial != pending["trial"]
            or event.x is None
            or event.y is None
            or time.monotonic() - pending["sent_at"] > 10
        ):
            raise OpenLoloError("CALIBRATION_UNEXPECTED_FEEDBACK")
        error = math.hypot(event.x - pending["expected_x"], event.y - pending["expected_y"])
        scale = session["options"]["native_width"] / event.width
        measured_offset = pending["y"] * session["options"]["native_height"] - event.y * scale
        session["samples"].append(
            {
                "trial": pending["trial"],
                "phase": "training"
                if pending["trial"] < 9
                else "held_out"
                if pending["trial"] < 14
                else "grid",
                "error_css_px": error,
                "correct": error <= 22,
                "viewport_top_measured": measured_offset,
            }
        )
        session["next"] += 1
        session["pending"] = None

    def finish(self, binding, payload):
        session = self.current(payload.get("session"))
        if session["device"] != device_tag(binding):
            raise OpenLoloError("IDENTITY_MISMATCH")
        if len(session["samples"]) != len(session["targets"]):
            raise OpenLoloError("CALIBRATION_INCOMPLETE", "Complete nine training and five held-out targets")
        samples = session["samples"]
        grid = samples[14:]
        grid_correct = sum(s["correct"] for s in grid)
        grid_gate_met = len(grid) == 1000 and grid_correct >= 990
        result = {
            "recorded_at": time.time(),
            "geometry": session["geometry"],
            "native_width": session["options"]["native_width"],
            "native_height": session["options"]["native_height"],
            "viewport_top_supplied": session["options"]["viewport_top"],
            "viewport_top_measured": statistics.median(s["viewport_top_measured"] for s in samples[:9]),
            "held_out_correct": sum(s["correct"] for s in samples[9:14]),
            "held_out_trials": 5,
            "grid_trials": len(grid),
            "grid_correct": grid_correct,
            "grid_accuracy": grid_correct / len(grid) if grid else None,
            "grid_gate_met": grid_gate_met,
            "max_error_css_px": max(s["error_css_px"] for s in samples),
            "passed": all(s["correct"] for s in samples[:14])
            and (grid_gate_met if len(grid) == 1000 else all(s["correct"] for s in grid)),
            "samples": samples,
            "scope": "This Safari viewport only; no global pointer correction; other PRD gates are separate",
        }
        profile = self.journal.profile(device_tag(binding))
        profile["calibration"] = result
        self.journal.save_profile(device_tag(binding), profile)
        self.session = None
        return result

    def validate_shortcut(self, binding, payload):
        name = payload.get("name")
        if name not in SHORTCUTS or payload.get("owner_confirmed") is not True:
            raise OpenLoloError("SHORTCUT_VALIDATION_REQUIRED")
        evidence = payload.get("evidence", "")
        prerequisites = payload.get("prerequisites", "")
        if not 10 <= len(evidence) <= 300 or not 5 <= len(prerequisites) <= 300:
            raise OpenLoloError(
                "VALIDATION_EVIDENCE_REQUIRED", "Record visible behavior and device prerequisites"
            )
        profile = self.journal.profile(device_tag(binding))
        profile.setdefault("shortcuts", {})[name] = {
            "evidence": evidence,
            "prerequisites": prerequisites,
            "recorded_at": time.time(),
        }
        self.journal.save_profile(device_tag(binding), profile)
        return {"validated": name}
