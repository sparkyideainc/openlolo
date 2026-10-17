# Lolo Pocket: buttons and phone search

**Status:** top button and LEDs implemented (plan `docs/plans/lolo-pocket-top-button.md`, 2026-10-17); power button and battery handling still planned. This page records the behaviour agreed on 2026-10-13 for the Lolo Pocket (Radxa ZERO 3W + PiSugar3). Register details are in `pisugar3.md`; board details are in `radxa-zero3w.md`.

## Buttons

| Button | Gesture | Action |
|---|---|---|
| Power (PiSugar side button, near the LEDs) | Short tap while off | Power on. Needs anti-mistaken touch off (`0x02` bit 3 = 0); the setting survives power-off. |
| Power | Hold 3 s, then release | Clean shutdown. |
| Power | Hold 10 s | Force off: immediate power cut. |
| Top (custom) | Press (`0x08 == 1`) | One phone search for the saved phone. While a reset is armed: cancel it, no search. |
| Top | Hold (`0x08 == 3`) | Pairing mode = setup mode: setup access point, box page and cable pairing window, 10 min. A different phone on the cable replaces the saved one after Trust. LEDs blink 1 Hz. |
| Top | Hold again, in pairing mode | Reset armed for 10 s; LEDs blink fast. Nothing is erased yet. |
| Top | Hold again, within the 10 s | Factory reset (same unit as the box page reset); LEDs all on. |
| Top | Double press (`0x08 == 2`) | Unused. Clear the event and ignore it. |
| Back (board) reset | Press | PiSugar hardware reset. Restores PiSugar defaults; software re-applies its settings on the next start. Also recovers a frozen Linux. |

- No watchdog, to keep the workload low.
- Top-button events arrive about 1 s after the press. A stale event can be latched at boot: clear it at start without acting on it.
- The button daemon only turns events into flag files and mirrors `/run/openlolo/indicator` (idle, setup, reset_armed, resetting) on the LEDs; the API decides what a press means. In idle the daemon stops writing `0xE0` and the firmware's battery gauge shows.
- Until the power-button work lands, the daemon never writes `0x02`, `0x03` or `0x09`; the power button keeps the PiSugar3 defaults.
- Raspberry Pi: the GPIO 5 s hold is the same "hold", so the same arm and confirm apply; a Pi reset can only be cancelled by the 10 s timeout.

## Phone search

The phone is found through events. Each event opens at most one 10 s Wi-Fi search window; a window that is already open is joined, not extended. Wi-Fi tries the phone's last known address first, then Bonjour, re-asking once a second.

| Trigger | Action |
|---|---|
| Boot | Check the cable at once. Once the box is on a network (Wi-Fi may join after the worker starts), one Wi-Fi search, starting with the address saved from the last session. |
| Cable plugged in | Connect at once, triggered by the usbmuxd attach event. No search. |
| Cable unplugged | One Wi-Fi search. |
| Wi-Fi session lost | One Wi-Fi search. |
| Phone advertises on the LAN (avahi) | One Wi-Fi search, at most one per 15 s. An unlocked iPhone advertises at once, so unlocking it reconnects the box. |
| Top-button press | One Wi-Fi search. |
| AI/MCP request while disconnected | One search. Found: the request proceeds. Not found: `DEVICE_DISCONNECTED`, "Press the top button or plug in the phone". Locked: `PHONE_LOCKED`. |
| Still disconnected | Safety-net searches 1, 2, then every 5 min, only with a bound phone off the cable and the box on a network (paused in setup mode). Any event above restarts the sequence. |
| Connected | No search. USB is never probed; Wi-Fi is checked only after 30 s without a request. |

The 10 s bounds discovery only. A connection attempt that starts inside the window may finish after it.

A locked iPhone refuses new Wi-Fi sessions (about 30 s after locking) and drops an idle one within a few minutes. The box shows "Unlock the phone" and waits for the phone to advertise again. USB is not affected.

## Battery

- Clean shutdown at 5% or less while on battery.
- No LED warning for now.
- No shutdown control on the box page or in MCP.
