import { Smartphone, Users } from "lucide-react";

import type { AppStatus, Client } from "@/api/types";
import { BusyButton, StatusDot } from "@/components/primitives";
import { activeUntil } from "@/lib/format";
import type { BoxSummary } from "@/lib/summary";
import {
	connectionState,
	isPhoneConnected,
	transportText,
} from "@/lib/summary";

function Tile({
	icon,
	label,
	value,
}: {
	icon: React.ReactNode;
	label: string;
	value: string;
}) {
	return (
		<div className="flex min-w-0 flex-1 items-center gap-2.5 rounded-xl bg-muted p-3">
			<span className="text-primary [&_svg]:size-5">{icon}</span>
			<span className="flex min-w-0 flex-col">
				<span className="text-muted-foreground text-xs">{label}</span>
				<span className="truncate font-semibold text-[15px] text-foreground">
					{value}
				</span>
			</span>
		</div>
	);
}

export function StatusCard({
	summary,
	status,
	clients,
	busy,
	onAction,
}: {
	summary: BoxSummary;
	status: AppStatus | undefined;
	clients: Client[];
	busy: boolean;
	onAction: () => void;
}) {
	const transport = transportText(status);
	const connected = isPhoneConnected(status);
	const phoneLabel =
		connected && transport !== "" ? `iPhone · ${transport}` : "iPhone";
	const phoneValue = connected
		? "Connected"
		: connectionState(status) === ""
			? "Unknown"
			: "Not connected";
	const ai = clients.filter((c) => !c.self);
	const active = ai.filter((c) => activeUntil(c) !== null).length;
	const clientsValue =
		active > 0
			? `${active} active`
			: ai.length === 0
				? "None yet"
				: "None active";
	return (
		<section
			aria-live="polite"
			className="flex flex-col gap-3.5 rounded-2xl bg-card p-4 py-5"
		>
			<div className="flex items-center gap-3">
				<StatusDot tone={summary.tone} />
				<div className="flex min-w-0 flex-col gap-0.5">
					<h2 className="font-bold text-[22px] text-foreground leading-tight">
						{summary.headline}
					</h2>
					<p className="text-[15px] text-muted-foreground">{summary.detail}</p>
				</div>
			</div>
			{summary.instruction ? (
				<div className="flex flex-col gap-3">
					<p className="text-[15px] text-foreground">{summary.instruction}</p>
					{summary.actionTitle && summary.action ? (
						<BusyButton busy={busy} onClick={onAction}>
							{summary.actionTitle}
						</BusyButton>
					) : null}
				</div>
			) : (
				<div className="flex gap-2.5">
					<Tile icon={<Smartphone />} label={phoneLabel} value={phoneValue} />
					<Tile icon={<Users />} label="AI clients" value={clientsValue} />
				</div>
			)}
		</section>
	);
}
