import { useSyncExternalStore } from "react";

import { storeBoxName, storedBoxName } from "@/lib/store";

const listeners = new Set<() => void>();

function subscribe(listener: () => void) {
	listeners.add(listener);
	return () => listeners.delete(listener);
}

/** The local box label: the owner's name for it, else the box's own setup name, else "OpenLolo". */
export function useBoxName(device?: string | null): string {
	const stored = useSyncExternalStore(subscribe, storedBoxName, () => null);
	return stored ?? device ?? "OpenLolo";
}

export function renameBox(name: string | null) {
	storeBoxName(name);
	for (const listener of listeners) listener();
}
