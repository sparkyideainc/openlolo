// Auto sleep labels shared by Settings and the Auto sleep page.

/** "Off", "5 min", "1 hour". */
export function sleepLabel(minutes: number): string {
	if (minutes === 0) return "Off";
	if (minutes % 60 === 0)
		return minutes === 60 ? "1 hour" : `${minutes / 60} hours`;
	return `${minutes} min`;
}
