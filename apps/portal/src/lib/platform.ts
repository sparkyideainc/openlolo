// Where the page runs: the Home Screen app (standalone), Safari on an iPhone, another iPhone
// browser, or something else. The owner surface opens only from the Home Screen app (ADR 0012).

export function isStandalone(): boolean {
	return (
		(navigator as { standalone?: boolean }).standalone === true ||
		window.matchMedia("(display-mode: standalone)").matches
	);
}

export function isIPhone(): boolean {
	return /iPhone|iPod/.test(navigator.userAgent);
}

/** Safari itself, not Chrome/Firefox/Edge shells (which cannot add Home Screen web apps). */
export function isSafari(): boolean {
	const ua = navigator.userAgent;
	return /Safari/.test(ua) && !/CriOS|FxiOS|EdgiOS|OPiOS|DuckDuckGo/.test(ua);
}

/** The owner surface must be reached from the Home Screen app on an iPhone. */
export function needsInstall(): boolean {
	return isIPhone() && !isStandalone();
}
