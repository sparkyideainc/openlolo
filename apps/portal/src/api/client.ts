// The box API as the page uses it. Same origin, cookie session, one custom header on every
// request (the box refuses writes without it; see setup_portal.py).

import type {
	AppInfo,
	AppStatus,
	Client,
	Consent,
	ConsentWindow,
	DiscoveredPhone,
	NetworkStatus,
	OperationsPage,
	ProvisioningHealth,
	SetupKind,
	SetupStatus,
	WifiNetwork,
} from "@/api/types";

export class ApiError extends Error {
	readonly code: string;
	readonly status: number;

	constructor(code: string, message: string, status: number) {
		super(message);
		this.name = "ApiError";
		this.code = code;
		this.status = status;
	}
}

export function isApiError(error: unknown, code?: string): error is ApiError {
	return (
		error instanceof ApiError && (code === undefined || error.code === code)
	);
}

/** The box could not be reached at all (page offline, box switching Wi-Fi). */
export class OfflineError extends Error {
	constructor() {
		super("The box did not answer.");
		this.name = "OfflineError";
	}
}

const HEADERS = { "X-OpenLolo-Client": "portal" };

async function request<T>(
	method: "GET" | "POST" | "DELETE",
	path: string,
	body?: unknown,
): Promise<T> {
	let response: Response;
	try {
		response = await fetch(path, {
			method,
			cache: "no-store",
			credentials: "same-origin",
			headers:
				body === undefined
					? HEADERS
					: { ...HEADERS, "Content-Type": "application/json" },
			body: body === undefined ? undefined : JSON.stringify(body),
		});
	} catch {
		throw new OfflineError();
	}
	const text = await response.text();
	let data: unknown = null;
	if (text !== "") {
		try {
			data = JSON.parse(text);
		} catch {
			data = null;
		}
	}
	if (!response.ok) {
		const error = (data as { error?: unknown } | null)?.error;
		if (error && typeof error === "object") {
			const { code, message } = error as { code?: string; message?: string };
			throw new ApiError(
				code ?? "REQUEST_FAILED",
				message ?? `Request failed (${response.status}).`,
				response.status,
			);
		}
		const message =
			typeof error === "string"
				? error
				: `Request failed (${response.status}).`;
		throw new ApiError("REQUEST_FAILED", message, response.status);
	}
	return data as T;
}

export interface BoxApi {
	setupStatus(): Promise<SetupStatus>;
	openSession(): Promise<void>;
	closeSession(): Promise<void>;
	status(): Promise<AppStatus>;
	info(): Promise<AppInfo>;
	consents(): Promise<Consent[]>;
	clients(): Promise<Client[]>;
	operations(cursor?: string | null, limit?: number): Promise<OperationsPage>;
	network(): Promise<NetworkStatus>;
	scan(): Promise<WifiNetwork[]>;
	connect(
		ssid: string,
		psk: string,
	): Promise<{ attempt_id?: string } & ProvisioningHealth>;
	forget(ssid: string, force: boolean): Promise<void>;
	setupMode(enabled: boolean): Promise<ProvisioningHealth>;
	pause(): Promise<void>;
	decide(
		handle: string,
		allow: boolean,
		admin: boolean,
		window: ConsentWindow,
	): Promise<{ decided: string }>;
	pin(
		handle: string,
		admin: boolean,
		window: ConsentWindow,
	): Promise<{ pin: string; expires_in: number }>;
	revoke(clientId: string): Promise<void>;
	discover(): Promise<DiscoveredPhone[]>;
	setup(
		kind: SetupKind,
		payload: Record<string, unknown>,
	): Promise<{ state?: string; id?: string }>;
	diagnostics(): Promise<unknown>;
	/** Back to first-time setup; the box checks the typed name too. */
	reset(device: string): Promise<{ state: string }>;
}

function operationId(): string {
	return crypto.randomUUID();
}

export const liveApi: BoxApi = {
	setupStatus: () => request("GET", "/setup/status"),
	openSession: () => request("POST", "/setup/session", {}),
	closeSession: () => request("DELETE", "/setup/session"),
	status: () => request("GET", "/app/status"),
	info: () => request("GET", "/app/info"),
	consents: async () =>
		(await request<{ consents: Consent[] }>("GET", "/app/consents")).consents,
	clients: async () =>
		(await request<{ clients: Client[] }>("GET", "/app/clients")).clients,
	operations: (cursor, limit = 20) => {
		const query = new URLSearchParams({ limit: String(limit) });
		if (cursor) query.set("cursor", cursor);
		return request("GET", `/app/operations?${query}`);
	},
	network: () => request("GET", "/app/network"),
	scan: async () =>
		(
			await request<{ networks: WifiNetwork[] }>(
				"POST",
				"/app/network/scan",
				{},
			)
		).networks,
	connect: (ssid, psk) =>
		request("POST", "/app/network/connect", { ssid, psk }),
	forget: (ssid, force) =>
		request("POST", "/app/network/forget", { ssid, force }),
	setupMode: (enabled) =>
		request("POST", "/app/network/setup_mode", { enabled }),
	reset: (device) => request("POST", "/app/reset", { device }),
	pause: () => request("POST", "/app/pause", { operation_id: operationId() }),
	decide: (handle, allow, admin, window) =>
		request("POST", `/app/consents/${encodeURIComponent(handle)}`, {
			decision: allow ? "allow" : "deny",
			admin,
			window,
		}),
	pin: (handle, admin, window) =>
		request("POST", `/app/consents/${encodeURIComponent(handle)}/pin`, {
			admin,
			window,
		}),
	revoke: (clientId) =>
		request("POST", `/app/clients/${encodeURIComponent(clientId)}/revoke`, {}),
	discover: async () =>
		(await request<{ usb?: DiscoveredPhone[] }>("GET", "/app/discover")).usb ??
		[],
	setup: (kind, payload) =>
		request("POST", `/app/setup/${kind}`, {
			operation_id: operationId(),
			payload,
		}),
	diagnostics: () => request("GET", "/app/diagnostics"),
};
