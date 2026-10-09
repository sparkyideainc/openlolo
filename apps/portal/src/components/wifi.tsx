// Wi-Fi pieces shared by the setup flow and Settings › Wi-Fi: the network list with the join
// dialog, the hidden-network dialog, and the handoff card shown while the box switches radios.

import { Input } from "@openlolo/ui/components/input";
import { Label } from "@openlolo/ui/components/label";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Loader2, Lock } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

import { api } from "@/api";
import { keys } from "@/api/queries";
import type { WifiAttempt, WifiNetwork } from "@/api/types";
import { ConfirmDialog } from "@/components/dialogs";
import { ButtonRow, Row, Section } from "@/components/list";
import { SignalBars } from "@/components/primitives";
import { isSecured, wifiErrorText } from "@/lib/wifi";

export function useJoinWifi(onStarted: (ssid: string) => void) {
	const queryClient = useQueryClient();
	return useMutation({
		mutationFn: ({ ssid, password }: { ssid: string; password: string }) =>
			api.connect(ssid, password),
		onSuccess: (_, { ssid }) => {
			void queryClient.invalidateQueries({ queryKey: keys.network });
			void queryClient.invalidateQueries({ queryKey: keys.setupStatus });
			onStarted(ssid);
		},
		onError: (error) => toast.error(error.message),
	});
}

export function NetworkList({
	networks,
	scanning,
	currentSsid,
	onPick,
	onHidden,
}: {
	networks: WifiNetwork[];
	scanning: boolean;
	currentSsid: string | null;
	onPick: (network: WifiNetwork) => void;
	onHidden: () => void;
}) {
	const others = networks.filter(
		(n) => n.ssid !== "" && n.ssid !== currentSsid,
	);
	return (
		<Section
			title={
				<>
					Other networks
					{scanning && others.length > 0 ? (
						<span className="ml-auto flex items-center gap-1 normal-case tracking-normal">
							<Loader2 className="size-3 animate-spin" /> Refreshing…
						</span>
					) : null}
				</>
			}
			footer="The box joins the network you pick. Your iPhone stays on its own Wi-Fi."
		>
			{others.length === 0 ? (
				<Row title={scanning ? "Scanning…" : "No other networks found"} />
			) : null}
			{others.map((network) => (
				<ButtonRow
					key={network.ssid}
					title={network.ssid}
					onClick={() => onPick(network)}
					trailing={
						<span className="flex items-center gap-3">
							{isSecured(network) ? (
								<Lock className="size-4 text-muted-foreground" />
							) : null}
							<SignalBars percent={network.signal ?? 0} />
						</span>
					}
				/>
			))}
			<ButtonRow
				title="Hidden network…"
				subtitle="Enter a name the box cannot see"
				onClick={onHidden}
				accessory="chevron"
			/>
		</Section>
	);
}

export function JoinDialog({
	network,
	onClose,
	onJoin,
	joining,
}: {
	network: WifiNetwork | null;
	onClose: () => void;
	onJoin: (ssid: string, password: string) => void;
	joining: boolean;
}) {
	const [password, setPassword] = useState("");
	const secured = network ? isSecured(network) : false;
	const disabled = joining || (secured && password.length < 8);
	return (
		<ConfirmDialog
			open={network !== null}
			onOpenChange={(open) => {
				if (!open) {
					onClose();
					setPassword("");
				}
			}}
			title={`Join ${network?.ssid ?? ""}`}
			message={
				secured
					? "Enter the Wi-Fi password for the box to use."
					: "This network is open."
			}
			choices={[
				{
					label: "Join",
					onSelect: () => {
						if (!disabled && network) {
							onJoin(network.ssid, password);
							setPassword("");
						}
					},
				},
			]}
		>
			{secured ? (
				<Input
					type="password"
					autoComplete="off"
					placeholder="Password"
					value={password}
					onChange={(e) => setPassword(e.target.value)}
					className="h-11 rounded-xl text-base"
					aria-label="Wi-Fi password"
				/>
			) : null}
		</ConfirmDialog>
	);
}

export function HiddenDialog({
	open,
	onClose,
	onJoin,
}: {
	open: boolean;
	onClose: () => void;
	onJoin: (ssid: string, password: string) => void;
}) {
	const [ssid, setSsid] = useState("");
	const [password, setPassword] = useState("");
	return (
		<ConfirmDialog
			open={open}
			onOpenChange={(value) => {
				if (!value) onClose();
			}}
			title="Hidden network"
			message="Enter the network name exactly as the router has it, and its password if it has one."
			choices={[
				{
					label: "Join",
					onSelect: () => {
						if (ssid.trim() !== "") onJoin(ssid.trim(), password);
					},
				},
			]}
		>
			<div className="flex flex-col gap-2">
				<Label htmlFor="hidden-ssid" className="sr-only">
					Network name
				</Label>
				<Input
					id="hidden-ssid"
					autoComplete="off"
					autoCapitalize="none"
					maxLength={32}
					placeholder="Network name"
					value={ssid}
					onChange={(e) => setSsid(e.target.value)}
					className="h-11 rounded-xl text-base"
				/>
				<Input
					type="password"
					autoComplete="off"
					maxLength={63}
					placeholder="Password (leave empty if open)"
					value={password}
					onChange={(e) => setPassword(e.target.value)}
					className="h-11 rounded-xl text-base"
					aria-label="Wi-Fi password"
				/>
			</div>
		</ConfirmDialog>
	);
}

/** While the single radio switches from the setup network to the home network, the page says
 * what to do next; once the box reports the result it says so. */
export function HandoffCard({
	ssid,
	hostname,
	attempt,
	errorCode,
	setupMode,
}: {
	ssid: string;
	hostname: string;
	attempt: WifiAttempt | null | undefined;
	errorCode: string | null | undefined;
	setupMode: boolean;
}) {
	const finished = attempt?.ssid === ssid && attempt?.result === "connected";
	if (errorCode) {
		return (
			<div className="rounded-2xl bg-destructive-tint p-4 text-[15px] text-destructive">
				<p className="font-semibold">Could not join {ssid}</p>
				<p className="mt-1">{wifiErrorText(errorCode)}</p>
			</div>
		);
	}
	if (finished) {
		return (
			<div className="rounded-2xl bg-good-tint p-4 text-[15px] text-good">
				<p className="font-semibold">Connected to {ssid}</p>
				<p className="mt-1">
					Join that Wi-Fi network on this iPhone, then open http://{hostname} in
					Safari.
				</p>
			</div>
		);
	}
	return (
		<div className="flex flex-col gap-2 rounded-2xl bg-warn-tint p-4 text-[15px] text-warn">
			<p className="flex items-center gap-2 font-semibold">
				<Loader2 className="size-4 animate-spin" /> OpenLolo is joining {ssid}
			</p>
			<p>
				{setupMode
					? `This page will disconnect while the box switches Wi-Fi. Join ${ssid} on this iPhone, then open http://${hostname} in Safari.`
					: `The box may drop off this network for a moment. If this page stops responding, reopen http://${hostname}.`}
			</p>
			<p>
				If the new network cannot be joined, OpenLolo restores its previous
				connection or reopens its setup Wi-Fi.
			</p>
		</div>
	);
}
