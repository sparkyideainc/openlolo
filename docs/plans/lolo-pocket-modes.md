# Lolo Pocket: Wi-Fi, Local and Setup modes, pairing search, reset by button presses and hold

**Status: plan, 2026-10-18. Supersedes the phone-switch part of `lolo-pocket-top-button.md` (PR 2 `jingerpie/lolo-phone-switch` is dropped) and the typed-name reset. Not started.**

## Decisions (2026-10-18)

### Modes

| Mode | Box network | Phone saved | Entered by | Left by | Status LED |
|---|---|---|---|---|---|
| **Wi-Fi mode** | Joined to the saved Wi-Fi | Yes | Normal state | Hold (Local mode) | Steady on (phone connected) or off |
| **Local mode** | Broadcasts its own Wi-Fi (`OpenLolo-XXXX`) | Yes | Hold | Press, 10 min, a Wi-Fi join from the page | Fast blink |
| **Setup mode** | Broadcasts its own Wi-Fi | No | New box, after a reset, no saved Wi-Fi at boot, offline after the grace period | Phone paired and Wi-Fi joined | Fast blink |

**Pairing mode** is not a network mode: a press (outside Local mode) makes the box actively look for the saved phone on the network for up to 2 minutes, or until it connects. LED slow blink, steady on once connected.

### Buttons (PiSugar3 top button)

| Gesture | Wi-Fi mode | Local mode | Setup mode | Reset challenge open | Reset armed (30 s) |
|---|---|---|---|---|---|
| Press | Pairing mode (2 min search) | Leave Local mode | Ignored | Counts 1 | Cancel the reset |
| Double press | Ignored | Ignored | Ignored | Counts 2 | Cancel the reset |
| Hold, then release | Local mode | Ignored | Ignored | Fails the challenge | **Confirm the reset** |

Holds act on release (the firmware reports no hold length; see `lolo-pocket-top-button.md`). During a Wi-Fi join, presses and holds are ignored. Raspberry Pi GPIO: the 5 s hold is the same "hold" (Local mode, reset confirm); no press.

### Status LED (Radxa green LED)

| Pattern | Meaning | Priority |
|---|---|---|
| Triple flash (3 x 150 ms on/off, then 1.1 s off) | Reset armed: hold the button to erase | 1 |
| One 200 ms flash per counted press, else off | Reset challenge open | 1 |
| Steady on | Reset running | 1 |
| Fast blink, 2 Hz | Local mode or Setup mode | 2 |
| Steady on | Phone connected | 3 |
| Slow blink, 1 Hz | Pairing mode (user-started search) | 4 |
| Off | Not connected, not searching | 5 |

Background safety-net searches do not blink (the LED stays off while the box sleeps).

### Who gets the box page (B: the saved phone's identity is the key)

| Visitor | Bound box | Unbound box |
|---|---|---|
| Saved phone, recognised (below) | Full page, as today | n/a |
| Saved phone on the cable (UDID = binding) | Full page | n/a |
| Any other browser, on home Wi-Fi or the box's Wi-Fi | **Reset prompt only** ("This box belongs to another iPhone") | First-time setup, as today |

Recognition = the browser's address equals an address where the saved phone advertises `_remotepairing._tcp` with an authTag that matches its pairing record (`coredevice/transports.py:196-204`, `interfaces/setup_portal.py:156-170`). Apple's pairing proves the phone; the address links the browser to it. A box-page session cookie (30 days, renewed on use) keeps the owner in after the first match.

Changes against today (`setup_portal.py:172-186`, `allowed = cable and (setup_ap or paired_phone)`):
- The cable is no longer required. `allowed = owner_session or paired_phone or cable_is_bound_phone`.
- "Cable" means the bound UDID on USB, not any one iPhone.
- On the box's own Wi-Fi the blanket `setup_ap` grant is removed for a bound box; the saved phone is recognised the same way as on home Wi-Fi. An unbound box keeps today's setup-AP access for first-time setup.

### Reset

- Only from the box page. Two entry points, one flow:
  - Owner: Settings > Reset this box.
  - Any other browser on a bound box: the reset prompt.
- Flow:
  1. Page shows what is erased (saved phone, Wi-Fi, AI clients and grants) and **Reset**.
  2. **Challenge:** the box picks a random number from 5 to 8 and the page shows "Press the top button 6 times". The page shows the live count (3 / 6); the LED flashes once per counted press. Window: 30 s.
  3. The count reaches exactly N and no further press comes for 2 s: **reset armed** for 30 s. LED triple flash. Page: "Hold the top button to erase. Press once to cancel." with a countdown and **Cancel**.
  4. A hold confirms: reset unit runs, page shows "Resetting", box comes back in Setup mode.
- The challenge fails (nothing armed; page offers "Try again" with a new number) on: more than N presses, a hold, 30 s without reaching N, page Cancel, API restart.
- Armed reset cancels on: a press or double press, page Cancel, 30 s without a hold, API restart.
- The typed box name is removed.
- Why: a stranger on the home Wi-Fi can open the challenge but cannot count presses on a box they cannot touch, so nothing is ever armed and the owner's next hold stays a Local-mode hold. The number is random so it cannot be told to someone in advance.

## Guards (attempts, cancellation, abuse)

- **One challenge or arm at a time.** `POST /reset/start` returns the open attempt (same `attempt` id, `target`, deadline) instead of starting another. Refused while the reset unit runs (`RESET_RUNNING`) or during a Wi-Fi join (`PROVISIONING_BUSY`).
- **Fresh events only.** Presses and holds count only if their flag mtime is newer than the challenge start (or the arm time for the hold). Events latched before are dropped.
- **Press counting needs ordered, uncoalesced events.** The two flag files merge repeated presses into one mtime. The daemon gets a third output: an append-only event log `/run/openlolo/button-events` (one line per event: monotonic sequence number, kind `single|double|long`, daemon receive time). Provisioning reads new lines by sequence number; a gap in sequence numbers fails an open challenge. The flag files stay for the Raspberry Pi backend and older API builds. The log is truncated by the daemon at start and capped at 256 lines (rewritten keeping the last 64).
- **No debounce while counting.** The daemon's same-event debounce (2 s holds, 0.5 s presses) does not apply to the event log; it applies only to the flag files.
- **Double press counts 2.** Three or more quick taps: firmware classification unknown (bench step 0). If taps are lost, the page asks for "about one press per second".
- **A hold during a challenge never opens Local mode;** it fails the challenge. A hold while armed confirms. A hold after either closed is a normal hold.
- **While a challenge is open, presses do not start pairing mode.** If a stranger opened the challenge while the owner presses for a search, the count will not match and nothing happens; the challenge closes after 30 s.
- **Status reads are cheap and coalesced.** The page polls `/setup/status` every 3 s (existing), every 1 s while a challenge or arm is open. Attempt state (`reset: {stage: challenge|armed|resetting, attempt, target, count, expires_in}`) rides on it; `target` is returned only to the browser that started the attempt (attempt id in its request), so another browser cannot read the number.
- **Pairing search is bounded.** One 2 min search at a time; a second press joins it (never extends). It ends on connect, on Local mode, on cable attach, or at 2 min. It is a series of the existing 10 s windows (`manager.search`), so each window keeps today's caps.
- **Phone recognition re-browse stays throttled** (once per 5 s, 30 s cache, `phone.py:267-289`). A non-owner request never triggers more than that.

## Work

### PR 1 (current branch `jingerpie/pisugar`): buttons, LED, modes

- Provisioning:
  - `mode` in health: `wifi` | `local` | `setup` (setup window open and no binding = `setup`; open and bound = `local`; closed = `wifi`). Internal `ProvisioningState` unchanged.
  - Press: in Local mode leave it (`finish("button")`); in Setup mode ignore; otherwise start pairing mode.
  - Hold: Wi-Fi mode -> Local mode; Local and Setup mode -> ignored. (Reset confirm comes in PR 2.)
- Pairing mode: worker op `search` gains `seconds` (cap 120). `Manager.pairing(seconds)` runs windows back to back until connected or the deadline; `connection.pairing` reports it. Background searches set `connection.searching` but not `pairing`.
- Indicator words: `setup` (Local or Setup mode), `connected`, `pairing`, `idle`. The daemon maps `pairing` to slow blink; `searching` no longer drives the LED.
- Daemon: press debounce 0.5 s (holds keep 2 s).
- Avahi reclaim: when setup mode ends, if avahi's host name is not `config.lan_name` (a `-2` suffix after a self-conflict), run `avahi-set-host-name <lan_name>` (polkit/sudo-free: `avahi-daemon` allows it over D-Bus for the `netdev` group, which the API user already has; verify). Logged once.
- Portal: `setup_mode` alone no longer sends a bound box to `/setup` (`routes/index.tsx:14-16`); `mode === "local"` shows the normal app with a "Local mode: the box is broadcasting OpenLolo-XXXX" banner and an "Exit Local mode" button.

### PR 2 (new branch `jingerpie/lolo-reset-hold`): page access B, reset by hold

- `setup_portal.access()`: owner session, paired-phone address match (on any interface, AP included), or the bound UDID on USB. Unbound box keeps today's rules. New field `access.owner` and `access.reason` (`session` | `address` | `cable` | `none`).
- Reset API (portal-mounted; any browser that can load the page may start or cancel; only the box's button advances it): `POST /reset/start` -> `{attempt, target, expires_in}`; `POST /reset/cancel {attempt}`; attempt state in `/setup/status`. A `ResetChallenge` object in the application layer owns the stages (challenge -> armed -> resetting), reads the button event log, and starts the existing `openlolo-reset.service` (`app_api.py:251`). Remove the `POST /app/reset` typed-name path.
- Daemon: append-only event log `/run/openlolo/button-events` (see Guards); flash once per event while the indicator says `challenge`.
- Portal:
  - New route `/reset` for non-owners on a bound box (replaces the `/connect` dead end for them): what is erased, Reset, then the hold screen.
  - Settings: Reset opens the same hold screen.
  - Challenge screen: big number, live count, 30 s countdown, Cancel; fail -> "Try again" (new number).
  - Hold screen: countdown, Cancel, "Press the button once to cancel"; then `resetting` -> `done` (page reconnects to `http://<lan name>/setup`).
- Drop PR 2 of the top-button plan: close `jingerpie/lolo-phone-switch` without merging (branch kept). The forget unit and autopair replace are not needed: reset wipes everything.

### PR 3 (small, optional): setup AP subnet

- Move the box's own Wi-Fi from `192.168.4.1/24` to `10.42.0.1/24` (`adapters/system/setup_ap.py:20`, `setup_portal.py:31`, `cli.py:122`, docs). The bench home LAN uses `192.168.4.0/22` with the router at `192.168.4.1`, the same address as the AP today. Separate PR because it touches the captive portal and every doc that prints the address.

## Steps

0. **Bench checks (block PR 2):**
   - Fast taps: what does `0x08` report for 2, 3, 5 and 8 taps at about 3 per second and at about 1 per second (singles, doubles, lost taps)? Decides the counting rule and the page's pace hint.
   - **Local-mode recognition:** in Local mode, does the saved iPhone advertise `_remotepairing._tcp` on the box's AP (`avahi-browse -rt _remotepairing._tcp` on wlan0 while the phone is joined), and does the authTag match? If not, Local mode falls back to the owner session cookie and the bound-UDID cable; record the result here.
1. PR 1 provisioning modes + pairing op + indicator + daemon debounce + avahi reclaim, unit tests.
2. PR 1 portal Local-mode banner and redirect fix; portal tests.
3. PR 1 bench pass (acceptance below), commit, docs.
4. PR 2 access rule + event log + reset challenge API + guards, unit tests (owner by session / address / cable; non-owner prompt; start idempotent; random target 5-8; exact count arms, N+1 / hold / timeout fail; double counts 2; sequence gap fails; fresh events only; target hidden from other browsers; armed cancel by press, page and timeout; restart clears).
5. PR 2 portal reset flow (non-owner route, Settings, challenge screen, hold screen), portal tests.
6. PR 2 bench pass with a second iPhone as the non-owner.
7. Restore the bench (`setup_timeout_seconds = 600`, remove the reset stub, decide on `allowed_origins`), memory, push and open PRs when asked.

## Acceptance (bench, Radxa ZERO 3W + PiSugar3)

PR 1:
- Press in Wi-Fi mode, phone unlocked on the LAN: LED slow blink within 1 s of release, steady on within 30 s.
- Press with the phone away: slow blink for 120 s (plus or minus 5 s), then off. A second press during it does not extend it.
- Background safety-net search: LED stays off (watch 10 min with the phone away).
- Hold: fast blink within 1 s of release; `OpenLolo-XXXX` visible on a phone within 15 s; `/setup/status.mode == "local"`.
- Press in Local mode: back to Wi-Fi mode, LED to connection state within 2 s; box on the home Wi-Fi within 30 s.
- Two Local-mode toggles 5 s apart: `openlolo-<id>.local` still resolves afterwards (no `-2`).
- Button daemon under 1% of one core; API idle CPU not above today's by more than 0.5% of one core.

PR 2:
- Saved phone, no cable, home Wi-Fi: full page. Same in Local mode (or the documented fallback from step 0).
- Second iPhone, no cable, home Wi-Fi and box Wi-Fi: reset prompt only; every `/app/*` call 401.
- Reset from the second iPhone: page shows a number from 5 to 8; exactly that many presses at one per second arm the reset (triple flash within 2.5 s of the last press); a hold within 30 s erases (box back in Setup mode, no binding, no `openlolo-*` Wi-Fi profile, no AI clients).
- Wrong count (N+1), a hold during the challenge, 31 s without finishing, or Cancel: nothing armed, everything intact.
- Armed, then a press, Cancel, or 31 s without a hold: nothing erased.
- Challenge opened from a second browser while the owner presses 3 times for a phone search: nothing armed; no pairing search during the challenge; normal behaviour after it closes.
- A second browser polling `/setup/status` never sees the target number.
- Unit and portal suites green.

## Open questions

- Should Setup mode end by itself (today: 10 min timeout, then the box keeps re-entering only at boot)? Proposed: unchanged.
- Local mode and Wi-Fi join from the page: joining a new Wi-Fi from Local mode ends Local mode on success (today's behaviour). Keep.
