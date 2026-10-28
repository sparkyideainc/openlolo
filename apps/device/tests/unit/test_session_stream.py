"""DeviceSession's media-stream lifetime: what idleness closes, and what an input failure keeps."""

import asyncio
import time
from types import SimpleNamespace

from openlolo.config import Config
from openlolo.coredevice.session import DeviceSession
from openlolo.domain.errors import OpenLoloError


class FakeDecoder:
    def __init__(self):
        self.running = True
        self.stops = 0

    async def stop(self):
        self.stops += 1
        self.running = False
        return 0


class FakeService:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


def session(tmp_path, **config):
    config = Config(
        backend="simulated", state_dir=tmp_path, runtime_dir=tmp_path, hid_idle_seconds=1, **config
    )
    device = DeviceSession(SimpleNamespace(), config, lambda: None)
    device.decoder = FakeDecoder()  # ty: ignore[invalid-assignment]
    return device


def test_idle_input_closes_only_the_input_services(tmp_path):
    async def run():
        device = session(tmp_path)
        device.hid = hid = FakeService()
        device.hid_used = time.monotonic() - 5
        device.stream_used = time.monotonic()  # a screenshot just used the stream
        await device.idle_check()
        assert hid.closed and device.hid is None
        assert device.decoder is not None and device.decoder.running

    asyncio.run(run())


def test_held_stream_never_closes_for_idleness_and_closes_once_released(tmp_path):
    async def run():
        device = session(tmp_path)
        decoder = device.decoder
        device.stream_held = True
        device.stream_used = device.hid_used = time.monotonic() - 60  # the AI is thinking
        await device.idle_check()
        assert device.decoder is decoder and decoder.running
        device.stream_held = False
        await device.idle_check()
        assert device.decoder is None and decoder.stops == 1

    asyncio.run(run())


def test_input_timeout_keeps_the_stream(tmp_path):
    async def run():
        device = session(tmp_path)
        device.hid = hid = FakeService()

        async def hang():
            await asyncio.sleep(1)

        try:
            await device._hid_call(hang(), timeout=0.01)
        except OpenLoloError as exc:
            assert exc.code == "INPUT_INTERRUPTED"
        assert hid.closed and device.hid is None and device.decoder.running

    asyncio.run(run())


def test_png_source_keeps_closing_everything_together(tmp_path):
    async def run():
        device = session(tmp_path, screenshot_source="png")
        device.decoder = None
        device.hid = hid = FakeService()
        device.hid_used = time.monotonic() - 5
        await device.idle_check()
        assert hid.closed and device.hid is None

    asyncio.run(run())
