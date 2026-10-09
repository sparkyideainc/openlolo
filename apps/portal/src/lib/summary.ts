// The Home status card: box reachability, phone connection and the manual bind fallback.

import type { AppStatus } from "@/api/types";
import { humanize, text } from "@/lib/format";

export type Tone = "good" | "warn" | "bad" | "idle";

/** What the card's button does; the Home screen maps it to an API call. */
export type SummaryAction =
	| { kind: "discover" }
	| {
			kind: "setup";
			step: "pair" | "developer_mode" | "mount_ddi" | "recover";
	  };

export interface BoxSummary {
	headline: string;
	detail: string;
	tone: Tone;
	instruction?: string;
	actionTitle?: string;
	action?: SummaryAction;
}

export function connectionState(status: AppStatus | null | undefined): string {
	return status?.connection?.state ?? "";
}

export function isPhoneConnected(
	status: AppStatus | null | undefined,
): boolean {
	return connectionState(status) === "connected";
}

export function isPaused(status: AppStatus | null | undefined): boolean {
	return status?.control?.paused === true;
}

export function transportText(status: AppStatus | null | undefined): string {
	switch ((status?.connection?.transport ?? "").toLowerCase()) {
		case "usb":
			return "USB";
		case "wifi":
			return "Wi-Fi";
		default:
			return "";
	}
}

export function autopairState(status: AppStatus | null | undefined): string {
	const value = status?.autopair;
	if (typeof value === "string") return value;
	return value?.state ?? "";
}

export function autopairError(
	status: AppStatus | null | undefined,
): string | null {
	const value = status?.autopair;
	if (!value || typeof value === "string") return null;
	return value.last_error?.message ?? null;
}

/** The box is pairing or binding the cabled phone by itself; the owner only answers the phone. */
export function autopairBusy(status: AppStatus | null | undefined): boolean {
	return ["pairing", "trust_pending", "locked", "binding"].includes(
		autopairState(status),
	);
}

export interface SummaryContext {
	/** The page could reach the box on its last polls. */
	reachable: boolean;
	hostname: string;
}

export function boxSummary(
	status: AppStatus | null | undefined,
	context: SummaryContext,
): BoxSummary {
	if (!context.reachable) {
		return {
			headline: "Can't reach the box",
			detail: "No answer from the box page",
			tone: "bad",
			instruction: `Make sure this device is on the same Wi-Fi as the box, then open http://${context.hostname} again.`,
		};
	}
	if (!status) {
		return {
			headline: "Checking the box",
			detail: "Waiting for status",
			tone: "idle",
		};
	}
	if (isPaused(status)) {
		return {
			headline: "Phone control paused",
			detail: "No AI client can act until it resumes",
			tone: "warn",
		};
	}
	const transport = transportText(status);
	const plugged =
		transport === "" ? "Plugged in" : `Plugged in by ${transport}`;
	const message = status.connection?.message;
	switch (connectionState(status)) {
		case "connected":
			return {
				headline: "Everything is working",
				detail: `Box online · reachable at ${context.hostname}`,
				tone: "good",
			};
		case "trust_required":
			return {
				headline: "iPhone needs setup",
				detail: `${plugged} · not trusted yet`,
				tone: "warn",
				instruction:
					"Unlock the iPhone, tap Trust when it asks, then continue.",
				actionTitle: "Continue setup",
				action: { kind: "setup", step: "pair" },
			};
		case "wifi_pairing_required":
			return {
				headline: "iPhone needs setup",
				detail: `${plugged} · Wi-Fi pairing needed`,
				tone: "warn",
				instruction: "Keep the iPhone unlocked and plugged in, then continue.",
				actionTitle: "Continue setup",
				action: { kind: "setup", step: "pair" },
			};
		case "developer_mode_required":
			return {
				headline: "iPhone needs setup",
				detail: `${plugged} · Developer Mode off`,
				tone: "warn",
				instruction:
					"Turn on Developer Mode under Settings › Privacy & Security, then continue.",
				actionTitle: "Continue setup",
				action: { kind: "setup", step: "developer_mode" },
			};
		case "ddi_required":
		case "mounting_ddi":
			return {
				headline: "iPhone needs setup",
				detail: `${plugged} · developer image missing`,
				tone: "warn",
				instruction:
					"The box installs a developer image on the iPhone. Keep it unlocked and continue.",
				actionTitle: "Continue setup",
				action: { kind: "setup", step: "mount_ddi" },
			};
		case "connecting":
		case "switching":
		case "reconnecting":
			return {
				headline: "Connecting to iPhone",
				detail: text(message, "One moment"),
				tone: "idle",
			};
		case "waiting_for_device":
		case "disconnected":
		case "":
			if (status.setup_required) return unboundSummary(status);
			return {
				headline: "iPhone not connected",
				detail: text(message, "Plug it into the box by USB"),
				tone: "warn",
				instruction:
					"Check the cable and unlock the iPhone, then try to recover the connection.",
				actionTitle: "Reconnect iPhone",
				action: { kind: "setup", step: "recover" },
			};
		default:
			return {
				headline: humanize(connectionState(status)),
				detail: text(message, "See activity for details"),
				tone: "idle",
			};
	}
}

/** The card while no phone is bound: the box pairs the cabled phone by itself in setup mode. */
function unboundSummary(status: AppStatus): BoxSummary {
	switch (autopairState(status)) {
		case "no_phone":
		case "idle":
			return {
				headline: "Plug the iPhone into the box",
				detail: "USB cable · the box pairs it automatically",
				tone: "warn",
			};
		case "pairing":
		case "trust_pending":
			return {
				headline: "Tap Trust on the iPhone",
				detail: "Plugged in · waiting for Trust",
				tone: "warn",
				instruction:
					"Unlock the iPhone and tap Trust when it asks, then enter the passcode.",
			};
		case "locked":
			return {
				headline: "Unlock the iPhone",
				detail: "Plugged in · locked",
				tone: "warn",
				instruction: "The Trust prompt appears once the iPhone is unlocked.",
			};
		case "binding":
			return {
				headline: "Binding the iPhone",
				detail: "Trusted · one moment",
				tone: "idle",
			};
		case "denied":
			return {
				headline: "Trust was declined",
				detail: "Plugged in · not trusted",
				tone: "bad",
				instruction: "Unplug the iPhone and plug it back in to be asked again.",
			};
		case "multiple_phones":
			return {
				headline: "Two phones on the cable",
				detail: "The box pairs one phone only",
				tone: "warn",
				instruction: "Unplug the one you do not want to use.",
			};
		default:
			return {
				headline: "No iPhone bound yet",
				detail: autopairError(status) ?? "Plug the iPhone into the box by USB",
				tone: "warn",
				instruction:
					"Connect the iPhone to the box with a cable, then find it.",
				actionTitle: "Find iPhone",
				action: { kind: "discover" },
			};
	}
}

/** The Phone section (manual Find/Bind) shows only when the supervisor is off or failed. */
export function showManualPhoneSection(
	status: AppStatus | null | undefined,
	discovered: number,
): boolean {
	if (discovered > 0) return true;
	if (!status?.setup_required || autopairBusy(status)) return false;
	return ["", "off", "failed"].includes(autopairState(status));
}

/** Footnote under the Pause button (three states, as in the app). */
export function pauseFootnote(status: AppStatus | null | undefined): string {
	if (isPaused(status))
		return "Paused. Resume phone actions from your AI client when ready.";
	if (!isPhoneConnected(status))
		return "Nothing to pause until the iPhone is connected.";
	return "Stops every AI action at once. Resume from your AI client.";
}
