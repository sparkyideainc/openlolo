// Wi-Fi copy shared by the setup flow and the Settings › Wi-Fi screen.

import type { NetworkStatus, WifiNetwork } from "@/api/types";

/** USB pairing state copy from the captive page (setup.js), keyed by autopair state. */
export function pairingText(state: string | null | undefined): string {
	switch (state) {
		case "idle":
			return "Connect the iPhone to this box with USB. OpenLolo pairs the cabled phone during setup.";
		case "no_phone":
			return "Plug the iPhone into the box with USB to pair it automatically.";
		case "pairing":
			return "Pairing the cabled iPhone. Unlock it and tap Trust if iOS asks.";
		case "trust_pending":
			return "Unlock the iPhone and tap Trust This Computer to continue.";
		case "locked":
			return "The iPhone is locked. Unlock it; OpenLolo will retry pairing.";
		case "denied":
			return "Trust was declined on the iPhone. Unplug and reconnect it to retry setup.";
		case "multiple_phones":
			return "More than one phone is connected. Leave only the iPhone you want to pair connected.";
		case "binding":
			return "iPhone trusted. Finishing setup…";
		case "bound":
			return "The cabled iPhone is paired with OpenLolo. Some phone-control features may also require Developer Mode.";
		case "failed":
			return "USB pairing needs attention. Check the iPhone connection and retry setup.";
		case "off":
			return "Automatic USB pairing is disabled on this box.";
		default:
			return "USB pairing status is unavailable.";
	}
}

export function wifiErrorText(code: string | null | undefined): string {
	switch (code) {
		case "WIFI_AUTH_FAILED":
			return "That password was not accepted. Check it and try again.";
		case "WIFI_NOT_FOUND":
			return "That network is not available. Check its signal and try again.";
		case "WIFI_TIMEOUT":
			return "OpenLolo could not join in time. Its previous network is being restored; if it cannot reconnect, the setup Wi-Fi will return.";
		default:
			return "The connection failed. Check the network and try again.";
	}
}

/** "Connected · internet OK" / "Connected" / "Connected · no internet". */
export function currentDetail(
	network: NetworkStatus | null | undefined,
): string {
	const raw = network?.internet;
	const internet = (
		typeof raw === "boolean" ? (raw ? "ok" : "down") : (raw ?? "")
	).toLowerCase();
	switch (internet) {
		case "ok":
		case "online":
		case "up":
		case "full":
		case "true":
		case "yes":
			return "Connected · internet OK";
		case "":
			return "Connected";
		default:
			return "Connected · no internet";
	}
}

/** Box profiles are named `openlolo-<network slug>`; show the network part. */
export function savedTitle(profile: string): string {
	if (profile === "openlolo-setup" || !profile.startsWith("openlolo-"))
		return profile;
	return profile.slice("openlolo-".length);
}

export function isSecured(network: WifiNetwork): boolean {
	const security = (network.security ?? "").trim();
	return security !== "" && security !== "--";
}

/** 1–4 bars lit by signal percent. */
export function litBars(percent: number): number {
	return Math.max(1, Math.min(4, Math.ceil(percent / 25)));
}

export function currentSsid(
	network: NetworkStatus | null | undefined,
): string | null {
	const ssid = network?.ssid;
	return typeof ssid === "string" && ssid !== "" ? ssid : null;
}
