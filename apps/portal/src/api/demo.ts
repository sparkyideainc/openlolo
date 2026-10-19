// Demo box for screenshots and development: every screen filled with plausible data, nothing
// sent anywhere. Enable with `?demo=1`; `?screen=` picks a preset.

import type { BoxApi } from "@/api/client";
import { ApiError } from "@/api/client";
import type {
	AppStatus,
	Client,
	Consent,
	NetworkStatus,
	Operation,
	ResetAttempt,
	SetupStatus,
	WifiNetwork,
} from "@/api/types";

export type DemoScreen =
	| "access"
	| "settings"
	| "activity"
	| "wifi"
	| "about"
	| "request"
	| "attention"
	| "paused"
	| "setup"
	| "connect"
	| "stranger";

function now(): number {
	return Date.now() / 1000;
}

function delay(ms = 150): Promise<void> {
	return new Promise((resolve) => setTimeout(resolve, ms));
}

export function createDemoApi(screen: DemoScreen | null): BoxApi {
	const t = now();
	let session = screen !== "connect" && screen !== "stranger";
	// Demo reset: the "button" presses itself once a second, arms, then a hold confirms.
	let resetStarted: number | null = null;
	let resetAttempt: string | null = null;
	let resetTarget = 6;
	let resetClosed: ResetAttempt | null = null;
	const resetView = (attempt?: string): ResetAttempt | null => {
		if (resetStarted === null) return resetClosed;
		const elapsed = now() - resetStarted;
		const count = Math.min(resetTarget, Math.floor(elapsed / 1.2));
		const armedAt = resetTarget * 1.2 + 2;
		const stage =
			elapsed < armedAt
				? "challenge"
				: elapsed < armedAt + 6
					? "armed"
					: "resetting";
		return {
			stage,
			attempt: resetAttempt,
			count,
			...(attempt === resetAttempt ? { target: resetTarget } : {}),
			expires_in:
				stage === "resetting"
					? null
					: Math.max(
							0,
							Math.round((stage === "armed" ? armedAt + 30 : 30) - elapsed),
						),
		};
	};
	const cable = true;
	let setupMode = screen === "setup";
	let paused = screen === "paused";
	let status: AppStatus = {
		connection: {
			state: "connected",
			transport: "usb",
			hid_active: true,
			product_version: "27.0",
		},
		transport: "usb",
		control: { active: true, paused, queue_depth: 0 },
		setup_required: false,
		autopair: { state: "bound" },
	};
	if (screen === "attention")
		status.connection = { state: "trust_required", transport: "usb" };
	if (screen === "setup") {
		status = {
			connection: { state: "disconnected" },
			control: { active: false, paused: false, queue_depth: 0 },
			setup_required: true,
			autopair: { state: "pairing", device: "iPhone" },
		};
	}
	let consents: Consent[] =
		screen === "attention"
			? []
			: [
					{
						handle: "demo-request",
						client_id: "claude-desktop",
						client_name: "Claude Desktop",
						redirect_hosts: ["claude.ai"],
						scopes: ["phone:read", "phone:control"],
						expires_in: 240,
						created: t - 120,
					},
					{
						handle: "demo-script",
						client_id: "nightly-script",
						client_name: "nightly.py",
						kind: "device",
						user_code: "BCDF-GHJK",
						scopes: ["phone:read", "phone:control"],
						expires_in: 480,
						created: t - 60,
					},
				];
	let clients: Client[] = [
		{
			client_id: "claude-desktop",
			client_name: "Claude Desktop",
			kind: "oauth",
			scopes: ["phone:read", "phone:control"],
			created: t - 86_400,
			last_used: t - 120,
			active_until: { access: t + 900, refresh: t - 120 + 30 * 86_400 },
			renewed_while_used: true,
			redirect_uris: ["https://claude.ai/api/mcp/auth_callback"],
			self: false,
		},
		{
			client_id: "cursor",
			client_name: "Cursor",
			kind: "oauth",
			scopes: ["phone:read", "phone:control"],
			created: t - 3 * 86_400,
			last_used: t - 1080,
			active_until: { access: t + 2520, refresh: t + 100 * 365 * 86_400 },
			renewed_while_used: false,
			never_expires: true,
			redirect_uris: ["https://cursor.com/callback"],
			self: false,
		},
	];
	let operations: Operation[] = [
		{
			id: "1",
			kind: "tap",
			state: "SUCCEEDED",
			client: "Claude Desktop",
			updated: t - 120,
		},
		{
			id: "2",
			kind: "screenshot",
			state: "SUCCEEDED",
			client: "Claude Desktop",
			updated: t - 125,
		},
		{
			id: "3",
			kind: "type_text",
			state: "OUTCOME_UNKNOWN",
			client: "Cursor",
			updated: t - 1080,
		},
		{
			id: "4",
			kind: "tap",
			state: "SUCCEEDED",
			client: "Cursor",
			updated: t - 1140,
		},
		{
			id: "5",
			kind: "screenshot",
			state: "SUCCEEDED",
			client: "Cursor",
			updated: t - 86_400 - 600,
		},
		{
			id: "6",
			kind: "tap",
			state: "FAILED",
			client: "Claude Desktop",
			updated: t - 86_400 - 20_000,
		},
		{
			id: "7",
			kind: "autopair",
			state: "SUCCEEDED",
			client: "Box",
			updated: t - 4 * 86_400,
		},
	];
	const network: NetworkStatus = {
		ssid: screen === "setup" ? null : "Home-5G",
		ip: screen === "setup" ? "192.168.4.1" : "192.168.1.40",
		internet: screen === "setup" ? "none" : "ok",
		saved:
			screen === "setup"
				? ["openlolo-setup"]
				: ["openlolo-home-5g", "openlolo-studio", "openlolo-setup"],
	};
	const wifi: WifiNetwork[] = [
		{
			ssid: "Home-5G",
			signal: 92,
			security: "WPA2",
			active: screen !== "setup",
		},
		{ ssid: "Home-2.4G", signal: 88, security: "WPA2" },
		{ ssid: "Studio", signal: 61, security: "WPA3" },
		{ ssid: "Guest", signal: 38, security: "" },
	];
	let lastAttempt: SetupStatus["last_attempt"] = null;

	const provisioning = () => ({
		state: setupMode ? "setup_mode" : "idle",
		reason: setupMode ? "demo" : null,
		expires_in: setupMode ? 540 : null,
		setup_name: setupMode ? "OpenLolo-4B2C" : null,
		last_error: null,
		last_attempt: lastAttempt,
	});

	const requireSession = () => {
		if (!session) throw new ApiError("LOGIN_REQUIRED", "Sign in first", 401);
	};

	return {
		async setupStatus() {
			await delay(50);
			return {
				device: "OpenLolo-4B2C",
				hostname: "openlolo-4b2c.local",
				setup_mode: setupMode,
				mode: !setupMode ? "wifi" : screen === "setup" ? "setup" : "local",
				bound: screen !== "setup",
				session,
				access: {
					bound: screen !== "setup",
					cable,
					setup_ap: screen === "setup",
					paired_phone:
						screen !== "connect" && screen !== "setup" && screen !== "stranger",
					allowed: cable && screen !== "connect" && screen !== "stranger",
					reason:
						screen === "connect" || screen === "stranger"
							? "none"
							: screen === "setup"
								? "cable"
								: "address",
				},
				reset_method: "button",
				reset: resetView(),
				state: setupMode ? "setup_mode" : "idle",
				last_attempt: lastAttempt,
				last_error: null,
				usb_pairing:
					typeof status.autopair === "object" ? status.autopair : null,
			};
		},
		async openSession() {
			await delay();
			if (screen === "connect")
				throw new ApiError(
					"PHONE_REQUIRED",
					"Open this page on the iPhone plugged into the box",
					403,
				);
			session = true;
		},
		async closeSession() {
			await delay();
			session = false;
		},
		async status() {
			requireSession();
			return {
				...status,
				control: { ...status.control, paused },
				setup_mode: setupMode,
				pending_consents: consents.length,
			};
		},
		async info() {
			requireSession();
			return {
				version: "0.0.1",
				board: "4b2c",
				entry: "portal",
				setup_mode: setupMode,
				features: { network: true, diagnostics: true },
			};
		},
		async consents() {
			requireSession();
			return consents;
		},
		async clients() {
			requireSession();
			return clients;
		},
		async operations(cursor, limit = 20) {
			requireSession();
			const start = cursor ? Number(cursor) : 0;
			const page = operations.slice(start, start + limit);
			const end = start + page.length;
			return {
				operations: page,
				next_cursor: end < operations.length ? String(end) : null,
				has_more: end < operations.length,
			};
		},
		async network() {
			requireSession();
			return { ...network, provisioning: provisioning() };
		},
		async scan() {
			requireSession();
			await delay(800);
			return wifi;
		},
		async connect(ssid, psk) {
			requireSession();
			await delay(400);
			if (psk === "wrong")
				throw new ApiError(
					"WIFI_AUTH_FAILED",
					"That password was not accepted",
					502,
				);
			lastAttempt = { id: "demo", ssid, result: null };
			setTimeout(() => {
				lastAttempt = { id: "demo", ssid, result: "connected" };
				network.ssid = ssid;
				network.internet = "ok";
				setupMode = false;
			}, 4000);
			return { attempt_id: "demo", ...provisioning(), state: "joining" };
		},
		async forget(ssid) {
			requireSession();
			network.saved = (network.saved ?? []).filter((s) => s !== ssid);
		},
		async setupMode(enabled) {
			requireSession();
			setupMode = enabled;
			return provisioning();
		},
		async pause() {
			requireSession();
			await delay();
			paused = true;
			operations = [
				{
					id: String(operations.length + 1),
					kind: "pause",
					state: "SUCCEEDED",
					client: "Box page",
					updated: now(),
				},
				...operations,
			];
		},
		async decide(handle, allow) {
			requireSession();
			await delay();
			consents = consents.filter((c) => c.handle !== handle);
			return { decided: allow ? "allow" : "deny" };
		},
		async revoke(clientId) {
			requireSession();
			await delay();
			clients = clients.filter((c) => c.client_id !== clientId);
		},
		async discover() {
			requireSession();
			await delay(600);
			return [{ udid: "00008030-001A2B3C4D5E6F7G", name: "iPhone" }];
		},
		async setup(kind) {
			requireSession();
			await delay(300);
			if (kind === "unbind") {
				status = {
					...status,
					setup_required: true,
					connection: { state: "disconnected" },
					autopair: { state: "pairing" },
				};
				return { state: "SUCCEEDED" };
			}
			if (kind === "bind")
				status = {
					...status,
					setup_required: false,
					connection: { state: "connecting" },
				};
			return { state: "QUEUED", id: crypto.randomUUID() };
		},
		async reset(device) {
			await delay(200);
			if (device.trim().toLowerCase() !== "openlolo-dev1") {
				throw new ApiError(
					"RESET_NAME_MISMATCH",
					"Type the box's name exactly as printed on its card",
					400,
				);
			}
			setupMode = true;
			return { state: "reset" };
		},
		async resetStart() {
			await delay();
			resetStarted = now();
			resetAttempt = crypto.randomUUID();
			resetTarget = 5 + Math.floor(Math.random() * 4);
			resetClosed = null;
			return { attempt: resetAttempt, target: resetTarget, expires_in: 30 };
		},
		async resetCancel(attempt) {
			await delay();
			const open = resetView();
			if (!open || (attempt !== resetAttempt && !session))
				return { cancelled: false };
			resetClosed = {
				stage: open.stage === "armed" ? "cancelled" : "failed",
				attempt: resetAttempt,
				reason: "page",
			};
			resetStarted = null;
			return { cancelled: true };
		},
		async resetStatus(attempt) {
			const status = await this.setupStatus();
			return { ...status, reset: resetView(attempt) };
		},
		async diagnostics() {
			requireSession();
			return {
				version: "0.0.1",
				board: "4b2c",
				demo: true,
				exported: new Date().toISOString(),
			};
		},
	};
}
