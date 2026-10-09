// Per-browser conveniences, like the app's UserDefaults: the local box name and whether the
// owner finished onboarding here. Both survive a box restart and never reach the box.

const NAME = "box.name";
const COMPLETE = "onboarding.complete";

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
