// Everything the box knows about one connected client, and the way to end its access.

import { CircleX } from "lucide-react";

import type { Client } from "@/api/types";
import { Sheet, SheetBar } from "@/components/dialogs";
import { LabeledRow, Section } from "@/components/list";
import { Avatar, BusyButton } from "@/components/primitives";
import {
	activeUntil,
	clientName,
	dateText,
	hostOf,
	shortId,
	shortScopeText,
} from "@/lib/format";

export function clientRows(client: Client): [string, string][] {
	const rows: [string, string][] = [];
	const scopes = client.scopes ?? [];
	rows.push(["Access", shortScopeText(scopes)]);
	const until = activeUntil(client);
	rows.push([
		"Active",
		until ? `Until ${dateText(until.getTime() / 1000)}` : "No session",
	]);
	if (scopes.includes("owner:admin")) rows.push(["Box settings", "Allowed"]);
	const hosts = [
		...new Set(
			(client.redirect_uris ?? [])
				.map(hostOf)
				.filter((h): h is string => h !== null),
		),
	].sort();
	if (hosts.length > 0) rows.push(["From", hosts.join(", ")]);
	else if (client.issuer && hostOf(client.issuer))
		rows.push(["From", hostOf(client.issuer) as string]);
	rows.push(["Connected", dateText(client.created)]);
	if (client.last_used !== null && client.last_used !== undefined)
		rows.push(["Last used", dateText(client.last_used)]);
	rows.push(["Client ID", shortId(client.client_id)]);
	return rows;
}

export function ClientDialog({
	client,
	open,
	onOpenChange,
	onEndAccess,
}: {
	client: Client | null;
	open: boolean;
	onOpenChange: (open: boolean) => void;
	onEndAccess: (client: Client) => void;
}) {
	return (
		<Sheet
			open={open}
			onOpenChange={onOpenChange}
			title={client ? clientName(client) : "Client"}
		>
			<SheetBar
				right={
					<button type="button" onClick={() => onOpenChange(false)}>
						Done
					</button>
				}
			/>
			{client ? (
				<div className="flex flex-col gap-6 pt-2 pb-2">
					<div className="flex flex-col items-center gap-3 text-center">
						<Avatar name={clientName(client)} size={72} />
						<div className="flex flex-col gap-1">
							<h2 className="font-bold text-[22px] text-foreground">
								{clientName(client)}
							</h2>
							<p className="text-base text-muted-foreground">AI client</p>
						</div>
					</div>
					<Section>
						{clientRows(client).map(([label, value]) => (
							<LabeledRow key={label} label={label} value={value} />
						))}
					</Section>
					{client.self ? null : (
						<BusyButton
							look="danger"
							icon={<CircleX className="size-5" />}
							onClick={() => {
								onOpenChange(false);
								onEndAccess(client);
							}}
						>
							End access
						</BusyButton>
					)}
				</div>
			) : null}
		</Sheet>
	);
}
