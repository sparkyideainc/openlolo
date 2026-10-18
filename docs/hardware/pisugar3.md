# PiSugar3 (1200 mAh) battery board

Reference notes for the PiSugar3 UPS board used in the Lolo Pocket (Radxa ZERO 3W box). Sources are listed at the end. Facts marked **Bench** were verified on our hardware on 2026-10-13 and override the datasheet where they differ. The decided button behaviour is in `lolo-pocket-buttons.md`.

## Product

| Model | Battery | Output | Input | PCB |
|---|---|---|---|---|
| PiSugar 3 (ours) | 1200 mAh, 3.7 V single cell (~4.4 Wh) | 5 V, 2.5 A max | 5 V, 3 A max | 65 × 30 mm |
| PiSugar 3 Plus | 5000 mAh | 5 V, 3 A max | 5 V, 3 A max | 65 × 56 mm |
| PiSugar 3 Air | user-supplied 3.7 V cell | 5 V, 2.5 A max | 5 V, 3 A max | 65 × 30 mm |

- Attaches to the back of a Pi Zero-format board with pogo pins. Does not occupy GPIO header pins; talks to the host over I2C.
- Full UPS: the host keeps running when external power is removed, and the board can detect whether external power is present.
- The charging chip gets hot: 50-60 °C at 2 A input, up to 80 °C at 3 A. Do not touch the chip area while charging.
- Official compatibility list covers Raspberry Pi Zero through Pi 5 only. **Bench:** the pogo pins line up on the Radxa ZERO 3W; the board answers on `/dev/i2c-3` once the I2C3_M0 overlay is enabled (see `radxa-zero3w.md`).

## Buttons and LEDs

- **Power button** (side, near the LEDs):
  - Power on: short press, then press and hold (click and hold). The 4 LEDs count 1 to 4.
  - **Bench:** with anti-mistaken touch off (`0x02` bit 3 = 0), a short tap powers on.
  - Power off: long press. **Bench:** in the default mode the hold cuts power at once; in soft mode the PiSugar only sets the request bit and never cuts power by itself.
- **Anti-mistaken touch:** on by default; requires the click-and-hold pattern to power on.
- **Custom (top) button:** a separate button. While running it reports single tap, double tap and long press through register `0x08`. Software decides what each does.
- **Reset button** (back): hardware reset of the PiSugar MCU. **Bench:** it restores the defaults, including anti-mistaken touch on.
- **LEDs:** 4 battery-level LEDs. Software can drive them independently through register `0xE0` bits 0-3.

## I2C interface

- MCU address `0x57` (configurable, register `0x50`). RTC address `0x68` (MCU-emulated DS3231).
- Write protection: write `0x29` to `0x0B` before writing any register; any other value re-enables protection. Reading `0x0B` returns 1 when writable.
- The official server polls the button every 500 ms.
- **Bench:** the PiSugar answers only when it powers the board. With USB power plugged straight into the Radxa it does not answer at all.

### Access reliability (bench)

- Back-to-back accesses fail about 18% of the time (164 of 200 reads succeeded). One read every 500 ms failed 0 of 40.
- Space accesses 20-50 ms apart.
- Retry a failed access up to 3 times, 50 ms apart.
- Unlock (`0x0B = 0x29`) before each write, and read back every write to confirm it.

### Register map (MCU, 0x57)

| Reg | Bits | R/W | Default | Meaning |
|---|---|---|---|---|
| `0x02` | 7 | R | - | External power present (1 = connected) |
| `0x02` | 6 | R/W | on | Charging switch (resets on at shutdown) |
| `0x02` | 5 | R/W | on | **Output switch** (5 V to host). Clear it to cut power after the `0x09` delay. **Bench:** this is the bit that cuts power; it returns to on at power-on |
| `0x02` | 4 | R/W | on | Auto power-on when external power returns |
| `0x02` | 3 | R/W | on | Anti-mistaken touch. **Bench:** persists across power-off; only the reset button restores it |
| `0x02` | 2 | R/W | on | Named the output switch in the datasheet table. **Bench:** clearing it did nothing; use bit 5 |
| `0x02` | 0 | R | - | Power button held right now. **Bench:** stayed high ~7 s through a hold |
| `0x03` | 6 | R/W | on | Auto-hibernate (affects I2C availability) |
| `0x03` | 4 | R/W | off | Soft shutdown enable. **Bench:** resets to off at power-on |
| `0x03` | 3 | R | - | Soft shutdown requested. **Bench:** set about 2 s into a power-button hold in soft mode; cannot be cleared by writing; resets at power-off |
| `0x04` | all | R | - | PiSugar chip temperature, -40 to 85 °C |
| `0x06` | 7 | R/W | off | Software watchdog enable |
| `0x06` | 5 | W | - | Software watchdog feed (write 1) |
| `0x06` | 4 | R/W | off | Boot watchdog enable (firmware ≥ 1.0.6) |
| `0x06` | 3 | W | - | Boot watchdog feed (must feed within ~90 s of boot) |
| `0x07` | all | R/W | - | Watchdog timeout = value × 2 s |
| `0x08` | 1-0 | R/W | - | Top-button event: 1 single, 2 double, 3 long, 0 none. **Bench:** arrives ~1 s after the press |
| `0x09` | all | R/W | 0 | Output-off delay, 0-255 s (approximate). **Bench:** resets to 0 at power-on |
| `0x0A` | all | R/W | - | Boot watchdog restart limit (min 1) |
| `0x0B` | all | R/W | - | Write protection (`0x29` = unlocked) |
| `0x20` | 7 | R/W | off | Charging protection: stop at ~80% for longer battery life |
| `0x20` | 3 | R/W | off | SCL wake: pull SCL low on power-on |
| `0x21` | - | R/W | - | Battery control 2 |
| `0x22` / `0x23` | all | R | - | Battery voltage, high/low byte, mV (`0x10 0x1F` = 4127 mV) |
| `0x26` / `0x27` | all | R | - | Output current, high/low byte |
| `0x2A` | all | R | - | Battery percent (calculated) |
| `0x30`-`0x3B` | - | R/W | - | RTC time (BCD in `0x31`-`0x37`) and adjust |
| `0x40` | 7 | R/W | - | Scheduled power-on (alarm) enable |
| `0x44`-`0x47` | all | R/W | - | Alarm: weekday repeat (`0x44`), hour/min/sec (`0x45`-`0x47`) |
| `0x50` | all | R/W | `0xD7` | I2C address, `0x03`-`0x77`, even parity in bit 7 |
| `0xE0` | 3-0 | R/W | - | LED control, one bit per LED (1 on). Non-exclusive: the firmware also drives the LEDs. On USB power its charge animation (`0x07, 0x0f, 0x00, 0x01, 0x03`, about every 0.4 s) overwrites any pattern, so OpenLolo leaves `0xE0` alone and uses the board LED |
| `0xE2` | - | R | - | Firmware version |

Notes:
- **Tap events:** read `0x08`, act on `tap & 0x03`, then clear with `write(0x08, tap & 0xFC)`. **Bench:** a stale event can be latched at power-on; clear it at start without acting on it.
- **Soft shutdown:** when `(0x03 & 0x18) == 0x18`, a soft power-off was requested. Act on it once per boot: the request bit stays set until power-off.
- **Power cut (bench-verified recipe):** unlock, set `0x09 = <delay s>`, clear `0x02` bit 5, then halt the host. The output goes off after the delay.
- **Stuck state (bench):** a board halted with soft mode on and no cut armed ignores the power button; only the reset button recovers it. Before `poweroff`, always arm the cut **and** turn soft mode off.
- **Persistence (bench):** at power-on `0x03`, `0x09` and `0x02` bit 5 return to defaults, so software must set soft mode every boot. `0x02` bit 3 (anti-mistaken touch) persists until the reset button; software re-applies it at start.
- **Battery voltage:** cannot be read accurately while charging. The board pauses charging every 3 s to measure.
- **RTC:** with the kernel `ds3231` driver bound at `0x68`, `i2cdetect` shows `UU`, and `hwclock -r` / `hwclock -w` work. The clock runs for over a year while the board is off.

## pisugar-server (official daemon)

We do not run it on the box: it is Pi-targeted and opens ports on the LAN. It is useful as a reference implementation (`pisugar-core/src/pisugar3.rs`).

- Install: `wget https://cdn.pisugar.com/release/pisugar-power-manager.sh && bash pisugar-power-manager.sh -c release`. Uninstall: `sudo dpkg -P pisugar-server`.
- Config: `/etc/pisugar-server/config.json` (`i2c_bus`, `i2c_addr` in decimal).
- Interfaces:
  - Unix socket `/tmp/pisugar-server.sock`
  - TCP 8423 (raw commands)
  - WebSocket 8422
  - HTTP + WebSocket web UI 8421 (`/ws`)
- Commands:
  - Battery: `get battery`, `get battery_v`, `get battery_power_plugged`, `get battery_charging_range`, `get battery_allow_charging`.
  - Buttons: `set_button_enable single|double|long 0|1`, `set_button_shell single|double|long <cmd>`, `get button_enable`.
  - Power: `set_safe_shutdown_level <pct>`, `set_safe_shutdown_delay <s>`, `set_battery_output true|false`, `set_battery_charging_range <restart>,<stop>`, `set_allow_charging true|false`.
  - RTC: `rtc_alarm_set <ISO8601> <repeat>`, `get rtc_alarm_time`, `rtc_pi2rtc`, `rtc_rtc2pi`, `rtc_web`.
  - Auth: `set_auth <user> <password>`.
- Firmware update: `curl https://cdn.pisugar.com/release/PiSugarUpdate.sh | sudo bash`.

## Sources

- [PiSugar 3 series wiki](https://docs.pisugar.com/docs/product-wiki/battery/pisugar3/pisugar-3-series)
- [PiSugar 3 I2C datasheet](https://docs.pisugar.com/docs/product-wiki/battery/pisugar3/pisugar-3-i2c)
- [PiSugar 3 RTC](https://docs.pisugar.com/docs/product-wiki/battery/pisugar3/pisugar-3-rtc)
- [PiSugar Power Manager](https://docs.pisugar.com/docs/product-wiki/battery/pisugar-power-manager)
- [pisugar3.rs reference implementation](https://github.com/PiSugar/pisugar-power-manager-rs/blob/master/pisugar-core/src/pisugar3.rs)
