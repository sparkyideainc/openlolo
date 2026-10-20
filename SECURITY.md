# Security Policy

OpenLolo controls a real iPhone, so we treat security reports as a priority.

## Supported versions

Only the latest [GitHub Release](https://github.com/sparkyideainc/openlolo/releases) receives
security fixes. Update a box to the latest release before you report a problem.

## Reporting a vulnerability

**Do not open a public issue, discussion, or pull request for a security problem.**

Report it privately through GitHub:
[Report a vulnerability](https://github.com/sparkyideainc/openlolo/security/advisories/new).

Include:

- The affected version or commit.
- The component: box service (`apps/device`), box page (`apps/portal`), Python SDK
  (`packages/sdk-python`), or installer (`apps/device/install.sh`).
- Steps to reproduce, and what an attacker gains.

Do not include real credentials, device identifiers, or screenshots of private data. Use
placeholders.

We aim to acknowledge a report within 3 business days and to agree on a disclosure date with
you. We credit reporters in the advisory unless you ask us not to.

## Scope

In scope are weaknesses that break the owner trust boundary, for example:

- Control of the phone, or of the box page, without owner approval.
- Bypassing owner binding, cable-gated sessions, control leases, or explicit pairing.
- Reading the phone screen, clipboard, or typed content without authorization.
- Remote code execution on the box, or tampering with a release during install or update.

Out of scope:

- Attacks that need root on the box or an unlocked phone in the attacker's hands.
- Findings in third-party dependencies with no demonstrated impact on OpenLolo. Report those
  upstream.
- Denial of service from a device on the same LAN.
