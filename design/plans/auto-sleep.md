# Lolo Pocket: phone search only at boot or on request, auto power-off when idle

**Status: PR 1 (search policy) and PR 2 (auto sleep, box page setting) implemented 2026-10-26 on `jingerpie/auto-sleep` and `jingerpie/auto-off`; tests green, not yet run on hardware. `CUT_DELAY` is 45 s until the bench measures shutdown time. PR 3 (Raspberry Pi + PiSugar3) waits for the owner's bench test.**

The user asked for two changes:

1. Remove the periodic phone connection check. The box searches once at boot. After that it searches only when someone asks.
2. When the phone is not connected and the box is not in use, turn the box off or put it to sleep.

The work is one design delivered in three PRs. PR 1 (connection policy) is useful alone and ships first. PR 2 (auto power-off) depends on the connection states that PR 1 defines. PR 3 brings the same power-off to the Raspberry Pi + PiSugar3 after a bench test.

## Today (main 0baa161)

Automatic phone contact still happens in these places (`apps/device/src/openlolo/coredevice/manager.py`):

| Path | Where | What it does |
|---|---|---|
| Boot search | `connect` sets `booting = True` (`:198`); `_wait_for_device` (`:588`) | One window after the box gets a default route. Runs again on **every** worker start, so a crash loop (`Restart=on-failure`, 3 s) searches every time. |
| Cable pulled | `_usb_update` (`:271`) | Opens a Wi-Fi search window. |
| Phone advertises | `_follow_announcements` / `_announced` (`:386`, `:401`) | avahi sees the bound phone's RemotePairing advertisement (every wake or unlock) and opens a window, at most once per 15 s. This is the check that looks periodic: the LED blinks each time. |
| Request while disconnected | `_session` (`:729`) | Any MCP/SDK/page call that needs the phone opens a 10 s window. AI clients retry, so this repeats. |
| Session lost | `_supervise` (`:456`) | Pauses `reconnect_min_seconds`, then searches again; transient failures retry with backoff up to `RETRY_LIMIT` (`:68`). |
| Safety-net windows | `_background_delay` / `_event` (`:423`, `:567`) | Off by default since 10-18 (`search_backoff_seconds = []`, `config.py:43`). |
| Wi-Fi idle probe | `_probe` (`:662`) | Asks the phone for device info after `probe_interval` (30 s) without a request, for as long as a Wi-Fi session is up. |

Not phone contact, and staying: the usbmuxd Listen stream and its local presence fallback (`:288`), the provisioning LED status read over the worker socket (`provisioning.py:462`), and the page-access RemotePairing browse (`phone.py:268`, cached 30 s, throttled 5 s). That browse proves which browser is the owner's phone. It must never start a device session or count as activity.

Power: the PiSugar3 can cut the 5 V output (bench recipe in `design/hardware/pisugar3.md:83`). The button daemon (`deploy/openlolo_pisugar3.py`) owns I2C and today never writes the power registers (`:23`). The Raspberry Pi base unit and a Radxa on direct USB power have no PiSugar (the PiSugar does not answer then, `pisugar3.md:34`).

## Decisions (proposed)

### Connection policy (PR 1)

| Trigger | After this change |
|---|---|
| Boot | One bounded search window, once per OS boot (keyed on `/proc/sys/kernel/random/boot_id`, stored in `runtime_dir`). A worker restart in the same boot does not search again. Locked-phone retries inside that window stay. |
| Top button press | One search window (unchanged). |
| Box page "Reconnect iPhone" (`recover`) | Unchanged: drops the session, one search. |
| Pairing, transport change, DDI mount (`connect`, `request_mount`) | Unchanged explicit triggers. A fresh `connect` after pairing searches once; it is not a boot. |
| Cable attach of the bound phone | Connects over USB at once (physical action, not a check). |
| Cable pulled | One Wi-Fi search window, as today (`_usb_update`, `:271`). The unplug is a physical action, so it counts as a trigger (owner decision 2026-10-26). Skipped when the session already runs on Wi-Fi. |
| Phone advertises | No search. avahi still updates the cached address (`wifi.remember`) so the next explicit search is fast and page access keeps working. |
| MCP/SDK request while disconnected | Fails at once with `DEVICE_DISCONNECTED` and a hint: "Press the button on the box or tap Reconnect iPhone." No search (owner decision 2026-10-26). |
| Session lost while connected | Teardown, state disconnected, no retry, no search. |
| Transient failure during an explicit attempt | Retries stay, but only until that attempt's deadline (the window deadline plus the connect that started inside it). Nothing outlives the trigger. |
| Wi-Fi idle probe | Removed. |
| Safety-net windows | Removed. `Config` is a `StrictModel` (unknown keys fail), so the key stays as an unused field with an "Unused since" comment, like `host`/`port` (`config.py:19`). |

Consequence to accept: a Wi-Fi session that dies silently while idle (phone leaves the network, no TCP close) is seen only at the next request. The request fails, `_fail` marks the session lost (`session.py:73`), and the state becomes disconnected. The box never calls an old idle session disconnected by itself, so it never powers off on a session that may still work.

HID errors: today an input timeout closes only the touch session and raises `INPUT_INTERRUPTED` (`session.py:504`). Keep that. Do not promote every HID failure to a full transport loss.

### Auto power-off (PR 2)

| Item | Decision |
|---|---|
| Action | Clean OS shutdown, then the PiSugar cuts the 5 V output (owner decision 2026-10-26). |
| Wake | PiSugar power button, or external power returning (`0x02` bit 4, PiSugar default). No RTC alarm: a timed wake would bring back automatic checks. |
| Where it runs | Lolo Pocket (PiSugar present) on **battery only** (owner decision 2026-10-26). External power or unknown power: never power off. Any board with a PiSugar3: Radxa ZERO 3W today, Raspberry Pi + PiSugar3 next (owner decision 2026-10-26, bench soon; see PR 3). Support is decided at runtime by the button daemon reaching the PiSugar, not by board name. No PiSugar (bare Pi, Radxa on direct USB power): feature off. |
| Idle timeout | 5 min of continuous eligibility by default (owner decision 2026-10-26). The owner changes it in the box page: Settings > Auto sleep (below). |
| Warning | 10 s LED pattern `shutdown` (new; proposal: three slow flashes). Any button press or accepted request in that time cancels. |
| Eligible | All true at the same time: worker status fresh (read under 2 s ago) **and** says disconnected (not "worker unreachable"); not searching, connecting, switching or mounting DDI; no queued or running action; no live control lease; no Setup or Local mode window; no Wi-Fi join or verify; no reset challenge or reset running; no maintenance inhibitor (update install). |
| Resets the countdown | An accepted authenticated request (MCP, SDK, page action), any button event, a failed explicit connect attempt, leaving Setup/Local mode, any eligibility flip. |
| Does not reset it | Page status polls, open MCP transports with no calls, owner cookies, the page-access RemotePairing browse. |

Design:

- **Policy** lives in a new `application/autooff.py` controller owned by `Runtime` (`bootstrap.py:36`), where coordinator, provisioning and reset state already meet. It keeps one monotonic deadline and one activity generation counter. Activity bumps the generation; the controller re-reads the deadline (one sleeper task, no polling loop beyond the existing 1 s provisioning tick).
- **Power** stays in the button daemon (sole I2C owner). The API talks to it over a new Unix socket `/run/openlolo/power.sock` with an acknowledged protocol, not a flag file, so a stale file can never shut the box down:
  - `status` returns `{pisugar: bool, external_power: bool | null, daemon_id}`.
  - `prepare(attempt_id)` starts the warning pattern and returns `daemon_id`; expires after 15 s.
  - `cancel(attempt_id)`.
  - `commit(attempt_id, daemon_id)` only if the attempt is still prepared, not expired, the daemon instance matches, and the daemon re-reads external power = absent. Then, in order: unlock, `0x09 = CUT_DELAY`, soft mode off (`0x03` bit 4 = 0), clear `0x02` bit 5, read back each, reply ok. Any write or read-back failure: reply error, write nothing more, restore bit 5 if it was cleared.
- **Shutdown**: after `commit` returns ok, the API starts `openlolo-poweroff.service` (new oneshot, `systemctl poweroff`), allowed by a new entry in the polkit unit list (`deploy/polkit/50-openlolo.rules:19`). The API process cannot reboot or power off anything else.
- **Order and timing**: the cut is armed before poweroff starts, so `CUT_DELAY` must exceed the measured shutdown time with margin (bench target: measure 10 runs, set delay = max + 15 s, cap 60 s). If `systemctl` fails after the cut is armed, the cut still happens; log the failure first. After commit the attempt is irreversible.
- **Commit guard**: right before `prepare` and again before `commit`, the controller atomically re-checks generation, all inhibitors, fresh worker status and power source. During the warning and after commit, the coordinator rejects new work with `SHUTTING_DOWN` (503); before commit, an accepted request cancels the attempt instead.
- **One attempt at a time**: attempt IDs are random; a second `prepare` while one is open returns the open one. Daemon restart drops all attempts (new `daemon_id`).
- **Maintenance inhibitor**: reset (`openlolo-reset.service`) and update install hold an inhibitor (a context manager in the controller, plus a file in `runtime_dir` for root-run scripts, honoured only if its PID is alive) before they start.

### Why power-off, not sleep (owner decision 2026-10-26)

Bench check on the Radxa ZERO 3W (Armbian, kernel 6.18.44, read-only):

- `/sys/power/state` = `freeze mem disk`, `mem_sleep` = `s2idle [deep]`: suspend to RAM exists.
- AIC8800 Wi-Fi reports WoWLAN "wake on magic packet" and "wake on anything" (both off today).
- Wake-capable devices: PMIC RTC alarm (`rk808-rtc`), PMIC power key, USB host (`xhci`, wake off).
- No cpuidle states exposed; `powersave` governor and 408 MHz available.

| Mode | Page / MCP from outside | Top button | Cable plug-in |
|---|---|---|---|
| Power off | No | No (PiSugar power button only) | No |
| Deep suspend | No: Tailscale/Funnel and TCP sessions drop; a magic packet must come from the LAN, which an MCP client cannot send | No: the PiSugar top button is polled over I2C, no interrupt line to the Radxa | Untested (USB wake) |
| Suspend + Wi-Fi wake on anything | Partly; LAN broadcast and mDNS would wake it constantly | No | Untested |
| Low-power idle (stay on) | Yes | Yes | Yes |

Suspend loses every trigger power-off loses, and adds resume risk (Wi-Fi driver, USB, I2C after resume). Low-power idle keeps every trigger but its saving is unmeasured and the box still drains the battery. The owner chose power-off: on battery with no phone and no use, nobody is driving the box, and the PiSugar power button is the wake.

Consequence: after auto power-off, MCP clients and the box page see the box as offline (tunnel down) until someone presses the PiSugar power button or plugs in power. Box page copy and docs must say this.

## Guards

| Risk | Guard |
|---|---|
| Worker crash loop re-searches each start | Boot search keyed on `boot_id`, consumed once. |
| Stale `wake` event replays an old attempt | `wake` cleared at the start of each explicit attempt; each attempt carries its deadline; the supervisor drops wakes older than the attempt. |
| USB first enumeration at worker start looks like a new attach | The first Listen snapshot sets `cable` without counting an attach. A phone already on the cable at boot connects through the boot search, not through an attach event. |
| Worker unreachable read as disconnected | Auto-off needs a fresh positive "disconnected" status; unreachable inhibits. The LED code may keep its current mapping. |
| Activity arrives during the warning | Generation check before `commit`; request cancels the attempt. |
| Request arrives after commit | Coordinator rejects with `SHUTTING_DOWN`. |
| Stale or duplicate commit | Attempt ID + daemon ID + 15 s expiry; daemon executes a commit at most once. |
| Cut fires before filesystems sync | `CUT_DELAY` from bench measurement; acceptance checks a clean journal on next boot. |
| Box stuck off with soft mode on (`pisugar3.md:84`) | Commit always clears soft mode before arming the cut. |
| External power plugged in during warning | Daemon re-reads `0x02` bit 7 at commit and refuses. |
| No PiSugar | `status` says `pisugar: false`; controller never prepares. |
| PiSugar not answering at start (the Radxa bench note: it answers only when it powers the board) | `supported` follows the daemon's live `status`; a box that boots on USB power and later runs on the PiSugar starts counting once the daemon reaches it. |

## Work

### PR 1 (`jingerpie/auto-sleep`): search only at boot or on request

- `manager.py`: remove `_probe` and its waiter in `_watch`; remove the safety-net path (`_background_delay`, `"background"` in `_event`, `backoff`); `_announced` only remembers the address; `_usb_update` keeps its unplug search; `_session` raises at once when not connected (it still waits `switch_hold_seconds` during a USB/Wi-Fi switch or a connect already started by an explicit trigger); `_supervise` after a lost session or a failed attempt past its deadline waits only for an explicit trigger or a cable attach.
- Boot-once marker (`boot_id`) in `CoreDeviceManager.connect` / `DeviceWorker.start`.
- `config.py`: `probe_interval` and `search_backoff_seconds` become unused fields with an "Unused since" comment so older `box.toml` files load (`StrictModel` rejects unknown keys). `wifi_announcements` stays: it now only controls address caching.
- Module docstring of `manager.py`, `config/box.example.toml`, `design/plans/lolo-pocket-modes.md` (pairing row: "the same search window the box opens by itself" becomes boot only), docs page for the LED.
- MCP/SDK error text for `DEVICE_DISCONNECTED` gains the reconnect hint.

### PR 2 (`jingerpie/auto-off`, on PR 1): auto power-off

- `application/autooff.py` controller, wired in `bootstrap.py`; activity hooks in the coordinator (accepted work), MCP/SDK entry points, provisioning button handlers.
- `deploy/openlolo_pisugar3.py`: power socket server, `shutdown` LED pattern, commit sequence.
- `deploy/systemd/openlolo-poweroff.service`, polkit entry, `scripts/install.sh` to install them.
- `config.py`: `auto_off_seconds` (default 300, used until the owner sets a value; 0 = off), `auto_off_warning_seconds` (10).
- Owner setting, stored by the API in `state_dir/power.json` (atomic write: temp file + rename, mode 0600, owner `openlolo`). Shape `{"auto_sleep_minutes": 0 | 5 | 15 | 30 | 60}`; a missing or invalid file falls back to `auto_off_seconds`. A change takes effect at once: the controller re-reads it, restarts the countdown, and cancels an open warning.
- App API (`interfaces/app_api.py`, owner session only, same auth as `/network`):
  - `GET /app/power` returns `{"auto_sleep_minutes", "supported", "on_battery", "battery_percent", "state"}`. `supported` is false without a PiSugar; `state` is `off | waiting | counting | warning`, with `seconds_left` while counting or warning.
  - `POST /app/power` with `{"auto_sleep_minutes": n}`; any other value returns 400 `INVALID_SETTING`. Counts as owner activity.
  - `POST /app/power/keep` cancels an open warning and restarts the countdown (the page's "Keep on" button). Counts as owner activity.
- Box page (`apps/portal`): Settings gets an **Auto sleep** row (`_shell.settings.index.tsx`, a new `_shell.settings.power.tsx` page), built from `@openlolo/ui` components. Choices: Off, 5 min (default), 15 min, 30 min, 1 hour. Status line: "Turns off after idle for 5 min"; footer: "Press the power button to turn it back on." Without a PiSugar the row shows "Not available on this box" and is disabled. The page polls `GET /app/power` only while it is open (TanStack Query, 5 s, as `useNetwork`).
- Design canvas (draft, not final): https://claude.ai/artifact/TESjyN9UaZu9feu9LPu6St. Three screens: the Settings row (Box section, after Wi-Fi, value "5 min"), the Auto sleep page (status card: on battery with percent and time left, iPhone connected, or on USB power; a "Turning off in 8 s" card with **Keep on** during the warning; the "Turn off after idle" choice list), and the page on a box without a PiSugar. Code waits until the owner says the design is done.
- The UI name is "Auto sleep" (owner's word); the action is a power-off, and the subtitle says how to turn the box back on.
- Docs (troubleshoot): the box is offline after auto power-off; how to wake it.

### PR 3 (`jingerpie/pi-pisugar3`, on PR 2): Raspberry Pi + PiSugar3

The Raspberry Pi is also sold with a PiSugar3 (owner, 2026-10-26). Same daemon and power protocol; only the board setup differs.

- `scripts/install.sh`: Pi profile `raspberry-pi-pisugar3`, chosen by `--pisugar3` or by the PiSugar answering at `0x57` on `/dev/i2c-1` during install. It enables I2C (`dtparam=i2c_arm=on` in `/boot/firmware/config.txt`, reboot notice as on the Radxa), installs `i2c-tools python3-smbus2`, and installs a `pisugar3-pi.conf` drop-in: `OPENLOLO_I2C_BUS=1`, `DeviceAllow=/dev/i2c-1 rw`, `OPENLOLO_STATUS_LED` = the Pi's `ACT` LED (`/sys/class/leds/ACT/brightness`, `led0` on older kernels; trigger set to `none` at boot like `openlolo-board.service`).
- `config.py`: `board` gains `"raspberry-pi-pisugar3"`.
- PiSugar3 uses the Pi's I2C pins (GPIO2/3). The GPIO button on BCM 17 does not collide; with the PiSugar profile the top button replaces it (one button daemon backend at a time).
- Bench first (owner will test soon): PiSugar answers on `i2c-1`, top button events, `0x02` bit 7 on battery vs USB, the power-cut recipe and shutdown time on the Pi (Pi shutdown may be slower than the Radxa, so `CUT_DELAY` is measured per board and set in the drop-in as `OPENLOLO_CUT_DELAY`).
- Acceptance: the PR 2 bench list, repeated on the Pi.

## Steps

1. PR 1 code and contract tests. Run `pytest` with a per-worktree `--basetemp=/tmp/olpt-autosleep`.
2. PR 1 bench: Radxa ZERO 3W + PiSugar3 with the iOS 27 phone, 30 min soak (below).
3. Bench measurements for PR 2: shutdown duration (10 runs), battery current idle-on vs off, `0x02` bit 7 behaviour while charging.
4. PR 2 code and tests, `CUT_DELAY` from step 3.
5. PR 2 bench acceptance.
6. Raspberry Pi + PiSugar3 bench (owner), then PR 3.

## Acceptance

### PR 1 (tests, `tests/contract/test_manager.py`)

- After the boot window ends with nothing found: advertisements, elapsed time (fake clock, 1 h), session loss and ordinary requests open **zero** windows and **zero** `transport.open` calls.
- Worker restart in the same boot: zero windows. New boot ID: one.
- Button, `recover`, `connect`, `request_mount`: exactly one window each; concurrent triggers coalesce into one.
- Connected over Wi-Fi with no requests for 1 h: zero probes.
- Cable pulled while on USB: exactly one Wi-Fi window; while already on Wi-Fi: none.
- Locked phone found inside an explicit window: retried until that window's deadline, then nothing.
- Request while disconnected: `DEVICE_DISCONNECTED` within 50 ms (no `switch_hold_seconds` wait) unless a switch or explicit connect is in progress.

### PR 1 (bench)

- Boot with phone on Wi-Fi: connects once. Lock/unlock the phone 10 times over 30 min while disconnected: LED never blinks, worker log shows no window.
- Press the button: one window, connects if the phone is unlocked.
- Wi-Fi connected and idle 30 min: no device-info calls in the worker log.

### PR 2 (tests)

- Fake clock: eligible 300 s, then `prepare`; activity at 299.9 s, at 300 s, and during the warning each cancel; stale status, unreachable worker, live lease, queued action, Setup/Local, join, reset, maintenance inhibitor and external power each block.
- Daemon (`tests/unit/test_pisugar3_button.py`): register write order, read-back failure stops the sequence and restores bit 5, stale attempt, wrong `daemon_id`, expired attempt, duplicate commit, external power at commit.

### PR 2 (bench)

- On battery, phone away: box powers off 5 min + 10 s after the last activity (±5 s). Next boot journal shows a clean shutdown.
- Power button wakes it; boot search runs once.
- Plug USB power during the warning: no power-off. On USB power for 30 min: stays on.
- Box page: set Auto sleep to 15 min; on battery the box powers off 15 min + 10 s after the last activity. Set Off: stays on for 1 h. The setting survives a reboot.
- Press the button during the warning: stays on, countdown restarts.
- Battery draw with the output cut: below 1 mA (measured on the PiSugar input; target to confirm in step 3).

## Owner decisions (2026-10-26)

1. MCP/SDK request while disconnected: fails, no search.
2. Timeout 5 min by default, changeable in the box page (Settings > Auto sleep).
3. Power off only on battery.
4. Cable pulled: automatic Wi-Fi search.
5. Power-off, not sleep (see "Why power-off, not sleep").
6. Raspberry Pi ships with a PiSugar3 too; auto sleep works there (PR 3, after a bench test).

## Open questions

1. **Auto sleep choices.** Off, 5, 15, 30 min, 1 hour. Other values wanted?
