// Small pieces: busy button, icon badge, avatar, status pill, signal bars, mascot, step mark,
// status dot.

import { Button } from "@openlolo/ui/components/button";
import { cn } from "@openlolo/ui/lib/utils";
import { Check, Loader2 } from "lucide-react";
import type { ComponentProps, ReactNode } from "react";
import type { PillTone } from "@/lib/operations";
import type { StepState } from "@/lib/setup-steps";
import type { Tone } from "@/lib/summary";
import { litBars } from "@/lib/wifi";
import mascot from "@/mascot.png";

type BusyButtonProps = ComponentProps<typeof Button> & {
	busy?: boolean;
	icon?: ReactNode;
	/** `prominent` = filled accent, `plain` = bordered, `danger` = bordered red, `link` = borderless. */
	look?: "prominent" | "plain" | "danger" | "link";
};

/** A full-width, 48 px button whose icon swaps to a spinner while an async action runs. */
export function BusyButton({
	busy = false,
	icon,
	look = "prominent",
	className,
	children,
	disabled,
	...props
}: BusyButtonProps) {
	const looks = {
		prominent: "bg-primary text-primary-foreground hover:bg-primary/90",
		plain: "border-border bg-card text-primary hover:bg-muted",
		danger:
			"border-destructive/30 bg-card text-destructive hover:bg-destructive-tint",
		link: "bg-transparent text-primary hover:bg-muted/60",
	};
	return (
		<Button
			variant={look === "prominent" ? "default" : "outline"}
			className={cn(
				"h-12 w-full gap-2 rounded-xl font-semibold text-[15px]",
				looks[look],
				className,
			)}
			disabled={disabled || busy}
			{...props}
		>
			{busy ? <Loader2 className="size-5 animate-spin" /> : icon}
			{children}
		</Button>
	);
}

type BadgeFill =
	| "grey"
	| "dark"
	| "accent"
	| "good"
	| "info"
	| "warn"
	| "destructive";

const fills: Record<BadgeFill, string> = {
	grey: "bg-icon-grey",
	dark: "bg-icon-dark",
	accent: "bg-primary",
	good: "bg-good",
	info: "bg-info",
	warn: "bg-warn",
	destructive: "bg-destructive",
};

/** 29 px rounded icon tile, like Settings.app. */
export function IconBadge({
	icon,
	fill = "grey",
	size = 29,
	className,
}: {
	icon: ReactNode;
	fill?: BadgeFill;
	size?: number;
	className?: string;
}) {
	return (
		<span
			className={cn(
				"inline-flex shrink-0 items-center justify-center text-white [&_svg]:size-[55%]",
				fills[fill],
				className,
			)}
			style={{ width: size, height: size, borderRadius: size * 0.24 }}
			aria-hidden="true"
		>
			{icon}
		</span>
	);
}

const AVATAR_FILLS = [
	"#B8540A",
	"#4A6B8A",
	"#5B7F4A",
	"#7A5A8A",
	"#8A5A4A",
	"#3F7F7A",
];

function initials(name: string): string {
	const words = name.split(" ").filter(Boolean).slice(0, 2);
	if (words.length === 2)
		return `${words[0]?.[0] ?? ""}${words[1]?.[0] ?? ""}`.toUpperCase();
	return name.slice(0, 1).toUpperCase();
}

function avatarFill(name: string): string {
	let hash = 5381;
	for (const char of name)
		hash = ((hash << 5) + hash + (char.codePointAt(0) ?? 0)) | 0;
	return AVATAR_FILLS[Math.abs(hash) % AVATAR_FILLS.length] ?? "#B8540A";
}

/** Initial-letter avatar for an AI client, color picked by a stable hash of its name. */
export function Avatar({ name, size = 36 }: { name: string; size?: number }) {
	return (
		<span
			className="inline-flex shrink-0 items-center justify-center rounded-full font-bold text-white"
			style={{
				width: size,
				height: size,
				fontSize: size * 0.44,
				backgroundColor: avatarFill(name),
			}}
			aria-hidden="true"
		>
			{initials(name)}
		</span>
	);
}

const pillTones: Record<PillTone, string> = {
	good: "bg-good-tint text-good",
	warn: "bg-warn-tint text-warn",
	bad: "bg-destructive-tint text-destructive",
	info: "bg-info-tint text-info",
	neutral: "bg-disabled text-muted-foreground",
};

/** Small status capsule ("Done", "Failed", "Unknown"). */
export function StatusPill({ text, tone }: { text: string; tone: PillTone }) {
	return (
		<span
			className={cn(
				"shrink-0 rounded-full px-2 py-0.5 font-semibold text-xs",
				pillTones[tone],
			)}
		>
			{text}
		</span>
	);
}

const dotTones: Record<Tone, string> = {
	good: "bg-dot-good",
	warn: "bg-dot-warn",
	bad: "bg-dot-bad",
	idle: "bg-dot-idle",
};

export function StatusDot({ tone }: { tone: Tone }) {
	return (
		<span
			className={cn(
				"inline-block size-3.5 shrink-0 rounded-full",
				dotTones[tone],
			)}
			aria-hidden="true"
		/>
	);
}

/** Wi-Fi strength bars, 1–4 lit by signal percent. */
export function SignalBars({ percent }: { percent: number }) {
	const lit = litBars(percent);
	return (
		<span
			className="inline-flex items-end gap-0.5"
			role="img"
			aria-label={`Signal ${percent} percent`}
		>
			{[0, 1, 2, 3].map((index) => (
				<span
					key={index}
					className={cn(
						"w-[3px] rounded-[1px]",
						index < lit ? "bg-foreground" : "bg-border",
					)}
					style={{ height: 6 + index * 3 }}
				/>
			))}
		</span>
	);
}

export function Mascot({
	size,
	radius,
	className,
}: {
	size: number;
	radius?: number;
	className?: string;
}) {
	return (
		<img
			src={mascot}
			alt=""
			width={size}
			height={size}
			className={cn("shrink-0 object-cover", className)}
			style={{ width: size, height: size, borderRadius: radius ?? size * 0.22 }}
		/>
	);
}

/** Numbered circle for the Setting-up list: done (check), now (spinner), bad (!), todo (number). */
export function StepMark({ id, state }: { id: number; state: StepState }) {
	const base =
		"mt-px flex size-[26px] shrink-0 items-center justify-center rounded-full text-xs font-bold";
	switch (state) {
		case "done":
			return (
				<span className={cn(base, "bg-good text-white")}>
					<Check className="size-3.5" strokeWidth={3} />
				</span>
			);
		case "now":
			return (
				<span className={cn(base, "bg-primary text-white")}>
					<Loader2 className="size-3.5 animate-spin" />
				</span>
			);
		case "bad":
			return <span className={cn(base, "bg-destructive text-white")}>!</span>;
		default:
			return (
				<span
					className={cn(
						base,
						"border-[1.5px] border-border text-muted-foreground",
					)}
				>
					{id}
				</span>
			);
	}
}
