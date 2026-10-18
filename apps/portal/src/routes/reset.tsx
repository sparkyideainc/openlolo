// Factory reset, finished at the box with its top button (Lolo Pocket). Any browser may open
// this: the owner from Settings, anyone else from the "belongs to another iPhone" prompt. The
// box shows a number from 5 to 8; pressing the button that many times arms the reset and a hold
// erases. Someone who cannot touch the box can start it but never finish it.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { Trash2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { api, isApiError } from "@/api";
import { keys } from "@/api/queries";
import type { ResetAttempt } from "@/api/types";
import { BusyButton, Mascot } from "@/components/primitives";
import { Plain } from "@/components/shell";
import { setOnboardingComplete } from "@/lib/store";

export const Route = createFileRoute("/reset")({
	component: Reset,
});

// The attempt id lets this browser read its own number again after a reload.
const ATTEMPT_KEY = "openlolo.reset.attempt";

function savedAttempt(): string | null {
	try {
		return sessionStorage.getItem(ATTEMPT_KEY);
	} catch {
		return null;
	}
}

function saveAttempt(attempt: string | null) {
	try {
		if (attempt) sessionStorage.setItem(ATTEMPT_KEY, attempt);
		else sessionStorage.removeItem(ATTEMPT_KEY);
	} catch {
		// Private mode: the attempt still runs; a reload just loses the number.
	}
}

const REASONS: Record<string, string> = {
	too_many: "The box counted more presses than the number.",
	hold: "The button was held before the count was reached.",
	timeout: "Time ran out.",
	page: "Cancelled.",
	press: "Cancelled with a press of the button.",
	lost: "The box missed a press. Start again.",
	reset_failed: "The box could not start the reset.",
};

function Reset() {
	const navigate = useNavigate();
	const queryClient = useQueryClient();
	const [attempt, setAttempt] = useState<string | null>(savedAttempt);
	const [error, setError] = useState<string | null>(null);
	const status = useQuery({
		queryKey: [...keys.setupStatus, "reset", attempt],
		queryFn: () => (attempt ? api.resetStatus(attempt) : api.setupStatus()),
		refetchInterval: 1000,
		retry: false,
	});
	const data = status.data;
	const view: ResetAttempt | null | undefined = data?.reset;
	const mine = !!view && !!attempt && view.attempt === attempt;
	const stage = mine ? view.stage : null;
	// Armed and then silent: the hold went in and the reset unit stopped the box's services.
	const lastStage = useRef<string | null>(null);
	if (stage) lastStage.current = stage;
	const resetting =
		stage === "resetting" ||
		(status.isError &&
			(lastStage.current === "armed" || lastStage.current === "resetting"));

	const start = useMutation({
		mutationFn: () => api.resetStart(),
		onSuccess: (started) => {
			saveAttempt(started.attempt);
			setAttempt(started.attempt);
			setError(null);
			void queryClient.invalidateQueries({ queryKey: keys.setupStatus });
		},
		onError: (failure) =>
			setError(
				isApiError(failure) && failure.code === "RESET_IN_PROGRESS"
					? "Someone else started a reset on this box. Try again in 30 seconds."
					: failure.message,
			),
	});
	const cancel = useMutation({
		mutationFn: () => api.resetCancel(attempt),
		onSettled: () => void status.refetch(),
	});

	// The reset unit stops the box's services: once the box goes quiet, wait for it to return
	// in first-time setup.
	useEffect(() => {
		if (!resetting && stage !== "done") return;
		saveAttempt(null);
		setOnboardingComplete(false);
		const timer = setTimeout(() => {
			queryClient.clear();
			if (stage === "done") void navigate({ to: "/setup", replace: true });
			else
				void navigate({
					to: "/offline",
					search: { next: "/setup" },
					replace: true,
				});
		}, 4000);
		return () => clearTimeout(timer);
	}, [resetting, stage, navigate, queryClient]);

	if (data && data.reset_method === "name") {
		return (
			<Screen title="Reset from Settings">
				<p className="text-base text-muted-foreground">
					This box has no top button for resets. Open Settings on the iPhone
					paired with it and choose Reset box.
				</p>
			</Screen>
		);
	}

	if (resetting || stage === "done") {
		return (
			<Screen title="Resetting">
				<p className="text-base text-muted-foreground">
					The box is erasing everything and restarts into first-time setup in
					about a minute. Join its Wi-Fi from the card to set it up again.
				</p>
			</Screen>
		);
	}

	if (stage === "challenge" && view) {
		const target = view.target ?? 0;
		return (
			<Screen title={`Press the top button ${target} times`}>
				<div className="flex flex-col items-center gap-1">
					<span className="font-bold font-heading text-[5rem] text-foreground tabular-nums leading-none">
						{view.count ?? 0}
						<span className="text-muted-foreground"> / {target}</span>
					</span>
					<span className="text-[13px] text-muted-foreground">
						{view.expires_in ?? 0} s left
					</span>
				</div>
				<p className="text-base text-muted-foreground">
					Press at your own pace; the count updates as the box sees each press.
					A quick double press counts as two. One press too many starts over.
				</p>
				<CancelButton busy={cancel.isPending} onClick={() => cancel.mutate()} />
			</Screen>
		);
	}

	if (stage === "armed" && view) {
		return (
			<Screen title="Hold the top button to erase">
				<p className="text-base text-muted-foreground">
					The box light flashes three times in a row. Hold the button and let go
					to erase everything. Press it once to cancel.
				</p>
				<span className="text-[13px] text-muted-foreground">
					{view.expires_in ?? 0} s left
				</span>
				<CancelButton busy={cancel.isPending} onClick={() => cancel.mutate()} />
			</Screen>
		);
	}

	const ended =
		mine && (view.stage === "failed" || view.stage === "cancelled")
			? (REASONS[view.reason ?? ""] ?? "The reset stopped.")
			: null;
	const owner = data?.session ?? false;
	return (
		<NotOwner
			owner={owner}
			ended={ended}
			error={error}
			starting={start.isPending}
			onStart={() => start.mutate()}
			onBack={() =>
				void navigate({ to: owner ? "/settings" : "/", replace: true })
			}
			recognised={!owner && !attempt && (data?.access.allowed ?? false)}
		/>
	);
}

function NotOwner({
	owner,
	ended,
	error,
	starting,
	onStart,
	onBack,
	recognised,
}: {
	owner: boolean;
	ended: string | null;
	error: string | null;
	starting: boolean;
	onStart: () => void;
	onBack: () => void;
	recognised: boolean;
}) {
	// The paired phone was briefly not recognised (its address is re-read every few seconds):
	// once the box admits it, go back to the app.
	useEffect(() => {
		if (recognised) onBack();
	}, [recognised, onBack]);
	return (
		<Screen
			title={owner ? "Reset this box?" : "This box belongs to another iPhone"}
		>
			<p className="text-base text-muted-foreground">
				{owner
					? "Resetting erases the iPhone pairing, AI clients, activity and saved Wi-Fi, then restarts into first-time setup on the card's Wi-Fi."
					: "Only the iPhone paired with it can open this page. If the box is yours, reset it to set it up with this iPhone: the pairing, AI clients, activity and saved Wi-Fi are erased."}
			</p>
			<p className="text-base text-muted-foreground">
				You need the box in your hand: it asks you to press its top button a
				number of times, then hold it.
			</p>
			{ended ? (
				<p className="rounded-2xl bg-card px-4 py-3 text-[15px] text-foreground">
					{ended}
				</p>
			) : null}
			{error ? (
				<p className="rounded-2xl bg-card px-4 py-3 text-[15px] text-destructive">
					{error}
				</p>
			) : null}
			<div className="flex w-full flex-col gap-2">
				<BusyButton
					look="danger"
					icon={<Trash2 className="size-5" />}
					busy={starting}
					onClick={onStart}
				>
					{ended ? "Try again" : "Reset box"}
				</BusyButton>
				{owner ? (
					<BusyButton look="link" onClick={onBack}>
						Back to Settings
					</BusyButton>
				) : null}
			</div>
		</Screen>
	);
}

function Screen({
	title,
	children,
}: {
	title: string;
	children: React.ReactNode;
}) {
	return (
		<Plain className="justify-center">
			<div className="flex flex-col items-center gap-5 text-center">
				<Mascot size={72} radius={18} />
				<h1 className="font-bold font-heading text-[1.75rem] text-foreground leading-tight">
					{title}
				</h1>
				{children}
			</div>
		</Plain>
	);
}

function CancelButton({
	busy,
	onClick,
}: {
	busy: boolean;
	onClick: () => void;
}) {
	return (
		<div className="w-full max-w-xs">
			<BusyButton look="plain" busy={busy} onClick={onClick}>
				Cancel
			</BusyButton>
		</div>
	);
}
