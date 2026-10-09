import asyncio

import pytest

from openlolo.application.actions import ASCII, MODIFIERS, chord, execute, plan
from openlolo.domain.errors import OpenLoloError
from openlolo.domain.geometry import geometry_epoch, touch_point
from openlolo.domain.models import Action


def test_geometry_edges_and_portrait():
    assert touch_point(0, 1) == (0, 65535)
    assert touch_point(0.5, 0.5) == (32768, 32768)
    for x in (-0.1, 1.1, float("nan"), float("inf")):
        with pytest.raises(OpenLoloError):
            touch_point(x, 0.5)
    with pytest.raises(OpenLoloError):
        geometry_epoch(844, 390, "phone")
    assert geometry_epoch(390, 844, "a") != geometry_epoch(390, 844, "b")


def test_ascii_complete_and_keyboard_usages_only():
    assert set(ASCII) == {chr(i) for i in range(32, 127)}
    assert ASCII["#"] == (True, 32)
    events = plan(Action(kind="type_text", text="A#z !"))
    assert len(events) == 4 + 4 + 2 + 2 + 4
    for kind, value, _ in events:
        assert kind == "keyboard" and all(0 < usage <= 0xE7 for usage in value)
    assert [v for _, v, _ in events[:4]] == [
        [MODIFIERS["shift"]],
        [MODIFIERS["shift"], 4],
        [MODIFIERS["shift"]],
        [],
    ]
    assert events[8][1] == [29] and events[9][1] == []


async def test_unsupported_text_before_any_report(runtime, lease):
    before = len(runtime.phone.device.reports)
    with pytest.raises(OpenLoloError, match="ASCII"):
        runtime.phone.action("any", "owner", lease, Action(kind="type_text", text="hello💥"))
    assert len(runtime.phone.device.reports) == before


def test_chord_staged():
    reports = [r for _, r, _ in chord("a", ["cmd", "shift"])]
    assert reports == [[0xE3, 0xE1], [0xE3, 0xE1, 4], [0xE3, 0xE1], []]
    with pytest.raises(OpenLoloError):
        chord("fn", [])


def test_button_events_hold_then_release():
    events = plan(Action(kind="button", button="lock"))
    assert events == [("button", [0x0C, 0x30, "down"], 0.5), ("button", [0x0C, 0x30, "up"], 0.05)]
    with pytest.raises(ValueError):
        Action(kind="button", button="power")


async def test_release_on_cancel(runtime):
    task = asyncio.create_task(
        execute(
            runtime.phone.device,
            runtime.phone.binding(),
            [("keyboard", [0xE3, 4], 2)],
            lambda: None,
        )
    )
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runtime.phone.device.generation is None
    assert runtime.phone.device.reports[-2] == ("keyboard", [])


def test_swipe_releases_endpoint():
    action = Action(kind="swipe", x=0, y=0, x2=1, y2=1, frame_id="f", geometry_epoch="g")
    events = plan(action)
    assert events[0][1] == ["contact", 0, 0]
    assert events[-1][1] == ["release", 65535, 65535]
    assert len(events) == 22 and all(kind == "touch" for kind, _, _ in events)
    tap = plan(Action(kind="tap", x=0.5, y=0.5, frame_id="f", geometry_epoch="g"))
    assert [v[0] for _, v, _ in tap] == ["contact", "release"] and tap[0][2] == 0.08


def test_swipe_hold_rests_before_release():
    action = Action(kind="swipe", x=0.5, y=0.8, x2=0.5, y2=0.2, hold=0.3, frame_id="f", geometry_epoch="g")
    events = plan(action)
    assert events[-2][1][0] == "contact" and events[-2][2] == 0.3
    assert events[-1][1][0] == "release" and events[-1][2] == 0.05
    quick = plan(Action(kind="swipe", x=0.5, y=0.8, x2=0.5, y2=0.2, frame_id="f", geometry_epoch="g"))
    assert quick[-2][2] == pytest.approx(0.6 / 20)
