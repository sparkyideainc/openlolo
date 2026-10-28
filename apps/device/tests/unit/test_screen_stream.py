import asyncio
import concurrent.futures
import struct
import time
from types import SimpleNamespace

import pytest
from PIL import Image

from openlolo.coredevice import screen_stream
from openlolo.coredevice.screen_stream import ScreenDecoder, picture_loss, receiver_report


class FakeTransport:
    def __init__(self):
        self.sent = []

    async def sendto(self, data, ip, port):
        self.sent.append((data, ip, port))


class FakeFrame:
    """A decoded frame: ``reformat`` returns a frame of the requested size (PyAV is Linux-only)."""

    def __init__(self, width, height):
        self.width, self.height = width, height
        self.reformats = []

    def reformat(self, width=None, height=None, format=None, interpolation=None):
        self.reformats.append((width, height, format, interpolation))
        size = (width or self.width, height or self.height)
        return SimpleNamespace(to_image=lambda: Image.new("RGB", size))


def decoder():
    rsd = SimpleNamespace(service=SimpleNamespace(address=("fd00::1", 1)))
    screen = ScreenDecoder(rsd, SimpleNamespace(), lambda: None)
    screen._rtcp_config(
        {"connection": {"streamConfig": {"SourcePort": 5000, "RemoteSSRC": 11, "LocalSSRC": 22}}}
    )
    return screen


def test_rtcp_packets_match_their_length_fields():
    report = receiver_report(11, 22, 70000)
    # RR (8 words) + SDES (3 words); each header's length field counts words minus one.
    assert len(report) == 44
    assert struct.unpack("!H", report[2:4])[0] == 7 and struct.unpack("!H", report[34:36])[0] == 2
    assert struct.unpack("!I", report[16:20])[0] == 70000
    assert picture_loss(11, 22) == struct.pack("!BBHII", 0x81, 0xCE, 2, 11, 22)


def test_rtcp_config_uses_phone_side_names():
    screen = decoder()
    assert screen.rtcp_dest == ("fd00::1", 5000)
    assert screen.ssrc == (11, 22)  # ours is the phone's RemoteSSRC
    screen._rtcp_config({"connection": {}})
    assert screen.rtcp_dest is None


def running(screen, decode):
    """Make ``screen`` look started without a phone: a live receive task, a decode thread and
    ``decode`` standing in for PyAV (returns the frame for an access unit, or None)."""
    screen.transport, screen.task = FakeTransport(), asyncio.create_task(asyncio.sleep(30))
    screen.executor = concurrent.futures.ThreadPoolExecutor(1)
    screen._decode = lambda decoder, unit, flush: (decoder, decode(unit))
    screen.decode_task = asyncio.create_task(screen._decode_loop())


async def stopped(screen):
    await screen.stop()


async def feed_later(screen, units, delay=0.05):
    await asyncio.sleep(delay)
    for unit, key in units:
        screen._queue(unit, key)


def test_idle_stream_queues_nothing():
    screen = decoder()
    screen._queue(b"key", key=True)
    screen._queue(b"delta", key=False)
    assert not screen.pending and not screen.keyed


def test_armed_queue_waits_for_a_keyframe_drops_the_backlog_and_is_bounded(monkeypatch):
    screen = decoder()
    screen.wanted = 1
    screen._queue(b"delta", key=False)
    assert not screen.pending  # nothing to apply a delta to yet
    screen._queue(b"key", key=True)
    screen._queue(b"d1", key=False)
    screen._queue(b"d2", key=False)
    assert [unit for _, unit, _ in screen.pending] == [b"key", b"d1", b"d2"]
    screen._queue(b"key2", key=True)
    assert [unit for _, unit, _ in screen.pending] == [b"key2"] and screen.pending_bytes == 4
    monkeypatch.setattr(screen_stream, "PENDING_MAX_BYTES", 10)
    screen._queue(b"0123456789", key=False)  # over the bound: dropped, wait for a keyframe
    assert not screen.pending and not screen.keyed and screen.pending_bytes == 0


def test_capture_asks_for_a_keyframe_and_ignores_frames_from_before_the_request():
    async def run():
        screen = decoder()
        frames = {b"key-new": FakeFrame(64, 48)}
        running(screen, frames.get)
        # A keyframe decoded before this request cannot answer it.
        screen.keyframe = (FakeFrame(64, 48), time.monotonic() - 1)
        landing = asyncio.create_task(feed_later(screen, [(b"key-new", True)]))
        image, age, native = await screen.capture(1.0)
        await landing
        assert screen.transport.sent[0] == (picture_loss(11, 22), "fd00::1", 5000)
        assert (image.size, native) == ((64, 48), (64, 48)) and 0.04 <= age < 0.5
        # Back to idle: nothing queued, the next request starts from scratch.
        assert screen.wanted == 0 and not screen.pending and screen.keyframe is None
        screen._queue(b"key-new", key=True)
        assert not screen.pending
        await stopped(screen)

    asyncio.run(run())


def test_a_delta_before_any_keyframe_picture_or_an_empty_decode_answers_nothing():
    async def run():
        screen = decoder()
        # The decoder outputs nothing for the first keyframe (still buffering), then a picture.
        frames = {b"key1": None, b"d1": FakeFrame(64, 48), b"key2": FakeFrame(32, 24)}
        running(screen, frames.get)
        landing = asyncio.create_task(feed_later(screen, [(b"key1", True), (b"d1", False)]))
        assert await screen.capture(0.3) is None
        await landing
        landing = asyncio.create_task(feed_later(screen, [(b"key2", True)]))
        image, _, _ = await screen.capture(1.0)
        await landing
        assert image.size == (32, 24)
        await stopped(screen)

    asyncio.run(run())


def test_capture_gives_up_when_no_keyframe_arrives_or_the_stream_is_down():
    async def run():
        screen = decoder()
        assert await screen.capture(1.0) is None  # not started
        running(screen, lambda unit: FakeFrame(64, 48))
        started = time.monotonic()
        assert await screen.capture(0.4) is None
        assert time.monotonic() - started < 0.6
        # One request every KEYFRAME_RETRY_SECONDS, not a burst.
        assert len(screen.transport.sent) == 2
        await stopped(screen)

    asyncio.run(run())


def test_stop_waits_for_a_decode_running_in_its_thread():
    async def run():
        screen = decoder()
        done = []

        def slow(unit):
            time.sleep(0.2)
            done.append(unit)
            return FakeFrame(64, 48)

        running(screen, slow)
        screen.wanted = 1
        screen._queue(b"key", key=True)
        await asyncio.sleep(0.05)  # the decode is now running in its thread
        await screen.stop()
        assert done == [b"key"] and screen.keyframe is None and screen.decoder is None

    asyncio.run(run())


def test_render_crops_the_padding_and_scales_to_fit_without_upscaling():
    screen = decoder()
    screen.set_content(60, 40)
    frame = FakeFrame(64, 48)
    image, native = screen._render(frame, 30)
    assert (image.mode, image.size, native) == ("RGB", (30, 20), (60, 40))
    assert frame.reformats[-1] == (32, 24, "rgb24", "BILINEAR")  # the padded frame, same scale
    image, native = screen._render(frame, 1536)
    assert (image.size, native) == ((60, 40), (60, 40))
    screen.content = None
    image, native = screen._render(frame, None)
    assert (image.size, native) == ((64, 48), (64, 48))


def rtp(seq, nal, marker=True):
    return bytes([0x80, (0x80 if marker else 0) | 96]) + seq.to_bytes(2, "big") + bytes(8) + nal


class PacketTransport(FakeTransport):
    def __init__(self, packets):
        super().__init__()
        self.packets = list(packets)

    async def recv(self, size):
        if not self.packets:
            await asyncio.sleep(30)
        return self.packets.pop(0)


def test_receiver_drops_damaged_units_and_adds_parameter_sets_to_a_bare_keyframe():
    pytest.importorskip("pymobiledevice3")

    async def run():
        screen = decoder()
        screen.wanted = 1
        vps, sps, pps = bytes([32 << 1, 1, 0xAA]), bytes([33 << 1, 1, 0xBB]), bytes([34 << 1, 1, 0xCC])
        idr, idr2 = bytes([19 << 1, 1, 0x01]), bytes([19 << 1, 1, 0x03])
        packets = [
            rtp(1, vps, marker=False),
            rtp(2, sps, marker=False),
            rtp(3, pps, marker=False),
            rtp(4, idr),  # complete: queued
            rtp(6, idr),  # packet 5 lost: dropped, and the deltas after it with it
            rtp(7, bytes([1 << 1, 1, 0x02])),
            rtp(8, idr2),  # a bare keyframe: the cached parameter sets go in front
        ]
        screen.transport = PacketTransport(packets)
        queued = []
        screen._queue = lambda unit, key: queued.append((unit, key))
        screen.keyed = True  # as if the first keyframe were queued; the damage must reset it
        task = asyncio.create_task(screen._run())
        await asyncio.sleep(0.05)
        task.cancel()
        start = b"\x00\x00\x00\x01"
        params = start + vps + start + sps + start + pps
        # Packet 6 never reaches the queue; the delta after it does, and the queue drops it
        # because the damage reset its reference chain.
        assert queued == [
            (params + start + idr, True),
            (start + bytes([1 << 1, 1, 0x02]), False),
            (params + start + idr2, True),
        ]
        assert not screen.keyed  # _broken ran
        assert screen.highest_seq == 8 and screen.parameters == {32: vps, 33: sps, 34: pps}

    asyncio.run(run())
