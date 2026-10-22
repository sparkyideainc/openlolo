# Plan: app permissions enforced by the box

Status: PR 1 (observer + bench) on `jingerpie/app-permissions`, 2026-10-20. Design agreed between
Claude and Codex; PRs 2-4 not started. Base: `main` at `619d3b3`.

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
  (docs/plans/lolo-pocket-modes.md), not its state machine. One arbiter routes button events
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

### Not measured yet

- USB transport (the phone stayed on Wi-Fi).
- The untested rows of the overlay matrix, repeated Home, lock/unlock, reconnect and observer death.
- Read-only clients after idle, zero settle, long text input, notification storms.
- A lighter DVT call than the full process list. At 0.2 s CPU per query, this decides whether
  poll mode is affordable on the Radxa.
