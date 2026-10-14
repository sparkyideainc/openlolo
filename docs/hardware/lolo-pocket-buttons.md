# Lolo Pocket: buttons and phone search

**Status: planned, not implemented.** Button and power handling land with the PiSugar3 work; phone search windows land separately. This page records the behaviour agreed on 2026-10-13 for the Lolo Pocket (Radxa ZERO 3W + PiSugar3). Register details are in `pisugar3.md`; board details are in `radxa-zero3w.md`.

## Buttons

| Button | Gesture | Action |
|---|---|---|
| Power (PiSugar side button, near the LEDs) | Short tap while off | Power on. Needs anti-mistaken touch off (`0x02` bit 3 = 0); the setting survives power-off. |
| Power | Hold 3 s, then release | Clean shutdown. |
| Power | Hold 10 s | Force off: immediate power cut. |
| Top (custom) | Press (`0x08 == 1`) | Pairing mode: one 10 s Wi-Fi search for the bound phone. |
| Top | Hold 2-3 s (`0x08 == 3`) | Setup mode: setup access point, box page and cable pairing window, 10 min. |
| Top | Double press (`0x08 == 2`) | Unused. Clear the event and ignore it. |
| Back (board) reset | Press | PiSugar hardware reset. Restores PiSugar defaults; software re-applies its settings on the next start. Also recovers a frozen Linux. |

- No watchdog, to keep the workload low.
- Top-button events arrive about 1 s after the press. A stale event can be latched at boot: clear it at start without acting on it.

## Phone search

There is no background search loop. Each trigger starts at most one 10 s Wi-Fi discovery window.

| Trigger | Action |
|---|---|
| Boot | Check the cable at once, plus one 10 s Wi-Fi search. |
| Cable plugged in | Automatic pair and connect, triggered by the usbmuxd attach event. |
| Cable unplugged | 10 s Wi-Fi search, then stop. |
| Wi-Fi link drops | 10 s Wi-Fi search, then stop. |
| Top-button press | 10 s Wi-Fi search. |
| AI/MCP request while disconnected | One 10 s search. Phone found: the request proceeds. Not found: `DEVICE_DISCONNECTED`, "Press the top button or plug in the phone". |
| Otherwise | Idle; no polling. |

The 10 s bounds discovery only. A connection attempt that starts inside the window may finish after it.

## Battery

- Clean shutdown at 5% or less while on battery.
- No LED warning for now.
- No shutdown control on the box page or in MCP.
