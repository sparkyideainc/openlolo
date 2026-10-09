// Presentation formatters for the box page. Pure; unit-tested.

import type { Client, Consent } from "@/api/types";

export const DASH = "—";

/** Human text for a loose JSON value, with a fallback for empty and missing values. */
export function text(value: unknown, fallback = DASH): string {
	if (value === null || value === undefined) return fallback;
	if (typeof value === "string") return value === "" ? fallback : value;
	if (typeof value === "boolean") return value ? "Yes" : "No";
	return String(value);
}

export function clientName(item: {
	client_name?: string | null;
	client_id?: string | null;
}): string {
	return text(item.client_name, text(item.client_id, "Unknown client"));
}

/** Human wording for granted or requested scopes. */
export function scopeText(scopes: readonly string[]): string {
	const control = scopes.includes("phone:control");
	const read = scopes.includes("phone:read");
	const parts: string[] = [];
	if (control) parts.push("See screen, tap, type");
	else if (read) parts.push("See screen");
	if (scopes.includes("owner:admin")) parts.push("box settings");
	return parts.length === 0 ? "No access" : parts.join(" and ");
}

export function shortScopeText(scopes: readonly string[]): string {
	if (scopes.includes("phone:control")) return "Screen and input";
	if (scopes.includes("phone:read")) return "Screen only";
	return "No access";
}

/** Seconds-since-epoch → Date, or null for anything that is not a number. */
export function date(value: unknown): Date | null {
	if (typeof value !== "number" || !Number.isFinite(value)) return null;
	return new Date(value * 1000);
}

const timeFormat = new Intl.DateTimeFormat(undefined, {
	hour: "numeric",
	minute: "2-digit",
});
const dayFormat = new Intl.DateTimeFormat(undefined, {
	month: "short",
	day: "numeric",
});
const dateTimeFormat = new Intl.DateTimeFormat(undefined, {
	month: "short",
	day: "numeric",
	year: "numeric",
	hour: "numeric",
	minute: "2-digit",
});
const dateOnlyFormat = new Intl.DateTimeFormat(undefined, {
	month: "short",
	day: "numeric",
	year: "numeric",
});

export function timeText(value: Date): string {
	return timeFormat.format(value);
}

export function dayText(value: Date): string {
	return dayFormat.format(value);
}

/** "Oct 3, 2026, 9:40 PM" (abbreviated date + short time). */
export function dateText(value: unknown): string {
	const when = date(value);
	return when ? dateTimeFormat.format(when) : DASH;
}

export function dateOnlyText(value: unknown): string {
	const when = date(value);
	return when ? dateOnlyFormat.format(when) : DASH;
}

function startOfDay(value: Date): number {
	return new Date(
		value.getFullYear(),
		value.getMonth(),
		value.getDate(),
	).getTime();
}

export function isToday(value: Date, now = new Date()): boolean {
	return startOfDay(value) === startOfDay(now);
}

export function isTomorrow(value: Date, now = new Date()): boolean {
	return startOfDay(value) === startOfDay(now) + 86_400_000;
}

export function isYesterday(value: Date, now = new Date()): boolean {
	return startOfDay(value) === startOfDay(now) - 86_400_000;
}

/** "until 9:40 PM", "until tomorrow", "until Oct 30": short enough for one row line. */
export function untilText(expiry: Date, now = new Date()): string {
	if (isToday(expiry, now)) return `until ${timeText(expiry)}`;
	if (isTomorrow(expiry, now)) return "until tomorrow";
	return `until ${dayText(expiry)}`;
}

const relativeFormat = new Intl.RelativeTimeFormat(undefined, {
	numeric: "auto",
});

export function relativeText(value: unknown, now = new Date()): string {
	const when = date(value);
	if (!when) return DASH;
	const seconds = (now.getTime() - when.getTime()) / 1000;
	if (seconds < 60) return "Just now";
	if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
	if (isToday(when, now)) return timeText(when);
	const days = Math.round((startOfDay(now) - startOfDay(when)) / 86_400_000);
	if (days < 7) return capitalize(relativeFormat.format(-days, "day"));
	return dayText(when);
}

export function expiresText(seconds: unknown): string {
	const value = typeof seconds === "number" ? Math.trunc(seconds) : 0;
	if (value <= 0) return "Expired";
	if (value < 60) return `Expires in ${value} s`;
	const minutes = Math.ceil(value / 60);
	return `Expires in ${minutes} ${minutes === 1 ? "minute" : "minutes"}`;
}

export function windowText(window: string): string {
	switch (window) {
		case "1h":
			return "1 hour";
		case "8h":
			return "8 hours";
		case "24h":
			return "24 hours";
		case "30d":
			return "30 days";
		default:
			return window;
	}
}

export function capitalize(value: string): string {
	return value.length === 0 ? value : value[0]?.toUpperCase() + value.slice(1);
}

/** Humanized form of a snake_case state ("waiting_for_device" → "Waiting for device"). */
export function humanize(value: string): string {
	return capitalize(value.replaceAll("_", " "));
}

/** The latest future expiry among a client's grants, or null without an active one. */
export function activeUntil(client: Client, now = new Date()): Date | null {
	const values = Object.values(client.active_until ?? {}).filter(
		(v): v is number => typeof v === "number" && v * 1000 > now.getTime(),
	);
	if (values.length === 0) return null;
	return new Date(Math.max(...values) * 1000);
}

/** Second line of an Access row: what the client may do and until when. */
export function clientSubtitle(client: Client, now = new Date()): string {
	const until = activeUntil(client, now);
	if (until)
		return `${shortScopeText(client.scopes ?? [])} · ${untilText(until, now)}`;
	return "No active session";
}

export function consentFrom(consent: Consent): string {
	const hosts = (consent.redirect_hosts ?? []).filter((h) => h !== "");
	return hosts.length > 0 ? hosts.join(", ") : text(consent.issuer, "Unknown");
}

export function hostOf(value: string): string | null {
	try {
		return new URL(value).host || null;
	} catch {
		return null;
	}
}

/** Client IDs longer than 20 characters are cut with an ellipsis, as in the app. */
export function shortId(id: string): string {
	return id.length > 20 ? `${id.slice(0, 20)}…` : id;
}
