// Software update rules for the page (design/plans/software-update.md): which screen the
// Software update page shows, when the dialog asks, and the words for each outcome.

import type { UpdateAttempt, UpdateView } from "@/api/types";

export type UpdatePhase =
	| "dev"
	| "offline"
	| "checking"
	| "updating"
	| "done"
	| "failed"
	| "available"
	| "uptodate"
	| "unchecked";

/** "v0.0.3" for a release, the raw name for a dev build. */
export function versionText(version: string | null | undefined): string {
	if (!version) return "—";
	return /^\d+\.\d+\.\d+$/.test(version) ? `v${version}` : version;
}

/** The attempt is over and its outcome has not been shown and closed on this browser yet. */
function unacknowledged(
	attempt: UpdateAttempt | null,
	acknowledged: string | null,
): attempt is UpdateAttempt {
	return attempt !== null && attempt.id !== acknowledged;
}

export function updatePhase(
	view: UpdateView | undefined,
	{
		checking = false,
		acknowledged = null,
	}: { checking?: boolean; acknowledged?: string | null } = {},
): UpdatePhase {
	if (!view) return "unchecked";
	const attempt = view.attempt;
	if (attempt?.result === "running") return "updating";
	if (
		unacknowledged(attempt, acknowledged) &&
		attempt.result === "succeeded" &&
		attempt.version === view.installed
	)
		return "done";
	if (view.dev_build) return "dev";
	if (
		unacknowledged(attempt, acknowledged) &&
		attempt.result === "failed" &&
		view.available &&
		attempt.version === view.latest
	)
		return "failed";
	if (checking) return "checking";
	if (view.check.blocked === "NO_INTERNET") return "offline";
	if (view.available) return "available";
	return view.checked_at === null ? "unchecked" : "uptodate";
}

/** The dialog asks once per version: an offered release, nothing running, not yet asked here. */
export function shouldPrompt(
	view: UpdateView | undefined,
	prompted: string | null,
): boolean {
	if (!view) return false;
	return (
		view.available &&
		view.latest !== null &&
		view.latest !== prompted &&
		view.check.blocked === null &&
		view.attempt?.result !== "running"
	);
}

export function stepLabels(version: string): string[] {
	return [
		`Downloading ${versionText(version)}`,
		"Installing",
		"Restarting the box",
	];
}

/** 0 downloading, 1 installing, 2 restarting (root's restart and verify steps). */
export function stepIndex(attempt: UpdateAttempt | null): number {
	if (!attempt) return 0;
	return { download: 0, install: 1, restart: 2, verify: 2 }[attempt.step];
}

const FAILURES: Record<string, string> = {
	DOWNLOAD_FAILED: "The download stopped before it finished.",
	CHECKSUM_MISMATCH: "The download did not match its checksum.",
	INVALID_RELEASE: "The release on GitHub is incomplete.",
	LOW_DISK: "The box ran out of space for the update.",
	INSTALL_FAILED: "The new version did not install.",
	INSTALL_TIMEOUT: "Installing took too long.",
	VERIFY_FAILED: "The new version did not start, so the box went back.",
	INTERRUPTED: "The box lost power or restarted during the update.",
	NOT_STARTED: "The update did not start.",
	STALE: "The update stopped answering.",
};

/** One sentence on why it failed, then the reassurance the canvas shows. */
export function failureText(
	attempt: UpdateAttempt | null,
	installed: string,
): string {
	const reason =
		(attempt?.error_code && FAILURES[attempt.error_code]) ??
		"The update did not finish.";
	return `${reason} The box is still on ${versionText(installed)} and works as before.`;
}

/** Words for a refused install or check (the box's error codes). */
export function refusalText(code: string, fallback: string): string {
	return (
		{
			NO_INTERNET: "Connect the box to Wi-Fi to update it.",
			RESET_RUNNING: "The box is resetting. Try again when it is done.",
			PROVISIONING_BUSY:
				"The box is joining a Wi-Fi network. Try again shortly.",
			LOW_DISK: "The box needs 600 MB free to update.",
			UPDATE_NOT_AVAILABLE: "That update is no longer offered. Check again.",
			DEV_BUILD: "Development builds are not updated from the box page.",
		}[code] ?? fallback
	);
}
