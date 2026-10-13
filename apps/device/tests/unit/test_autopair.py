"""Setup-mode auto-pairing follows cable events and the setup window, never a timer."""

import asyncio
from types import SimpleNamespace

from openlolo.application.autopair import AutoPair
from openlolo.domain.errors import OpenLoloError

UDID = "00008110-000A1B2C3D4E5F60"


class Window:
    def __init__(self):
        self.active = False
        self.since = 1.0
        self.changed = asyncio.Event()

    async def window_change(self):
        await self.changed.wait()

    def set(self, active):
        self.active = active
        changed, self.changed = self.changed, asyncio.Event()
        changed.set()


class Cable:
    """Worker long poll: answers at once for None, else when the counter moves or on timeout."""

    def __init__(self):
        self.changes, self.serials, self.calls = 0, [], []
        self.moved = asyncio.Event()

    async def usb_events(self, since, timeout):
        self.calls.append((since, timeout))
        if since == self.changes:
            moved = self.moved
            try:
                await asyncio.wait_for(moved.wait(), timeout)
            except TimeoutError:
                pass
        return {"changes": self.changes, "serials": list(self.serials)}

    def plug(self, serials):
        self.serials, self.changes = serials, self.changes + 1
        moved, self.moved = self.moved, asyncio.Event()
        moved.set()


class Bindings:
    def __init__(self):
        self.value = None

    def read(self):
        return self.value


class Journal:
    def reserve(self, *args):
        pass

    def update(self, *args):
        pass


def build(errors=()):
    cable, window, bindings = Cable(), Window(), Bindings()
    pairs = []
    errors = list(errors)

    async def pair(udid):
        pairs.append(udid)
        if errors:
            raise errors.pop(0)
        return {"trusted": True}

    async def bind(binding):
        bindings.value = binding
        return {"bound": True}

    async def observe(function):
        return await function()

    phone = SimpleNamespace(
        usb_events=cable.usb_events,
        device=SimpleNamespace(pair=pair),
        bind=bind,
        coordinator=SimpleNamespace(observe=observe),
    )
    config = SimpleNamespace(setup_auto_pair=True)
    autopair = AutoPair(config, phone, window, bindings, Journal())
    return autopair, cable, window, bindings, pairs


async def wait_for(predicate, timeout=2):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


async def test_no_worker_calls_outside_the_setup_window():
    autopair, cable, window, bindings, pairs = build()
    autopair.start()
    try:
        await asyncio.sleep(0.05)
        cable.plug([UDID])
        await asyncio.sleep(0.05)
        assert cable.calls == [] and pairs == [] and autopair.state == "idle"
        window.set(True)
        await wait_for(lambda: autopair.state == "bound")
        assert pairs == [UDID] and bindings.value.udid == UDID
        window.set(False)
        await asyncio.sleep(0.05)
        calls = len(cable.calls)
        await asyncio.sleep(0.1)
        assert len(cable.calls) == calls
    finally:
        await autopair.close()


async def test_attach_event_starts_pairing_and_locked_phone_retries_after_5_s():
    autopair, cable, window, bindings, pairs = build([OpenLoloError("PHONE_LOCKED", "unlock")])
    clock = [100.0]
    autopair.clock = lambda: clock[0]
    window.set(True)
    autopair.start()
    try:
        await wait_for(lambda: autopair.state == "no_phone")
        cable.plug([UDID])  # Attach event: no tick needed.
        await wait_for(lambda: autopair.state == "locked")
        assert pairs == [UDID]
        await asyncio.sleep(0.05)
        assert pairs == [UDID]  # Waits for the retry time, not a 2 s tick.
        assert cable.calls[-1][1] <= 5
        clock[0] += 5
        cable.plug([UDID])
        await wait_for(lambda: autopair.state == "bound")
        assert pairs == [UDID, UDID]
    finally:
        await autopair.close()
