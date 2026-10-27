// Shapes of the box's JSON as the page reads them. The box is the source of truth
// (docs/api.md); every field the page does not read is left open.

export interface ApiErrorBody {
	code: string;
	message?: string;
}

/** GET /setup/status — unauthenticated bootstrap. */
export interface SetupStatus {
	device: string | null;
	hostname: string;
	setup_mode: boolean;
	/** wifi: on the saved Wi-Fi; local: own Wi-Fi for the saved phone; setup: own Wi-Fi, no phone saved. */
	mode?: "wifi" | "local" | "setup";
	bound: boolean;
	session: boolean;
	/** Who may open the page from this browser, and why not (ADR 0012). */
	access: PageAccess;
	state: "disabled" | "idle" | "setup_mode" | "joining" | "verifying" | string;
	last_attempt: WifiAttempt | null;
	last_error: { code: string } | null;
	usb_pairing: AutopairStatus | null;
	/** button: the top button finishes a reset (Lolo Pocket); name: the owner types the box name. */
	reset_method?: "button" | "name";
	/** The open or just-finished reset attempt; `target` only for the browser that started it. */
	reset?: ResetAttempt | null;
	/** Installed release (`0.0.3`, or a dev build name). */
	version?: string;
}

export interface PageAccess {
	/** A phone is saved: only that phone gets the page; everyone else sees the reset prompt. */
	bound?: boolean;
	/** Exactly one phone is on the box's USB port. */
	cable: boolean;
	/** This browser is on the box's setup Wi-Fi. */
	setup_ap: boolean;
	/** This browser is the paired phone (its LAN address matched). */
	paired_phone: boolean;
	allowed: boolean;
	/** The proof that admitted this browser. */
	reason?: "address" | "cable" | "none";
}

export interface ResetAttempt {
	stage: "challenge" | "armed" | "resetting" | "failed" | "cancelled" | "done";
	attempt: string | null;
	count?: number;
	target?: number;
	expires_in?: number | null;
	/** Why a failed or cancelled attempt ended. */
	reason?:
		| "too_many"
		| "hold"
		| "timeout"
		| "page"
		| "press"
		| "lost"
		| "reset_failed"
		| "reset";
}

export interface ResetStart {
	attempt: string;
	target: number;
	expires_in: number;
}

export interface WifiAttempt {
	id?: string;
	ssid?: string;
	result?: "connected" | "failed" | string | null;
	error?: ApiErrorBody | null;
	[key: string]: unknown;
}

export interface AutopairStatus {
	state: string;
	device?: string | null;
	last_error?: ApiErrorBody | null;
	retry_in?: number | null;
}

/** GET /app/status — the Phone API status plus `setup_mode` and `pending_consents`. */
export interface AppStatus {
	connection?: {
		state?: string;
		transport?: string;
		message?: string;
		[key: string]: unknown;
	};
	/** `active`: someone holds the control lease; `owned`: this page holds it. */
	control?: {
		active?: boolean;
		owned?: boolean;
		paused?: boolean;
		queue_depth?: number;
	};
	transport?: string;
	setup_required?: boolean;
	setup_mode?: boolean;
	pending_consents?: number;
	autopair?: AutopairStatus | string | null;
	[key: string]: unknown;
}

export interface AppInfo {
	version?: string;
	board?: string | null;
	entry?: string;
	/** Remote MCP URL (Tailscale Funnel or tunnel) for AI apps; null when remote access is off. */
	mcp_url?: string | null;
	setup_mode?: boolean;
	features?: { network?: boolean; diagnostics?: boolean };
}

export interface Consent {
	handle: string;
	client_id?: string;
	client_name?: string | null;
	redirect_hosts?: string[];
	scopes?: string[];
	issuer?: string;
	// "device": a script using the device grant; it prints the same user_code.
	kind?: "oauth" | "device" | string;
	user_code?: string | null;
	expires_in?: number;
	created?: number;
}

export interface Client {
	client_id: string;
	client_name?: string | null;
	kind?: "oauth" | string;
	redirect_uris?: string[];
	approved?: boolean;
	created?: number;
	last_used?: number | null;
	active_until?: Record<string, number>;
	/** The grant's end moves to 30 days after each use. */
	renewed_while_used?: boolean;
	/** Lasts until the owner removes the client. */
	never_expires?: boolean;
	issuer?: string | null;
	scopes?: string[];
	self?: boolean;
}

export interface Operation {
	id: string;
	kind: string;
	state?: string;
	client?: string | null;
	created?: number;
	updated?: number;
	error_code?: string;
}

export interface OperationsPage {
	operations: Operation[];
	next_cursor?: string | null;
	has_more?: boolean;
}

export interface WifiNetwork {
	ssid: string;
	signal?: number;
	security?: string;
	active?: boolean;
}

export interface ProvisioningHealth {
	state: string;
	reason?: string | null;
	expires_in?: number | null;
	setup_name?: string | null;
	last_error?: ApiErrorBody | null;
	last_attempt?: WifiAttempt | null;
}

export interface NetworkStatus {
	ssid?: string | null;
	ip?: string | null;
	internet?: string | boolean | null;
	saved?: string[];
	provisioning?: ProvisioningHealth;
	[key: string]: unknown;
}

export interface DiscoveredPhone {
	udid: string;
	name?: string;
	[key: string]: unknown;
}

export type ConsentWindow = "24h" | "30d" | "never";
export type SetupKind =
	| "bind"
	| "transport"
	| "recover"
	| "pair"
	| "developer_mode"
	| "mount_ddi"
	| "unbind";

/** One software update attempt as root recorded it (state.json), or the one just requested. */
export interface UpdateAttempt {
	id: string;
	version: string;
	step: "download" | "install" | "restart" | "verify";
	result: "running" | "succeeded" | "failed" | "cancelled";
	/** Why a failed attempt ended: DOWNLOAD_FAILED, CHECKSUM_MISMATCH, INSTALL_FAILED, VERIFY_FAILED… */
	error_code: string | null;
	started: number | null;
	updated: number | null;
}

/** GET /app/update — what is installed, what GitHub offers, and the latest attempt. */
export interface UpdateView {
	installed: string;
	/** Not a `X.Y.Z` release: updates are off. */
	dev_build: boolean;
	latest: string | null;
	available: boolean;
	checked_at: number | null;
	/** GitHub release page of `latest` when available, else of the installed release. */
	changelog_url: string | null;
	check: {
		/** DEV_BUILD, or NO_INTERNET in Local and Setup mode. */
		blocked: "DEV_BUILD" | "NO_INTERNET" | null;
		/** The last check failed (CHECK_FAILED) or GitHub asked the box to wait (RATE_LIMITED). */
		error: string | null;
		/** The box checks once a day by itself. */
		automatic: boolean;
	};
	attempt: UpdateAttempt | null;
}
