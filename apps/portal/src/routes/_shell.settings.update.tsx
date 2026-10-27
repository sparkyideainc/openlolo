// Software update (canvas "Box Page Software Update"): the installed version, then exactly one
// of available, checking, up to date, updating, updated or failed. While an update runs, Back
// and the tabs are locked and the page polls through the box's restart by itself.

import { Button } from "@openlolo/ui/components/button";
import { cn } from "@openlolo/ui/lib/utils";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { Check, ExternalLink, Loader2, WifiOff } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

import { api, isApiError } from "@/api";
import { keys, useAppStatus, useUpdate } from "@/api/queries";
import type { UpdateView } from "@/api/types";
import { Page, PageHeader, Section } from "@/components/list";
import { Mascot } from "@/components/primitives";
import { useAiInControl, useStartUpdate } from "@/components/update-dialog";
import { relativeText } from "@/lib/format";
import { acknowledgedUpdate, setAcknowledgedUpdate } from "@/lib/store";
import {
	failureText,
	refusalText,
	stepIndex,
	stepLabels,
	type UpdatePhase,
	updatePhase,
	versionText,
} from "@/lib/update";

export const Route = createFileRoute("/_shell/settings/update")({
	component: SoftwareUpdate,
});

function SoftwareUpdate() {
	const queryClient = useQueryClient();
	const navigate = useNavigate();
	const update = useUpdate();
	const [acknowledged, setAcknowledged] = useState(acknowledgedUpdate);
	const check = useMutation({
		mutationFn: () => api.updateCheck(),
		onSuccess: (view) => queryClient.setQueryData(keys.update, view),
		onError: (error) =>
			toast.error(
				isApiError(error)
					? refusalText(error.code, error.message)
					: error.message,
			),
	});
	const start = useStartUpdate();
	const view = update.data;
	const phase = updatePhase(view, {
		checking: check.isPending,
		acknowledged,
	});
	const aiInControl = useAiInControl(phase === "available");
	const reconnected =
		useAppStatus(phase === "done" ? 5000 : false, { enabled: phase === "done" })
			.data?.connection?.state === "connected";

	const finish = () => {
		if (view?.attempt) {
			setAcknowledgedUpdate(view.attempt.id);
			setAcknowledged(view.attempt.id);
		}
		void navigate({ to: "/settings" });
	};
	const updating = phase === "updating";
	const target = view?.attempt?.version ?? view?.latest ?? "";
	const shown = view?.installed;

	return (
		<Page>
			<PageHeader
				title="Software update"
				back={updating ? undefined : "/settings"}
				inline
				action={updating ? <span className="w-16" /> : undefined}
			/>
			{updating ? <span className="sr-only">Updating</span> : null}
			<div className="flex flex-col items-center gap-2.5">
				<Mascot
					size={72}
					radius={18}
					className="shadow-[0_20px_45px_-20px_rgba(43,37,33,0.25)]"
				/>
				<div className="flex flex-col items-center gap-0.5">
					<span className="font-heading font-semibold text-[20px] text-foreground leading-[26px]">
						OpenLolo {versionText(shown)}
					</span>
					<span className="text-[14px] text-muted-foreground leading-5">
						{caption(phase, target)}
					</span>
				</div>
			</div>
			{view ? (
				<Body
					phase={phase}
					view={view}
					aiInControl={aiInControl}
					reconnected={reconnected}
					unreachable={update.isError}
					starting={start.isPending}
					onUpdate={(version) => start.mutate(version)}
					onDone={finish}
				/>
			) : update.isError ? (
				<p className="px-4 text-center text-[15px] text-muted-foreground">
					Can't reach the box.
				</p>
			) : null}
			{view && showsCheck(phase) ? (
				<Section footer={checkFooter(view)}>
					<button
						type="button"
						onClick={() => check.mutate()}
						disabled={check.isPending}
						className="flex min-h-11 items-center px-4 py-2 text-left text-base text-primary transition-colors active:bg-muted disabled:opacity-50"
					>
						Check for updates
					</button>
				</Section>
			) : null}
		</Page>
	);
}

function caption(phase: UpdatePhase, target: string): string {
	if (phase === "updating") return `Updating to ${versionText(target)}`;
	if (phase === "done") return "Installed just now";
	if (phase === "dev") return "Development build, updates are off";
	return "Installed";
}

/** "Just now" reads "just now" mid-sentence; "2:14 AM" stays as it is. */
function sentenceCase(value: string): string {
	return value.charAt(0).toLowerCase() + value.slice(1);
}

function showsCheck(phase: UpdatePhase): boolean {
	return phase === "uptodate" || phase === "failed" || phase === "unchecked";
}

function checkFooter(view: UpdateView): string {
	if (view.check.error === "RATE_LIMITED")
		return "GitHub asked the box to wait. Try again later.";
	if (view.check.error)
		return "The box couldn't reach GitHub. Try again later.";
	return view.check.automatic
		? "The box checks once a day and asks before installing."
		: "Daily checks are off. The box asks before installing.";
}

function StatusRow({
	icon,
	title,
	subtitle,
}: {
	icon: React.ReactNode;
	title: string;
	subtitle?: string;
}) {
	return (
		<div className="flex min-h-14 items-center gap-3 rounded-2xl bg-card px-4 py-2">
			{icon}
			<span className="flex flex-1 flex-col">
				<span className="text-base text-foreground leading-[22px]">
					{title}
				</span>
				{subtitle ? (
					<span className="text-[13px] text-muted-foreground leading-[18px]">
						{subtitle}
					</span>
				) : null}
			</span>
		</div>
	);
}

function GoodMark({ size = 30 }: { size?: number }) {
	return (
		<span
			className="flex shrink-0 items-center justify-center rounded-full bg-good-tint text-good"
			style={{ width: size, height: size }}
		>
			<Check className="size-[60%]" strokeWidth={2.6} />
		</span>
	);
}

function ChangelogLink({
	href,
	children,
	center = false,
}: {
	href: string | null;
	children: React.ReactNode;
	center?: boolean;
}) {
	if (!href) return null;
	return (
		<a
			href={href}
			target="_blank"
			rel="noreferrer"
			className={cn(
				"flex min-h-8 items-center gap-1 font-semibold text-[15px] text-primary",
				center ? "justify-center" : "self-start",
			)}
		>
			{children}
			<ExternalLink className="size-3.5" strokeWidth={2.2} />
		</a>
	);
}

function Body({
	phase,
	view,
	aiInControl,
	reconnected,
	unreachable,
	starting,
	onUpdate,
	onDone,
}: {
	phase: UpdatePhase;
	view: UpdateView;
	aiInControl: boolean;
	reconnected: boolean;
	unreachable: boolean;
	starting: boolean;
	onUpdate: (version: string) => void;
	onDone: () => void;
}) {
	const latest = view.latest ?? "";
	switch (phase) {
		case "available":
			return (
				<Section
					bare
					footer={`Takes about 3 minutes. Phone control stops while the box restarts; AI clients keep their access.${aiInControl ? " An AI client is using the iPhone now; updating stops it." : ""}`}
				>
					<div className="flex flex-col gap-3.5 rounded-2xl bg-card p-4">
						<span className="flex items-center gap-2">
							<span className="flex h-6 items-center rounded-lg bg-accent px-2 font-semibold text-[13px] text-primary">
								New
							</span>
							<span className="font-semibold text-base text-foreground">
								{versionText(latest)} is available
							</span>
						</span>
						<ChangelogLink href={view.changelog_url}>
							View changelog on GitHub
						</ChangelogLink>
						<Button
							className="h-11 rounded-xl font-semibold text-[15px]"
							disabled={starting}
							onClick={() => onUpdate(latest)}
						>
							{starting ? <Loader2 className="size-5 animate-spin" /> : null}
							Update now
						</Button>
					</div>
				</Section>
			);
		case "checking":
			return (
				<StatusRow
					icon={
						<Loader2
							className="size-[22px] shrink-0 animate-spin text-primary"
							strokeWidth={2.4}
						/>
					}
					title="Checking for updates…"
				/>
			);
		case "uptodate":
			return (
				<StatusRow
					icon={<GoodMark />}
					title="OpenLolo is up to date"
					subtitle={`Checked ${sentenceCase(relativeText(view.checked_at))}`}
				/>
			);
		case "offline":
			return (
				<StatusRow
					icon={
						<WifiOff
							className="size-[22px] shrink-0 text-muted-foreground"
							strokeWidth={2}
						/>
					}
					title="Connect the box to Wi-Fi to check for updates"
					subtitle="Local and Setup mode have no internet."
				/>
			);
		case "failed":
			return (
				<section className="flex flex-col gap-3 rounded-2xl bg-destructive-tint p-4 text-destructive">
					<span className="flex flex-col gap-1">
						<span className="font-semibold text-base">Update failed</span>
						<span className="text-[15px] leading-[21px]">
							{failureText(view.attempt, view.installed)}
						</span>
					</span>
					<Button
						variant="outline"
						className="h-11 rounded-xl border-0 bg-card font-semibold text-[15px] text-destructive hover:bg-card/80"
						disabled={starting}
						onClick={() => onUpdate(latest)}
					>
						{starting ? <Loader2 className="size-5 animate-spin" /> : null}
						Try again
					</Button>
				</section>
			);
		case "updating": {
			// The API stops while the new build starts: an unreachable box is the restart step.
			const current = unreachable ? 2 : Math.max(stepIndex(view.attempt), 0);
			return (
				<Section
					bare
					footer="Keep this page open. It reconnects by itself when the box is back."
				>
					<ol className="flex flex-col divide-y divide-border overflow-hidden rounded-2xl bg-card">
						{stepLabels(view.attempt?.version ?? latest).map((label, index) => (
							<li
								key={label}
								className="flex min-h-[52px] items-center gap-3 px-4 py-2"
								aria-current={index === current ? "step" : undefined}
							>
								<span className="flex size-[26px] shrink-0 items-center justify-center">
									{index < current ? (
										<GoodMark size={26} />
									) : index === current ? (
										<Loader2
											className="size-[22px] animate-spin text-primary"
											strokeWidth={2.4}
										/>
									) : (
										<span className="size-2.5 rounded-full bg-chevron" />
									)}
								</span>
								<span
									className={cn(
										"flex-1 text-base",
										index > current
											? "text-muted-foreground"
											: "text-foreground",
									)}
								>
									{label}
								</span>
							</li>
						))}
					</ol>
				</Section>
			);
		}
		case "done":
			return (
				<section className="flex flex-col gap-4">
					<StatusRow
						icon={<GoodMark />}
						title={`Updated to ${versionText(view.installed)}`}
						subtitle={
							reconnected
								? "iPhone reconnected"
								: "Waiting for the iPhone to reconnect"
						}
					/>
					<ChangelogLink href={view.changelog_url} center>
						What's new in {versionText(view.installed)}
					</ChangelogLink>
					<Button
						className="h-11 rounded-xl font-semibold text-[15px]"
						onClick={onDone}
					>
						Done
					</Button>
				</section>
			);
		default:
			return null;
	}
}
