# Lolo Pocket top button: pairing mode and reset mode

**Status: PR 1 implemented on `jingerpie/pisugar` (2026-10-17), not bench-tested; PR 2 in progress on `jingerpie/lolo-phone-switch`.** Supersedes the top-button rows of
`docs/hardware/lolo-pocket-buttons.md`; that page is updated in PR 1.

## Decisions (2026-10-17)

| Gesture on the PiSugar3 top button | Box state | Action |
|---|---|---|
| Single press | Any, nothing armed | One phone search for the saved phone (immediate attempt, backoff reset). |
| Long press | Not in setup mode | **Pairing mode** = setup mode: setup Wi-Fi, box page, cable trust window, 10 min. |
| Long press | In pairing mode | **Reset armed** for 10 s. LEDs flash fast. Nothing is erased yet. |
| Long press | Reset armed | **Reset confirmed**: factory reset (same unit as the box page reset). |
| Single press | Reset armed | Cancel the reset. No phone search. |
| (10 s pass) | Reset armed | Cancel the reset; stay in pairing mode. |
| Double press | Any | Ignored (event cleared). |

- In pairing mode, a phone on the cable that is not the saved phone replaces it after the owner taps Trust (Bluetooth-style). Outside pairing mode a different phone is still ignored.
- Switching phones keeps every AI client and grant. The old phone's pairing records are deleted after the new phone is bound, so the old phone needs a fresh Trust to come back.
- Raspberry Pi GPIO button gets the same arm/confirm for free: the logic is in the API, and each 5 s hold already touches the setup flag. No single press there, so a Pi reset is cancelled only by the 10 s timeout. No Pi button hardware to test; unit tests only.
- Scope: top button and LEDs only. Power button, battery sampling and low-battery shutdown from the parked PR 2 (`jingerpie/lolo-pr2-pisugar-daemon`, 3699bac) stay parked.

## What exists on main (b68e134)

- API consumes two edge flags in `Provisioning.tick` (`application/provisioning.py:230-288`):
  - `/run/openlolo/setup-button` -> `enter("button")` when IDLE.
  - `/run/openlolo/pair-button` -> one `search()`.
  Both are consumed (unlinked) on read, so a restart never replays a press.
- Button daemon `deploy/openlolo-button.py` is GPIO only (BCM 17, 5 s hold). The unit has `ConditionPathExists=/dev/gpiochip0`, which the Radxa also has, so on the Lolo Pocket it polls a pin that is not wired.
- Factory reset: `POST /reset` (`interfaces/app_api.py:234`) checks the typed box name and runs `systemctl start --no-block openlolo-reset.service`; polkit already allows the API user to start that unit.
- Autopair (`application/autopair.py`) pairs only when unbound and in setup mode; a bound box raises `ALREADY_BOUND`.
- PiSugar3 I2C facts and the bus retry recipe: `docs/hardware/pisugar3.md`. The parked PR 2 has a tested `Bus` class and tap handling (`deploy/openlolo_pisugar3.py`).

## Design

### Who decides what

The daemon stays dumb: it turns firmware events into flag files and mirrors one indicator file onto the LEDs. All mode logic lives in the API, where it is unit-tested with a fake clock and knows the setup state.

```
PiSugar3 0x08 --(daemon, 500 ms poll)--> /run/openlolo/pair-button   (single)
                                     --> /run/openlolo/setup-button  (long)
API Provisioning.tick ----------------> /run/openlolo/indicator     (idle | setup | reset_armed | resetting)
daemon (stat each tick) --------------> PiSugar3 0xE0 LED pattern
```

### API state (PR 1)

Add to `Provisioning`:

- `reset_armed_until: float | None`.
- Long flag:
  - IDLE -> `enter("button")` (unchanged).
  - SETUP_MODE, not armed -> arm for `RESET_ARM_SECONDS = 10`; log; extend the setup-mode expiry to at least the arm deadline.
  - SETUP_MODE, armed and not expired -> start the reset; clear the arm.
  - JOINING / VERIFYING -> ignored and logged (no reset mid-join).
- Single flag while armed -> disarm only; the phone search is skipped.
- `tick` disarms after the deadline. Leaving setup mode for any reason (Wi-Fi joined, timeout, API exit) disarms.
- Reset start is a shared helper, `start_factory_reset(reason)` in a small module used by both `POST /reset` and the button. Simulated backend keeps its current in-process path. A second confirm while the unit is starting is a no-op.
- Indicator: `Provisioning` writes `/run/openlolo/indicator` (one word, atomic rename) only when the value changes. Values: `idle`, `setup`, `reset_armed`, `resetting`.
- Health: `health()["reset_armed_in"]` (seconds or null). Box page shows one line while armed: "Hold the top button again to erase this box. Press once to cancel."
- Ordering guard: the flags are checked in a fixed order (long, then single) per tick. Two presses in one tick cannot arm and confirm together, because one long flag mtime is one event.

### Daemon (PR 1)

New backend `pisugar3` in `deploy/openlolo-button.py`, picked by `OPENLOLO_BUTTON_BACKEND`, code in `deploy/openlolo_pisugar3.py`, trimmed from 3699bac:

- Keep: `Bus` (20 ms spacing, 3 retries 50 ms apart, unlock + read-back on write), stale-event clear at start, per-event 2 s debounce, offline detection with 30 s retry, `<N>` journal priorities.
- Drop: soft mode, cut delay, anti-mistaken-touch write, power-button hold logic, battery sampling, `power.json`, polkit power-off rule. The daemon must not write `0x02`, `0x03` or `0x09`: without the power-button code, soft mode on would leave the power button unable to turn the box off.
- LEDs: on indicator change, write `0xE0`. `setup` = all four on/off at 1 Hz; `reset_armed` = 2 Hz (250 ms tick while armed); `resetting` = all on; `idle` = stop writing: `0xE0` is non-exclusive (datasheet), so the firmware's battery gauge takes the LEDs back. Blink writes only while a pattern runs; idle adds no I2C traffic.
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

1. **Top button + LEDs + reset arm** (daemon backend, drop-in, installer hook, API arm/confirm, indicator, shared reset helper, box-page line, docs page update).
2. **Phone replacement in pairing mode** (autopair, worker `forget` op, `openlolo-forget@` unit + polkit).

PR 2 does not depend on the daemon; it can be tested with the box-page "setup mode" command. Developed in order, shipped together.

## Steps

0. **Bench check first (box offline at plan time; find it by MAC `cc:c8:37:3d:1b:7d`).**
   - Long-press timing and latency of `0x08 == 3` on current firmware (`0xE2`).
   - `0xE0`: do the per-tick rewrites hold a steady pattern against the firmware's own LED updates, and does the gauge come back when writes stop?
   - Confirm `0x08` long press is distinct from the power button's `0x02` bit 0.
   Record results in `docs/hardware/pisugar3.md` before writing LED code.
1. PR 1 daemon: port `Bus` + tap handling + LED writer; unit tests with a fake smbus (stale clear, debounce, I2C failure, LED pattern per indicator, no writes to `0x02`/`0x03`/`0x09`).
2. PR 1 API: arm/confirm/cancel/timeout tests with a fake clock and fake reset starter; `POST /reset` uses the helper and its tests stay green.
3. PR 1 installer + drop-in; deploy to the Radxa bench.
4. PR 2 autopair replacement + tests (different UDID in setup mode, same UDID, outside setup mode ignored, trust denied keeps the old binding, phone unplugged mid-replace).
5. Bench pass, then docs and memory.

## Acceptance (bench, Radxa ZERO 3W + PiSugar3)

- Single press -> journal shows one phone search within 2 s of the event; nothing else changes.
- Long press from idle -> setup Wi-Fi visible on a phone within 15 s; LEDs blink 1 Hz.
- Long, long within 10 s -> reset unit runs; after it the box is in setup mode with no saved phone, no `openlolo-*` Wi-Fi profile and no AI clients.
- Long, then single -> no reset; LEDs back to 1 Hz; still in setup mode.
- Long, then wait 11 s, then long -> no reset (re-arms instead).
- Service restart with a press latched before start -> no action.
- Pairing mode with a second iPhone on the cable -> Trust prompt on it; after Trust the box controls the new phone; box-page sessions end; AI clients still listed and an existing MCP client drives the new phone; no `<old>` file left in `/var/lib/openlolo/device/lockdown`, the RemotePairing dir or `/var/lib/lockdown`; old phone on the cable outside pairing mode is ignored.
- Idle daemon cost with no indicator pattern: one I2C read per 500 ms, under 1% of one core (`pidstat 60 1`).
- Unit and portal test suites green.

## Resolved questions (2026-10-17)

- Phone switch keeps all AI clients.
- Old phone records deleted after a successful switch (worker op + root template unit).
- GPIO parity: yes, through the shared API logic; unit-tested only.
