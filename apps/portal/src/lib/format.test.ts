import { describe, expect, it } from "vitest";

import {
	activeUntil,
	clientName,
	clientSubtitle,
	consentFrom,
	expiresText,
	humanize,
	relativeText,
	scopeText,
	shortId,
	shortScopeText,
	text,
	untilText,
	windowText,
} from "@/lib/format";
import { groupByDay, operationLook, operationPill } from "@/lib/operations";
import {
	currentDetail,
	isSecured,
	litBars,
	pairingText,
	savedTitle,
	wifiErrorText,
} from "@/lib/wifi";

const now = new Date(2026, 9, 6, 12, 0, 0); // Oct 6 2026, noon local
const at = (d: Date) => d.getTime() / 1000;

describe("format", () => {
	it("text, names, scopes", () => {
		expect(text(undefined)).toBe("—");
		expect(text("", "x")).toBe("x");
		expect(text(true)).toBe("Yes");
		expect(clientName({ client_name: null, client_id: "abc" })).toBe("abc");
		expect(clientName({})).toBe("Unknown client");
		expect(scopeText(["phone:read", "phone:control", "owner:admin"])).toBe(
			"See screen, tap, type and box settings",
		);
		expect(scopeText(["phone:read"])).toBe("See screen");
		expect(scopeText([])).toBe("No access");
		expect(shortScopeText(["phone:control"])).toBe("Screen and input");
		expect(humanize("waiting_for_device")).toBe("Waiting for device");
		expect(shortId("a".repeat(25))).toBe(`${"a".repeat(20)}…`);
	});

	it("times", () => {
		expect(relativeText(at(now) - 30, now)).toBe("Just now");
		expect(relativeText(at(now) - 300, now)).toBe("5 min ago");
		expect(relativeText(at(now) - 86_400, now)).toBe("Yesterday");
		expect(relativeText(at(new Date(2026, 9, 6, 9, 0)), now)).toMatch(/9:00/);
		expect(relativeText(null)).toBe("—");
		expect(untilText(new Date(2026, 9, 6, 21, 40), now)).toMatch(/^until 9:40/);
		expect(untilText(new Date(2026, 9, 7, 8, 0), now)).toBe("until tomorrow");
		expect(untilText(new Date(2026, 9, 30, 8, 0), now)).toBe("until Oct 30");
		expect(expiresText(0)).toBe("Expired");
		expect(expiresText(45)).toBe("Expires in 45 s");
		expect(expiresText(61)).toBe("Expires in 2 minutes");
		expect(expiresText(60)).toBe("Expires in 1 minute");
		expect(windowText("8h")).toBe("8 hours");
	});

	it("clients and consents", () => {
		const until = at(now) + 3600;
		const client = {
			client_id: "c",
			kind: "oauth",
			scopes: ["phone:control"],
			active_until: { access: until, stale: at(now) - 10 },
		};
		expect(activeUntil(client, now)?.getTime()).toBe(until * 1000);
		expect(clientSubtitle(client, now)).toMatch(
			/^Screen and input · until 1:00/,
		);
		expect(clientSubtitle({ client_id: "c", active_until: {} }, now)).toBe(
			"No active session",
		);
		expect(
			consentFrom({ handle: "h", redirect_hosts: ["claude.ai", ""] }),
		).toBe("claude.ai");
		expect(consentFrom({ handle: "h", issuer: "https://box" })).toBe(
			"https://box",
		);
		expect(consentFrom({ handle: "h" })).toBe("Unknown");
	});
});

describe("operations", () => {
	it("looks and pills", () => {
		expect(operationLook("tap")).toEqual({ icon: "pointer", title: "Tap" });
		expect(operationLook("send_text").title).toBe("Type");
		expect(operationLook("mystery_kind")).toEqual({
			icon: "circle-dashed",
			title: "Mystery kind",
		});
		expect(operationPill("SUCCEEDED")).toEqual({ text: "Done", tone: "good" });
		expect(operationPill("OUTCOME_UNKNOWN").tone).toBe("warn");
		expect(operationPill(undefined).text).toBe("—");
	});

	it("groups by day keeping order", () => {
		const groups = groupByDay(
			[
				{ id: "1", kind: "tap", updated: at(now) - 60 },
				{ id: "2", kind: "tap", updated: at(now) - 86_400 },
				{ id: "3", kind: "tap", updated: at(new Date(2026, 9, 3, 10)) },
				{ id: "4", kind: "tap" },
			],
			now,
		);
		expect(groups.map((g) => g.title)).toEqual([
			"Today",
			"Yesterday",
			"Oct 3",
			"Earlier",
		]);
	});
});

describe("wifi", () => {
	it("copy", () => {
		expect(pairingText("denied")).toContain("declined");
		expect(pairingText("nope")).toBe("USB pairing status is unavailable.");
		expect(wifiErrorText("WIFI_AUTH_FAILED")).toContain("password");
		expect(currentDetail({ internet: "ok" })).toBe("Connected · internet OK");
		expect(currentDetail({ internet: true })).toBe("Connected · internet OK");
		expect(currentDetail({ internet: "none" })).toBe("Connected · no internet");
		expect(currentDetail({})).toBe("Connected");
		expect(savedTitle("openlolo-home-5g")).toBe("home-5g");
		expect(savedTitle("openlolo-setup")).toBe("openlolo-setup");
		expect(isSecured({ ssid: "a", security: "--" })).toBe(false);
		expect(isSecured({ ssid: "a", security: "WPA2" })).toBe(true);
		expect([0, 10, 25, 26, 100].map(litBars)).toEqual([1, 1, 1, 2, 4]);
	});
});
