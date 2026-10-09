import pytest

from openlolo.domain.errors import OpenLoloError


async def test_calibration_separate_offset_and_complete_heldout(runtime):
    runtime.config.calibration_host = "192.168.1.2"
    cal = runtime.calibration
    b = runtime.phone.binding()
    started = cal.start(b, {"native_width": 1170, "native_height": 2532, "viewport_top": 390})
    token = started["session"]
    geometry = {"width": 390, "height": 616, "scale": 1}
    cal.telemetry(token, {"type": "geometry", **geometry})
    with pytest.raises(OpenLoloError):
        cal.finish(b, {"session": token})
    for i in range(14):
        x, y = cal.target(b)
        pending = cal.current()["pending"]
        cal.telemetry(
            token,
            {"type": "down", **geometry, "x": pending["expected_x"], "y": pending["expected_y"], "trial": i},
        )
    result = cal.finish(b, {"session": token})
    assert result["held_out_correct"] == 5 and result["passed"]
    assert result["grid_accuracy"] is None and not result["grid_gate_met"]
    assert result["viewport_top_measured"] == pytest.approx(390)
    from openlolo.domain.geometry import touch_point

    assert touch_point(0.5, 0.5) == (32768, 32768)


async def test_geometry_change_invalidates_session(runtime):
    runtime.config.calibration_host = "192.168.1.2"
    cal = runtime.calibration
    token = cal.start(
        runtime.phone.binding(), {"native_width": 390, "native_height": 844, "viewport_top": 130}
    )["session"]
    cal.telemetry(token, {"type": "geometry", "width": 390, "height": 616, "scale": 1})
    with pytest.raises(OpenLoloError):
        cal.telemetry(token, {"type": "geometry", "width": 390, "height": 600, "scale": 1})
    with pytest.raises(OpenLoloError):
        cal.current(token)


@pytest.mark.parametrize("correct,accepted", [(990, True), (989, False)])
async def test_declared_grid_acceptance_boundary(runtime, correct, accepted):
    runtime.config.calibration_host = "192.168.1.2"
    cal = runtime.calibration
    binding = runtime.phone.binding()
    token = cal.start(
        binding,
        {"native_width": 1170, "native_height": 2532, "viewport_top": 390, "grid_trials": 1000},
    )["session"]
    geometry = {"width": 390, "height": 616, "scale": 1}
    cal.telemetry(token, {"type": "geometry", **geometry})
    for trial in range(1014):
        cal.target(binding)
        pending = cal.current()["pending"]
        # Put the final misses outside the physical target, retaining valid telemetry.
        miss = trial >= 14 + correct
        cal.telemetry(
            token,
            {
                "type": "down",
                **geometry,
                "trial": trial,
                "x": pending["expected_x"],
                "y": pending["expected_y"] + (30 if miss else 0),
            },
        )
    result = cal.finish(binding, {"session": token})
    assert result["grid_trials"] == 1000 and result["grid_correct"] == correct
    assert result["grid_accuracy"] == pytest.approx(correct / 1000)
    assert result["grid_gate_met"] is accepted and result["passed"] is accepted
