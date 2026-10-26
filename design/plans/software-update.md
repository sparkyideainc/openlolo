# Software update from the box page

**Status: plan, 2026-10-26. Nothing built yet. UI design: [Box Page Software Update canvas](https://claude.ai/artifact/3H3qXsiiowm6PGVhEEHsTx).**

Today a box changes version only when someone runs `curl … install.sh | sudo sh` over SSH. Owners of a ready-made box have no SSH, so in practice they never update. This plan lets the box find a new GitHub release, ask the owner on the box page, and install it, with no SSH and no way to end up with a box that does not start.

## Decisions (2026-10-26)

### What the owner sees (canvas)

- **The dialog.** When the box knows of a newer release, the box page shows an alert once per version, on whichever tab is open when the page loads: "Update to OpenLolo v0.0.3?", "You have v0.0.2. The update takes about 3 minutes, and phone control stops while the box restarts.", a **View changelog** link, then **Update now** and **Later**.
- **Later** hides the dialog for that version only. The version is kept in the browser (`localStorage`, per viewer convenience); the box does not store it.
- **The badge.** While an update is available, Settings › Box › **Software update** carries a red count badge and the Settings tab icon a red dot.
- **The Software update page** (`/settings/update`): the installed version, then exactly one of: available (card with **View changelog on GitHub** and **Update now**), checking, up to date ("Checked <time>"), updating (three steps: Downloading, Installing, Restarting the box), updated ("Updated to v0.0.3", **What's new in v0.0.3**), failed (red card, "The box is still on v0.0.2 and works as before", **Try again**). **Check for updates** shows when no update is known or after a failure. Footer: "The box checks once a day and asks before installing."
- **While updating**, Back and the tabs are locked and the page says "Keep this page open. It reconnects by itself when the box is back."

### Changelog: a link, not in-app text

The release notes are GitHub's generated list of PR titles. They are written for contributors ("Fix ty check: drop the dead renew control command"), run to 18 lines for 0.0.2, and are Markdown from outside the box, so showing them would mean a Markdown renderer and HTML sanitising on the owner's control page. The page links to the release instead: `html_url` from the GitHub API (`https://github.com/<repo>/releases/tag/v<version>`), opened in a new tab.

Later (PR 4): an owner-facing `## Highlights` section in each release. The box extracts only that section's `- ` lines as plain text (at most 4 lines, 120 characters each, no Markdown rendering) and the dialog shows them above the link. A release without the section shows the link alone.

### Where versions come from

- **Installed version**: `release=` in `/opt/openlolo/current/RELEASE` (stamped by `scripts/package.sh`, the tag without `v`). `openlolo.__version__` is the fallback.
- **Latest version**: `GET https://api.github.com/repos/<update_repo>/releases/latest`. `update_repo` is a new `box.toml` key, default `sparkyideainc/openlolo` (forks set their own, matching `OPENLOLO_REPO` in `install.sh`). Drafts and prereleases are never offered (`/latest` already excludes them).
- **Comparison**: only tags of the form `vMAJOR.MINOR.PATCH` count, compared numerically. A box on a dev build (`dev-…` or `YYYYMMDD-<commit>`) is never offered an update; the page says "Development build, updates are off" and keeps **Check for updates** hidden. Never a downgrade.
- **Asset**: the release must carry exactly one `openlolo-device-<version>.tar.gz` and its `.sha256`, with download URLs under `https://github.com/<update_repo>/releases/download/v<version>/`. Anything else: no offer, logged once per version.

### Who checks, who installs

- **The API checks** (it already has the network and the owner's session). Once a day at a random minute, at boot after the box has internet for 5 minutes, and when the owner taps **Check for updates**. Wi-Fi mode only: in Local and Setup mode the box has no internet, so the check is skipped and the page says "Connect the box to Wi-Fi to check for updates".
- **Root installs**, through a new oneshot unit, the same pattern as `openlolo-reset.service`: `openlolo-update.service` runs `/opt/openlolo/current/deploy/update/openlolo-update.sh`. The API user may only `start` and `stop` it (one more entry in `deploy/polkit/50-openlolo.rules`). The API never passes arguments: it writes the requested version to `/run/openlolo/update-request` (directory owned by the API user), and the root script re-validates it against the GitHub API itself before downloading anything.
- **The script reuses the installer**: download the tarball and `.sha256` with `curl --proto =https`, `sha256sum -c`, unpack under `/var/tmp/openlolo-update/`, run `scripts/install.sh <dir> --upgrade`. `install.sh` already builds in `.staging`, checks imports before touching `current`, swaps, and restores the previous build when the new entry points do not run.
- **Progress** is a JSON file the root script writes atomically (`/var/lib/openlolo/update/state.json`, readable by the API group): `{attempt, version, step: download|install|restart|verify, result: running|succeeded|failed, error_code, started, updated}`. The API reads it, so the page sees progress before the restart and the outcome after it.

### Installer changes for unattended upgrades

Re-running `install.sh` today is not safe without a person at the terminal:

- It defaults to `--remote tailscale`, which runs `remote-access.sh` and can wait for a Tailscale login. An upgrade must leave remote access exactly as it is.
- Without `--debug` it removes the persistent-journal setting a bench box was installed with.
- It prints the setup card and waits up to 30 s for the database.

So:

- `install.sh` records the options it was run with in `/etc/openlolo/install.env` (`remote=`, `debug=`, `wifi_country=`; never auth keys or tunnel tokens).
- New `--upgrade`: read `install.env`, skip the remote-access step and the setup card, keep the previous build as `/opt/openlolo/previous` instead of deleting `current.old`, and print one machine-readable result line. Without `install.env` (boxes installed before this change), `--upgrade` keeps today's journal setting by checking `/etc/systemd/journald.conf.d/openlolo-debug.conf`, and still skips remote access.
- After `--upgrade` starts the services, the update script waits for the new API to answer `GET http://127.0.0.1/setup/status` with the new version within 120 s (`/setup/status` is unauthenticated and gains a `version` field in PR 1). If it does not, it stops the services, moves `previous` back to `current`, starts them again and records `failed` with `VERIFY_FAILED`. On success it deletes `previous`.

## Guards (attempts, cancellation, abuse)

- **One update at a time.** `POST /app/update/install` returns the running attempt (same `attempt` id) instead of starting another. The root script holds `flock /run/openlolo/update.lock`; a second start exits at once.
- **Refused while busy**: reset challenge or reset running (`RESET_RUNNING`), a Wi-Fi join (`PROVISIONING_BUSY`), Local or Setup mode (`NO_INTERNET`), less than 600 MB free on `/opt` and `/var/tmp` (`LOW_DISK`), and on the Lolo Pocket a battery under 30 % while not charging (`LOW_BATTERY`).
- **The version is checked twice**: the API offers only what it read from GitHub, and the root script re-reads the release for the requested version and refuses anything that is not newer than `current/RELEASE`, not a `vX.Y.Z` tag on `update_repo`, or missing the asset or checksum (`INVALID_RELEASE`).
- **Bounded downloads.** The release JSON is read with a 256 KB cap and 10 s timeout; the tarball with a 200 MB cap (`curl --max-filesize`) and 10 min timeout; three attempts with backoff, then `DOWNLOAD_FAILED`. The previous install stays untouched until the checksum passes.
- **Coalesced checks.** Manual checks within 60 s of the last one return the cached result. Conditional requests with `ETag` keep the daily check inside GitHub's 60 requests per hour unauthenticated limit; a 403 or 429 backs off to the `Retry-After` value and the page shows the last known result.
- **Cancellation.** Cancel is allowed only during `download` (the script stops, nothing changed). From `install` on there is no cancel: the staging build either swaps in or is discarded, and `previous` is the way back.
- **Power loss.** Before the swap, the old `current` is untouched (staging is discarded at next start). After the swap, `previous` exists until the health check passes; at boot, a separate root oneshot `openlolo-update-recover.service` (before `openlolo-api`) finishes an interrupted attempt: no healthy API within 120 s means roll back to `previous`.
- **Phone control.** Starting an update ends any control lease (the API stops anyway). If an AI holds control when the owner taps **Update now**, the dialog says so first: "Claude is using the iPhone. Updating stops it."
- **Who can start it**: box page owner session only (`ADMIN_SCOPE`, portal entry). Never exposed through MCP or the SDK.
- **What the page trusts**: version strings and URLs come from the box API, never from GitHub directly, and `html_url` is rebuilt from `update_repo` and the version rather than taken from the response.

## Work

### PR 1: installer `--upgrade` and the update unit (box only, no UI)

- `install.sh`: write `/etc/openlolo/install.env`; `--upgrade` as above; keep `previous`.
- `deploy/update/openlolo-update.sh`, `deploy/systemd/openlolo-update.service`, `openlolo-update-recover.service`, polkit entry.
- `/setup/status` gains `version` (the health check and the page's reconnect read it).
- Usable from SSH on its own: `sudo systemctl start openlolo-update` with a request file, so PR 1 can be benched before any API work.
- Tests: shell-level test for version comparison and request validation (bats-free: a `sh` test script run in CI on Ubuntu with a fake release directory).

### PR 2: API check and endpoints

- `application/update.py`: `UpdateChecker` (daily timer, ETag cache, coalescing, Wi-Fi-mode gate) and attempt state from `state.json`.
- `GET /app/update` → `{installed, latest, available, checked_at, changelog_url, attempt}`; `POST /app/update/check`; `POST /app/update/install {version}`; `POST /app/update/cancel {attempt}`.
- `update_repo` and `update_check` (default `true`) in `config.py`.
- Unit tests: version parsing and ordering, dev builds never offered, asset URL validation, refusal codes, idempotent install, cancel only during download, ETag and 429 handling, state file parsing (missing, partial, stale attempt).

### PR 3: box page (canvas)

- `/settings/update` route, Settings row with badge, tab dot, update dialog in the shell, updating screen that survives the API restart (polls `/setup/status` until the new version answers, then `/app/update`), demo-mode data for every state.
- Portal tests for the dialog rule (once per version, Later remembered per version).

### PR 4 (later): Highlights and release provenance

- Release template gains `## Highlights`; API extracts it as plain text lines (bounded as above); dialog shows them.
- Sign releases with GitHub artifact attestations (`actions/attest-build-provenance`) and verify on the box before installing. Needs a verifier that runs on the box without the `gh` CLI; evaluate `sigstore-python` size and memory on the Radxa first.

## Steps

1. Measure on the Radxa bench: time and peak memory of `install.sh` over an existing install (expected 3 to 5 minutes, mostly `apt-get` and `uv sync`), and free space needed. The dialog's "about 3 minutes" comes from this.
2. PR 1, bench: update 0.0.2 → a test tag on a fork (`update_repo = jingerpie/openlolo`), including a forced verify failure and a power cut during `install`.
3. PR 2 with unit tests; bench the daily check and the manual check against the fork.
4. PR 3 from the canvas; full flow on the bench phone.
5. Docs: the owner guide gets "Updating your box"; the build guide says that `install.sh` over SSH still works.
6. PR 4 when the Highlights template and the verifier are ready.

## Acceptance (bench, Radxa ZERO 3W + PiSugar3, and devkit-3b Pi)

- A newer release on `update_repo` shows the dialog within one page load after the box's next check; **Check for updates** finds it within 15 s.
- **Update now** to a working release: page shows the three steps, reconnects by itself, shows "Updated to vX" within 6 minutes; `current/RELEASE` matches; iPhone reconnects; AI clients, Wi-Fi, pairing and remote access unchanged (Funnel URL still answers `/mcp` 401).
- A release whose API does not start: box rolls back within 3 minutes of the swap, page shows "Update failed" and the old version, everything works as before.
- Power cut during `install` and during `restart`: box boots into a working build (old or new), `state.json` says which.
- No update offered on a dev build, in Local or Setup mode, or for a prerelease or a lower version.
- Second **Update now** during an attempt: same attempt id, one download.
- Idle cost: the checker adds no wakeups outside its daily run; API idle CPU unchanged within 0.5 % of one core.
- Unit, shell and portal suites green.

## Open questions

- Auto-install: never in this plan. Revisit only with attestation verification (PR 4) and an owner opt-in.
- `apt-get update` on every upgrade: needed when `os-packages.txt` changes, slow otherwise. Option: skip it when the new `os-packages.txt` matches the installed one.
- Boxes on 0.0.2 have no `openlolo-update.service`: their first update still needs SSH (`install.sh` once), or the page shows the SSH command when the unit is missing.
