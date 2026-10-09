// Box details and the local rename.

import { Input } from "@openlolo/ui/components/input";
import { createFileRoute } from "@tanstack/react-router";
import { useState } from "react";

import { useInfo, useNetwork } from "@/api/queries";
import { ConfirmDialog } from "@/components/dialogs";
import {
	ButtonRow,
	LabeledRow,
	Page,
	PageHeader,
	Section,
} from "@/components/list";
import { Mascot } from "@/components/primitives";
import { renameBox, useBoxName } from "@/lib/box-name";
import { text } from "@/lib/format";

export const Route = createFileRoute("/_shell/settings/about")({
	component: About,
});

function About() {
	const { hostname, device } = Route.useRouteContext();
	const boxName = useBoxName(device);
	const info = useInfo();
	const network = useNetwork(15_000);
	const [renaming, setRenaming] = useState(false);
	const [newName, setNewName] = useState("");
	const board = text(info.data?.board, "—");
	const provisioning = network.data?.provisioning;
	const setupMode = provisioning?.state === "setup_mode";
	const expires = provisioning?.expires_in;
	return (
		<Page>
			<PageHeader title={boxName} back="/settings" inline />
			<div className="flex flex-col items-center gap-2.5 py-2">
				<Mascot size={96} radius={24} />
				<h2 className="font-bold text-[22px] text-foreground">{boxName}</h2>
			</div>
			<Section>
				<LabeledRow label="Software" value={text(info.data?.version)} />
				<LabeledRow
					label="Board"
					value={board === "—" ? board : `Board ${board}`}
				/>
				<LabeledRow label="Address" value={text(network.data?.ip)} />
				<LabeledRow label="Hostname" value={hostname} />
				<LabeledRow
					label="Setup mode"
					value={
						setupMode
							? `On${typeof expires === "number" ? ` · ${Math.ceil(expires / 60)} min left` : ""}`
							: "Off"
					}
				/>
			</Section>
			<Section footer="The name is kept in this browser only.">
				<ButtonRow
					title="Rename box"
					accessory="chevron"
					onClick={() => {
						setNewName(boxName);
						setRenaming(true);
					}}
				/>
			</Section>
			<ConfirmDialog
				open={renaming}
				onOpenChange={setRenaming}
				title="Rename box"
				choices={[{ label: "Save", onSelect: () => renameBox(newName) }]}
			>
				<Input
					autoFocus
					maxLength={40}
					placeholder="Box name"
					value={newName}
					onChange={(e) => setNewName(e.target.value)}
					className="h-11 rounded-xl text-base"
					aria-label="Box name"
				/>
			</ConfirmDialog>
		</Page>
	);
}
