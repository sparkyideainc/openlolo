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
	bound: boolean;
	session: boolean;
	/** Who may open the page from this browser, and why not (ADR 0012). */
	access: PageAccess;
	state: "disabled" | "idle" | "setup_mode" | "joining" | "verifying" | string;
	last_attempt: WifiAttempt | null;
	last_error: { code: string } | null;
	usb_pairing: AutopairStatus | null;
}

export interface PageAccess {
	/** Exactly one phone is on the box's USB port. */
	cable: boolean;
	/** This browser is on the box's setup Wi-Fi. */
	setup_ap: boolean;
	/** This browser is the paired phone (its LAN address matched). */
	paired_phone: boolean;
	allowed: boolean;
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
	control?: { active?: boolean; paused?: boolean; queue_depth?: number };
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

export type ConsentWindow = "1h" | "8h" | "24h" | "30d";
export type SetupKind =
	| "bind"
	| "transport"
	| "recover"
	| "pair"
	| "developer_mode"
	| "mount_ddi"
	| "unbind";
