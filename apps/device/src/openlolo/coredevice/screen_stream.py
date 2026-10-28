"""Receive the phone's mirroring video and decode one frame of it when a screenshot asks.

The phone hardware-encodes an H.265 stream for screen mirroring; the same stream also gates
HID input authentication and makes the phone show its screen-recording indicator. While a
control lease is held the box keeps the stream open, so a screenshot needs no stream start and
the indicator stays on between actions.

Decoding every frame costs a core on a small board, and after a change the phone sends 60 a
second, more than software HEVC keeps up with. So nothing is decoded between screenshots: the
receiver reads and drops the video, keeping only RTP sequence numbers (for RTCP receiver reports)
and the latest parameter sets. A screenshot arms a request, asks the phone for a keyframe (RTCP
PLI), and decodes from the first keyframe that arrives after the request. Only a frame the
decoder actually output from that keyframe answers it; an older frame never does.
"""

import asyncio
import collections
import concurrent.futures
import contextlib
import logging
import struct
import time

from openlolo.coredevice.state import classify, transport_lost
from openlolo.domain.errors import OpenLoloError

logger = logging.getLogger(__name__)

# HEVC NAL unit types that carry a keyframe (IDR/CRA); a fresh one lets the decoder resync.
_KEY_NAL_TYPES = {19, 20, 21}
# VPS, SPS, PPS: kept from the stream so a keyframe that arrives without them still decodes.
_PARAMETER_NAL_TYPES = {32, 33, 34}
_PARAMETER_MAX_BYTES = 4096


# The phone answers most keyframe requests within ~0.1 s but ignores some; ask again this often.
# Fewer than a handful per screenshot: a sustained burst of requests can stall its encoder.
KEYFRAME_RETRY_SECONDS = 0.3
# Access units queued for the decoder while a request is armed; past this the queue is dropped
# and the request waits for the next keyframe.
PENDING_MAX_BYTES = 8 * 1024 * 1024
# A fragmented NAL unit larger than this is malformed or lost its end marker: dropped.
FRAGMENT_MAX_BYTES = 2 * 1024 * 1024
# How long stop() waits for a decode already running in its thread.
DECODE_JOIN_SECONDS = 2.0


def _nal_type(nal: bytes) -> int:
    return (nal[0] >> 1) & 0x3F if nal else -1


def receiver_report(ours: int, theirs: int, highest_seq: int) -> bytes:
    """RTCP Receiver Report plus an empty-CNAME SDES, the compound Xcode's mirror sends once a
    second (the stream config sets RTCPTimeoutEnabled)."""
    report = struct.pack("!BBHII4xIIII", 0x81, 0xC9, 7, ours, theirs, highest_seq & 0xFFFFFFFF, 0, 0, 0)
    return report + struct.pack("!BBHIBBBB", 0x81, 0xCA, 2, ours, 1, 0, 0, 0)


def picture_loss(ours: int, theirs: int) -> bytes:
    """RTCP Picture Loss Indication (RFC 4585): asks the phone for a keyframe."""
    return struct.pack("!BBHII", 0x81, 0xCE, 2, ours, theirs)


async def stream_sessions(rsd) -> int | None:
    """Number of media-stream sessions the phone still holds, or None when unknown."""
    from pymobiledevice3.remote.core_device.display_service import DisplayService

    try:
        async with DisplayService(rsd) as display:
            status = await asyncio.wait_for(display.get_media_stream_server_status(), 5)
    except asyncio.CancelledError:
        raise
    except Exception:
        return None
    sessions = status.get("sessions") if isinstance(status, dict) else None
    return len(sessions) if isinstance(sessions, list) else None


async def stop_streams(rsd) -> int | None:
    """Stop every media stream on a fresh connection and wait until the phone reports none.

    The screen-capture daemon wedges permanently (until a phone reboot) when a screenshot races
    a stream that was only abandoned, so a stop is confirmed, not assumed. Returns the number of
    sessions left: 0 when confirmed, None when the phone did not say."""
    from pymobiledevice3.remote.core_device.display_service import DisplayService

    started = time.monotonic()
    remaining = None
    for attempt in range(3):
        with contextlib.suppress(Exception):
            await asyncio.wait_for(DisplayService.stop_all_streams(rsd), 5)
        deadline, unknown = time.monotonic() + 5, 0
        while time.monotonic() < deadline:
            remaining = await stream_sessions(rsd)
            if remaining == 0:
                logger.info(
                    "media stream stopped in %.2fs (attempt %d)", time.monotonic() - started, attempt + 1
                )
                return 0
            if remaining is None:
                unknown += 1
                if unknown >= 2:
                    break  # The phone does not answer status requests; asking again will not help.
            await asyncio.sleep(0.25)
        if remaining is None:
            break
    logger.warning(
        "media stream still reported after %.1fs (%s left); screenshots may stall",
        time.monotonic() - started,
        "unknown" if remaining is None else remaining,
    )
    return remaining


class ScreenDecoder:
    """One H.265 mirroring stream, decoded only on request.

    ``start`` opens the media stream and its receive and RTCP tasks. ``capture`` asks for a
    keyframe and returns the screen as a PIL image cropped to the device's content size, or
    ``None`` when no keyframe arrived in time. A transport failure marks the session lost
    through ``on_lost`` so the manager reconnects; any end of the stream calls ``on_end``.
    """

    def __init__(self, rsd, config, on_lost, on_end=None):
        self.rsd, self.config, self.on_lost = rsd, config, on_lost
        self.on_end = on_end
        self.display = None
        self.transport = None
        self.task = None
        self.decoder = None
        self.decode_task = None
        self.report_task = None
        self.started_at = 0.0
        self.content: tuple[int, int] | None = None
        self.lost = False
        # Phone streams left after the last stop: 0 confirmed, None unknown.
        self.left: int | None = 0
        # (arrival time, access unit, is keyframe) waiting for the decoder; starts at a keyframe.
        self.pending: collections.deque[tuple[float, bytes, bool]] = collections.deque()
        self.pending_bytes = 0
        self.ready = asyncio.Event()
        # RTCP: the phone's report port and both SSRCs (ours, the phone's), from the stream answer.
        self.rtcp_dest: tuple[str, int] | None = None
        self.ssrc = (0, 0)
        self.highest_seq = 0
        self.keyed = False  # A keyframe has been queued, so deltas have something to apply to.
        # Armed screenshot requests; the receiver queues access units only while one is armed.
        self.wanted = 0
        # Bumped whenever decoding restarts; a decode that finishes in an older epoch is ignored.
        self.epoch = 0
        self.flush = False  # Reset the decoder's references before the next access unit.
        # The newest frame the decoder output in this epoch from a keyframe, and its arrival.
        self.keyframe: tuple[object, float] | None = None
        self.decoded = asyncio.Event()
        # VPS/SPS/PPS seen in the stream, by NAL type.
        self.parameters: dict[int, bytes] = {}
        self.executor: concurrent.futures.ThreadPoolExecutor | None = None
        self.native: concurrent.futures.Future | None = None
        self._lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def set_content(self, width: int, height: int) -> None:
        """The device's native portrait content size; decoded frames are padded larger and cropped
        to it."""
        self.content = (width, height)

    async def start(self) -> None:
        from pymobiledevice3.remote.core_device.display_service import DisplayService, is_media_in_use_error
        from pymobiledevice3.remote.core_device.screen_stream import open_media_receiver

        async with self._lock:
            if self.running:
                return
            # Published at once, so a failed or cancelled start is torn down by stop().
            self.display = display = DisplayService(self.rsd)
            try:
                await asyncio.wait_for(display.connect(), 10)
                transport, receiver_ip = open_media_receiver(display, (4 * 1024 * 1024, 1024 * 1024))
                self.transport = transport
                answer = await asyncio.wait_for(
                    display.start_video_stream(
                        receiver_ip=receiver_ip,
                        receiver_port=transport.port,
                        sender_ip=self.rsd.service.address[0],
                        display_id=1,
                    ),
                    10,
                )
            except BaseException as exc:
                await self._teardown_locked()
                if isinstance(exc, asyncio.CancelledError):
                    raise
                if isinstance(exc, TimeoutError):
                    raise OpenLoloError("HID_UNAVAILABLE", "The phone's media stream did not start") from None
                if isinstance(exc, OpenLoloError):
                    raise
                if is_media_in_use_error(exc):
                    raise OpenLoloError(
                        "HID_UNAVAILABLE", "Quit the app using the camera or microphone on the phone"
                    ) from None
                raise self._fail(exc) from None
            self.lost = False
            self._idle()
            self.started_at = time.monotonic()
            self.executor = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix="hevc")
            self._rtcp_config(answer)
            self.task = asyncio.create_task(self._run())
            self.decode_task = asyncio.create_task(self._decode_loop())
            if self.rtcp_dest is not None:
                self.report_task = asyncio.create_task(self._report())

    async def capture(self, wait: float, max_size: int | None = None):
        """The screen as it is now: ``(image, age, native size)``, or None when no keyframe was
        decoded within ``wait`` seconds. ``age`` is how long the phone took to send the keyframe
        after the request (local arrival; the phone reports no capture time)."""
        if not self.running:
            return None
        armed = time.monotonic()
        self.wanted += 1
        if self.wanted == 1:
            self._restart()
        try:
            frame = await self._await_keyframe(armed, wait)
        finally:
            self.wanted -= 1
            if not self.wanted:
                self._idle()
        if frame is None:
            return None
        image, arrival = frame
        rendered, native = await asyncio.get_running_loop().run_in_executor(
            None, self._render, image, max_size
        )
        return rendered, max(0.0, arrival - armed), native

    async def _await_keyframe(self, armed: float, wait: float):
        requested = 0.0
        while True:
            frame = self.keyframe
            if frame is not None and frame[1] >= armed:
                return frame
            now = time.monotonic()
            if now - armed >= wait or not self.running:
                return None
            if self.rtcp_dest is not None and now - requested >= KEYFRAME_RETRY_SECONDS:
                requested = now
                await self._send_rtcp(picture_loss(*self.ssrc))
            self.decoded.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.decoded.wait(), min(0.05, max(0.0, armed + wait - now)))

    def _restart(self) -> None:
        """Start decoding afresh: nothing queued or decoded before now can answer a request."""
        self.epoch += 1
        self.pending.clear()
        self.pending_bytes = 0
        self.keyed = False
        self.keyframe = None
        self.flush = True

    def _idle(self) -> None:
        """Stop decoding; the receiver drops everything until the next request."""
        self.epoch += 1
        self.pending.clear()
        self.pending_bytes = 0
        self.keyed = False
        self.keyframe = None

    def _render(self, frame, max_size: int | None):
        # Scale in swscale while converting from YUV: ~5x cheaper on the Radxa than converting at
        # full size and resizing in PIL. The decoded frame is padded past the content on the
        # right and bottom, so scale the whole frame by the content's factor and crop.
        width, height = self.content or (frame.width, frame.height)
        width, height = min(width, frame.width), min(height, frame.height)
        scale = min(1.0, max_size / max(width, height)) if max_size else 1.0
        size = (max(1, round(width * scale)), max(1, round(height * scale)))
        if scale < 1.0:
            rgb = frame.reformat(
                round(frame.width * scale), round(frame.height * scale), "rgb24", interpolation="BILINEAR"
            )
        else:
            rgb = frame.reformat(format="rgb24")
        return rgb.to_image().crop((0, 0, *size)), (width, height)

    async def stop(self) -> int | None:
        """Close the stream; returns the phone streams left (0 confirmed, None unknown)."""
        async with self._lock:
            tasks = [task for task in (self.task, self.decode_task, self.report_task) if task is not None]
            self.task = self.decode_task = self.report_task = None
            self._idle()
            for task in tasks:
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
            # A decode running in its thread keeps using the context; let it finish first.
            native = self.native
            if native is not None and not native.done():
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(asyncio.wrap_future(native), DECODE_JOIN_SECONDS)
            self.native = None
            executor, self.executor = self.executor, None
            if executor is not None:
                executor.shutdown(wait=False)
            self.decoder = None
            await self._teardown_locked()
            return self.left

    # ----- internals ---------------------------------------------------------------

    def _fail(self, exc: BaseException) -> OpenLoloError:
        if transport_lost(exc):
            self.lost = True
            self.on_lost()
        return classify(exc)

    async def _teardown_locked(self) -> None:
        display, self.display = self.display, None
        transport, self.transport = self.transport, None
        if transport is not None:
            with contextlib.suppress(Exception):
                transport.close()
        if display is not None and not self.lost:
            self.left = await stop_streams(self.rsd)
        if display is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(display.close(), 2)

    def _rtcp_config(self, answer) -> None:
        """Where RTCP goes, from the stream answer; its SSRC names are from the phone's side."""
        connection = answer.get("connection") if isinstance(answer, dict) else None
        config = connection.get("streamConfig") if isinstance(connection, dict) else None
        config = config if isinstance(config, dict) else {}
        port, ours, theirs = (int(config.get(key) or 0) for key in ("SourcePort", "RemoteSSRC", "LocalSSRC"))
        if port and ours and theirs:
            self.rtcp_dest, self.ssrc, self.highest_seq = (
                (self.rsd.service.address[0], port),
                (ours, theirs),
                0,
            )
        else:
            self.rtcp_dest = None
            logger.warning("screen stream answer has no RTCP config; no keyframe requests")

    async def _send_rtcp(self, packet: bytes) -> None:
        transport, dest = self.transport, self.rtcp_dest
        if transport is None or dest is None:
            return
        try:
            await transport.sendto(packet, *dest)
        except OSError as exc:
            logger.debug("rtcp send failed: %s", type(exc).__name__)

    async def _report(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            if self.highest_seq:
                await self._send_rtcp(receiver_report(*self.ssrc, self.highest_seq))

    def _queue(self, access_unit: bytes, key: bool) -> None:
        if not self.wanted:
            return  # Nothing asked for a frame: the video is dropped undecoded.
        if key:
            self.pending.clear()  # Everything queued is superseded; decoding resumes here.
            self.pending_bytes = 0
            self.keyed = True
        elif not self.keyed:
            return  # No keyframe yet: a delta has nothing to apply to.
        if self.pending_bytes + len(access_unit) > PENDING_MAX_BYTES:
            logger.debug("hevc queue over %d bytes; awaiting keyframe", PENDING_MAX_BYTES)
            self.pending.clear()
            self.pending_bytes = 0
            self.keyed = False
            return
        self.pending.append((time.monotonic(), access_unit, key))
        self.pending_bytes += len(access_unit)
        self.ready.set()

    def _broken(self) -> None:
        """An access unit was damaged: the deltas after it have nothing sound to apply to."""
        self.pending.clear()
        self.pending_bytes = 0
        self.keyed = False

    def _decode(self, decoder, access_unit: bytes, flush: bool):
        """Runs in the decode thread; returns the context and its last output frame, if any."""
        import av

        if decoder is not None and flush:
            reset = getattr(decoder, "flush_buffers", None)
            if reset is None:
                decoder = None
            else:
                reset()
        if decoder is None:
            # Not frame threads: they hold the last frames back until more input arrives, and a
            # still screen sends none.
            decoder = av.CodecContext.create("hevc", "r")
            decoder.thread_count = 2
        frame = None
        for frame in decoder.decode(av.Packet(access_unit)):
            pass
        return decoder, frame

    async def _decode_loop(self) -> None:
        while True:
            if not self.pending:
                self.ready.clear()
                await self.ready.wait()
                continue
            arrival, access_unit, key = self.pending.popleft()
            self.pending_bytes -= len(access_unit)
            epoch, flush, self.flush = self.epoch, self.flush, False
            executor = self.executor
            if executor is None:
                return
            self.native = executor.submit(self._decode, self.decoder, access_unit, flush)
            try:
                self.decoder, frame = await asyncio.wrap_future(self.native)
            except Exception:
                # A partial or out-of-order access unit; a later keyframe resyncs the decoder.
                logger.debug("hevc decode error; awaiting keyframe", exc_info=True)
                if epoch == self.epoch:
                    self._broken()
                    self.flush = True
                continue
            if epoch != self.epoch or frame is None:
                continue  # Decoding restarted meanwhile, or the decoder output nothing yet.
            if key or self.keyframe is not None:
                # A keyframe's picture, or a later delta on top of one, shows the screen at
                # ``arrival``; a delta before any keyframe output proves nothing.
                self.keyframe = (frame, arrival)
                self.decoded.set()

    def _ended(self) -> None:
        if self.on_end is not None:
            self.on_end()

    async def _run(self) -> None:
        from pymobiledevice3.remote.core_device.screen_stream import depacketize_hevc

        transport = self.transport
        if transport is None:
            return
        fu_buffer = bytearray()
        nals: list[bytes] = []
        damaged = False
        last_seq: int | None = None
        try:
            while True:
                try:
                    data = await transport.recv(65535)
                except asyncio.CancelledError:
                    raise
                except (OSError, Exception) as exc:
                    logger.warning("screen stream recv ended: %s", type(exc).__name__)
                    self._fail(exc)
                    self._ended()
                    return
                if len(data) < 12 or 64 <= (data[1] & 0x7F) <= 95:
                    continue  # too short, or RTCP
                marker = (data[1] >> 7) & 1
                # RTCP reports the extended (wrap-counting) highest sequence; late packets don't move it.
                seq = int.from_bytes(data[2:4], "big")
                ahead = (seq - self.highest_seq) & 0xFFFF
                if not self.highest_seq:
                    self.highest_seq = seq
                elif 0 < ahead < 0x8000:
                    self.highest_seq += ahead
                if last_seq is not None and seq != (last_seq + 1) & 0xFFFF:
                    damaged = True  # A lost or reordered packet: this access unit is incomplete.
                last_seq = seq
                header_len = 12 + (data[0] & 0x0F) * 4
                if data[0] & 0x10:  # RTP extension header
                    ext_words = int.from_bytes(data[header_len + 2 : header_len + 4], "big")
                    header_len += 4 + ext_words * 4
                depacketize_hevc(data[header_len:], fu_buffer, nals)
                if len(fu_buffer) > FRAGMENT_MAX_BYTES:
                    fu_buffer.clear()
                    damaged = True
                if not marker:
                    continue
                unit, nals = nals, []
                if damaged:
                    damaged = False
                    if self.wanted:
                        self._broken()
                    continue
                if not unit:
                    continue
                types = [_nal_type(nal) for nal in unit]
                for nal, kind in zip(unit, types):
                    if kind in _PARAMETER_NAL_TYPES and len(nal) <= _PARAMETER_MAX_BYTES:
                        self.parameters[kind] = nal
                if not self.wanted:
                    continue
                key = any(kind in _KEY_NAL_TYPES for kind in types)
                if key:
                    # A keyframe without its parameter sets still decodes after a flush.
                    missing = [
                        self.parameters[kind]
                        for kind in sorted(_PARAMETER_NAL_TYPES - set(types))
                        if kind in self.parameters
                    ]
                    unit = missing + unit
                self._queue(b"".join(b"\x00\x00\x00\x01" + nal for nal in unit), key)
        except asyncio.CancelledError:
            pass
