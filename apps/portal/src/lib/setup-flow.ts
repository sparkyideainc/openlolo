// The guided setup as one view model: four tracks (Cable, Trust, Developer Mode, Wi-Fi) that the
// box fills in from `/setup/status` and `/app/status`, plus the one card and the one waiting
// line the owner sees for the step that needs them. Pure, so every state is testable.

import type { AppStatus, SetupStatus } from "@/api/types";
import { wifiErrorText } from "@/lib/wifi";

export type TrackState = "done" | "now" | "todo" | "bad";
export type StepKey = "cable" | "trust" | "devmode" | "wifi" | "finish";

export const TRACKS = ["Cable", "Trust", "Dev Mode", "Wi-Fi"] as const;

export interface FlowInput {
	setup: SetupStatus | undefined;
	app: AppStatus | undefined;
	/** The status poll fails: the box is switching radios (or gone). */
	unreachable: boolean;
	/** The network the owner just asked the box to join from this page. */
	joiningSsid: string | null;
}

export interface FlowView {
	step: StepKey;
	/** 1-based for "Step n of 4"; 4 on Finish too. */
	stepNumber: number;
	track: [TrackState, TrackState, TrackState, TrackState];
	headline: string;
	description: string;
	/** Text of the faded primary button at the bottom; null when nothing is being waited for. */
	waiting: string | null;
	/** Red line replacing the waiting button. */
	error: string | null;
	/** Wi-Fi step only: the box reports it left the setup network for this one. */
	switchingTo: string | null;
}

const PHONE_SEEN = new Set([
	"pairing",
	"trust_pending",
	"locked",
	"binding",
	"bound",
]);
const DEV_MODE_DONE = new Set(["ddi_required", "mounting_ddi", "connected"]);

export function setupFlow({
	setup,
	app,
	unreachable,
	joiningSsid,
}: FlowInput): FlowView {
	const track: FlowView["track"] = ["todo", "todo", "todo", "todo"];
	const base = {
		track,
		waiting: null as string | null,
		error: null as string | null,
		switchingTo: null as string | null,
	};
	if (!setup) {
		track[0] = "now";
		return {
			...base,
			step: "cable",
			stepNumber: 1,
			headline: "Connect your iPhone",
			description: "Use the USB cable. This page moves on by itself.",
			waiting: "Reaching the box",
		};
	}
	const autopair = setup.usb_pairing?.state ?? "idle";
	const bound = setup.bound;
	const conn = app?.connection?.state;
	// A bound phone that is off the cable (and not reachable over Wi-Fi) is back at step 1.
	const unplugged =
		bound && !setup.access.cable && !(conn && DEV_MODE_DONE.has(conn));

	// 1 · Cable
	if (
		unplugged ||
		(!bound && !PHONE_SEEN.has(autopair) && autopair !== "denied")
	) {
		track[0] = autopair === "multiple_phones" ? "bad" : "now";
		return {
			...base,
			step: "cable",
			stepNumber: 1,
			headline:
				autopair === "multiple_phones"
					? "Two phones on the cable"
					: "Connect your iPhone",
			description:
				autopair === "multiple_phones"
					? "Unplug the one you do not want to use."
					: "Use the USB cable. This page moves on by itself.",
			waiting: autopair === "multiple_phones" ? null : "Waiting for the iPhone",
			error:
				autopair === "multiple_phones"
					? "More than one phone is connected."
					: null,
		};
	}
	track[0] = "done";

	// 2 · Trust
	const trustLost =
		conn === "trust_required" || conn === "wifi_pairing_required";
	if (!bound || trustLost) {
		const view = {
			...base,
			step: "trust" as const,
			stepNumber: 2,
			headline: "Trust This Computer",
			description: "Tap Trust on the iPhone, then enter its passcode.",
			waiting: "Waiting for Trust on the iPhone" as string | null,
		};
		switch (trustLost ? "lost" : autopair) {
			case "pairing":
			case "binding":
				track[1] = "now";
				return {
					...view,
					headline: "iPhone found",
					description: "Pairing on the cable. Keep it plugged in.",
					waiting: "Pairing the iPhone",
				};
			case "locked":
				track[1] = "now";
				return {
					...view,
					headline: "Unlock the iPhone",
					description: "The Trust prompt appears once it is unlocked.",
					waiting: "Waiting for the iPhone to unlock",
				};
			case "denied":
				track[1] = "bad";
				return {
					...view,
					headline: "Trust was declined",
					description:
						"Unplug the cable, plug it back in, then tap Trust when the prompt returns.",
					waiting: null,
					error: "Trust was declined on the iPhone.",
				};
			case "failed":
			case "off":
				track[1] = "bad";
				return {
					...view,
					headline: "The box could not pair the iPhone",
					description: "Check the cable and plug it in again.",
					waiting: null,
					error: setup.usb_pairing?.last_error?.message ?? "Pairing failed.",
				};
			case "lost":
				track[1] = "bad";
				return {
					...view,
					headline: "Trust the box again",
					description:
						"The box lost trust. Unplug the cable, plug it back in, then tap Trust.",
					waiting: "Waiting for Trust on the iPhone",
				};
			default:
				track[1] = "now";
				return view;
		}
	}
	track[1] = "done";

	// 3 · Developer Mode (the developer image installs itself afterwards; the owner never sees it)
	if (!conn || !DEV_MODE_DONE.has(conn)) {
		track[2] = "now";
		if (conn === "developer_mode_required") {
			return {
				...base,
				step: "devmode",
				stepNumber: 3,
				headline: "Turn on Developer Mode",
				description: "Progress is saved. Reopen this page after the restart.",
				waiting: "Waiting for Developer Mode",
			};
		}
		return {
			...base,
			step: "devmode",
			stepNumber: 3,
			headline: "Checking the iPhone",
			description: "Keep it plugged in and unlocked.",
			waiting: "Checking the iPhone",
		};
	}
	track[2] = "done";

	// 4 · Box Wi-Fi
	const attempt = setup.last_attempt;
	const joining = setup.state === "joining" || setup.state === "verifying";
	const ssid = joiningSsid ?? attempt?.ssid ?? null;
	if (unreachable && ssid) {
		track[3] = "now";
		return {
			...base,
			step: "wifi",
			stepNumber: 4,
			headline: `The box is switching to ${ssid}`,
			description:
				"It has left its temporary network, so this page cannot reach it for a moment. Nothing is wrong.",
			switchingTo: ssid,
		};
	}
	if (joining) {
		track[3] = "now";
		return {
			...base,
			step: "wifi",
			stepNumber: 4,
			headline: `Joining ${ssid ?? "your Wi-Fi"}`,
			description: "Checking the password with the router.",
			waiting: `Joining ${ssid ?? "your Wi-Fi"}`,
		};
	}
	if (setup.setup_mode) {
		const failed = setup.last_error?.code ?? null;
		track[3] = failed ? "bad" : "now";
		return {
			...base,
			step: "wifi",
			stepNumber: 4,
			headline: failed
				? `Could not join ${attempt?.ssid ?? "that network"}`
				: "Connect the Wi-Fi",
			description: failed
				? "The box reopened its own Wi-Fi so you could come back here."
				: "Your home Wi-Fi. Then join the same network on this iPhone.",
			error: failed ? wifiErrorText(failed) : null,
		};
	}
	track[3] = "done";
	return {
		...base,
		step: "finish",
		stepNumber: 4,
		headline: "Your box is ready",
		description:
			"AI clients you approve can use the iPhone through this box. You can pause them at any time from Home.",
	};
}
