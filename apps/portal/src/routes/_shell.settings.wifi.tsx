// The box's Wi-Fi: current network, scan results to join, saved profiles to forget.

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { createFileRoute } from "@tanstack/react-router";
import { Trash2 } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

import { api } from "@/api";
import { keys, useNetwork, useSetupStatus, useWifiScan } from "@/api/queries";
import type { WifiNetwork } from "@/api/types";
import { ConfirmDialog } from "@/components/dialogs";
import { Page, PageHeader, Row, Section } from "@/components/list";
import { BusyButton } from "@/components/primitives";
import {
	HandoffCard,
	HiddenDialog,
	JoinDialog,
	NetworkList,
	useJoinWifi,
} from "@/components/wifi";
import { currentDetail, currentSsid, savedTitle } from "@/lib/wifi";

export const Route = createFileRoute("/_shell/settings/wifi")({
	component: WifiSettings,
});

function WifiSettings() {
	const { hostname } = Route.useRouteContext();
	const queryClient = useQueryClient();
	const network = useNetwork(5000);
	const setup = useSetupStatus(5000);
	const scan = useWifiScan();
	const [picked, setPicked] = useState<WifiNetwork | null>(null);
	const [hidden, setHidden] = useState(false);
	const [forgetting, setForgetting] = useState<string | null>(null);
	const [joiningSsid, setJoiningSsid] = useState<string | null>(null);
	const join = useJoinWifi((ssid) => setJoiningSsid(ssid));
	const forget = useMutation({
		mutationFn: ({ ssid, force }: { ssid: string; force: boolean }) =>
			api.forget(ssid, force),
		onSuccess: () =>
			void queryClient.invalidateQueries({ queryKey: keys.network }),
		onError: (error) => toast.error(error.message),
	});
	const current = currentSsid(network.data);
	const provisioning = network.data?.provisioning;
	const joining =
		provisioning?.state === "joining" || provisioning?.state === "verifying";
	const saved = network.data?.saved ?? [];
	const attempt = provisioning?.last_attempt ?? setup.data?.last_attempt;
	const errorCode =
		joiningSsid && attempt?.ssid === joiningSsid
			? (provisioning?.last_error?.code ?? setup.data?.last_error?.code ?? null)
			: null;
	const resolved =
		joiningSsid !== null &&
		attempt?.ssid === joiningSsid &&
		attempt?.result === "connected";

	return (
		<Page>
			<PageHeader title="Wi-Fi" back="/settings" inline />
			{joiningSsid && !resolved ? (
				<HandoffCard
					ssid={joiningSsid}
					hostname={hostname}
					attempt={attempt}
					errorCode={errorCode}
					setupMode={setup.data?.setup_mode ?? false}
				/>
			) : null}
			<Section title="Box is on">
				{current ? (
					<Row
						minHeight={56}
						title={current}
						subtitle={
							joining ? "Joining a new network…" : currentDetail(network.data)
						}
						accessory="check"
					/>
				) : joining ? (
					<Row
						minHeight={56}
						title="Joining…"
						subtitle="The box is connecting"
					/>
				) : (
					<Row
						minHeight={56}
						title="Not connected"
						subtitle="Pick a network below"
					/>
				)}
			</Section>
			<NetworkList
				networks={scan.data ?? []}
				scanning={scan.isFetching}
				currentSsid={current}
				onPick={setPicked}
				onHidden={() => setHidden(true)}
			/>
			{saved.length > 0 ? (
				<Section title="Saved" footer="Tap the bin to forget a saved network.">
					{saved.map((profile) => (
						<div key={profile} className="flex items-center">
							<Row
								title={savedTitle(profile)}
								subtitle={
									profile === "openlolo-setup"
										? "Setup network"
										: "Saved by the box"
								}
								className="min-w-0 flex-1"
							/>
							{profile !== "openlolo-setup" ? (
								<button
									type="button"
									onClick={() => setForgetting(profile)}
									className="flex size-11 shrink-0 items-center justify-center text-destructive"
									aria-label={`Forget ${savedTitle(profile)}`}
								>
									<Trash2 className="size-5" />
								</button>
							) : null}
						</div>
					))}
				</Section>
			) : null}
			<BusyButton
				look="plain"
				busy={scan.isFetching}
				onClick={() => void scan.refetch()}
			>
				{scan.isFetching ? "Scanning…" : "Scan again"}
			</BusyButton>
			<JoinDialog
				network={picked}
				onClose={() => setPicked(null)}
				joining={join.isPending}
				onJoin={(ssid, password) => {
					setPicked(null);
					join.mutate({ ssid, password });
				}}
			/>
			<HiddenDialog
				open={hidden}
				onClose={() => setHidden(false)}
				onJoin={(ssid, password) => {
					setHidden(false);
					join.mutate({ ssid, password });
				}}
			/>
			<ConfirmDialog
				open={forgetting !== null}
				onOpenChange={(open) => {
					if (!open) setForgetting(null);
				}}
				title={`Forget ${forgetting ? savedTitle(forgetting) : ""}?`}
				choices={[
					{
						label: "Forget",
						destructive: true,
						onSelect: () => {
							if (forgetting) forget.mutate({ ssid: forgetting, force: false });
						},
					},
					{
						label: "Forget even if in use",
						destructive: true,
						onSelect: () => {
							if (forgetting) forget.mutate({ ssid: forgetting, force: true });
						},
					},
				]}
			/>
		</Page>
	);
}
