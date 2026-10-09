// Launch screen: mascot, wordmark, one line of status, a sliding bar. The same picture is
// painted statically in index.html before scripts run, so launch, route loading and the
// first box round-trip look like one screen.

import mascot from "@/mascot.png";

export function Splash({ status = "Starting…" }: { status?: string }) {
	return (
		<div
			role="status"
			aria-live="polite"
			className="flex min-h-dvh flex-col items-center justify-center gap-[18px] bg-background px-6 pt-[env(safe-area-inset-top)] pb-[env(safe-area-inset-bottom)] text-center text-foreground"
		>
			<img
				src={mascot}
				alt=""
				width={112}
				height={112}
				className="size-28 rounded-[26px] object-cover"
			/>
			<h1 className="font-bold font-heading text-[30px] leading-none tracking-tight">
				OpenLolo
			</h1>
			<p className="text-[15px] text-muted-foreground">{status}</p>
			<div
				aria-hidden="true"
				className="h-[3px] w-[120px] overflow-hidden rounded-full bg-border"
			>
				<div className="splash-slide h-full w-2/5 rounded-full bg-primary motion-reduce:w-full motion-reduce:animate-none" />
			</div>
		</div>
	);
}
