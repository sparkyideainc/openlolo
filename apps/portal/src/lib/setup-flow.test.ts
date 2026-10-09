import { describe, expect, it } from "vitest";

import type { AppStatus, SetupStatus } from "@/api/types";
import { setupFlow } from "@/lib/setup-flow";

function status(
	over: Partial<SetupStatus> = {},
	autopair = "no_phone",
): SetupStatus {
	return {
		device: "OpenLolo-3E4A",
		hostname: "openlolo-3e4a.local",
		setup_mode: true,
		bound: false,
		session: true,
		access: {
			cable: false,
			setup_ap: true,
			paired_phone: false,
			allowed: true,
		},
		state: "setup_mode",
		last_attempt: null,
		last_error: null,
		usb_pairing: { state: autopair },
		...over,
	};
}

const app = (state: string): AppStatus => ({ connection: { state } });

describe("setupFlow", () => {
	it("waits for the cable first", () => {
		const view = setupFlow({
			setup: status(),
			app: undefined,
			unreachable: false,
			joiningSsid: null,
		});
		expect(view.step).toBe("cable");
		expect(view.track).toEqual(["now", "todo", "todo", "todo"]);
		expect(view.waiting).toBe("Waiting for the iPhone");
	});

	it("moves to trust once the box sees a phone, and flags a declined prompt", () => {
		const pending = setupFlow({
			setup: status({}, "trust_pending"),
			app: undefined,
			unreachable: false,
			joiningSsid: null,
		});
		expect(pending.step).toBe("trust");
		expect(pending.track).toEqual(["done", "now", "todo", "todo"]);
		expect(pending.waiting).toBe("Waiting for Trust on the iPhone");
		const denied = setupFlow({
			setup: status({}, "denied"),
			app: undefined,
			unreachable: false,
			joiningSsid: null,
		});
		expect(denied.track[1]).toBe("bad");
		expect(denied.waiting).toBeNull();
		expect(denied.error).toMatch(/declined/);
	});

	it("asks for Developer Mode after binding and treats the image install as done", () => {
		const bound = status(
			{
				bound: true,
				access: {
					cable: true,
					setup_ap: true,
					paired_phone: false,
					allowed: true,
				},
			},
			"bound",
		);
		const dev = setupFlow({
			setup: bound,
			app: app("developer_mode_required"),
			unreachable: false,
			joiningSsid: null,
		});
		expect(dev.step).toBe("devmode");
		expect(dev.track).toEqual(["done", "done", "now", "todo"]);
		expect(dev.waiting).toBe("Waiting for Developer Mode");
		const image = setupFlow({
			setup: bound,
			app: app("mounting_ddi"),
			unreachable: false,
			joiningSsid: null,
		});
		expect(image.step).toBe("wifi");
		expect(image.track).toEqual(["done", "done", "done", "now"]);
		expect(image.waiting).toBeNull();
	});

	it("shows the Wi-Fi list on the setup network and the switch afterwards", () => {
		const bound = status(
			{
				bound: true,
				access: {
					cable: true,
					setup_ap: true,
					paired_phone: false,
					allowed: true,
				},
			},
			"bound",
		);
		const joining = setupFlow({
			setup: { ...bound, state: "joining", last_attempt: { ssid: "NETGEARZ" } },
			app: app("connected"),
			unreachable: false,
			joiningSsid: "NETGEARZ",
		});
		expect(joining.waiting).toBe("Joining NETGEARZ");
		const switching = setupFlow({
			setup: bound,
			app: app("connected"),
			unreachable: true,
			joiningSsid: "NETGEARZ",
		});
		expect(switching.switchingTo).toBe("NETGEARZ");
		expect(switching.waiting).toBeNull();
		const failed = setupFlow({
			setup: {
				...bound,
				last_attempt: { ssid: "NETGEARZ", result: "WIFI_AUTH_FAILED" },
				last_error: { code: "WIFI_AUTH_FAILED" },
			},
			app: app("connected"),
			unreachable: false,
			joiningSsid: null,
		});
		expect(failed.track[3]).toBe("bad");
		expect(failed.error).toMatch(/password/);
	});

	it("sends a bound but unplugged phone back to the cable step", () => {
		const view = setupFlow({
			setup: status({ bound: true }, "bound"),
			app: { connection: { state: "disconnected" } },
			unreachable: false,
			joiningSsid: null,
		});
		expect(view.step).toBe("cable");
		expect(view.waiting).toBe("Waiting for the iPhone");
	});

	it("finishes once the box is off the setup network with the phone bound", () => {
		const done = setupFlow({
			setup: status(
				{
					bound: true,
					setup_mode: false,
					state: "idle",
					access: {
						cable: true,
						setup_ap: false,
						paired_phone: true,
						allowed: true,
					},
				},
				"bound",
			),
			app: app("connected"),
			unreachable: false,
			joiningSsid: null,
		});
		expect(done.step).toBe("finish");
		expect(done.track).toEqual(["done", "done", "done", "done"]);
	});
});
