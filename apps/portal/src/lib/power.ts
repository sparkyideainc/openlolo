// Auto sleep labels shared by Settings and the Auto sleep page.

/** "Off", "30 seconds", "1 minute", "3 minutes": the iPhone Auto-Lock wording. */
export function sleepLabel(seconds: number): string {
	if (seconds === 0) return "Off";
	if (seconds % 60 !== 0) return `${seconds} seconds`;
	return seconds === 60 ? "1 minute" : `${seconds / 60} minutes`;
}
