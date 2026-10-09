// iOS 27 Home Screen web apps (measured on the bench with Web Inspector over USB): until the
// first touch the layout viewport is 809 px on an 844 px window while env(safe-area-inset-bottom)
// is already 34 px, and WebKit paints nothing below those 809 px, so a bar fixed at bottom: 0
// ends 35 px above the screen edge. The same measurement found a trigger: make the document
// taller than the viewport for one frame and WebKit recomputes it to the full window, exactly
// as the first touch does. `--viewport-gap` stays as the fallback anchor while that settles.

const standalone = (navigator as { standalone?: boolean }).standalone === true;

function gap(): number {
	return standalone ? Math.max(0, window.outerHeight - window.innerHeight) : 0;
}

function publish() {
	document.documentElement.style.setProperty("--viewport-gap", `${gap()}px`);
}

let attempts = 0;

function nudge() {
	if (gap() === 0 || attempts >= 5) return;
	attempts += 1;
	document.body.style.minHeight = "150vh";
	window.setTimeout(() => {
		document.body.style.minHeight = "";
		publish();
		window.setTimeout(nudge, 250);
	}, 50);
}

export function installViewportGap() {
	publish();
	window.addEventListener("resize", publish);
	window.addEventListener("orientationchange", publish);
	window.visualViewport?.addEventListener("resize", publish);
	if (standalone) {
		if (document.readyState === "complete") nudge();
		else window.addEventListener("load", nudge, { once: true });
	}
}
