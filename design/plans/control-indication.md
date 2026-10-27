# Control indication: screen-recording indicator for the whole session, and a notice when control starts

**Status: PR 1 implemented 2026-10-27 on `jingerpie/control-inidication` (owner said proceed), after a Codex review and owner feedback the same day (notification skipped; indicator held for the lease; lease rules unchanged). Unit, contract and integration tests pass (408 + 1 skipped without the device extra). Not yet run on hardware: the bench box was offline when the code was written. PR 2 not started.**

The owner asked for two things while the phone is being controlled:

1. The iOS screen-recording indicator (top-left status bar) stays on for the whole control session, not only around input.
2. A notification when a control session starts.

The owner also pointed out that a stream held open for the session gives a warm screenshot path, and that decoding is needed only when a screenshot is requested.

Owner decisions 2026-10-27:

- Skip the notification for now.
- The indicator is on for the whole session while an AI controls the phone, including while it thinks between steps. As long as there is a session, the indicator is on.

Delivery: PR 1 (stream held for the lease, decode on request) ships first. PR 2 (box page "controlled" state) is small.

## Today (main 9d5a7c3)

Paths are under `apps/device/src/openlolo/` unless spelled out.

The indicator is shown by iOS while a DisplayService media stream the box started is open. The box cannot change its color or position. Input can never happen without it: touch and keyboard are authenticated only while the stream is alive (`apps/device/README.md:155`).

### Process boundary

MCP, the SDK and the box page call the application core (`Phone`, `Coordinator`) in the API process. Hardware stays in a separate worker: on hardware `bootstrap.py:34` builds `CoreDeviceClient`, which talks RPC over `runtime_dir/device.sock` (`adapters/device/coredevice.py:9`). The worker owns `CoreDeviceManager` (`coredevice/worker.py:53`) and checks the persisted binding for `BOUND_OPS` (`worker.py:20`). An RPC that times out on the client side keeps running in the worker (`workers/protocol.py`). So the lease lives in one process and the stream in another.

### Stream and decode

| Item | Where | Today |
|---|---|---|
| Open | `ScreenDecoder.start` (`coredevice/screen_stream.py:106`) via `_ensure_stream_locked` (`coredevice/session.py:245`) | On demand: first touch/keyboard `begin` (`coredevice/manager.py:738`) or a streamed screenshot (`session.py:263`). Button-only `begin` does not open it (`manager.py:735`). |
| Idle close | `idle_check` (`session.py:493`) polled by `_hid_idle` (`manager.py:638`) every 5 s | Closes stream and HID services once `hid_used` is more than `hid_idle_seconds` = 20 s old (`config.py:51`). `hid_used` is shared by input and screenshots. In practice the indicator goes off 20-25 s after the last use. The check reads state before taking `hid_lock`. |
| Other closes | `_hid_call` timeout (`session.py:515`), stream-screenshot miss (`session.py:186`), session teardown (`manager.py:243`) | All stop the decoder too. |
| HID refresh | `_ensure_hid_stream` (`session.py:388`) | On the next ensure after `hid_refresh_seconds` = 15 s (`config.py:52`), touch/keyboard/Indigo services are recycled; the decoder stream stays. |
| Teardown | `ScreenDecoder._teardown` (`screen_stream.py:224`) | One suppressed `stop_all_streams`, no check. The legacy `_stop_stream` (`session.py:466`) polls for zero sessions up to 3 times, because a screenshot racing an abandoned stream wedges the capture daemon until a phone reboot (`session.py:467`). The decoder path does not use it. |
| Decode | `_queue` / `_decode_loop` (`screen_stream.py:268`, `:288`) | Every queued access unit is decoded in order; a keyframe drops the queue. Software HEVC on the Radxa cannot keep up with 60 fps bursts (`screen_stream.py:8`). The worker thread writes `self.latest` directly (`:277`). A decode that yields no frame still advances `latest_arrival` / `key_decoded` (`:302`). |
| Freshness | `frame_age` (`screen_stream.py:91`), `catch_up` (`:159`) | Queue empty means "current" (age 0). PLI is sent only when behind. `_stream_screenshot` ignores the `catch_up` result (`session.py:272`). |
| RTCP | `_report` (`screen_stream.py:262`) | Separate task, receiver report about every 1 s once a packet arrived. `running` checks only the receive task (`:87`). |
| Warm screenshot | `_stream_screenshot` (`session.py:260`) | About 0.1 s when caught up, plus a keyframe round trip when behind. |
| Cold screenshot | same | Content size lookup, DisplayService connect and stream start (10 s timeouts each), then up to `stream_start_seconds` = 3 s (`config.py:60`) for a first frame, then up to 3 s more in `catch_up`. On a miss: stop and the ~2.8 s PNG capture (`config.py:56`). |

### Lease

| Item | Where | Today |
|---|---|---|
| Lease | `Coordinator` (`application/coordinator.py:29`) | `acquire` (`:116`) needs no binding and no connected phone. Deadline is `lease_seconds` = 90 s (`config.py:26`) after the last accepted interaction; queued or running work under the lease holds it. `changed` is set on acquire and drop, not on touch. |
| Drop | `_drop` (`coordinator.py:151`) | Clears `self.lease`, then `stop_input` → `release()` → `device.release_all` (1 s bound). |
| Pause | `pause` (`coordinator.py:168`) | Not under `control_lock`. Keeps the lease; the lease still expires. |
| Shutdown | `Coordinator.close` (`coordinator.py:296`) | Pauses and cancels tasks. Does not drop the lease. |
| Lease-less screenshot | `Phone.screenshot` (`application/phone.py:73`) | Through `coordinator.observe`, no lease needed, so a screenshot alone turns the indicator on for 20-25 s. |
| Auto sleep | `_box_idle` (`application/autooff.py:237`) | A live lease already blocks sleep. |

### Session replacement

`_supervise` (`manager.py:459`) builds a new `DeviceSession` per connect and tears the old one down on loss or a planned Wi-Fi → USB switch (`manager.py:243`). The switch waits for no input generation and a free `io_lock` (`manager.py:626`), not for `hid_lock` or a lease.

### Box page

HTTP SPA on port 80 (`config.py:87`, `interfaces/cli.py:36`). A manifest exists (`apps/portal/public/manifest.webmanifest`). No service worker, no push. Owner admission is the paired-phone LAN address match or setup Wi-Fi with the cabled phone (`interfaces/setup_portal.py:160`, `:191`); after that, a persisted HttpOnly cookie session (`setup_portal.py:71`). The LAN TLS listener and `public_url` serve the MCP gateway, not the box page.

Consequence: inside one 90 s lease the indicator goes off whenever the agent pauses for more than 20-25 s, and the next screenshot pays the cold start.

## Decisions (proposed)

### What a session is

MCP is stateless HTTP (`interfaces/mcp_http.py:959`), so the box has no MCP connection to follow, and an AI sends nothing while its model thinks. The only session the box knows is the control lease, and its rules stay as today:

- Starts with `openlolo_control acquire` or the client's first action (`LocalPhone.ensure_control`, `interfaces/local_phone.py:213`).
- Renewed by every action or screenshot from the holder (`coordinator.touch`, `accept`). Nothing else renews it.
- Ends `lease_seconds` = 90 s after the holder's last call, on `openlolo_control release`, or when the owner revokes the client on the box page (`interfaces/mcp_http.py:607`).

"Indicator on for the whole session" therefore means "stream held while the lease is live". Thinking time is covered as long as the gap between two calls stays under 90 s. Success criterion 10 measures real gaps; the default changes only if they run past it.

| Item | Decision |
|---|---|
| Before the lease | An AI that screenshots before its first action has no lease yet. That screenshot already opens the stream, so the indicator shows for 20-25 s (today's warm window), and the first action then creates the lease. No change. |
| Pause | Keeps the indicator on (the session still exists). |
| Lease-less screenshots | Keep today's warm window (box page views and an AI's look before its first action). |

### PR 1: stream held while control is held

#### Ownership across the process boundary

| Item | Decision |
|---|---|
| Hold record | The worker keeps one desired hold: `hold_id` (random correlation ID, never the lease token) and `expires` (monotonic). It applies to the bound phone; `disconnect`, `unbind` and worker close clear it. New RPC ops `hold_stream {hold_id, ttl}` (create or renew, idempotent, `ttl` 1-120 s) and `release_stream {hold_id}` (idempotent; unknown ID is a no-op, `null` releases any hold). Not `BOUND_OPS`: the coordinator has no binding, and a release must work whatever the binding. |
| Revocation first | `release_stream` and expiry clear the desired hold synchronously, before any await. A slow open that finishes later sees no desired hold and closes its stream. This works even when the client RPC has timed out. |
| API side | `Coordinator` gets a hold task. On lease create it calls `hold_stream` and then renews every 20 s while `live()`, with `ttl` = 45 s. On drop it calls `release_stream` with the old `hold_id` (saved before `self.lease` is cleared). Renewal failures are logged and retried at the next tick; they never fail the lease. |
| Crash and restart | API crash: the worker drops the hold 45 s after the last renewal. Worker restart: the next renewal recreates the hold. API restart: no lease exists, so no renewal; the old hold expires. |
| Shutdown | `Coordinator.close` drops the lease (calls `release_stream`) before cancelling tasks. |
| Pause | Keeps the hold (the lease is still held). Resume does not re-send `hold_stream` beyond the normal renewal. |
| Simulated backend | `SimulatedDevice` records `hold_stream` / `release_stream` calls separately from `release_all`. Per-action `release_all` (`application/actions.py:121`) and the start-up `release()` must not touch the hold. |

#### Stream lifecycle in the worker

| Item | Decision |
|---|---|
| One owner | A single reconcile task in `CoreDeviceManager` owns stream open/close. It wakes on hold changes, hold expiry, session publication, session teardown and stream loss (via `_pulse`). It compares desired (hold live and binding matches) with actual (decoder running on the current session) and acts. Nothing else opens a stream in the background. |
| Locks | Reconcile takes `io_lock`, then `session.hid_lock`, the same order as `capture` and `begin` (`manager.py:716`, `:736`). It never waits for a session while holding either lock. Because `capture` holds `io_lock` for the whole stop → PNG → done sequence, a reopen can never run between the stop and the PNG capture. |
| Session generation | Each published session gets a generation number. Reconcile records the generation it opened on; a stream on a session being torn down is never reused, and the old teardown finishes before a stream opens on the new session. The Wi-Fi → USB switch is unchanged (it may happen while a stream is held); reconcile reopens on the new session. |
| Transport loss | Reconcile does not retry DisplayService on a lost session. It waits for the manager to publish a new session. It never triggers a search (the search policy in `design/plans/auto-sleep.md` stays). |
| Retries | On open failure on a healthy session: retry after 2 s, 5 s, 10 s, then every 30 s while the hold lives. One attempt in flight at a time. `HID_UNAVAILABLE` (camera/mic in use) uses the same schedule. |
| Confirmed teardown | Decoder teardown uses the same stop-and-poll-for-zero-sessions loop as `_stop_stream` (moved to a shared helper). If zero sessions cannot be confirmed after 3 attempts, the PNG capture is refused with `CAPTURE_FAILED` ("The phone's screen stream did not stop; try again") instead of risking the wedge. |
| Cancellation | `ScreenDecoder.start` assigns `display` and `transport` to the instance as soon as they exist, so a cancelled or failed open is torn down by `stop()`. |
| Idle close | `idle_check` takes `hid_lock` before reading state. Separate clocks: `input_used` (touch, keyboard, buttons, Indigo) and `stream_used` (screenshots, input). HID services close after 20 s without input. The stream closes after 20 s without use only when no hold is live. Background receive, RTCP and reconcile never update either clock. |
| Input timeout | `_hid_call` timeout closes only the HID services. The stream closes only on its own failure. Service connect timeouts through `_call` keep today's session-loss handling (`session.py:73`). |
| PNG fallback | Kept as today. Under a hold, the indicator blinks off for that capture (accepted), then reconcile reopens after `capture` releases `io_lock`. A PNG reply never waits for the reopen. |
| `hold_stream_with_lease` | Config key, default `true`. `false` = no holds; decode stays on request. Bench baselines use it. |
| Status | `health().connection` gains `indication: "on" \| "opening" \| "off" \| "unavailable"` and `indication_error` (the last open error). `lease_status` does not carry it; PR 2 reads it from the connection status. |

#### Acquire contract

The lease is granted whether or not the stream opens: acquire never waits on the stream, and its latency stays as today. The indicator is best-effort while idle and guaranteed during touch and keyboard input, because input cannot authenticate without the stream. `indication` tells clients and the box page which case they are in. Acquiring while disconnected gives `indication: "off"`; reconcile opens when a session is published.

#### Decode on request

| Item | Decision |
|---|---|
| Idle | The receive loop keeps reading and depacketizing (needed for RTCP sequence numbers and parameter sets) but queues no access units while no request is armed. It caches the latest VPS/SPS/PPS (bounded, a few KiB). |
| Request | `request_frame(deadline)` arms a request with a new generation **before** sending PLI. The previous output is not valid for it. The decoder is flushed (`avcodec_flush_buffers` via PyAV; verify the installed PyAV wrapper) and the queue cleared. |
| Fulfilment | The request is fulfilled only by a frame the decoder actually output, in this decode epoch, from a keyframe that arrived after the request was armed (or a delta decoded on top of that keyframe, which is newer). A decode with zero output fulfils nothing. Cached parameter sets are fed before a keyframe that lacks them. |
| Keyframe types | IDR and CRA (19, 20, 21) as today, since the bench has decoded the phone's keyframes with that set. Bench check: log the NAL types the phone sends after a PLI; drop CRA if it decodes with artifacts. |
| Publication | Decoding runs on a one-thread executor per stream, which serializes decode, flush and dispose on one context. The thread returns frames; the event loop publishes them after checking the epoch. `stop()` waits up to 2 s for in-flight native work before freeing the context. |
| Fallback | `request_frame` returning no frame is decisive: `_stream_screenshot` returns `None` and today's PNG fallback runs. The cold path calls `request_frame` directly (no separate `wait_frame`). |
| After | Once the last armed request is fulfilled or cancelled, decoding stops, the queue is cleared and `keyed` reset. |
| Bounds | Armed queue: max 8 MiB of access units; overflow clears it and waits for the next keyframe. `fu_buffer`: max 2 MiB, dropped on a sequence gap. Access units with a sequence gap are dropped. |
| Age | `source_age_seconds` becomes time from PLI to keyframe arrival; `source_clock` says "pi arrival of requested keyframe". |
| Cost | Every screenshot now pays one keyframe round trip (about 0.1 s, 0.3 s on a retry), including on a still screen. In return, idle CPU drops to receive-only and stale-backlog screenshots go away. |

Not in scope: hardware decode (planned on `jingerpie/hw-decode`, not on main), changing `lease_seconds`, lease-less screenshot policy (open question 3).

### PR 2: box page shows control

The box page status shows "Controlled by <client name>" with the `indication` state while a lease is live. It uses existing `lease_status`; it needs the lease owner's display name added. No push. This is a notice only while the page is open.

### Notification

Skipped (owner decision 2026-10-27). The Codex review notes for a later plan: the box page is plain HTTP, so Web Push needs a new trusted HTTPS enrollment surface; a provider app (ntfy, Pushover) is the cheap alternative.

## Success criteria (PR 1, bench: Radxa ZERO 3W + iOS 27 phone)

Ground truth for the indicator is an external camera on the phone's status bar, not an on-phone recording (that would use the same media path). Time zero is the acquire or release RPC reaching the API.

1. **Indicator on**: for 20 acquires on a connected phone (empty lease, button-only lease, touch lease), indicator on within 2 s. Measured on camera.
2. **Indicator held**: with 60 s gaps between screenshots for 5 min (renewing the lease), the indicator never goes off, except during a PNG fallback, which is logged.
3. **Indicator off**: within 7 s of release or expiry (5 s confirmed teardown plus close), over 20 cycles. The worker log shows zero phone stream sessions.
4. **Fault cycles**, each 10 times, ending with the indicator off and no stream sessions within 50 s: release during open, release during PNG fallback, API kill (hold expiry), worker restart during a hold, Wi-Fi → USB switch during a hold, cable pull during a hold.
5. **Screenshot latency**: client request to image, 50 samples each, after a 30 s gap under a lease: median < 0.5 s, p95 < 1.0 s, fallback rate < 2%. Same build with `hold_stream_with_lease = false` as baseline. Report ordered samples.
6. **Freshness**: 50 tap → screenshot pairs on a known visual change (toggle a switch): 0 screenshots that miss the change.
7. **Box CPU and memory**, worker process plus tunnel, `pidstat` 60 s: held lease on a still screen < 5% of one core; during video playback (worst case) reported against the decode-all baseline; RSS steady (< 10 MiB growth) over 30 min of holds and screenshots.
8. **Phone battery and heat**: 30 min held lease (renewed) vs no lease, same brightness and content, 2 runs each. Reported for the owner to set a budget before merge.
9. **Unit and fake-worker tests**: hold create/renew/expire/release idempotency; release before a slow open finishes; old-session teardown vs new open; `idle_check` never closes a held stream; input timeout keeps the stream; reopen schedule and stop on hold loss; PNG fallback never overlaps an open; request fulfilled only by post-request keyframe output; zero-output decode; decode error recovery; queue and fragment bounds; `stop()` during native decode. One small recorded HEVC fixture checks the real decoder path without a phone.
10. **Gaps between calls**: from the journal and worker logs, the time between consecutive calls of one client over 10 real agent tasks (at least 2 clients, for example Claude Code and Claude Desktop). Report the distribution and how many gaps exceed 90 s, which is how many times the indicator would have gone off mid-task.

## Open questions

Resolved with defaults (owner unsure, 2026-10-27); change any of these at review:

1. Stream lifetime on a still screen: unknown. First bench step, before the reopen code: a 10 min hold on today's code with decode off. The reopen schedule above covers the case where iOS times it out.
2. Pause keeps the indicator on.
3. Lease-less screenshots (box page, or an AI's look before its first action) keep today's warm window.
4. Every screenshot pays one keyframe round trip (about 0.1 s). Success criterion 5 measures it. If the median misses 0.5 s, keep decoding for 2 s after each screenshot.
5. Lease length stays 90 s unless criterion 10 shows longer gaps.
