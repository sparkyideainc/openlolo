import { describe, expect, it } from "vitest";

import type { AppStatus } from "@/api/types";
import {
	boxSummary,
	pauseFootnote,
	showManualPhoneSection,
} from "@/lib/summary";

const context = { reachable: true, hostname: "openlolo-4b2c.local" };

describe("boxSummary", () => {
	it("reports an unreachable box before anything else", () => {
		const summary = boxSummary(
			{ connection: { state: "connected" } },
			{ ...context, reachable: false },
		);
		expect(summary.headline).toBe("Can't reach the box");
		expect(summary.tone).toBe("bad");
		expect(summary.instruction).toContain("http://openlolo-4b2c.local");
	});

	it("narrates the connection states", () => {
		expect(boxSummary(undefined, context).headline).toBe("Checking the box");
		expect(boxSummary({ control: { paused: true } }, context).tone).toBe(
			"warn",
		);
		const good = boxSummary(
			{ connection: { state: "connected", transport: "usb" } },
			context,
		);
		expect(good.tone).toBe("good");
		expect(good.detail).toBe("Box online · reachable at openlolo-4b2c.local");
		const trust = boxSummary(
			{ connection: { state: "trust_required", transport: "usb" } },
			context,
		);
		expect(trust.detail).toBe("Plugged in by USB · not trusted yet");
		expect(trust.action).toEqual({ kind: "setup", step: "pair" });
		const dev = boxSummary(
			{ connection: { state: "developer_mode_required" } },
			context,
		);
		expect(dev.action).toEqual({ kind: "setup", step: "developer_mode" });
		const lost = boxSummary(
			{ connection: { state: "disconnected", message: "Cable out" } },
			context,
		);
		expect(lost.detail).toBe("Cable out");
		expect(lost.action).toEqual({ kind: "setup", step: "recover" });
		expect(
			boxSummary({ connection: { state: "weird_state" } }, context).headline,
		).toBe("Weird state");
	});

	it("narrates automatic pairing while unbound", () => {
		const unbound = (autopair: AppStatus["autopair"]) =>
			boxSummary(
				{
					connection: { state: "disconnected" },
					setup_required: true,
					autopair,
				},
				context,
			);
		expect(unbound({ state: "no_phone" }).headline).toBe(
			"Plug the iPhone into the box",
		);
		expect(unbound("pairing").headline).toBe("Tap Trust on the iPhone");
		expect(unbound({ state: "locked" }).headline).toBe("Unlock the iPhone");
		expect(unbound({ state: "denied" }).tone).toBe("bad");
		expect(unbound({ state: "multiple_phones" }).headline).toBe(
			"Two phones on the cable",
		);
		const failed = unbound({
			state: "failed",
			last_error: { code: "X", message: "Worker died" },
		});
		expect(failed.detail).toBe("Worker died");
		expect(failed.action).toEqual({ kind: "discover" });
	});

	it("shows the manual phone list only when the supervisor cannot act", () => {
		expect(
			showManualPhoneSection(
				{ setup_required: true, autopair: { state: "pairing" } },
				0,
			),
		).toBe(false);
		expect(
			showManualPhoneSection(
				{ setup_required: true, autopair: { state: "failed" } },
				0,
			),
		).toBe(true);
		expect(showManualPhoneSection({ setup_required: false }, 0)).toBe(false);
		expect(showManualPhoneSection({ setup_required: false }, 1)).toBe(true);
	});

	it("explains the pause button", () => {
		expect(pauseFootnote({ control: { paused: true } })).toContain("Paused.");
		expect(pauseFootnote({ connection: { state: "disconnected" } })).toContain(
			"Nothing to pause",
		);
		expect(pauseFootnote({ connection: { state: "connected" } })).toContain(
			"Stops every AI action",
		);
	});
});
