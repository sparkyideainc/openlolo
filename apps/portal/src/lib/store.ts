// Per-browser conveniences, like the app's UserDefaults: the local box name, whether the
// owner finished onboarding here, and which update the dialog already asked about. All survive
// a box restart and never reach the box.

const NAME = "box.name";
const COMPLETE = "onboarding.complete";
const PROMPTED = "update.prompted";
const ACKNOWLEDGED = "update.acknowledged";

function read(key: string): string | null {
	try {
		return localStorage.getItem(key);
	} catch {
		return null;
	}
}

function write(key: string, value: string | null): void {
	try {
		if (value === null) localStorage.removeItem(key);
		else localStorage.setItem(key, value);
	} catch {
		// Private mode or blocked storage: the page still works without the convenience.
	}
}

export function storedBoxName(): string | null {
	return read(NAME);
}

export function storeBoxName(name: string | null): void {
	write(NAME, name && name.trim() !== "" ? name.trim() : null);
}

export function onboardingComplete(): boolean {
	return read(COMPLETE) === "true";
}

export function setOnboardingComplete(value: boolean): void {
	write(COMPLETE, value ? "true" : null);
}

/** The release the update dialog last asked about: it asks once per version. */
export function promptedUpdate(): string | null {
	return read(PROMPTED);
}

export function setPromptedUpdate(version: string): void {
	write(PROMPTED, version);
}

/** The update attempt whose outcome (updated or failed) the owner already closed here. */
export function acknowledgedUpdate(): string | null {
	return read(ACKNOWLEDGED);
}

export function setAcknowledgedUpdate(attempt: string): void {
	write(ACKNOWLEDGED, attempt);
}
