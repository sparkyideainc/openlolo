// Home: box status, the pending request, the pause control, and recent activity.

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { createFileRoute } from "@tanstack/react-router";
import { Pause, Search, Smartphone } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

import { api } from "@/api";
import {
	keys,
	useAppStatus,
	useClients,
	useConsents,
	useOperations,
} from "@/api/queries";
import type { DiscoveredPhone } from "@/api/types";
import { ConfirmDialog } from "@/components/dialogs";
import {
	ButtonRow,
	LinkRow,
	Page,
	PageHeader,
	Row,
	Section,
} from "@/components/list";
import { OperationRow } from "@/components/operation-row";
import { Avatar, BusyButton, IconBadge } from "@/components/primitives";
import { RequestDialog } from "@/components/request-dialog";
import { StatusCard } from "@/components/status-card";
import { useBoxName } from "@/lib/box-name";
import { clientName, expiresText } from "@/lib/format";
import { useReachable } from "@/lib/hooks";
import {
	boxSummary,
	isPaused,
	isPhoneConnected,
	pauseFootnote,
	type SummaryAction,
	showManualPhoneSection,
} from "@/lib/summary";

export const Route = createFileRoute("/_shell/home")({
	component: Home,
});

function Home() {
	const { hostname, device } = Route.useRouteContext();
	const boxName = useBoxName(device);
	const queryClient = useQueryClient();
	const status = useAppStatus(5000);
	const consents = useConsents(5000);
	const clients = useClients(15_000);
	const operations = useOperations(3, 10_000);
	const reachable = useReachable(
		status.dataUpdatedAt,
		status.errorUpdatedAt,
		status.isError,
	);
	const [confirmPause, setConfirmPause] = useState(false);
	const [request, setRequest] = useState<string | null>(null);
	const [phones, setPhones] = useState<DiscoveredPhone[]>([]);

	const refresh = () => {
		void queryClient.invalidateQueries({ queryKey: keys.status });
		void queryClient.invalidateQueries({ queryKey: keys.operations });
	};
	const pause = useMutation({
		mutationFn: () => api.pause(),
		onSuccess: () => {
			toast.success("Pause sent to the box.");
			refresh();
		},
		onError: (error) => toast.error(error.message),
	});
	const discover = useMutation({
		mutationFn: () => api.discover(),
		onSuccess: (found) => {
			setPhones(found);
			if (found.length === 0)
				toast.info("No iPhone is connected to the box by USB.");
		},
		onError: (error) => toast.error(error.message),
	});
	const bind = useMutation({
		mutationFn: (udid: string) =>
			api.setup("bind", {
				udid,
				transport: "auto",
				owner_confirmed: true,
				portrait_locked: true,
			}),
		onSuccess: () => {
			setPhones([]);
			toast.info("Phone binding started. Follow the status on this screen.");
			refresh();
		},
		onError: (error) => toast.error(error.message),
	});
	const setup = useMutation({
		mutationFn: (step: Exclude<SummaryAction, { kind: "discover" }>["step"]) =>
			api.setup(step, step === "pair" ? { owner_confirmed: true } : {}),
		onSuccess: (_, step) => {
			toast.info(
				`${step === "recover" ? "Recovery" : "Setup"} started. Follow the status on this screen.`,
			);
			refresh();
		},
		onError: (error) => toast.error(error.message),
	});

	const summary = boxSummary(status.data, { reachable, hostname });
	const busy =
		pause.isPending || discover.isPending || bind.isPending || setup.isPending;
	const onAction = () => {
		const action = summary.action;
		if (!action) return;
		if (action.kind === "discover") discover.mutate();
		else setup.mutate(action.step);
	};
	const pending = consents.data?.[0];
	const pendingConsent = request
		? consents.data?.find((c) => c.handle === request)
		: undefined;
	const recent = operations.data?.pages[0]?.operations.slice(0, 3) ?? [];

	return (
		<Page>
			<PageHeader title={boxName} />
			<StatusCard
				summary={summary}
				status={status.data}
				clients={clients.data ?? []}
				busy={busy}
				onAction={onAction}
			/>
			{pending ? (
				<button
					type="button"
					onClick={() => setRequest(pending.handle)}
					className="flex items-center gap-3 rounded-[14px] bg-warn-tint px-4 py-3.5 text-left"
				>
					<Avatar name={clientName(pending)} />
					<span className="flex min-w-0 flex-1 flex-col">
						<span className="truncate font-semibold text-base text-foreground">
							{clientName(pending)} wants access
						</span>
						<span className="text-[13px] text-warn">
							{expiresText(pending.expires_in)}
						</span>
					</span>
					<span className="font-semibold text-[15px] text-warn">Review</span>
				</button>
			) : null}
			<Section bare footer={pauseFootnote(status.data)}>
				<BusyButton
					look="danger"
					icon={<Pause className="size-5" fill="currentColor" />}
					busy={pause.isPending}
					disabled={
						!reachable ||
						busy ||
						isPaused(status.data) ||
						!isPhoneConnected(status.data)
					}
					onClick={() => setConfirmPause(true)}
				>
					{isPaused(status.data)
						? "Phone control is paused"
						: "Pause phone control"}
				</BusyButton>
			</Section>
			{showManualPhoneSection(status.data, phones.length) ? (
				<Section title="Phone">
					{phones.map((phone) => (
						<ButtonRow
							key={phone.udid}
							title={phone.name || "iPhone on USB"}
							subtitle={`Bind ${phone.udid.slice(0, 8)}…`}
							accessory="chevron"
							leading={<IconBadge icon={<Smartphone />} fill="dark" />}
							disabled={bind.isPending}
							onClick={() => bind.mutate(phone.udid)}
						/>
					))}
					<ButtonRow
						title="Find iPhone on USB"
						accessory="chevron"
						leading={<IconBadge icon={<Search />} fill="dark" />}
						disabled={discover.isPending}
						onClick={() => discover.mutate()}
					/>
				</Section>
			) : null}
			<Section title="Recent">
				{recent.length === 0 ? (
					<Row
						minHeight={56}
						title="No activity yet"
						subtitle="AI client actions appear here"
					/>
				) : null}
				{recent.map((operation) => (
					<OperationRow
						key={operation.id}
						operation={operation}
						timeStyle="relative"
					/>
				))}
				<LinkRow to="/activity" title="See all activity" />
			</Section>
			<ConfirmDialog
				open={confirmPause}
				onOpenChange={setConfirmPause}
				title="Pause phone control?"
				message="Stops every AI action at once. Resume from your AI client."
				choices={[
					{
						label: "Pause now",
						destructive: true,
						onSelect: () => pause.mutate(),
					},
				]}
			/>
			<RequestDialog
				consent={request ? pendingConsent : null}
				open={request !== null}
				onOpenChange={(open) => {
					if (!open) setRequest(null);
				}}
			/>
		</Page>
	);
}
