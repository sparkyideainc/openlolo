// Settings: the box card, Wi-Fi, activity, replacing the phone, reset, diagnostics. The box's
// network mode (Wi-Fi, Local, Setup) is set by the top button, not here.

import { Input } from "@openlolo/ui/components/input";
import { Label } from "@openlolo/ui/components/label";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { createFileRoute, useNavigate } from "@tanstack/react-router";
import {
	FileText,
	Info,
	Smartphone,
	SquarePlus,
	Trash2,
	Wifi,
} from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

import { api } from "@/api";
import { useNetwork, useSetupStatus } from "@/api/queries";
import { ConfirmDialog } from "@/components/dialogs";
import { InstallSheet } from "@/components/install-sheet";
import {
	ButtonRow,
	LinkRow,
	Page,
	PageHeader,
	Section,
} from "@/components/list";
import { IconBadge, Mascot } from "@/components/primitives";
import { useBoxName } from "@/lib/box-name";
import { text } from "@/lib/format";
import { useReachable } from "@/lib/hooks";
import { needsInstall } from "@/lib/platform";
import { setOnboardingComplete } from "@/lib/store";

export const Route = createFileRoute("/_shell/settings/")({
	component: Settings,
});

function Settings() {
	const { hostname, device } = Route.useRouteContext();
	const boxName = useBoxName(device);
	const navigate = useNavigate();
	const queryClient = useQueryClient();
	const network = useNetwork(15_000);
	const setup = useSetupStatus(5000);
	const reachable = useReachable(
		network.dataUpdatedAt,
		network.errorUpdatedAt,
		network.isError,
	);
	const [confirmReplace, setConfirmReplace] = useState(false);
	const [confirmReset, setConfirmReset] = useState(false);
	const [typedName, setTypedName] = useState("");
	const [install, setInstall] = useState(false);
	const hasButton = setup.data?.reset_method === "button";
	// Replacing the bound phone happens on the box's own Wi-Fi, so in Local mode (ADR 0012):
	// the new phone is on the cable and the page runs on it.
	const canReplace =
		(setup.data?.access.setup_ap ?? false) && (setup.data?.bound ?? false);

	const replacePhone = useMutation({
		mutationFn: () => api.setup("unbind", {}),
		onSuccess: async () => {
			toast.info(
				"The box forgot its iPhone. It pairs the one on its cable now.",
			);
			setOnboardingComplete(false);
			queryClient.clear();
			await navigate({
				to: "/connect",
				search: { next: undefined },
				replace: true,
			});
		},
		onError: (error) => toast.error(error.message),
	});
	const resetBox = useMutation({
		mutationFn: () => api.reset(typedName.trim()),
		onSuccess: async () => {
			toast.info("Resetting. The box restarts into setup in about a minute.");
			setOnboardingComplete(false);
			queryClient.clear();
			await navigate({
				to: "/offline",
				search: { next: "/setup" },
				replace: true,
			});
		},
		onError: (error) => toast.error(error.message),
	});
	const exportDiagnostics = useMutation({
		mutationFn: () => api.diagnostics(),
		onSuccess: (data) => {
			const json = JSON.stringify(data, null, 2);
			if (/[A-Za-z0-9_-]{40,}/.test(json)) {
				toast.error(
					"Diagnostics still contain a token-like value; export was stopped.",
				);
				return;
			}
			// One tap saves the file: Safari shows its download prompt straight away.
			const url = URL.createObjectURL(
				new Blob([json], { type: "application/json" }),
			);
			const link = document.createElement("a");
			link.href = url;
			link.download = "openlolo-diagnostics.json";
			link.click();
			setTimeout(() => URL.revokeObjectURL(url), 60_000);
		},
		onError: (error) => toast.error(error.message),
	});

	return (
		<Page>
			<PageHeader title="Settings" />
			<Section>
				<LinkRow
					to="/settings/about"
					minHeight={60}
					title={boxName}
					subtitle={
						reachable ? `Reachable at ${hostname}` : "Can't reach the box"
					}
					leading={<Mascot size={44} radius={10} />}
				/>
			</Section>
			<Section
				title="Box"
				footer={
					hasButton
						? "Hold the top button for Local mode: the box broadcasts its own Wi-Fi so this iPhone reaches it without a router. Press once to go back."
						: undefined
				}
			>
				<LinkRow
					to="/settings/wifi"
					title="Wi-Fi"
					value={text(network.data?.ssid, "Not set")}
					leading={<IconBadge icon={<Wifi />} fill="info" />}
				/>
				<LinkRow
					to="/activity"
					title="Activity"
					leading={<IconBadge icon={<FileText />} fill="grey" />}
				/>
				{canReplace ? (
					<ButtonRow
						title="Use another iPhone"
						subtitle="Forget the bound iPhone and pair the one on the cable"
						accessory="chevron"
						leading={<IconBadge icon={<Smartphone />} fill="warn" />}
						disabled={replacePhone.isPending}
						onClick={() => setConfirmReplace(true)}
					/>
				) : null}
			</Section>
			<Section
				title="Reset"
				footer="Erases the iPhone pairing, AI clients, activity and saved Wi-Fi, then restarts into first-time setup on the card's Wi-Fi. The box name and card password stay."
			>
				<ButtonRow
					title="Reset box"
					accessory="chevron"
					leading={<IconBadge icon={<Trash2 />} fill="destructive" />}
					disabled={resetBox.isPending}
					onClick={() => {
						// A box with the top button resets through the button challenge.
						if (setup.data?.reset_method === "button") {
							void navigate({ to: "/reset" });
							return;
						}
						setTypedName("");
						setConfirmReset(true);
					}}
				/>
			</Section>
			<Section
				title="Support"
				footer="Exports are scrubbed of tokens, keys, passwords and device identifiers before sharing."
			>
				{needsInstall() ? (
					<ButtonRow
						title="Add to Home Screen"
						subtitle="Open OpenLolo like an app"
						accessory="chevron"
						leading={<IconBadge icon={<SquarePlus />} fill="accent" />}
						onClick={() => setInstall(true)}
					/>
				) : null}
				<ButtonRow
					title="Export diagnostics"
					leading={<IconBadge icon={<Info />} fill="dark" />}
					disabled={exportDiagnostics.isPending}
					onClick={() => exportDiagnostics.mutate()}
				/>
			</Section>
			<InstallSheet
				open={install}
				onOpenChange={setInstall}
				hostname={hostname}
			/>
			<ConfirmDialog
				open={confirmReset}
				onOpenChange={setConfirmReset}
				title="Reset this box?"
				message={
					<>
						Everything the box knows about your iPhone, AI clients and Wi-Fi is
						erased. To confirm, type its name: <strong>{device}</strong>
					</>
				}
				choices={[
					{
						label: "Reset box",
						destructive: true,
						disabled:
							!device ||
							typedName.trim().toLowerCase() !== device.toLowerCase(),
						onSelect: () => resetBox.mutate(),
					},
				]}
			>
				<div className="flex flex-col gap-1.5 py-1">
					<Label htmlFor="reset-name" className="text-[13px]">
						Box name
					</Label>
					<Input
						id="reset-name"
						value={typedName}
						onChange={(e) => setTypedName(e.target.value)}
						placeholder={device ?? "OpenLolo-XXXX"}
						autoCapitalize="characters"
						autoCorrect="off"
						spellCheck={false}
						inputMode="text"
					/>
				</div>
			</ConfirmDialog>
			<ConfirmDialog
				open={confirmReplace}
				onOpenChange={setConfirmReplace}
				title="Use another iPhone?"
				message="The box forgets its current iPhone and every pairing record, then pairs the iPhone on its cable. AI clients keep their access."
				choices={[
					{
						label: "Forget and pair this one",
						destructive: true,
						onSelect: () => replacePhone.mutate(),
					},
				]}
			/>
		</Page>
	);
}
