import { describe, expect, it } from "vitest";

import type { UpdateAttempt, UpdateView } from "@/api/types";
import {
	failureText,
	shouldPrompt,
	stepIndex,
	stepLabels,
	updatePhase,
	versionText,
} from "@/lib/update";

function view(changes: Partial<UpdateView> = {}): UpdateView {
	return {
		installed: "0.0.2",
		dev_build: false,
		latest: "0.0.3",
		available: true,
		checked_at: 1_800_000_000,
		changelog_url:
			"https://github.com/sparkyideainc/openlolo/releases/tag/v0.0.3",
		check: { blocked: null, error: null, automatic: true },
		attempt: null,
		...changes,
	};
}

function attempt(changes: Partial<UpdateAttempt> = {}): UpdateAttempt {
	return {
		id: "a1",
		version: "0.0.3",
		step: "download",
		result: "running",
		error_code: null,
		started: 1,
		updated: 2,
		...changes,
	};
}

describe("shouldPrompt", () => {
	it("asks once per version", () => {
		expect(shouldPrompt(view(), null)).toBe(true);
		// Later (or Update now) on 0.0.3 hides it for 0.0.3 only.
		expect(shouldPrompt(view(), "0.0.3")).toBe(false);
		expect(shouldPrompt(view({ latest: "0.0.4" }), "0.0.3")).toBe(true);
	});

	it("never asks without an offer, while updating, or without internet", () => {
		expect(shouldPrompt(undefined, null)).toBe(false);
		expect(shouldPrompt(view({ available: false }), null)).toBe(false);
		expect(shouldPrompt(view({ attempt: attempt() }), null)).toBe(false);
		expect(
			shouldPrompt(
				view({
					check: { blocked: "NO_INTERNET", error: null, automatic: true },
				}),
				null,
			),
		).toBe(false);
	});
});

describe("updatePhase", () => {
	it("follows the offer and the last check", () => {
		expect(updatePhase(undefined)).toBe("unchecked");
		expect(updatePhase(view())).toBe("available");
		expect(updatePhase(view(), { checking: true })).toBe("checking");
		expect(updatePhase(view({ available: false, latest: "0.0.2" }))).toBe(
			"uptodate",
		);
		expect(
			updatePhase(view({ available: false, latest: null, checked_at: null })),
		).toBe("unchecked");
		expect(
			updatePhase(
				view({
					available: false,
					dev_build: true,
					installed: "20261027-1a2b3c4",
				}),
			),
		).toBe("dev");
		expect(
			updatePhase(
				view({
					check: { blocked: "NO_INTERNET", error: null, automatic: true },
				}),
			),
		).toBe("offline");
	});

	it("shows a running attempt before anything else", () => {
		expect(updatePhase(view({ attempt: attempt() }), { checking: true })).toBe(
			"updating",
		);
	});

	it("shows the outcome until the owner closes it", () => {
		const done = view({
			installed: "0.0.3",
			available: false,
			attempt: attempt({ result: "succeeded", step: "restart" }),
		});
		expect(updatePhase(done)).toBe("done");
		expect(updatePhase(done, { acknowledged: "a1" })).toBe("uptodate");
		const failed = view({
			attempt: attempt({ result: "failed", error_code: "VERIFY_FAILED" }),
		});
		expect(updatePhase(failed)).toBe("failed");
		expect(updatePhase(failed, { acknowledged: "a1" })).toBe("available");
		// A failure for a version no longer offered is old news.
		expect(updatePhase({ ...failed, latest: "0.0.4" })).toBe("available");
		// A cancelled download goes back to the offer.
		expect(
			updatePhase(view({ attempt: attempt({ result: "cancelled" }) })),
		).toBe("available");
	});
});

describe("words", () => {
	it("formats versions and steps", () => {
		expect(versionText("0.0.3")).toBe("v0.0.3");
		expect(versionText("20261027-1a2b3c4")).toBe("20261027-1a2b3c4");
		expect(stepLabels("0.0.3")).toEqual([
			"Downloading v0.0.3",
			"Installing",
			"Restarting the box",
		]);
		expect(stepIndex(attempt({ step: "install" }))).toBe(1);
		expect(stepIndex(attempt({ step: "verify" }))).toBe(2);
	});

	it("explains a failure and reassures", () => {
		expect(
			failureText(attempt({ error_code: "DOWNLOAD_FAILED" }), "0.0.2"),
		).toBe(
			"The download stopped before it finished. The box is still on v0.0.2 and works as before.",
		);
		expect(failureText(attempt({ error_code: "SOMETHING_NEW" }), "0.0.2")).toBe(
			"The update did not finish. The box is still on v0.0.2 and works as before.",
		);
	});
});
