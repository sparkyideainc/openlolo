import { describe, expect, it } from "vitest";

import { setupSteps } from "@/lib/setup-steps";

const base = {
	boxName: "OpenLolo 3E4A",
	hostname: "openlolo-3e4a.local",
	error: null,
};

describe("setupSteps", () => {
	it("follows the autopair state while unbound", () => {
		const view = setupSteps({
			...base,
			autopair: "pairing",
			connection: "disconnected",
			setupRequired: true,
		});
		expect(view.headline).toBe("Tap Trust on the iPhone");
		expect(view.steps.map((s) => s.state)).toEqual([
			"done",
			"now",
			"todo",
			"todo",
		]);
		expect(view.steps[0]?.title).toBe("Reachable at openlolo-3e4a.local");
		const denied = setupSteps({
			...base,
			autopair: "denied",
			connection: "disconnected",
			setupRequired: true,
		});
		expect(denied.steps[1]?.state).toBe("bad");
		expect(denied.headline).toBe("Trust was declined");
		const failed = setupSteps({
			...base,
			autopair: "failed",
			connection: "",
			setupRequired: true,
			error: "Cable fell out",
		});
		expect(failed.steps[1]?.detail).toBe("Cable fell out");
	});

	it("follows the connection once bound", () => {
		const dev = setupSteps({
			...base,
			autopair: "bound",
			connection: "developer_mode_required",
			setupRequired: false,
		});
		expect(dev.steps.map((s) => s.state)).toEqual([
			"done",
			"done",
			"now",
			"todo",
		]);
		const image = setupSteps({
			...base,
			autopair: "bound",
			connection: "mounting_ddi",
			setupRequired: false,
		});
		expect(image.steps.map((s) => s.state)).toEqual([
			"done",
			"done",
			"done",
			"now",
		]);
		const done = setupSteps({
			...base,
			autopair: "bound",
			connection: "connected",
			setupRequired: false,
		});
		expect(done.steps.map((s) => s.state)).toEqual([
			"done",
			"done",
			"done",
			"done",
		]);
		expect(done.headline).toBe("Everything is working");
		expect(done.subtitle).toContain("OpenLolo 3E4A controls the iPhone");
		const lost = setupSteps({
			...base,
			autopair: "bound",
			connection: "trust_required",
			setupRequired: false,
		});
		expect(lost.steps[1]?.state).toBe("bad");
	});
});
