# Lolo Pocket top button: pairing mode and reset mode

**Status: archived. PR 1 (`jingerpie/pisugar`) merged through [#13](https://github.com/sparkyideainc/openlolo/pull/13); PR 2 (`jingerpie/lolo-phone-switch`) was dropped. The modes and reset parts are superseded by [`../lolo-pocket-modes.md`](../lolo-pocket-modes.md). Design revised 2026-10-18 after the bench (see Decisions and Bench log): hold opens setup mode, press cancels it, factory reset is box-page only, status LED on the Radxa board shows the phone connection.** Supersedes the top-button rows of
`design/hardware/lolo-pocket-buttons.md`; that page is updated in PR 1.

## Decisions (final, 2026-10-18)

| Gesture on the PiSugar3 top button | Box state | Action |
|---|---|---|
| Hold, then release | Not in setup mode | **Setup mode** (pairing): own Wi-Fi on air, box page, cable Trust window, 10 min. |
| Hold, then release | Setup mode | Nothing. |
| Press | Setup mode | **Cancel setup mode.** |
| Press | Not in setup mode | One phone search (status LED blinks slowly while it looks). |
| Press or hold | Wi-Fi join in progress | Ignored. |
| Double press | Any | Ignored (event cleared). |

| Status LED (Radxa green LED) | Meaning |
|---|---|
| Fast blink, 2 Hz | Setup mode |
| Steady on | Phone connected |
| Slow blink, 1 Hz | Searching for the phone |
| Off | Not connected, not searching (sleep) |

- **Factory reset is a box page action only** (typed box name, unchanged). The button cannot do it: the PiSugar3 reports a hold only on release, as one "long" event with no length and no held-now bit (probe 2026-10-18: a 12 s and a 4 s hold arrived identically; the published I2C register map has no button timing or state register). So "hold 3 s = setup, hold 15 s = reset" is impossible on this button. Two-step button resets (hold-hold-hold, double-then-hold) were built or designed and dropped as error-prone or too hard to remember.
- A true older-AirPods behaviour (LED reacts at 5 s and 15 s while held) needs a button Linux reads directly: a tactile switch on a Radxa GPIO (proposed: header pin 11, GPIO3_A1, to GND pin 9, 10 kOhm pull-up). Not in this hardware revision. A PiSugar firmware that reports hold length would also do.
- In setup mode, a phone on the cable that is not the saved phone replaces it after Trust (PR 2). Switching keeps every AI client and grant; the old phone's pairing records are deleted after the new phone is bound.
- Raspberry Pi GPIO button: the 5 s hold opens setup mode; no press, so no cancel by button.
- PiSugar3 LEDs stay the battery gauge: on USB power the firmware's charge animation overwrites `0xE0` between writes.
- Scope: top button and status LED only. Power button, battery sampling and low-battery shutdown from the parked PR 2 (`jingerpie/lolo-pr2-pisugar-daemon`, 3699bac) stay parked.

## What exists on main (b68e134)

- API consumes two edge flags in `Provisioning.tick` (`application/provisioning.py:230-288`):
  - `/run/openlolo/setup-button` -> `enter("button")` when IDLE.
  - `/run/openlolo/pair-button` -> one `search()`.
  Both are consumed (unlinked) on read, so a restart never replays a press.
- Button daemon `deploy/openlolo-button.py` is GPIO only (BCM 17, 5 s hold). The unit has `ConditionPathExists=/dev/gpiochip0`, which the Radxa also has, so on the Lolo Pocket it polls a pin that is not wired.
- Factory reset: `POST /reset` (`interfaces/app_api.py:234`) checks the typed box name and runs `systemctl start --no-block openlolo-reset.service`; polkit already allows the API user to start that unit.
- Autopair (`application/autopair.py`) pairs only when unbound and in setup mode; a bound box raises `ALREADY_BOUND`.
- PiSugar3 I2C facts and the bus retry recipe: `design/hardware/pisugar3.md`. The parked PR 2 has a tested `Bus` class and tap handling (`deploy/openlolo_pisugar3.py`).

## Design

### Who decides what

The daemon stays dumb: it turns firmware events into flag files and mirrors one indicator file onto the LEDs. All mode logic lives in the API, where it is unit-tested with a fake clock and knows the setup state.

```
PiSugar3 0x08 --(daemon, 500 ms poll)--> /run/openlolo/pair-button   (single)
                                     --> /run/openlolo/setup-button  (long)
API Provisioning.tick ----------------> /run/openlolo/indicator     (idle | searching | connected | setup)
daemon (stat each tick) --------------> Radxa green LED (sysfs brightness)
```

### API state (PR 1)

- `Provisioning` consumes the two flags (setup flag before pair flag each tick):
  - Setup flag: IDLE -> `enter("button")`; SETUP_MODE -> ignored; JOINING / VERIFYING -> ignored.
  - Pair flag: SETUP_MODE -> `finish("button")`, unless the press is older than the hold that opened setup mode; JOINING / VERIFYING -> ignored; otherwise one `search()`.
- Button flags are checked every 200 ms (`BUTTON_POLL_SECONDS`); the Wi-Fi status check (nmcli) and the phone link check stay at about 1 s.
- Indicator: `Provisioning` writes `/run/openlolo/indicator` (one word, atomic rename) when it changes: `setup` while the setup window is open, else from the device health (`link`): `connected`, `searching` (`connection.searching`), or `idle`. An unreachable worker shows as `idle`.

### Daemon (PR 1)

New backend `pisugar3` in `deploy/openlolo-button.py`, picked by `OPENLOLO_BUTTON_BACKEND`, code in `deploy/openlolo_pisugar3.py`, trimmed from 3699bac:

- Keep: `Bus` (20 ms spacing, 3 retries 50 ms apart, unlock + read-back on write), stale-event clear at start, per-event 2 s debounce, offline detection with 30 s retry, `<N>` journal priorities.
- Drop: soft mode, cut delay, anti-mistaken-touch write, power-button hold logic, battery sampling, `power.json`, polkit power-off rule. The daemon must not write `0x02`, `0x03` or `0x09`: without the power-button code, soft mode on would leave the power button unable to turn the box off.
- Status LED: the Radxa board's `green:heartbeat` LED through sysfs. `setup` = 2 Hz; `searching` = 1 Hz; `connected` = steady on; `idle` = off. Blink timing is by time, not by poll count; the daemon polls `0x08` every 100 ms. `openlolo-board.sh` (root, at boot) turns the heartbeat trigger off and makes `brightness` writable for group `openlolo`. First version wrote PiSugar3 `0xE0`; dropped after the bench (2026-10-18): on USB power the firmware's charge animation overwrites the register between writes. LED output adds no I2C traffic.
- Systemd drop-in `openlolo-button.service.d/pisugar3.conf`, installed by `install.sh` only when `board = radxa-zero3w`: clears the GPIO condition and environment, sets the backend, `DeviceAllow=/dev/i2c-3 rw`, `SupplementaryGroups=i2c`, `RestrictAddressFamilies=AF_UNIX`.
- Exit 0 with a log line when `smbus2` is missing or the bus never answers (box on direct USB power: the PiSugar3 does not answer then). Keep retrying every 30 s in-process instead of a restart loop.

### Phone replacement in pairing mode (PR 2)

- `Autopair.tick`: when bound **and** setup mode is active **and** exactly one phone is on the cable **and** its UDID differs from the binding, start `_pair_and_replace`. Same UDID: state `bound`, nothing to do.
- `_pair_and_replace`: pair (Trust prompt on the new phone) -> verify the new phone is still the only one on the cable -> `phone.bind(new)` (already runs `release_all`, writes the binding, `record_owner` ends box-page sessions). -> forget the old phone (below). The worker only connects to the bound UDID.
- Kept on purpose: AI clients and grants.
- Forget the old phone, only after the new bind succeeded (a failed or cancelled switch keeps everything):
  - Worker records (user `openlolo-device`): new worker op `forget` -> `USBTransport.forget(old)` (lockdown cache `*.plist`) and `WifiTransport.forget(old)` (RemotePairing records + last-address entry). Both methods already exist in `coredevice/transports.py:115,180` and are unused. Device port gains `forget(udid)`; simulated adapter is a no-op.
  - usbmuxd record `/var/lib/lockdown/<old>.plist` (root): new template unit `openlolo-forget@.service` running `deploy/reset/openlolo-forget.sh %i`. The script accepts only a UDID (`^[0-9A-Fa-f-]{20,40}$`) and deletes only that file; `SystemConfiguration.plist` and other phones' records are untouched. Polkit: allow `start` of units matching `^openlolo-forget@[0-9A-Fa-f-]+\.service$`.
  - Cleanup failure does not undo the switch: logged, journal entry `SUCCEEDED` with `{"forgot_old": false}`; factory reset still clears leftovers.
  - Same UDID re-bound (switch back to old phone later): records are recreated by its Trust prompt.
- Trust denied on the new phone: the old binding stays; journal `FAILED`; same `denied` handling as today.
- The old phone stays saved until the new bind succeeds, so a cancelled replacement never leaves the box with no phone.

## PR split

1. **Top button + status LED** (daemon backend, drop-in, installer hook, board LED handover, API hold/press handling, indicator from setup state and phone connection, docs page update).
2. **Phone replacement in pairing mode** (autopair, worker `forget` op, `openlolo-forget@` unit + polkit).

PR 2 does not depend on the daemon; it can be tested with the box-page "setup mode" command. Developed in order, shipped together.

## Steps

0. **Bench check first (box offline at plan time; find it by MAC `cc:c8:37:3d:1b:7d`).**
   - Long-press timing and latency of `0x08 == 3` on current firmware (`0xE2`).
   - `0xE0`: do the per-tick rewrites hold a steady pattern against the firmware's own LED updates, and does the gauge come back when writes stop?
   - Confirm `0x08` long press is distinct from the power button's `0x02` bit 0.
   Record results in `design/hardware/pisugar3.md` before writing LED code.
1. PR 1 daemon: port `Bus` + tap handling + LED writer; unit tests with a fake smbus (stale clear, debounce, I2C failure, LED pattern per indicator, no writes to `0x02`/`0x03`/`0x09`).
2. PR 1 API: hold opens, press cancels, join ignores, stale press dropped, indicator follows setup state and phone link; tests with a fake clock.
3. PR 1 installer + drop-in; deploy to the Radxa bench.
4. PR 2 autopair replacement + tests (different UDID in setup mode, same UDID, outside setup mode ignored, trust denied keeps the old binding, phone unplugged mid-replace).
5. Bench pass, then docs and memory.

## Acceptance (bench, Radxa ZERO 3W + PiSugar3)

- Hold and release from normal -> status LED fast blink within 1 s of release; setup Wi-Fi visible on a phone within 15 s.
- Press in setup mode -> setup mode closes, LED back to the connection state; no phone search.
- Hold in setup mode -> nothing.
- Press outside setup mode -> LED slow blink while the search window is open; steady on when the phone connects; off when the window ends without a phone.
- Phone connected over cable or Wi-Fi -> LED steady on within about 1 s.
- Service restart with a press latched before start -> no action.
- Setup mode with a second iPhone on the cable -> Trust prompt on it; after Trust the box controls the new phone; box-page sessions end; AI clients still listed; no `<old>` file left in `/var/lib/openlolo/device/lockdown`, the RemotePairing dir or `/var/lib/lockdown`; old phone on the cable outside setup mode is ignored.
- Button daemon cost: under 1% of one core.
- Unit and portal test suites green.

## Resolved questions (2026-10-17)

- Phone switch keeps all AI clients.
- Old phone records deleted after a successful switch (worker op + root template unit).
- GPIO parity: the Pi hold opens setup mode through the same API logic; unit-tested only.
- Factory reset by button: dropped (2026-10-18), box page only.

## Bench log

2026-10-18, Radxa ZERO 3W + PiSugar3 firmware v1.3.4, build `20261018-ef13f65` (PR 1 + PR 2), reset unit replaced by a log-only stub, setup timeout 60 s.

- Physical presses: press, hold, hold registered; hold-to-LED latency 0.5-1.7 s; arm and arm timeout worked.
- A press 10.0 s after arming landed after the deadline: the timeout disarmed first, so the cancel path was not exercised. A confirming hold was not seen by the daemon (no log line; debounced events were not logged then).
- The PiSugar3 LEDs run a charge animation on USB power that overwrites `0xE0`.
- Changes after this pass: board LED instead of `0xE0`, 20 s arm window, debounced presses logged, disarm log names the cause.

2026-10-18, second pass (board LED, 20 s arm, 100 ms poll): all steps registered; cancel by press worked; confirm worked (stub). Event-to-API latency 0.2-1 s.

2026-10-18, probe with the button daemon stopped, polling `0x08` every ~50 ms and all of `0x00-0x3F` in rotation: holds of 12 s and 4 s each produced one `0x03` at release (8.9 s apart, matching release times); no register tracked the held button. Led to the final design above.
