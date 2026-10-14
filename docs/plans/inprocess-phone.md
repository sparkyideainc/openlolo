# Plan: call the phone core in-process (remove the internal HTTP hop)

Status: draft, revised 2026-10-14 after PRs #6-#8 merged. Base: `main` at `8e3f7b5` (merge of PR #8).
No code has changed for this plan. The merged delta since the first draft (refresh-token grace window
in `OAuthProvider`, device-grant form limits, endurance script, SDK example) does not touch
`mcp_client.py`, `http_api.py`, `app_api.py`, `setup_portal.py`, `mcp_server.py`, `auth.py`,
`application/` or `bootstrap.py`, and every fact in 1.2 was re-checked on `8e3f7b5`.

## 1. Where we are

Since PR #8 (now on `main`) the internal Phone API (`interfaces/http_api.py`, `create_app`) has no listener. Every
phone call still goes through it:

```
MCP tool (mcp_server.py)            /app route (app_api.py, behind setup_portal.owner)
        \                                   /
         Gateway.bridge(scope, exempt)  (mcp_http.py)   rate limit, scope, per-client entry
                      |
         PhoneClient (mcp_client.py)    lease token, _mutate, 100 ms polling, settle,
                      |                 observation, cancellation shield, BridgeError
         httpx.AsyncClient + ASGITransport, "Authorization: Bearer <session>"
                      |
         Security middleware -> Starlette routes (http_api.py)
                      |                 Auth.owner(session) = sha256(session); pydantic models;
                      |                 control-route journaling; error envelope
         Phone (application/phone.py) -> Coordinator -> Journal / device
```

Per OAuth client, `Gateway.bridge` opens a row in the `sessions` table (`Auth.open_session`, 8 h),
labels `sha256(session)` in `owner_labels`, and caches `(session, PhoneClient)` by `client_id`.
The owner identity used by leases and the journal is the session digest.

### 1.1 Measured cost of the hop

Simulator backend, Apple-silicon Mac, `device.delay = 0` so only overhead is timed, 50 samples
after 3 warm-ups, re-run on `main` `8e3f7b5`. Script: `bench_hop.py` / `bench_tool.py` (session scratchpad; to be committed as
`apps/device/scripts/bench_hop.py` in step 6).

| Call | Through bridge (p50 / p95) | Direct `Phone` (p50 / p95) | Delta |
|---|---|---|---|
| screenshot, simulator frame 390x844 JPEG, 21 KiB | 3.02 / 3.26 ms | 2.77 / 2.93 ms | 0.25 ms |
| screenshot, phone-sized 709x1536 JPEG, 673 KiB | 8.57 / 9.93 ms | 6.33 / 6.89 ms | 2.2 ms (26 %) |
| status | 8.72 / 13.06 ms | 7.38 / 13.55 ms | 1.3 ms |
| tap, observe=false (130 ms of HID sleeps inside) | 204.0 / 205.4 ms | 202.6 / 203.5 ms with the same 100 ms poll | 1.4 ms |
| tap, observe=false, waiting on the coordinator's `Work.future` | 204.0 ms | **133.6 / 136.7 ms** | **70 ms (35 %)** |
| tap + observation, phone-sized frame | 215.7 / 220.1 ms | **149.9 / 153.5 ms** | **66 ms (31 %)** |
| MCP in-memory `openlolo_tap` end to end (settle 0) | 214.8 / 220.3 ms | n/a | `timing.bridge_seconds` = 0.225 |
| peak allocation, one 673 KiB screenshot | 2,940 KiB | 2,705 KiB | +235 KiB (9 %) |

The last two rows are from the first draft (pre-merge base); the code they exercise is unchanged.

What this says:

- **The HTTP/JSON hop itself is cheap on a Mac**: about 0.2 ms per small call and about 2 ms per
  673 KiB image (base64 + `json.dumps` + `json.loads` + one extra copy). On the box's Cortex-A55
  (Radxa ZERO 3W) and Cortex-A53 (Pi 3B) this is expected to be 5-10x slower, so roughly 10-20 ms
  per observation. That ARM number is an estimate; step 6 measures it.
- **The real cost is the 100 ms polling loop in `PhoneClient._mutate`**: an action that finishes
  just after a poll waits for the next one. A tap loses 70 ms at p50 (35 % of its box time). This
  is not the HTTP hop, but only an in-process caller can await the coordinator's future directly.
  (A long-poll route could also fix it while keeping the hop; see section 9.)

### 1.2 Facts found while exploring (each one changes the plan)

1. **The MCP Pause tool is rate limited.** `openlolo_pause` calls `run(READ_SCOPE, ...)`, which calls
   `resolver.bridge(scope)` without `exempt=True`. Only `/app/pause` passes `exempt=True`. After
   120 calls in a minute, an MCP client's Pause returns `RATE_LIMITED`. ADR intent ("Pause must never
   be throttled") is broken on the MCP door today.
2. **The 30 s sweep drops the box page's bridge and lease.** `Gateway.sweep` drops every bridge whose
   `client_id` fails `provider.client_active`. The page's identity `"Box page"` has no
   `oauth_clients` row, so it is always inactive. Reproduced: the page acquires, `sweep()` runs, then
   `bridges == []` and `coordinator.lease is None`. The page then gets a new session, so it is a new
   owner and `/app/operations/{id}` returns `OPERATION_NOT_FOUND` for its own earlier operations.
   `/app/clients/{id}/revoke` calls `sweep()` too, so a revoke also drops the page's lease.
3. **Owner identity rotates.** Owner = digest of an 8 h session. When the session lapses, or the
   bridge is dropped, the same client becomes a new owner: it loses its lease mid-task and can no
   longer read its own operations. `owner_labels` gains one row per session and is never pruned.
4. **Per-call timeouts are no-ops today.** `httpx.ASGITransport` ignores `timeout=`; a 1 s app with
   `timeout=0.1` returned 200 after 1.0 s. So the 4 s control, 30 s read, 40 s discover and 60 s
   full-page-fetch deadlines in `PhoneClient`/`app_api` do nothing. The only waits that are bounded
   in practice are the outcome deadline in `_mutate` and the 5 s shielded cleanup.
5. **Unexpected exceptions are not mapped.** `ASGITransport` defaults to `raise_app_exceptions=True`,
   so an exception that is not `OpenLoloError`/`ValidationError`/`KeyError`/`TypeError` leaves
   `PhoneClient.request` raw (not as `BridgeError`). A call on a closed bridge raises
   `RuntimeError("Cannot send a request, as the client has been closed.")`, also raw.
6. **The real byte bound on `send_text` is the 16 KiB JSON body, not 10,000 characters.** Through the
   bridge: 10,000 ASCII characters succeed, 5,000 CJK characters (15 KB) succeed, 10,000 `é` (20 KB)
   and 10,000 emoji return `REQUEST_TOO_LARGE`, `dispatched=false`. `open_link` `params` has no count
   or length bound except that same body cap.
7. `KeyError`/`TypeError` raised inside a route are mapped to `INVALID_REQUEST` 400, and `_mutate`
   marks every 4xx as `rejected` (`dispatched=false`). If such an error came after
   `journal.reserve`, the model would be told "nothing ran" for a recorded operation. No current
   code path does this, but the in-process adapter must not keep the blanket catch.

Facts 1-3 and 6 are fixed in step 0 below, independent of the hop removal.

## 2. Goal and non-goals

Goal: MCP tools and `/app` routes call `Phone` and `Coordinator` directly. No JSON/HTTP
serialization, no `Security` middleware, no `sessions` table in-process. Keep, unchanged in
behavior: per-client owner identity, leases and lease tokens, journal dedup by `operation_id`,
`dispatched=false` semantics, the Pause exemption, per-client rate limits, the revocation sweep, the
`openlolo.mcp.entry` timing line and the `timing` object in results.

Non-goals: no change to OAuth, the device grant, the box-page session/cable rules, MCP tool names,
arguments or result shapes, the journal schema of `operations`, or the `/app` JSON contract. No new
public listener.

## 3. Design

### 3.1 What replaces `PhoneClient`: `LocalPhone`, same interface, typed

New `interfaces/local_phone.py` with class `LocalPhone`. It holds `phone`, `coordinator`, `journal`,
`owner: str`, `lease: str | None`, `control_lock`, `wait_seconds`, `observation`, `transport`, and
`closed: bool`. It keeps the `PhoneClient` public methods and their return shapes exactly:

| `PhoneClient` today | `LocalPhone` |
|---|---|
| `status()` | `phone.status(owner)`, then drop `profile.calibration.samples`, add `mcp.transport` |
| `request("GET", "capabilities")` | `capabilities()` -> `phone.capabilities()` |
| `request("GET", "apps?query=..")` | `apps(query, include_hidden)` -> `{"apps", "query", "matched"}`, query cut to 80 |
| `request("GET", "profiles")`, `profiles/{id}` | `app_profiles()`, `app_profile(bundle_id)` |
| `request("GET", "device" / "clipboard" / "discover")` | `device_info()`, `clipboard()`, `discover()` |
| `screenshot(options)` | `CaptureOptions.model_validate`, `phone.screenshot`, `coordinator.accept(owner, lease)`, base64 once |
| `operation(id)` | `phone.operation_status(id, owner)` |
| `action`, `link`, `full_page` | same signatures, through `_mutate` (3.4) |
| `control(command, op, target)` | same lock and lease logic, calls `phone.control(...)` (3.3) |
| `ensure_control()`, `forget(exc)` | unchanged logic |
| `request("POST", "setup/{kind}", ...)` in `app_api.setup` | `setup(kind, operation_id, payload)` |
| `close()` | release under shield, set `closed` |

Step 1 first adds the typed methods to `PhoneClient` (still HTTP) and moves `mcp_server.py` and
`app_api.py` off string paths. A `typing.Protocol` named `PhoneBridge` in `mcp_server.py` replaces
the concrete `PhoneClient` type in `BridgeResolver`, `create_server` and `run`. Then `LocalPhone`
is a drop-in, and the same tests run against both classes until `PhoneClient` is deleted.

Validation stays in `domain/models.py`: `LocalPhone` builds the same request models the routes
built (`ActionRequest`, `LinkRequest`, `FullPageRequest`, `SetupRequest`, `ControlRequest`,
`CaptureOptions`) with `model_validate`, so `StrictModel` (`extra="forbid"`, no inf/nan), the
36-character operation-id pattern and the 128-character lease-token cap apply exactly as before.

### 3.2 Owner identity without sessions

- Owner = `"client:" + digest(client_id)` (SHA-256 hex, the existing `auth.digest`). The prefix
  keeps new owners distinct from legacy 64-hex session digests in `operations.owner` and
  `owner_labels.owner`. The box page uses the same rule with `client_id = PORTAL_IDENTITY`.
- `client_id` is unique across the database (`oauth_clients.client_id` is the primary key, and a
  provider accepts only its own issuer's clients), so two doors never share an owner by accident.
- `Gateway.bridge` calls `journal.label_owner(owner, label)` once when it creates the `LocalPhone`.
  The row is an upsert on a stable key, so `owner_labels` grows by one row per client, not per
  session.
- The lease token stays. With a stable owner, the token is the only thing that tells a stale holder
  (lease lapsed, then re-taken by the same client) from the current one, so
  `LEASE_EXPIRED`/`CONTROL_NOT_OWNED` and "rejected once, then reacquired" keep working unchanged.
- What changes for users: a client keeps its owner across the old 8 h session rotation and across
  bridge drops, so it can still read its own operations. A revoked client's bridge is still dropped
  by the sweep and its lease released; its bearer is refused by the SDK's auth middleware before any
  tool runs.
- `Auth`, `open_session`, `owner`, `logout` and the `sessions` table go away in step 4.
  `Journal._migrate` gets `DROP TABLE IF EXISTS sessions` (an upgrade shim like the existing
  `app_keys` drop). `digest` stays in `interfaces/auth.py`, which then holds only that function.

### 3.3 Control-route journaling moves into `Phone.control`

The 50-line `control` route in `http_api.py` is real logic, not transport. It moves as-is to
`Phone.control(operation_id, owner, command, token=None, target=None)`:

1. `cancel` without `target` -> `OpenLoloError("INVALID_REQUEST", ..., 400)`.
2. `coordinator.check(owner, token, allow_paused=True)` unless the command is `acquire`, `pause`
   or `renew`. (Pause needs no lease and no token: the safety exception.)
3. Unknown command -> `UNKNOWN_OPERATION` 404.
4. `journal.reserve(operation_id, owner, command, {"target": target})`. If the record is not fresh,
   return it; for a replayed successful `acquire`, attach `lease_status(owner, include_token=True)`.
5. `journal.update(..., "DISPATCHED")`, run the command, strip `lease_token` from the stored
   result, `SUCCEEDED` with the public result. For `acquire`/`renew`, put the full result (with
   token) under `control` in the returned record only. On `OpenLoloError`: `FAILED` with
   `as_dict()`, then raise.

Moving it means the journal rows for control commands are written by the same lines as today.

### 3.4 `_mutate` in-process: same contract, no polling

Same steps, same result shape (`operation`, `timing`, `observation` or `observation_error`, or the
full-page fields):

1. `ensure_control()`. Any `BridgeError` here gets `rejected=True` (nothing was recorded).
2. Validate the request model, then call `phone.action` / `phone.open_link` / `phone.full_page`.
   These are synchronous up to `coordinator.submit` (no `await` before `journal.reserve`), so a
   cancellation can never land between "recorded" and "returned".
3. Wait for the outcome with a new `Coordinator.outcome(operation_id, timeout)`:
   `work = self.pending.get(operation_id)`; if present,
   `await asyncio.wait({work.future}, timeout=timeout)`. `asyncio.wait` neither cancels the future
   on timeout nor when the waiting task is cancelled, so a model's cancellation cannot cancel the
   coordinator's work behind its back. Then read `journal.get(operation_id, owner)` as the source of
   truth. Invariant the coordinator already keeps, and step 2 adds a test for: every path writes the
   journal before it resolves or cancels the future (`_loop`, `stop_input`, `cancel`, `close`).
4. Deadline: `wait_seconds` (30 s), or `30 + 15 * max_pages` for a full page, as today. On expiry
   the record is returned as `QUEUED`/`DISPATCHED`, as today.
5. Settle, then observation through the same `screenshot()`. A failed observation becomes
   `observation_error` and never turns a success into a retry. Full page: `fetch` reads
   `phone.full_page_result(op, owner)` and drops its `operation` key, as today.
6. `timing`: `action_seconds`, `settle_seconds`, `observation_seconds`, `bridge_seconds`, same keys
   and same meaning. (`bridge_seconds` keeps its name so existing logs and notes stay comparable.)

### 3.5 Error mapping and `dispatched=false`

`BridgeError` and `REJECTED_INSTRUCTIONS` move unchanged to `local_phone.py` (`mcp_client.py`
is deleted in step 5). One function maps exceptions at the adapter boundary:

| Raised | Becomes | `rejected` (`dispatched=false`) |
|---|---|---|
| `pydantic.ValidationError` while building the request model | `BridgeError("INVALID_REQUEST", op, 400)` | yes (before any phone call) |
| `OpenLoloError` with 400 <= status < 500 from the phone call in `_mutate` step 2 | `BridgeError(code, op, status)`, `forget()` on lost control | yes, as today (the 4xx rule) |
| `OpenLoloError` with status >= 500, or any `OpenLoloError` after `submit` returned | `BridgeError(code, op, status)` | no |
| `OpenLoloError` from reads/control | `BridgeError(code, None, status)` | no (not a mutation) |
| any other `Exception` | logged once with type only, `BridgeError("BACKEND_FAILED", op)` | no if an operation id was recorded, yes if it failed before `submit` |
| any call after `close()` | `BridgeError("LOGIN_REQUIRED", op, 401)` | yes |

The blanket `KeyError`/`TypeError` -> 400 mapping (fact 7) is not carried over: those become
`BACKEND_FAILED`, not "nothing ran". The code sanitizer (alphanumeric plus `_`, at most 80
characters) stays on the way out. The codes `API_UNREACHABLE` and `INVALID_API_RESPONSE` disappear
(no transport). `_acquire` without a token raises `BACKEND_FAILED`. `grep` shows neither old code in
tests or docs.

`app_api.guarded` keeps its status rule (`exc.status`, else 401/403/429/409 by code), so `/app`
statuses are identical. `setup` keeps its two-attempt `LOST_CONTROL` loop.

### 3.6 Cancellation and attempt guards

- **Model cancels a tool call during an action**: same as today. On `CancelledError` in `_mutate`,
  under `anyio.CancelScope(shield=True)` and `anyio.move_on_after(5)`: `phone.control(pause)`, then
  `phone.control(cancel, target=op)`, each with a fresh operation id, errors suppressed; then
  re-raise. The 5 s bound now covers direct coroutine calls, so it really bounds (fact 4).
- **Model cancels during a screenshot**: `coordinator.observe` awaits its own future; cancelling
  it cancels that capture only. Unchanged.
- **Same `operation_id` sent twice at once**: the first call reserves; the second gets the existing
  record from `journal.existing`/`reserve`, finds the same `Work` in `coordinator.pending`, and waits
  on the same future. One dispatch; both callers get the outcome. Different payload with the same id
  -> `OPERATION_CONFLICT` 409, rejected. Unchanged.
- **Two calls race to take control**: `control_lock` in `LocalPhone` serializes `_acquire`;
  `Coordinator.control_lock` serializes acquire and drop. Unchanged.
- **Bridge creation race**: `Gateway.lock` around lookup and creation. Unchanged.
- **Bridge dropped while a call is in flight** (sweep, revoke, shutdown): `_drop` sets `closed`,
  then releases the lease (`drop_control` cancels the running work, so the journal gets
  `OUTCOME_UNKNOWN` or `CANCELLED`). The waiting call wakes, reads the journal and returns that
  state truthfully. Every later call on that object raises `LOGIN_REQUIRED` (rejected) instead of
  today's raw `RuntimeError` (fact 5).
- **Stale lease token**: `forget()` on `LEASE_EXPIRED`/`CONTROL_NOT_OWNED`; the next call
  acquires again; the rejected mutation is never replayed. Unchanged.
- **Setup from the page**: two attempts at most, the second only after `LOST_CONTROL`, which is
  refused before journaling. Unchanged.
- **Deadlines**: the outcome deadline (3.4) and the 5 s shielded cleanup are the bounded waits, as
  in practice today. The httpx `timeout=` arguments that never applied (fact 4) are removed, not
  turned into new deadlines: a new deadline around `discover` or control commands could cancel a
  command between `DISPATCHED` and `SUCCEEDED` and leave a stuck journal row. If a deadline is
  wanted later, it goes inside `Phone`, after journaling is designed for it.

### 3.7 Bounded bytes and rates

| Bound | Today | After |
|---|---|---|
| MCP request body | `MAX_BODY` 65,536 at `EntrySecurity` and the SDK | unchanged |
| `/app` JSON body | `http_api.body()` 16,384 | unchanged; `body()` moves to `app_api.py` |
| `send_text` text | 10,000 chars, plus accidental 16 KiB JSON cap (fact 6) | 10,000 chars **and** explicit `SEND_TEXT_BYTES = 16_384` UTF-8 bytes, checked first in `Phone.action` (before the lease check and the journal), code `REQUEST_TOO_LARGE` 413, rejected (step 0, done) |
| `open_link` params | none except the 16 KiB body | `LinkRequest.params`: at most 8 keys (a profile deep link declares at most 8), key at most 40 chars, value at most 2,048 chars; the filled URL is already capped at 2,048 by `Action.url` (step 0, done) |
| observation image | JPEG, long edge 1,536 | unchanged |
| screenshot | long edge 128-4,096 | unchanged; one base64 copy instead of base64 + JSON + parse (-235 KiB peak per 673 KiB frame) |
| full page | 1-20 pages, long edge 128-2,048, 2 results kept in `Phone.full_pages` | unchanged |
| journal page | `PAGE_LIMIT`, `PAGE_BYTES`, `(created, id)` cursor | unchanged (owner is not part of the cursor) |
| `owner_labels` | one row per session, unpruned | one row per client (and legacy rows) |
| per-client calls | 120/min, 300/min with `owner:admin`; Pause exempt only on `/app` | unchanged limits; Pause exempt on MCP too (step 0) |
| `Gateway.calls` | one list per client id ever seen | pruned in `sweep()` for clients with no call in 60 s |
| action queue | `Coordinator` capacity 16, `QUEUE_FULL` | unchanged |
| outcome waiters | one 100 ms poll loop per in-flight call | one `asyncio.wait` per in-flight call, no wakeups |

### 3.8 Journal dedup and journaling stay identical

- `Phone.action`, `open_link`, `full_page`, `setup` and `Coordinator.submit` do not change. They
  are the only writers of mutation rows, and `Phone.control` (3.3) is the moved control route.
- The fingerprint is HMAC over `[kind, payload]`, and the payload is the same
  `Action.model_dump()` because the same model is validated. So an `operation_id` reused with the
  same arguments returns the recorded operation, and with different arguments returns
  `OPERATION_CONFLICT`, through either path.
- A contract test (step 2) runs one scenario through `PhoneClient` and `LocalPhone` against two
  fresh runtimes and compares the `operations` rows column by column, except `owner`, timestamps and
  ids: same `kind`, same state sequence, same `result`.

### 3.9 Timing lines

- `openlolo.mcp.entry` (`EntrySecurity`) is untouched: method, tool, wall time, status, bytes in/out.
- The `timing` object keeps its keys (3.4). After the change, the gap between the entry line and
  `timing.bridge_seconds` is the MCP framework plus serialization of the result only.

## 4. Steps (each one ships on its own and keeps the suite green)

**Step 0. Independent fixes (no architecture change).** One small PR.
- `openlolo_pause` resolves its bridge with `exempt=True` (add `exempt` to `run`).
- `Gateway.sweep` skips identities that are not OAuth clients: the bridge entry records
  `oauth: bool`, false for `PORTAL_IDENTITY`.
- Explicit `SEND_TEXT_BYTES` and `LinkRequest.params` bounds (3.7).
- Tests: MCP Pause still succeeds after the client hit `RATE_LIMITED`; the page keeps its bridge,
  its lease and its operation lookups across `sweep()`; 8,193 `é` rejected by `Phone.action` with
  `REQUEST_TOO_LARGE` and nothing journaled; 9 params, a 2,049-character value or a 41-character key
  rejected by `LinkRequest`.
- Done on branch `jingerpie/inprocess-step0` (uncommitted): the sweep skips bridges whose token
  holds `owner:admin` (only the box page holds it), so no import of `PORTAL_IDENTITY` is needed.
  Suite 243 -> 247 passed; the two door tests fail on `main` (`RATE_LIMITED` Pause,
  `KeyError: 'Box page'` after the sweep).

**Step 1. Typed bridge interface (still HTTP).**
- Add typed read methods and `setup()` to `PhoneClient`; add `PhoneBridge` Protocol; switch
  `mcp_server.py` and `app_api.py` to typed calls. No behavior change.
- Grep check: `rg 'request\("(GET|POST)"' src/openlolo/interfaces/mcp_server.py src/openlolo/interfaces/app_api.py` -> 0.

**Step 2. Application-layer pieces, used by HTTP first.**
- Add `Phone.control` (3.3); `http_api.control` becomes a 5-line call to it.
- Add `Coordinator.outcome` (3.4) and the journal-before-future invariant test.
- Add `LocalPhone` (3.1, 3.4-3.6) with the error map. Not wired into the gateway yet.
- Parametrize `tests/integration/test_mcp.py`'s `bridge_for` over `["http", "local"]`, so its 11
  tests run against both classes, plus the journal-row contract test (3.8).

**Step 3. Switch the gateway.**
- `Gateway.bridge` builds `LocalPhone(owner="client:"+digest(client_id))`; no session, no httpx;
  `label_owner` once. `_drop` sets `closed`, releases the lease and drops the entry.
- `Gateway.api`, `base_url` and the `create_app` import go away.
- Rollback: revert this one commit; steps 0-2 stay valid.

**Step 4. Remove sessions.**
- Delete `Auth` and `runtime.auth`; `interfaces/auth.py` keeps only `digest`.
- `Journal._migrate`: `DROP TABLE IF EXISTS sessions`; remove it from the schema.
- Move `test_old_app_keys_and_label_code_are_dropped_on_open` to `tests/unit/test_journal.py`.

**Step 5. Remove the HTTP Phone API and `PhoneClient`.**
- Delete `http_api.py` (move `body()` to `app_api.py`) and `mcp_client.py`.
- Drop the `"http"` parameter from `bridge_for`. Port `tests/integration/test_http.py` to
  `tests/integration/test_local_phone.py`: the control and journaling flow, discover/setup kinds,
  full page served once. The Host, Origin, content-type and header checks have no in-process
  equivalent and are dropped with the middleware.
- Port `test_lost_action_response_is_not_replayed` to "a failure after `submit` keeps the operation
  id and is not rejected" (make `journal.get` raise once after submit).
- Update `apps/device/README.md` ("each with its own session" -> per-client owner, no HTTP) and the
  docstrings in `mcp_http.py`, `app_api.py`, `mcp_server.py`.

**Step 6. Measure and record.**
- Commit the benchmark as `apps/device/scripts/bench_hop.py` (direct numbers only after step 5).
- Run it on the Radxa ZERO 3W bench box (simulated backend, no phone needed) and put the numbers in
  the PR description.

Steps 1-2 can be one PR, steps 3-5 one PR, each branched from `main`.

## 5. Tests that move or get added

| File | Today | Change |
|---|---|---|
| `tests/integration/test_mcp.py` | 11 tests (12 items with the 2-mode parametrize), `bridge_for` uses `create_app` + `PhoneClient` | `bridge_for` builds `LocalPhone`; during steps 2-4 parametrized over both classes |
| `tests/integration/test_http.py` | 4 tests on `/api/*` | deleted in step 5; flows move to `test_local_phone.py` |
| `tests/unit/test_auth.py` | 3 tests | 2 deleted with `Auth`; 1 moves to `test_journal.py` |
| `tests/integration/test_mcp_http.py` | 10 tests over OAuth + gateway | unchanged; add one: owner is stable across a bridge drop and the client reads its earlier operation |
| `tests/integration/test_app_api.py` | 9 tests | unchanged; add: page lease survives the sweep (step 0) |
| `tests/integration/test_coordinator.py` | 19 tests | add: journal written before future resolves (each path); `outcome()` timeout leaves the work running |
| new `tests/integration/test_local_phone.py` | - | control journaling parity, error map table (3.5) row by row, closed bridge -> `LOGIN_REQUIRED` rejected, drop during an action -> truthful `OUTCOME_UNKNOWN`/`CANCELLED`, concurrent same-id calls dispatch once, unexpected exception -> `BACKEND_FAILED` |
| `tests/unit/test_setup_portal.py` | 14 tests | unchanged |

## 6. Acceptance criteria

Checks run from `apps/device`:

```
uv run ruff check src tests
uv run --extra mcp ty check src tests
uv run --extra mcp pytest -q --basetemp=/tmp/olpt-inproc
```

1. All three pass after every step. Baseline on `main` `8e3f7b5`: ruff and ty clean, **243 passed** in 136 s.
2. Test count after step 5 is at least 247 (after step 0) - 4 (`test_http.py`) - 2 (`test_auth.py`)
   + 10 (new, listed in section 5) = **251 or more**, and no test is skipped or marked xfail.
3. Grep checks after step 5 (each must print nothing):
   - `rg -n 'create_app|PhoneClient|ASGITransport|open_session|runtime\.auth|class Auth' src`
   - `rg -n '^import httpx|^from httpx' src/openlolo/interfaces`
   - `rg -n 'API_UNREACHABLE|INVALID_API_RESPONSE' src tests`
   - `rg -n 'sessions' src/openlolo/adapters/storage/sqlite.py | rg -v 'portal_sessions|DROP TABLE IF EXISTS sessions'`
   - `rg -n 'asyncio.sleep\(0\.1\)' src/openlolo/interfaces`
4. Latency, simulator, `device.delay = 0`, 50 samples, p50, same machine as the baseline above:
   - tap with `observe=false`, `settle=0`: **at most 140 ms** (baseline 204.0 ms; floor 130 ms of
     HID sleeps).
   - tap with observation, phone-sized frame: **at most 160 ms** (baseline 215.7 ms).
   - screenshot, phone-sized frame: **at most 7.0 ms** (baseline 8.57 ms; direct today 6.33 ms).
5. On the Radxa ZERO 3W, simulated backend: the same three numbers are recorded in the PR; the
   tap-without-observation p50 is at least 60 ms below the bridge p50 measured there before the
   switch.
6. Behavior checks (tests): MCP Pause is never `RATE_LIMITED`; the box page keeps its lease across
   `sweep()`; an operation id reused with identical arguments dispatches once and with different
   arguments returns `OPERATION_CONFLICT` with `dispatched=false`; the journal-row contract test
   passes for every mutation kind; cancelling a long press pauses the coordinator, leaves
   `device.generation is None`, and records `CANCELLED` or `OUTCOME_UNKNOWN` within 0.5 s.
7. `openlolo.mcp.entry` lines and the `timing` keys are unchanged (an existing caplog test in
   `test_mcp_http.py` plus a key-set assertion in `test_mcp.py`).

## 7. Risks

- **Losing transport-side checks.** The Host, Origin and content-type checks and the hardening
  headers go away with `Security`. They guard a listener that no longer exists. The real doors
  (`EntrySecurity`, the box-page `Headers` and `owner` wrapper) keep theirs. Mitigation: grep check 3
  proves no ASGI app for the Phone API remains, so nothing can mount it by mistake.
- **Error semantics drift.** The only intentional changes are fact 5 and fact 7 (raw exceptions and
  `KeyError`/`TypeError` now map to `BACKEND_FAILED`) and the dead codes. Mitigation: the error-map
  table in 3.5 is a test, row by row, and the parametrized `test_mcp.py` runs both classes in steps
  2-4.
- **Future/journal ordering.** Waiting on a future instead of polling the journal relies on "journal
  first, then future". It holds in every coordinator path today; a regression test pins it, and the
  adapter still reads the journal after waking, so a wrong order would show a stale state for one
  read, not a replay.
- **Cancellation through `asyncio.wait`.** If someone changes it to `wait_for`, a timeout would
  cancel the coordinator's work. Covered by the `outcome()` timeout test.
- **Owner change on upgrade.** Operations recorded before the upgrade stay owned by old session
  digests: listed on the box page with their labels, not readable through `operation_status` by the
  client. That is already true today after any restart, because bridges and sessions are in memory.
- **Concurrent edits to `mcp_http.py`.** PRs #6-#8 are merged, so there is no stack any more, but
  `mcp_http.py` keeps changing (the refresh grace window landed with #8). Step 3 touches only
  `Gateway`, not `OAuthProvider`; keep it that way so OAuth work and this change do not conflict.
  The sweep still keys on `client_active`, which counts unrevoked refresh rows; the grace window
  leaves one unrevoked successor per family, so an active client is never swept by mistake.
- **Hardware not exercised.** The bench is blocked (no iOS 27 phone). The change is above the device
  adapter, and the simulator covers the coordinator paths, but HID timing interacts with how fast
  the next call arrives. With 70 ms less wait, a model's next tap reaches the phone sooner. Settle
  defaults (0.5 s, 1.5 s) still apply after every action, so no screen is read earlier than today.

## 8. Rollback

Each step is one commit. Step 3 is the switch; reverting it restores the HTTP bridge while the new
code stays unused. Steps 4-5 delete code and can be reverted as a pair. The `sessions` table drop is
harmless to revert: `CREATE TABLE IF NOT EXISTS` would recreate it empty.

## 9. Recommendation

Worth doing, in two parts:

- **Now: step 0.** Pause on MCP is throttled and the box page loses its lease every 30 s. Both are
  user-visible, small and independent of the hop.
- **Next: steps 1-5, as two PRs (1-2, then 3-5).** PR #8 has merged, so nothing blocks this now. The serialization hop itself is not the reason
  (0.2 ms per call, about 2 ms per image on a Mac, an estimated 10-20 ms on the box). The reasons are:
  the 70 ms p50 per action from polling, which goes away with a direct future wait; about 570 lines
  removed (`http_api.py` 252, `mcp_client.py` 273 mostly folded into `LocalPhone`, `Auth` 45) plus the
  `sessions` table; a stable owner per client; and the removal of timeouts that never worked.

Alternative considered and rejected: keep the hop and add a long-poll `?wait=` to
`/api/operations/{id}`. It gets the latency win in about 30 lines, but keeps a serialization layer,
a session table and a second validation boundary that exist only to talk to the same process.
Pick it only if `main` cannot take another structural change before the next release.
