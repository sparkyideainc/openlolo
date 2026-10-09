"""Decode the phone's mirroring video and serve its latest frame as a screenshot.

The phone hardware-encodes an H.265 stream for screen mirroring; the same stream also gates
HID input authentication. Draining and decoding it on the Pi turns a screenshot into "grab the
latest decoded frame" (~0.1s) instead of the ~2.8s discrete PNG capture, and one stream serves
both observation and input. All frame handling lives here; the rest of the worker sees an image.
"""

import asyncio
import contextlib
import logging
import time

from openlolo.coredevice.state import classify, transport_lost
from openlolo.domain.errors import OpenLoloError

logger = logging.getLogger(__name__)

# HEVC NAL unit types that carry a keyframe (IDR/CRA); a fresh one lets the decoder resync.
_KEY_NAL_TYPES = {19, 20, 21}


def _nal_type(nal: bytes) -> int:
    return (nal[0] >> 1) & 0x3F if nal else -1


class ScreenDecoder:
    """One H.265 mirroring stream, decoded to a single latest frame.

    ``start`` opens the media stream and a receive/decode task. ``grab`` returns the newest frame
    as a PIL image cropped to the device's content size, or ``None`` when no fresh frame exists.
    A transport failure marks the session lost through ``on_lost`` so the manager reconnects.
    """

    def __init__(self, rsd, config, on_lost):
        self.rsd, self.config, self.on_lost = rsd, config, on_lost
        self.display = None
        self.transport = None
        self.task = None
        self.decoder = None
        self.latest = None
        self.latest_ts = 0.0
        self.started_at = 0.0
        self.content: tuple[int, int] | None = None
        self.lost = False
        self._lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def frame_age(self) -> float:
        return time.monotonic() - self.latest_ts if self.latest is not None else float("inf")

    def set_content(self, width: int, height: int) -> None:
        """The device's native portrait content size; decoded frames are padded larger and cropped
        back to this so touch geometry matches the discrete-capture path exactly."""
        self.content = (width, height)

    async def start(self) -> None:
        from pymobiledevice3.remote.core_device.display_service import DisplayService, is_media_in_use_error
        from pymobiledevice3.remote.core_device.screen_stream import open_media_receiver

        async with self._lock:
            if self.running:
                return
            display = DisplayService(self.rsd)
            await asyncio.wait_for(display.connect(), 10)
            transport, receiver_ip = open_media_receiver(display, (4 * 1024 * 1024, 1024 * 1024))
            try:
                await asyncio.wait_for(
                    display.start_video_stream(
                        receiver_ip=receiver_ip,
                        receiver_port=transport.port,
                        sender_ip=self.rsd.service.address[0],
                        display_id=1,
                    ),
                    10,
                )
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                await self._teardown(display, transport)
                raise OpenLoloError("HID_UNAVAILABLE", "The phone's media stream did not start") from None
            except Exception as exc:
                await self._teardown(display, transport)
                if is_media_in_use_error(exc):
                    raise OpenLoloError(
                        "HID_UNAVAILABLE", "Quit the app using the camera or microphone on the phone"
                    ) from None
                raise self._fail(exc) from None
            self.display, self.transport = display, transport
            self.latest, self.latest_ts, self.lost = None, 0.0, False
            self.started_at = time.monotonic()
            self.task = asyncio.create_task(self._run())

    async def wait_frame(self, deadline: float) -> bool:
        end = time.monotonic() + deadline
        while time.monotonic() < end:
            if self.latest is not None:
                return True
            if not self.running:
                return False
            await asyncio.sleep(0.02)
        return self.latest is not None

    async def grab(self):
        """Latest decoded frame as a PIL image cropped to content size, or None if too stale."""
        frame = self.latest
        if frame is None or self.frame_age() > self.config.stream_frame_ttl:
            return None
        image = await asyncio.get_running_loop().run_in_executor(None, frame.to_image)
        if self.content is not None:
            width, height = self.content
            image = image.crop((0, 0, min(width, image.width), min(height, image.height)))
        return image

    async def stop(self) -> None:
        async with self._lock:
            task, self.task = self.task, None
            display, self.display = self.display, None
            transport, self.transport = self.transport, None
            self.latest = self.decoder = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        await self._teardown(display, transport)

    # ----- internals ---------------------------------------------------------------

    def _fail(self, exc: BaseException) -> OpenLoloError:
        if transport_lost(exc):
            self.lost = True
            self.on_lost()
        return classify(exc)

    async def _teardown(self, display, transport) -> None:
        if transport is not None:
            with contextlib.suppress(Exception):
                transport.close()
        if display is not None and not self.lost:
            from pymobiledevice3.remote.core_device.display_service import DisplayService

            with contextlib.suppress(Exception):
                await asyncio.wait_for(DisplayService.stop_all_streams(self.rsd), 5)
        if display is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(display.close(), 2)

    def _decode(self, access_unit: bytes) -> None:
        import av

        if self.decoder is None:
            self.decoder = av.CodecContext.create("hevc", "r")
            self.decoder.thread_count = 2
        for frame in self.decoder.decode(av.Packet(access_unit)):
            self.latest = frame
            self.latest_ts = time.monotonic()

    async def _run(self) -> None:
        from pymobiledevice3.remote.core_device.screen_stream import depacketize_hevc

        transport = self.transport
        if transport is None:
            return
        fu_buffer = bytearray()
        nals: list[bytes] = []
        loop = asyncio.get_running_loop()
        try:
            while True:
                try:
                    data = await transport.recv(65535)
                except asyncio.CancelledError:
                    raise
                except (OSError, Exception) as exc:
                    logger.warning("screen stream recv ended: %s", type(exc).__name__)
                    self._fail(exc)
                    return
                if len(data) < 12 or 64 <= (data[1] & 0x7F) <= 95:
                    continue  # too short, or RTCP
                marker = (data[1] >> 7) & 1
                header_len = 12 + (data[0] & 0x0F) * 4
                if data[0] & 0x10:  # RTP extension header
                    ext_words = int.from_bytes(data[header_len + 2 : header_len + 4], "big")
                    header_len += 4 + ext_words * 4
                depacketize_hevc(data[header_len:], fu_buffer, nals)
                if not marker or not nals:
                    continue
                access_unit = b"".join(b"\x00\x00\x00\x01" + nal for nal in nals)
                nals = []
                # Decoding is CPU-bound ffmpeg work; keep it off the event loop.
                try:
                    await loop.run_in_executor(None, self._decode, access_unit)
                except Exception:
                    # A partial or out-of-order access unit; wait for the next keyframe.
                    logger.debug("hevc decode error; awaiting keyframe", exc_info=True)
        except asyncio.CancelledError:
            pass
