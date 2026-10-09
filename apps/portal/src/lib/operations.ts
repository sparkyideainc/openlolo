// Operation journal presentation: the look and pill of each journal row.

import type { Operation } from "@/api/types";
import { date, dayText, humanize, isToday, isYesterday } from "@/lib/format";

export type PillTone = "good" | "warn" | "bad" | "info" | "neutral";

export type OperationIcon =
	| "pointer"
	| "hand"
	| "move"
	| "keyboard"
	| "command"
	| "circle-dot"
	| "camera"
	| "link"
	| "app-window"
	| "zap"
	| "pause"
	| "cable"
	| "circle-dashed";

/** Icon and label for a journaled phone operation kind. */
export function operationLook(kind: string): {
	icon: OperationIcon;
	title: string;
} {
	switch (kind) {
		case "tap":
			return { icon: "pointer", title: "Tap" };
		case "long_press":
			return { icon: "hand", title: "Long press" };
		case "swipe":
			return { icon: "move", title: "Swipe" };
		case "type_text":
		case "send_text":
			return { icon: "keyboard", title: "Type" };
		case "key":
			return { icon: "command", title: "Key" };
		case "button":
			return { icon: "circle-dot", title: "Button" };
		case "screenshot":
			return { icon: "camera", title: "Screenshot" };
		case "open_url":
			return { icon: "link", title: "Open link" };
		case "launch_app":
			return { icon: "app-window", title: "Open app" };
		case "shortcut":
			return { icon: "zap", title: "Shortcut" };
		case "pause":
			return { icon: "pause", title: "Pause" };
		case "autopair":
			return { icon: "cable", title: "USB pairing" };
		default:
			return { icon: "circle-dashed", title: humanize(kind) };
	}
}

export function operationPill(state: string | null | undefined): {
	text: string;
	tone: PillTone;
} {
	switch (state) {
		case "SUCCEEDED":
			return { text: "Done", tone: "good" };
		case "FAILED":
			return { text: "Failed", tone: "bad" };
		case "CANCELLED":
			return { text: "Cancelled", tone: "neutral" };
		case "OUTCOME_UNKNOWN":
			return { text: "Unknown", tone: "warn" };
		case "DISPATCHED":
		case "RUNNING":
			return { text: "Running", tone: "info" };
		case "QUEUED":
			return { text: "Queued", tone: "neutral" };
		default:
			return { text: state || "—", tone: "neutral" };
	}
}

export interface DayGroup {
	title: string;
	items: Operation[];
}

/** "Today", "Yesterday", "Oct 3", then "Earlier" for rows without a timestamp; journal order kept. */
export function groupByDay(
	operations: readonly Operation[],
	now = new Date(),
): DayGroup[] {
	const groups: DayGroup[] = [];
	for (const operation of operations) {
		const when = date(operation.updated ?? operation.created);
		let title = "Earlier";
		if (when) {
			if (isToday(when, now)) title = "Today";
			else if (isYesterday(when, now)) title = "Yesterday";
			else title = dayText(when);
		}
		const group = groups.find((g) => g.title === title);
		if (group) group.items.push(operation);
		else groups.push({ title, items: [operation] });
	}
	return groups;
}
