// The four-segment progress track at the top of the setup page: Cable · Trust · Dev Mode · Wi-Fi.

import { cn } from "@openlolo/ui/lib/utils";

import { TRACKS, type TrackState } from "@/lib/setup-flow";

const color: Record<TrackState, { text: string; bar: string }> = {
	done: { text: "text-good", bar: "bg-good" },
	now: { text: "text-primary", bar: "bg-primary" },
	bad: { text: "text-destructive", bar: "bg-destructive" },
	todo: { text: "text-muted-foreground", bar: "bg-border" },
};

export function TrackBar({ states }: { states: readonly TrackState[] }) {
	return (
		<ol className="grid grid-cols-4 gap-1.5" aria-label="Setup progress">
			{TRACKS.map((label, i) => {
				const state = states[i] ?? "todo";
				return (
					<li
						key={label}
						className={cn(
							"flex flex-col gap-1.5 font-semibold text-xs",
							color[state].text,
						)}
						aria-current={state === "now" ? "step" : undefined}
					>
						<span className={cn("block h-1 rounded-sm", color[state].bar)} />
						{label}
					</li>
				);
			})}
		</ol>
	);
}
