import { describe, expect, it } from "vitest";

import { sleepLabel } from "@/lib/power";

describe("sleepLabel", () => {
	it("names every auto sleep choice", () => {
		expect([0, 5, 15, 30, 60].map(sleepLabel)).toEqual([
			"Off",
			"5 min",
			"15 min",
			"30 min",
			"1 hour",
		]);
		expect(sleepLabel(120)).toBe("2 hours");
	});
});
