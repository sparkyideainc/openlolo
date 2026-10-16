# Plan: hardware HEVC decode for the screen stream

Status: draft 2026-10-16. Base: `jingerpie/touch-accuracy` (PR #11, the decode queue and keyframe
catch-up). No code has changed for this plan.

## 1. Where we are

The worker decodes the phone's H.265 mirroring stream with PyAV in software (`ScreenDecoder._decode`
in `coredevice/screen_stream.py`). After PR #11 the receive loop only queues access units, a decode
task drains the queue, a keyframe drops everything queued before it, and `catch_up` asks the phone for
a keyframe (RTCP PLI) before each screenshot. That makes screenshots fresh, but the board still spends
its CPU decoding frames nobody looks at.

### 1.1 Measured on the Radxa ZERO 3W (2026-10-16)

Same 8.1 s clip from the phone: 486 access units at 60 fps, 5 keyframes, 1184x2576, 892 KB.

| Decoder | Per frame | Max fps | CPU for the clip |
|---|---|---|---|
| PyAV software, `thread_count=2`, standalone | 23.5 ms | 43 | 23 s (about 2 cores) |
| PyAV software inside the worker (earlier bench) | ~32 ms | ~30 | n/a |
| GStreamer `v4l2slh265dec` on rkvdec | ~5 ms (2.5 s wall incl. startup) | ~190 | 0.74 s |

The hardware frame shows the same content as the software frame. A ~9/255 brightness offset in the
comparison came from converting NV12 to RGB with the wrong colour range, not from the decoder.

### 1.2 Facts that shape the design

- Kernel is mainline Armbian `6.18.44-current-rockchip64`. There is no `/dev/mpp_service`, so
  Rockchip MPP, `hevc_rkmpp` and ffmpeg-rockchip need the vendor kernel and are out.
- rkvdec is `/dev/video1` (`/dev/video-dec1`, media device `/dev/media-dec1`). It is a V4L2
  stateless decoder: `S265` (HEVC parsed slice data) in, `NV12` out.
- PyAV 18.1.0 (libavcodec 62.28) offers only `hevc_v4l2m2m`, which is the stateful API and cannot
  drive rkvdec. FFmpeg's `v4l2request` hwaccel is not in the build we ship.
- GStreamer 1.26.2 on Debian trixie ships `v4l2slh265dec` (`gstreamer1.0-plugins-bad`). It decodes
  the full 1184x2576 frame; buffers come out padded (6,099,968 bytes for 4,574,976 bytes of picture),
  so frames must be read through their video meta (strides and plane offsets), not as flat bytes.
- The worker venv uses the system interpreter (`/usr/bin/python3.13`,
  `include-system-site-packages = false`). Debian has `python3-gi` 3.50 and
  `gir1.2-gst-plugins-base-1.0` for that interpreter.
- `openlolo-device.service` runs as `openlolo-device` (groups: `openlolo` only), with
  `ProtectSystem=strict`, `DeviceAllow=/dev/net/tun rw` and `MemoryMax=1G`. As written, the unit
  cannot open `/dev/video*` or `/dev/media*`.
- The phone has no frame-rate setting in its stream offer, so lowering the stream rate is not an
  alternative.

## 2. Design

### 2.1 A decoder backend behind `ScreenDecoder`

`ScreenDecoder` keeps the queue, keyframe handling, RTCP and `catch_up` unchanged. Only the decode
step moves behind a small interface:

```python
class FrameBackend(Protocol):
    async def start(self) -> None: ...
    async def decode(self, access_unit: bytes, seq: int) -> bool: ...  # True once seq is decoded
    async def latest(self) -> Image.Image | None: ...                  # newest decoded frame
    async def close(self) -> None: ...
```

- `SoftwareBackend` is today's PyAV code, moved, no behaviour change.
- `V4l2Backend` drives a GStreamer helper process (2.2).

`_decode_loop` calls `backend.decode` instead of running `_decode` in the executor, and `grab` calls
`backend.latest`. `latest_arrival` and `key_decoded` keep their meaning, so `frame_age` and
`catch_up` need no change.

### 2.2 The helper process, not GStreamer in the worker

The helper is `openlolo/coredevice/hevc_helper.py`, run with `/usr/bin/python3` (system `gi`, not
the venv). Pipeline:

```
appsrc caps=video/x-h265,stream-format=byte-stream,alignment=au is-live=true format=time
  ! h265parse ! v4l2slh265dec ! appsink max-buffers=1 drop=true sync=false emit-signals=true
```

Why a subprocess:
- A wedged ioctl in a kernel driver blocks the calling thread. In a subprocess the worker can kill
  it; in-process it would hang the event loop's executor and possibly the worker.
- No `gi` and GStreamer in the worker's import graph, and the venv stays isolated
  (`include-system-site-packages` stays false).
- The memory of the decoder is accounted to the same unit, but a leak is reclaimed on restart.

Protocol over stdin and stdout, little-endian, length-prefixed:

| Direction | Message | Body |
|---|---|---|
| worker to helper | `AU` | `u32 seq`, `u32 len`, Annex-B access unit |
| worker to helper | `GRAB` | `u32 request id` |
| helper to worker | `READY` | `u16 width`, `u16 height` after caps negotiate (first frame) |
| helper to worker | `DONE` | `u32 seq` of each decoded frame (the AU's seq carried as PTS) |
| helper to worker | `FRAME` | `u32 request id`, `u32 seq`, `u16 width`, `u16 height`, `u8 full_range`, tight NV12 (`w*h*3/2` bytes) |
| helper to worker | `ERROR` | `u16 len`, UTF-8 text |

- Bounds: an `AU` over 4 MiB is refused by the worker before sending; a `FRAME` over 16 MiB, an
  `ERROR` over 1 KiB or an unknown type is a protocol violation that kills the helper.
- The helper copies planes through the GstVideo frame map, so strides and padding never cross the
  pipe. It crops to the content size only in the worker, as today.
- The colour range comes from the negotiated caps (`colorimetry`), so the worker converts NV12 to
  RGB with the right range. This fixes the 9/255 offset seen in the bench.
- The worker never has more than 4 `AU` messages in flight (sent and not `DONE`). The backlog
  stays in `ScreenDecoder.pending`, where a keyframe can still drop it.

### 2.3 Guards

- **Start:** the helper must send `READY` within 3 s of the first keyframe it receives, or it is killed.
- **Grab:** a `GRAB` has 1 s for its `FRAME`. A late `FRAME` whose request id is not the one
  waiting is discarded.
- **Generation:** each helper instance has a generation number. Replies are read by a task bound to
  one generation, so a reply from a killed helper never lands in a new stream.
- **Cancellation:** `ScreenDecoder.stop()` closes the helper's stdin, waits up to 1 s, then kills
  it and awaits exit. A cancelled `catch_up` or `grab` leaves the helper running; only `stop()` and
  a protocol failure end it.
- **Failure:** EOF, a timeout, a protocol violation or `ERROR` kills the helper and switches that
  `ScreenDecoder` to `SoftwareBackend` at the next keyframe (the queue already starts deltas only
  after a keyframe). Three helper failures within 10 minutes turn hardware decode off until the
  worker restarts, logged once at warning level.

### 2.4 Selection

New `Config` field, `stream_decoder: Literal["auto", "software", "v4l2"] = "auto"`.

`auto` picks `v4l2` when all of these hold, checked once per worker process:
1. A `/dev/video*` node with `VIDIOC_ENUM_FMT` offering `S265` on its output queue.
2. The helper's dependency probe (`/usr/bin/python3 hevc_helper.py --probe`) finds `gi`,
   `Gst 1.0`, `h265parse` and `v4l2slh265dec` within 5 s.

Otherwise it picks `software`. `v4l2` forces the helper and reports an error if the probe fails.
The choice and its reason are logged once and shown in `openlolo_status` capabilities as the
screen source (`hevc decode: v4l2 rkvdec` or `hevc decode: software`).

### 2.5 Deploy

- **`deploy/os-packages.txt`:** add `python3-gi`, `gir1.2-gstreamer-1.0`,
  `gir1.2-gst-plugins-base-1.0`, `gstreamer1.0-plugins-base` and `gstreamer1.0-plugins-bad`
  (`--no-install-recommends`; measure the added size on the Radxa image).
- **`openlolo-device.service`:** add `SupplementaryGroups=video`,
  `DeviceAllow=char-video4linux rw` and `DeviceAllow=char-media rw`. Check whether
  `/dev/dma_heap` is needed with system-memory output; add it only if it is.
- **`MemoryMax=1G`:** stays. Measure the helper's RSS; target below 150 MB.

## 3. Steps

Each step is one commit; steps 1-4 are one PR.

0. **Baseline.** On the Radxa with PR #11 installed, record the screenshot `request_seconds`, worker
   CPU during a 10 s 60 fps burst, and SoC temperature after 5 minutes of bursts.
1. **Backend interface.** Move the PyAV code into `SoftwareBackend`. No behaviour change; all tests
   pass unchanged.
2. **Helper and `V4l2Backend`.** Write the protocol and guards. Add unit tests against a fake helper
   script for decode, grab, timeout, EOF mid-frame, oversize message, stale generation, stop during
   grab, and fallback to software at the next keyframe.
3. **Selection and status.** Add the `stream_decoder` config field, the probe, and the status text.
4. **Deploy changes.** Update the packages and the unit, then reinstall the Radxa from the branch.
5. **Bench on the Radxa** against section 4.
6. **Pi 4 and Pi 3B.** Run the probe and the clip benchmark there. The Pi 4's HEVC block is a
   stateless V4L2 decoder (`rpi-hevc-dec`) on recent Raspberry Pi kernels and may work through the
   same `v4l2slh265dec`; unverified. If it fails, `auto` must pick software and nothing else changes.

## 4. Acceptance

Measured on the Radxa ZERO 3W with the iPhone 13 (iOS 27.0.1) over Wi-Fi unless stated.

1. **Clip decode:** the helper decodes all 486 units of the bench clip, averaging at most 8 ms per
   frame, using at most 10% of the software decode's CPU time.
2. **Picture match:** the last frame from hardware and software, both converted to RGB by the
   worker, differ by at most 2/255 mean absolute per channel.
3. **No backlog:** during a 10 s 60 fps burst, `pending` never holds more than 3 units, and 20
   screenshots taken during motion finish `catch_up` with an empty queue, without a keyframe
   request, in at least 18 of 20.
4. **Faster screenshots:** stream screenshot `request_seconds` p50 during motion is at least
   100 ms below the step 0 baseline.
5. **Lower load:** worker plus helper CPU during the burst is at most a third of the step 0
   baseline, and the SoC is at least 5 C cooler after 5 minutes.
6. **Fallback:** killing the helper during a burst gives a successful next screenshot (software)
   within `stream_start_seconds`, with no worker restart. Three kills in 10 minutes turn hardware
   decode off with one warning.
7. **Endurance:** one hour of a screenshot every 5 s with bursts in between causes no helper
   restart, and the helper's RSS grows by less than 20 MB.
8. **Other boards:** the Pi 3B (and the Pi 4, if reachable) select a working backend, and the unit
   and integration suites pass on the Mac.

## 5. Risks

- **Kernel driver maturity:** mainline rkvdec HEVC is recent. A kernel update could regress it, and
  some streams may decode with corruption that no error reports. Acceptance 2 checks one clip;
  endurance (7) should include a picture comparison every 10 minutes.
- **Package size:** GStreamer adds packages to every image, including boards that will use software
  decode.
- **Pi 4:** may need a different element or kernel module, or may not work at all.

Out of scope: decode on demand (decoding only around screenshots). With hardware decode it no
longer pays for itself; revisit only if a board without a hardware decoder needs it.
