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
	| "connect";

function now(): number {
	return Date.now() / 1000;
}

function delay(ms = 150): Promise<void> {
	return new Promise((resolve) => setTimeout(resolve, ms));
}

export function createDemoApi(screen: DemoScreen | null): BoxApi {
	const t = now();
	let session = screen !== "connect";
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
						scopes: ["phone:read", "phone:control", "owner:admin"],
						expires_in: 240,
						created: t - 120,
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
			active_until: { access: t + 8 * 3600 },
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
			active_until: { access: t + 11 * 3600 },
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
				bound: screen !== "setup",
				session,
				access: {
					cable,
					setup_ap: screen === "setup",
					paired_phone: screen !== "connect" && screen !== "setup",
					allowed: cable && screen !== "connect",
				},
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
		async pin() {
			requireSession();
			await delay();
			return {
				pin: String(Math.floor(100000 + Math.random() * 900000)),
				expires_in: 30,
			};
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
