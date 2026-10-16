import asyncio
import struct
import time
from types import SimpleNamespace

from PIL import Image

from openlolo.coredevice.screen_stream import ScreenDecoder, picture_loss, receiver_report


class FakeTransport:
    def __init__(self):
        self.sent = []

    async def sendto(self, data, ip, port):
        self.sent.append((data, ip, port))


def decoder():
    rsd = SimpleNamespace(service=SimpleNamespace(address=("fd00::1", 1)))
    screen = ScreenDecoder(rsd, SimpleNamespace(stream_frame_ttl=1.0), lambda: None)
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


def test_queue_waits_for_a_keyframe_and_a_keyframe_drops_the_backlog():
    async def run():
        screen = decoder()
        screen._queue(b"delta", key=False)
        assert not screen.pending  # nothing to apply a delta to yet
        screen._queue(b"key", key=True)
        screen._queue(b"d1", key=False)
        screen._queue(b"d2", key=False)
        assert [unit for _, unit, _ in screen.pending] == [b"key", b"d1", b"d2"]
        screen._queue(b"key2", key=True)
        assert [unit for _, unit, _ in screen.pending] == [b"key2"]

    asyncio.run(run())


def test_frame_age_is_zero_once_caught_up():
    screen = decoder()
    assert screen.frame_age() == float("inf")
    screen.latest, screen.latest_arrival = object(), 1.0
    assert screen.frame_age() == 0.0  # a still screen sends nothing: the last frame is current
    screen.pending.append((2.0, b"d", False))
    assert screen.frame_age() > 0


def test_catch_up_asks_for_a_keyframe_while_behind_and_stops_when_one_is_decoded():
    async def run():
        screen = decoder()
        screen.transport, screen.task = FakeTransport(), asyncio.create_task(asyncio.sleep(5))
        screen.latest, screen.latest_arrival = object(), 0.0
        screen.pending.append((0.0, b"d", False))

        async def keyframe_lands():
            await asyncio.sleep(0.05)
            screen.pending.clear()
            screen.key_decoded = float("inf")  # a keyframe that arrived after the request

        landing = asyncio.create_task(keyframe_lands())
        assert await screen.catch_up(1.0)
        await landing
        assert screen.transport.sent[0] == (picture_loss(11, 22), "fd00::1", 5000)
        screen.pending.append((0.0, b"d", False))
        screen.key_decoded = 0.0
        assert not await screen.catch_up(0.05)  # nothing lands: report not caught up
        screen.task.cancel()

    asyncio.run(run())


def test_grab_reports_the_age_when_the_frame_was_picked():
    async def run():
        screen = decoder()
        frame = SimpleNamespace(to_image=lambda: Image.new("RGB", (4, 4)))
        screen.latest, screen.latest_arrival = frame, time.monotonic() - 0.2
        screen.pending.append((time.monotonic(), b"d", False))
        image, age = await screen.grab()
        assert image.size == (4, 4) and 0.2 <= age < 0.3
        screen.latest_arrival = time.monotonic() - 2.0  # past stream_frame_ttl
        assert await screen.grab() is None

    asyncio.run(run())
