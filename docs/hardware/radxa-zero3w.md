# Radxa ZERO 3W

Reference notes for the Radxa ZERO 3W box build, the Lolo Pocket (with PiSugar3 battery, see `pisugar3.md`, and a heatsink). Button behaviour is in `lolo-pocket-buttons.md`. Bench observations are dated. Sources are listed at the end.

## Specs

| Item | Value |
|---|---|
| SoC | Rockchip RK3566 |
| CPU | 4 × Cortex-A55, up to 1.6 GHz (our Armbian kernel offers 408 / 600 / 816 / 1104 / 1416 MHz) |
| GPU | Mali-G52 2EE (OpenGL ES 3.2, Vulkan 1.1, OpenCL 2.0) |
| Video | H.264/H.265 decode up to 4K60, encode up to 1080p60 |
| RAM | LPDDR4 (bench unit: 2 GB) |
| Storage | eMMC, microSD boot |
| Wireless | Wi-Fi 6 (802.11ax) + Bluetooth 5.4 with BLE; Armbian loads the AIC8800 driver (`/etc/modprobe.d/aic8800-wireless.conf`) |
| USB-C 1 | USB 2.0 OTG, also the power input (5 V, 2 A or more) |
| USB-C 2 | USB 3.0 host. The iPhone goes here (xhci bus 1 at 480M; bus 2 is the 5000M side) |
| Display | micro HDMI, 1080p60 |
| Camera | 22-pin MIPI CSI, 0.5 mm pitch |
| GPIO | optional 40-pin header |
| Heatsink | Radxa Heatsink 5519A (ZERO 3W/3E) |

## 40-pin header

| Pin | Function |
|---|---|
| 1 | 3.3 V |
| 2, 4 | 5 V |
| 3 | GPIO1_A0, I2C3_SDA_M0 (also UART3_RX_M0) |
| 5 | GPIO1_A1, I2C3_SCL_M0 (also UART3_TX_M0) |
| 7 | GPIO3_C4 (PWM14_M0) |
| 11 | GPIO3_A1 |
| 27 / 28 | I2C4_SDA_M0 / I2C4_SCL_M0 |
| 29 / 31 | I2C5_SCL_M0 / I2C5_SDA_M0 |
| 6, 9, 14, 20, 25, 30, 34, 39 | GND |

- Pins 3, 5, 27 and 28 have extra I2C pull-up resistors, so they behave badly as plain GPIO.
- A Pi-style HAT on pins 3/5 (such as the PiSugar3 I2C link) uses I2C3_M0.
- Armbian needs a device-tree overlay to enable the bus: `overlays=rockchip-rk3566-i2c3-m0` in `/boot/armbianEnv.txt`, then a reboot. The bus appears as `/dev/i2c-3` (`rk3x-i2c fe5c0000.i2c`).
- Use the full overlay name. `armbianEnv.txt` sets `overlay_prefix=rk35xx`; `boot.cmd` tries `${overlay_prefix}-${name}.dtbo`, then `${name}.dtbo`, and only `rockchip-rk3566-i2c3-m0.dtbo` exists in `/boot/dtb/rockchip/overlay/`.
- Keep one `overlays=` line (space-separated names); a second line replaces the first.

## LEDs

- One user LED, `green:heartbeat`, with the kernel `heartbeat` trigger. The double blink means the kernel is alive; it speeds up with load. It is not a power indicator.
- Turn it off: `echo none | sudo tee /sys/class/leds/green:heartbeat/trigger` (not persistent). The installer's `openlolo-board.service` does this at every boot.
- Armbian's `armbian-led-state.service` saves LED triggers at shutdown and restores them at boot (`/etc/armbian-leds.conf`).
- `mmc0::` is the storage activity trigger.

## Thermal

- Sensors: `thermal_zone0` = `cpu-thermal`, `thermal_zone1` = `gpu-thermal`.
- CPU trip points: 70 °C, 75 °C, 95 °C (critical). Cooling devices: `cpufreq-cpu0` and GPU devfreq.

## Bench findings (2026-10-13, Armbian 26.8.1 trixie, kernel 6.18.44-current-rockchip64)

Host `radxa-zero3`, LAN 192.168.4.68, Tailscale 100.96.13.57. No heatsink at the time of these readings.

- **Idle heat:** 50 °C at 1 min uptime, 52-53 °C at 10 min. Governor `ondemand`, set at every boot by `armbian-hardware-optimize.service` from `GOVERNOR` in `/etc/default/cpufrequtils`. That script sets it from a background job, which can finish after the service, and after units ordered behind it. Cores spent 63% of the time at 1.416 GHz and 32% at 408 MHz while the system was ~98% idle.
- **Cause:** `openlolo-device` used ~30% of one core and made ~440 voluntary context switches/s with no phone reachable, looping USB list + Bonjour browse in `_wait_for_device`.
- **iPhone not seen on USB:** the kernel enumerates the iPhone (`05ac:12a8`), but Armbian's `apple_mfi_fastcharge` driver binds it and leaves it in USB config 1 (PTP only). usbmuxd needs config 3 or 4 (interface class 255/254/2). The udev rule sets config 0; the driver sets config 1 back; usbmuxd logs `Could not find a suitable USB interface`. Fix: `blacklist apple_mfi_fastcharge` in `/etc/modprobe.d/`. Raspberry Pi OS does not ship this driver.
- **Sleep:** the kernel offers `freeze mem disk` and `mem_sleep: s2idle [deep]`, but nothing is configured to suspend. The box serves the API, Funnel and MCP, and Wi-Fi wake is unreliable, so system suspend is not used.
- **Services running:** `bluetooth.service` (unused since BLE was removed), avahi, NetworkManager, wpa_supplicant, systemd-networkd, tailscaled, usbmuxd, openlolo-api, openlolo-device.
- **Sudo:** `/etc/sudoers.d/90-openlolo-nopasswd` gives `openlolo` passwordless sudo. Bench convenience only; the installer does not add it, nor SSH keys or a persistent journal.
- **Dropout:** the box dropped off LAN and Tailscale at about 00:30 while running on the PiSugar3. Cause not yet known (battery empty or crash).

## Radxa ZERO 3W install

Flash Armbian (Debian 13 trixie, `current` kernel) and run the standard installer (`apps/device/install.sh`). It detects the board from `/proc/device-tree/model` (`Radxa ZERO 3`) and then also:

| Step | What it does |
|---|---|
| Board profile | Appends `board = "radxa-zero3w"` to `/etc/openlolo/box.toml` unless a `board` key is already there. |
| Packages | Installs `i2c-tools` (support; ships the `i2c` group udev rule for `/dev/i2c-*`) and `python3-smbus2` (PiSugar3 access). |
| I2C overlay | Adds `rockchip-rk3566-i2c3-m0` to the single `overlays=` line of `/boot/armbianEnv.txt`, after a backup to `armbianEnv.txt.bak-<date>`. Prints that a reboot is needed when the line changed. |
| Boot tuning | Sets `GOVERNOR=schedutil` in `/etc/default/cpufrequtils`, which Armbian applies at boot. Installs and enables `openlolo-board.service`: governor `schedutil` (when offered) on every cpufreq policy and the `green:heartbeat` trigger set to `none`, after Armbian's LED restore. |
| Wi-Fi | Armbian hands Wi-Fi to netplan/systemd-networkd, so NetworkManager reports `wlan0` "unmanaged" and OpenLolo sees no saved network (it then retries setup mode every ~1.4 s). `deploy/radxa-zero3w/netplan-to-nm.sh` runs detached (`journalctl -u openlolo-netplan-to-nm`) with `openlolo-api` stopped: one NetworkManager keyfile per netplan access point, NetworkManager takes `wlan0`, and if no gateway answers within 45 s netplan is restored. On success it disables networkd and purges the netplan packages; the YAML is kept in `/etc/openlolo/netplan-backup`. The box may get a new DHCP address. |
| polkit | `polkitd` (in `deploy/os-packages.txt`) applies `50-openlolo.rules`; Armbian minimal does not ship it, so starting `openlolo-setup-ap.service` failed with `Access denied`. |
| Bluetooth | Disables `aic-bluetooth.service` and `bluetooth.service` (the BLE channel was removed). `aic-bluetooth.service` requires `bluetooth.service`, so disabling only the latter does not keep it off. |

On every board the installer also writes `/etc/modprobe.d/openlolo-usbmux.conf` (`blacklist apple_mfi_fastcharge`) and unloads the module if it is loaded; replug the iPhone afterwards. The module is not in the initramfs, so `update-initramfs` is not needed.

Every step is safe to run again. Check after the reboot (the box must run on PiSugar power for the I2C check):

```sh
grep '^overlays=' /boot/armbianEnv.txt          # one line containing rockchip-rk3566-i2c3-m0
sudo i2cdetect -y 3                              # 57 and 68
lsmod | grep apple_mfi                           # empty
ls /sys/bus/usb/devices/1-1/                     # iPhone plugged in: more than one interface (1-1:N.M)
cat /sys/devices/system/cpu/cpufreq/policy0/scaling_governor   # schedutil
cat /sys/class/leds/green:heartbeat/trigger      # [none]
systemctl is-enabled bluetooth                   # disabled
```

## Sources

- [Radxa ZERO 3W product page](https://radxa.com/products/zeros/zero3w/)
- [Radxa ZERO 3 docs](https://docs.radxa.com/en/zero/zero3)
- [Radxa ZERO 3 hardware interface](https://docs.radxa.com/en/zero/zero3/hardware-design/hardware-interface)
- [Radxa forum: Zero 3W with a Pi Zero UPS](https://forum.radxa.com/t/could-zero-3w-use-raspi-zero-ups-pls-help/21743)
- [Radxa forum: Zero 3W overlays](https://forum.radxa.com/t/radxa-zero-3w-overlays/22129)
