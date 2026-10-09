// Inset-grouped list on the warm ground, white cards, warm separators: the app's `groundedList`,
// `Section`, `Row`, `cardRow` and `bareRow`.

import { cn } from "@openlolo/ui/lib/utils";
import { Link, type LinkProps } from "@tanstack/react-router";
import { Check, ChevronRight } from "lucide-react";
import type { ComponentProps, ReactNode } from "react";

export function Page({
	children,
	className,
}: {
	children: ReactNode;
	className?: string;
}) {
	return <div className={cn("flex flex-col gap-6", className)}>{children}</div>;
}

export function PageHeader({
	title,
	back,
	action,
	inline = false,
}: {
	title: string;
	back?: LinkProps["to"];
	action?: ReactNode;
	inline?: boolean;
}) {
	return (
		<header className="flex min-h-11 items-center gap-2">
			{back ? (
				<Link
					to={back}
					className="-ml-2 flex h-11 items-center gap-0.5 pr-2 text-[15px] text-primary"
				>
					<ChevronRight className="size-5 rotate-180" />
					Back
				</Link>
			) : null}
			<h1
				className={cn(
					"flex-1 truncate font-bold font-heading text-foreground",
					inline ? "text-center text-[17px]" : "text-[2rem] leading-tight",
				)}
			>
				{title}
			</h1>
			{action ?? (back ? <span className="w-16" /> : null)}
		</header>
	);
}

export function Section({
	title,
	footer,
	children,
	bare = false,
	className,
}: {
	title?: ReactNode;
	footer?: ReactNode;
	children: ReactNode;
	bare?: boolean;
	className?: string;
}) {
	return (
		<section className={cn("flex flex-col gap-2", className)}>
			{title ? (
				<h2 className="flex items-center gap-2 px-4 font-medium text-[13px] text-muted-foreground uppercase tracking-wide">
					{title}
				</h2>
			) : null}
			{bare ? (
				children
			) : (
				<div className="flex flex-col divide-y divide-border overflow-hidden rounded-2xl bg-card">
					{children}
				</div>
			)}
			{footer ? (
				<p className="px-4 text-[13px] text-muted-foreground leading-snug">
					{footer}
				</p>
			) : null}
		</section>
	);
}

type RowProps = {
	title: ReactNode;
	subtitle?: ReactNode;
	value?: ReactNode;
	leading?: ReactNode;
	trailing?: ReactNode;
	accessory?: "none" | "chevron" | "check";
	minHeight?: 44 | 56 | 60;
	lines?: 1 | 2;
	className?: string;
};

function RowBody({
	title,
	subtitle,
	value,
	leading,
	trailing,
	accessory = "none",
	lines = 2,
}: RowProps) {
	return (
		<>
			{leading}
			<span className="flex min-w-0 flex-1 flex-col">
				<span
					className={cn("text-base text-foreground", lines === 1 && "truncate")}
				>
					{title}
				</span>
				{subtitle ? (
					<span
						className={cn(
							"text-[13px] text-muted-foreground leading-snug",
							lines === 1 && "truncate",
						)}
					>
						{subtitle}
					</span>
				) : null}
			</span>
			{value ? (
				<span className="max-w-[45%] text-right text-base text-muted-foreground">
					{value}
				</span>
			) : null}
			{trailing}
			{accessory === "chevron" ? (
				<ChevronRight
					className="size-5 shrink-0 text-chevron"
					strokeWidth={2.5}
				/>
			) : null}
			{accessory === "check" ? (
				<Check className="size-5 shrink-0 text-good" strokeWidth={2.5} />
			) : null}
		</>
	);
}

const rowClass = "flex w-full items-center gap-3 px-4 py-2 text-left";

export function Row(props: RowProps) {
	return (
		<div
			className={cn(rowClass, props.className)}
			style={{ minHeight: props.minHeight ?? 44 }}
		>
			<RowBody {...props} />
		</div>
	);
}

export function ButtonRow({
	onClick,
	disabled,
	...props
}: RowProps & { onClick: () => void; disabled?: boolean }) {
	return (
		<button
			type="button"
			onClick={onClick}
			disabled={disabled}
			className={cn(
				rowClass,
				"transition-colors active:bg-muted disabled:opacity-50",
				props.className,
			)}
			style={{ minHeight: props.minHeight ?? 44 }}
		>
			<RowBody {...props} />
		</button>
	);
}

export function LinkRow({
	to,
	params,
	search,
	...props
}: RowProps & Pick<ComponentProps<typeof Link>, "to" | "params" | "search">) {
	return (
		<Link
			to={to}
			params={params}
			search={search}
			className={cn(
				rowClass,
				"transition-colors active:bg-muted",
				props.className,
			)}
			style={{ minHeight: props.minHeight ?? 44 }}
		>
			<RowBody accessory="chevron" {...props} />
		</Link>
	);
}

/** `LabeledContent`: label left, value right, used on detail sheets. */
export function LabeledRow({
	label,
	value,
}: {
	label: string;
	value: ReactNode;
}) {
	return (
		<div className="flex min-h-11 items-center justify-between gap-4 px-4 py-2">
			<span className="text-base text-foreground">{label}</span>
			<span className="min-w-0 text-right text-base text-muted-foreground">
				{value}
			</span>
		</div>
	);
}
