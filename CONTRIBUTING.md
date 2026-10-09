# Contributing to OpenLolo

Thank you for taking the time to contribute to OpenLolo. Please read this guide
before opening a pull request. OpenLolo uses [vouch](https://github.com/mitchellh/vouch)
to manage contributor trust, so you need to be vouched before your first pull request.

> **Important:** We only accept pull requests that address a single issue. Please do
> not submit pull requests containing multiple unrelated fixes or features. If you have
> multiple contributions, open a separate pull request for each one.

## Getting vouched (required before opening a PR)

**Pull requests from unvouched users are closed automatically.**

Before you open your first pull request, a maintainer needs to vouch for you:

1. Open a [Vouch Request](https://github.com/sparkyideainc/openlolo/issues/new?template=vouch-request.yml) issue.
2. Tell us what you would like to work on and share any relevant background.
3. A maintainer reviews the request and vouches for you by commenting on the issue.
4. Once vouched, your pull requests are accepted normally.

The vouched list lives in [`.github/VOUCHED.td`](.github/VOUCHED.td). Collaborators
with write access and GitHub bots are allowed without an entry. If you are unsure
whether you are already vouched, open a pull request and the check will tell you.

### For maintainers

Comment on a Vouch Request issue to manage the list. The comment must come from a
collaborator with `triage` access or higher.

- `vouch` vouches the issue author. `vouch @user` vouches a different user.
- `unvouch` or `unvouch @user` removes a user from the list.
- `denounce` or `denounce @user` blocks a user. Their pull requests are closed even
  if `require-vouch` is later relaxed.

Add a reason after the keyword if you want it recorded in the issue thread. The
workflow commits the updated `.github/VOUCHED.td` to `main`.

## Developing

The development branch is `main`. All pull requests are made against `main`.

### Prerequisites

- [uv](https://docs.astral.sh/uv/) 0.12 or newer
- Python 3.11, 3.13, or 3.14

### Setup

```sh
uv sync --project apps/device --frozen --dev --extra mcp
uv run --project apps/device pre-commit install  # Once per clone; runs ruff and ty on staged files at each commit.
```

### Checks

Run the same checks as CI before you push:

```sh
cd apps/device
uv run --frozen ruff check src tests
uv run --frozen ruff format --check src tests
uv run --frozen --extra mcp ty check src tests
uv run --frozen --extra mcp pytest -q
uv build
```

TypeScript workspaces (`apps/portal`, `packages/ui`) use Bun:

```sh
bun install
bun run check-types
bun run check
```

Type hints are gradual: `ty` checks annotated code and infers the rest, so annotate
new and touched code.

### Project boundaries

Read [`apps/device/AGENTS.md`](apps/device/AGENTS.md) before changing behavior. Preserve
owner binding, authentication, control leases, and explicit pairing requirements. Do not
add cloud orchestration or persistent video without an accepted design discussion. The
`/app/*` API and the box page's cable-gated session are the owner trust boundary; a change
to it needs a design discussion before code.

## Pull request workflow

1. Open or find an issue describing the change. Reference it with `Closes #<issue>`.
2. Keep the pull request focused on that one issue.
3. Fill in the pull request template, including how you tested the change.
4. Make sure CI passes. A maintainer reviews once checks are green.

## Reporting issues

Use the issue templates for bug reports and feature requests. Do not include
credentials, device identifiers, screenshots of private data, or typed content in
issues.
