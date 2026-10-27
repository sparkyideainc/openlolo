import { describe, expect, it } from "vitest";

import { sleepLabel } from "@/lib/power";

describe("sleepLabel", () => {
	it("names every auto sleep choice", () => {
		expect([0, 30, 60, 120, 180, 240, 300].map(sleepLabel)).toEqual([
			"Off",
			"30 seconds",
			"1 minute",
			"2 minutes",
			"3 minutes",
			"4 minutes",
			"5 minutes",
		]);
	});
});
