# Plan: app permissions enforced by the box

**Status: plan, 2026-10-28. PR 1 (observer + bench, nothing enforced) on
`jingerpie/app-permissions`, draft PR #18, based on `main` 1931bd6. Design agreed between Claude
and Codex; the speed spike (section 3) found no cheaper foreground query. PRs 2-4 not started.**

## Why

A reviewer asked whether an agent that can tap anywhere on a logged-in phone can also tap Send in
the banking app, and whether any allowlist lives on the device or only in the prompt. A prompt
rule can be talked out of by a page the agent reads. A refusal on the box holds no matter what
the agent was told. So the owner's app policy must be enforced by the box, not by the prompt.

## What the box can promise

- **Hard:** the box refuses to dispatch an explicit `launch_app`, `open_url` or `open_link`
  whose normalized target or payload is outside the policy. It does not promise where an allowed
  payload ends up (redirects, in-app web views, deep links that act at once).
- **Best effort:** the foreground gate refuses input and screen reads once a denied, unknown or
  system-only context is detected. A race remains: one tap can open a denied app and act in it
  before the box notices. Surfaces the process list does not show (see section 4) are documented
  limits, not protected.

## Design (agreed)

### Policy

- At setup the owner chooses "Only apps I choose" (preselected; allows nothing until apps are
  picked) or "Full access" (chosen on purpose and confirmed physically).
- Missing, corrupt or reset policy means restricted with nothing allowed, never allow-all.
  v0.0.1 has no real installs, so there is no migration shim.
- Narrowing (removing apps) applies at once. Widening (adding apps, mixed edits, Full access)
  needs physical proof: the page shows an immutable proposal (bound phone, current revision,
  complete resulting policy), then the owner presses the top button the shown number of times
  (random 5-8) and holds. This reuses the mechanics of the reset challenge
  (design/plans/lolo-pocket-modes.md), not its state machine. One arbiter routes button events
  between reset, policy confirmation and normal modes.
- While a proposal is pending, remote input and remote content reads (screenshots, full page,
  clipboard, automatic observations) stop, acknowledged by the worker, and stay stopped until the
  owner hands control back from the page. MCP `resume` cannot clear this stop.
- Boxes without a button can only widen from the authenticated box CLI (console or SSH), never
  from the page. Cable presence alone is not proof.
- Reason: the agent controls the owner's phone, so it could open the box page in Safari and
  widen its own policy.
- The owner gets a separate, privileged inventory of installed apps. The agent sees only allowed
  apps in `apps`, app profiles and the MCP profile resource.

### Enforcement

- Gates live at the worker boundary, where an effect reaches the phone (`begin`, `report`,
  `gesture`, `launch_app`, `clipboard_set`, `capture`), not only in `Phone.action`. Full-page
  swipes and captures are covered too. The deny latch lives outside `io_lock`, so a paced
  gesture cannot hold it off.
- Foreground observer (this PR): owned by `DeviceSession`, on the session's RSD, lazy (opens on
  the first protected request, closes after `foreground_idle_seconds` without one).
- Each answer counts only while the invalidation sequence and session epoch it started under
  still match, and only for `foreground_max_age` measured from the query's start. Every input
  effect invalidates before it is sent. App state notifications only invalidate; they never
  authorize. A request that finds no valid answer waits for a query (bounded), then fails
  closed with `APP_CONTEXT_UNVERIFIED`.
- Generic HID needs exactly one allowed app in front. "system" (Home screen and system surfaces)
  and "ambiguous" (several or unidentified apps) contexts deny taps. The agent recovers with
  `launch_app` of an allowed app; a single Home press is a bounded recovery command.
- Reads: screenshots are refused (an error, not a blank image) in denied, unknown or system-only
  contexts, Home screen included. Clipboard reads are denied in restricted mode. Stored full-page
  results are reauthorized when fetched.
- The Siri button and the Spotlight and app-switcher chords are denied in restricted mode.
  Section 4 adds edge swipes.
- No automatic Home on denial. The guard stop is separate from pause.
- Journal: early refusal (`dispatched=false`), recorded non-dispatch, and partial dispatch are
  told apart.
- Errors: `APP_BLOCKED`, `APP_CONTEXT_UNVERIFIED`, `APP_GUARD_UNAVAILABLE`. The box fails closed
  when DVT or the DDI is unavailable.

### PRs

1. **This PR.** Observer behind `foreground_observer` (default `off`), worker op `foreground`
   for the bench, `scripts/foreground_bench.py`, and the measurements below. Nothing is enforced.
2. Policy store, box page UI and inventory, physical-proof widening with the remote stop,
   launch/URL/list/profile filtering. Kept disabled and unadvertised: it is not a restricted mode
   while raw input and captures still bypass it.
3. Worker gates for input and reads, invalidation of stored content, guard stop, journal contract.
4. Docs and limits, onboarding default, enable.

## 1. Observer (this PR)

`apps/device/src/openlolo/coredevice/foreground.py`:

- `classify()` turns the DVT process list into a `Snapshot`: identified foreground apps, the
  infrastructure processes (`com.apple.springboard`, `com.apple.InputUI`; ignored when deciding
  which app is in front, not a grant for what they show), unidentified foreground apps, and the
  query's start/finish times, sequence and epoch. Its context is `app`, `system` or `ambiguous`.
- `ForegroundObserver` runs one query at a time and lets concurrent requests share it. An answer
  that an invalidation overtook is dropped and retried once, then `FOREGROUND_UNVERIFIED`. A
  failed query closes the source (`FOREGROUND_UNAVAILABLE`) and the next request reopens it.
  `poll` mode polls start-to-start while requests keep coming.
- The manager invalidates in `begin`, `report`, `gesture`, `launch_app` and `clipboard_set`
  before and after the effect, so neither an earlier answer nor one from a query that overlapped
  the effect counts afterwards. The worker op `foreground` returns the answer plus counters and recent
  app state events.

Config (`box.toml`): `foreground_observer = "off" | "demand" | "poll"`, `foreground_max_age`
(1.5 s), `foreground_poll_seconds` (1.0 s), `foreground_idle_seconds` (90 s),
`foreground_query_timeout` (3 s).

## 2. Bench results (2026-10-20)

Radxa ZERO 3W box (hot-patched build 04caa74, same tree as `main` 619d3b3), iPhone 13 on
iOS 27.0.1, **Wi-Fi only**. Raw logs are in the session scratchpad, not committed. Measured
before the invalidation after each effect was added. In poll mode that change can only lengthen
the wait after an action, because a poll that overlapped the action no longer counts.

### Latency per step (`steps`, think gap 3-8 s, settle 0.5 s, floor 0.3 s)

| Mode | Extra wait after action (p50 / p95 / max) | Wait before next action (p50 / p95 / max) | Worker CPU during run |
|---|---|---|---|
| demand | 411 / 613 / 722 ms | 581 / 631 / 648 ms | 32.1% of one core (110 s, 15 steps) |
| poll (1 s) | 214 / 280 / 1178 ms | 9 / 10 / 54 ms | 24.0% of one core (90 s, 12 steps) |

The CPU column includes the actions themselves (launches, HID sessions), so the two runs are not
a clean comparison.

### Cost

- One process-list query: p50 431 ms, p95 468 ms, max 1.73 s (`cost`, 15 queries).
- Worker CPU per query: about 0.2 s on the Radxa. Back-to-back queries use 40% of one core, so
  1 Hz polling costs roughly 15-20% of a core while it runs.
- Observer open with only the notification listener: 8.0% of one core (80 s), against 6.9% with
  the observer closed (connected Wi-Fi baseline ~6%). Idle cost after `foreground_idle_seconds`
  is zero.

### Transition lag (`lag`, back-to-back queries after each action)

- `launch_app`: the new app is reported by the first query. The previous app stays listed for
  about 1 s, so the context reads `ambiguous`, which fails closed.
- Home button: the previous app is still the only one reported for queries started up to
  ~0.6 s after the press (once ~1.5 s), and `system` only after that. **This is a stale "allowed
  app" answer**: an icon tap sent in that window would pass an app gate. PR 3 must not authorize
  generic input from a query started less than ~1 s after a Home press or a gesture that can
  leave the app, or must treat Home as forcing `system` until a later query confirms.
- Icon tap from the Home screen: the observer reported `system` or `FOREGROUND_UNVERIFIED` for
  ~1.3 s, then the new app. Fails closed.

### Overlay matrix (watch mode, phone driven over MCP)

| Surface (over an app unless noted) | What the process list reported | Detectable? |
|---|---|---|
| Control Center (top-right swipe down) | the underlying app only | **No** |
| Siri (side button; setup sheet, Siri off) | the underlying app only | **No** |
| Notification Center (top-left swipe down) | `PassbookUIService` briefly, then `system` | Yes |
| Spotlight (Home screen Search) | `com.apple.Spotlight` as an app | Yes |
| Safari via `open_url` | `ambiguous` (Safari + previous app), then Safari | Yes |
| App switcher | not reached (the swipe scrolled the page) | not tested |
| Share sheet, photo picker, password autofill, Apple Pay, App Store purchase, banner quick reply, alerts | | not tested |

Consequences for PR 3:

- Control Center and Siri must be blocked at the input layer, because the observer cannot see
  them: the Siri button is denied, and restricted mode denies swipes that start in the top edge
  band (Control Center and Notification Center entry). A person can still open them on the
  phone; that is a documented limit.
- `com.apple.springboard` was not always in the foreground list (often only `com.apple.InputUI`),
  so the classifier must not depend on it being present.
- `FOREGROUND_UNVERIFIED` fired 9 times in ~165 s of continuous watching, all around transitions
  (bursts of app state events). Bounded retries are needed so that it does not become a usability
  problem.

## 3. Speed spike (2026-10-27)

Question: can the gate be enforced without slowing each step much? Ideas were reviewed with
Codex first (overlap the post-action check with settle: keep; cheaper query: spike; keep an
answer valid until a notification: needs evidence; skip checks for "safe" input or dispatch
first and check later: rejected). Same box and phone, Wi-Fi, box build `main` d23e736 with this
PR cherry-picked and a bench-only spike op (not committed). The phone now runs ~500 processes
(~340 on 10-20).

### Cheaper query: none found

- **`runningProcesses` (current):** p50 416-800 ms, ~250 ms worker CPU per call. Profile of one
  call: ~0.53 s waiting for the phone, ~0.3 s Python plist decoding (13k objects). Only this
  call reports `foregroundRunning`.
- **DTX round-trip floor** (`isRunningPid:`): ~100 ms. No query can be faster than this.
- **sysmontap** with only `pid` + `priority`: after a 32 KB first sample it streams only changed
  rows (~1.4 KB every 0.25-0.5 s). The foreground app goes to priority 31 and background apps to 4,
  but services such as `WidgetRenderer` and `MomentsUIService` also go to 31, the old app was
  demoted ~3 s after Home, and the stream cost ~10% of a core. Rejected.
- No other DVT service in pymobiledevice3 reports foreground state. The accessibility audit
  service was not tried (it drives the phone's accessibility inspector).

### Notifications cannot extend validity

App state events around agent-driven actions:

- Home from Notes: no event for Notes (it stays "Running" in the background); only
  `WidgetRenderer_ Running` 0.5 s later.
- Launching Notes over Preferences while Notes was still running in the background: no Notes
  event; the first event (`Preferences Suspended`) came 1.5 s later.

So a foreground change can produce no event about the app that changed, or an event 1.5 s late.
Keeping an answer valid until a notification is rejected; notifications stay invalidation-only.

### Latency rerun with invalidation after every effect (demand mode, 15 steps)

| | p50 | p95 | max |
|---|---|---|---|
| Extra wait after action (floor 0.3 s, settle 0.5 s) | 324 ms | 759 ms | 782 ms |
| Wait before next action | 596 ms | 608 ms | 611 ms |

12 of 15 post-action answers were `ambiguous` (old and new app both listed), so with
enforcement they would wait for a later query.

### New: the Home screen reports a widget renderer as an app

On the Home screen the process list now reports `com.apple.chrono.WidgetRenderer-Default` as a
foreground application (it was not seen on 10-20). The classifier therefore reads the Home
screen as context `app`. PR 3 must treat widget renderers (and similar system surfaces) as
`system`, never as an app an owner could allow. Lag after Home: old app alone up to ~0.5 s, old
app plus renderer to ~0.9 s, renderer alone after that. Launch: new app at once, old app listed
until ~0.5-1.2 s.

### Consequence

With the measured calls, the remaining lever is scheduling, not a cheaper query: overlap the
post-action check with settle and capture, and accept one ~0.6 s check before an action that
follows a long think. Expected cost per step is about +0.3 s (p95 ~0.8 s) after an action and
+0.6 s before the next one on Wi-Fi.

### Not measured yet

- USB transport (the phone stayed on Wi-Fi).
- The untested rows of the overlay matrix, repeated Home, lock/unlock, reconnect and observer death.
- Read-only clients after idle, zero settle, long text input, notification storms.
- Hand-driven changes (the agent-driven ones already rule out notification-only validity).
