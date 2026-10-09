// Approve or deny one AI client request, with the access window and an optional one-time PIN
// (the app's RequestSheet).

import { Switch } from "@openlolo/ui/components/switch";
import {
	ToggleGroup,
	ToggleGroupItem,
} from "@openlolo/ui/components/toggle-group";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ClockAlert } from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";

import { api } from "@/api";
import { keys } from "@/api/queries";
import type { Consent, ConsentWindow } from "@/api/types";
import { Sheet, SheetBar } from "@/components/dialogs";
import { LabeledRow, Section } from "@/components/list";
import { Avatar, BusyButton } from "@/components/primitives";
import {
	clientName,
	consentFrom,
	expiresText,
	scopeText,
	windowText,
} from "@/lib/format";
import { useTick } from "@/lib/hooks";

export function RequestDialog({
	consent,
	open,
	onOpenChange,
}: {
	consent: Consent | null | undefined;
	open: boolean;
	onOpenChange: (open: boolean) => void;
}) {
	const queryClient = useQueryClient();
	const [window, setWindow] = useState<ConsentWindow>("8h");
	const [admin, setAdmin] = useState(false);
	const [pin, setPin] = useState<string | null>(null);
	const [openedAt, setOpenedAt] = useState(() => Date.now());
	const tick = useTick(open);
	useEffect(() => {
		if (open) {
			setWindow("8h");
			setAdmin(false);
			setPin(null);
			setOpenedAt(Date.now());
		}
	}, [open]);
	useEffect(() => {
		if (!admin && window === "30d") setWindow("24h");
	}, [admin, window]);

	const scopes = consent?.scopes ?? [];
	const offersAdmin = scopes.includes("owner:admin");
	const windows: ConsentWindow[] = admin
		? ["1h", "8h", "24h", "30d"]
		: ["1h", "8h", "24h"];
	const done = () => {
		void queryClient.invalidateQueries({ queryKey: keys.consents });
		void queryClient.invalidateQueries({ queryKey: keys.clients });
		void queryClient.invalidateQueries({ queryKey: keys.status });
		onOpenChange(false);
	};
	const decide = useMutation({
		mutationFn: (allow: boolean) =>
			api.decide(consent?.handle ?? "", allow, admin, window),
		onSuccess: (_, allow) => {
			toast.success(allow ? "Access approved." : "Request denied.");
			done();
		},
		onError: (error) => toast.error(error.message),
	});
	const issuePin = useMutation({
		mutationFn: () => api.pin(consent?.handle ?? "", admin, window),
		onSuccess: (result) => setPin(result.pin),
		onError: (error) => toast.error(error.message),
	});
	const busy = decide.isPending || issuePin.isPending;
	void tick;
	const elapsed = Math.floor((Date.now() - openedAt) / 1000);
	const remaining = consent ? (consent.expires_in ?? 0) - elapsed : 0;

	return (
		<Sheet
			open={open}
			onOpenChange={onOpenChange}
			title={consent ? `${clientName(consent)} wants access` : "Request"}
		>
			<SheetBar
				left={
					<button type="button" onClick={() => onOpenChange(false)}>
						Cancel
					</button>
				}
			/>
			{consent ? (
				<div className="flex flex-col gap-6 pt-2">
					<div className="flex flex-col items-center gap-3 text-center">
						<Avatar name={clientName(consent)} size={72} />
						<div className="flex flex-col gap-1">
							<h2 className="font-bold text-[22px] text-foreground">
								{clientName(consent)}
							</h2>
							<p className="text-base text-muted-foreground">
								{scopes.includes("phone:control")
									? "wants to see and control your iPhone"
									: "wants to see your iPhone screen"}
							</p>
						</div>
					</div>
					<Section>
						<LabeledRow label="From" value={consentFrom(consent)} />
						<LabeledRow
							label="Can"
							value={scopeText(scopes.filter((s) => s !== "owner:admin"))}
						/>
						<LabeledRow
							label="Request expires"
							value={expiresText(remaining).replace("Expires ", "")}
						/>
					</Section>
					<Section title="Allow for" bare>
						<ToggleGroup
							value={[window]}
							onValueChange={(value) => {
								const next = value[0];
								if (next) setWindow(next as ConsentWindow);
							}}
							spacing={0}
							className="w-full rounded-xl bg-card p-1"
						>
							{windows.map((w) => (
								<ToggleGroupItem
									key={w}
									value={w}
									className="h-9 flex-1 rounded-lg text-[13px] aria-pressed:bg-muted data-[pressed]:bg-muted data-[pressed]:font-semibold data-[pressed]:text-foreground"
								>
									{windowText(w)}
								</ToggleGroupItem>
							))}
						</ToggleGroup>
						{offersAdmin ? (
							<div className="mt-2 flex min-h-14 items-center justify-between gap-4 rounded-2xl bg-card px-4 py-2">
								<span className="flex flex-col">
									<span className="text-base text-foreground">
										Allow box administration
									</span>
									<span className="text-[13px] text-muted-foreground">
										Lets the client change box settings and clients.
									</span>
								</span>
								<Switch
									checked={admin}
									onCheckedChange={setAdmin}
									aria-label="Allow box administration"
								/>
							</div>
						) : null}
					</Section>
					{pin ? (
						<Section>
							<div className="flex flex-col items-center gap-1.5 px-4 py-4 text-center">
								<span className="select-all font-bold font-mono text-[34px] text-foreground tracking-widest">
									{pin}
								</span>
								<span className="text-[13px] text-muted-foreground">
									Enter this PIN on the requesting page. It expires in 30
									seconds.
								</span>
							</div>
						</Section>
					) : null}
					<div className="flex flex-col gap-2.5 pb-2">
						<BusyButton
							busy={decide.isPending && decide.variables === true}
							disabled={busy}
							onClick={() => decide.mutate(true)}
						>
							Allow for {windowText(window)}
						</BusyButton>
						<BusyButton
							look="plain"
							busy={decide.isPending && decide.variables === false}
							disabled={busy}
							onClick={() => decide.mutate(false)}
						>
							Deny
						</BusyButton>
						<BusyButton
							look="link"
							busy={issuePin.isPending}
							disabled={busy}
							onClick={() => issuePin.mutate()}
							className="h-11 font-medium text-[15px]"
						>
							{pin ? "Issue a new PIN" : "Use a one-time PIN instead"}
						</BusyButton>
					</div>
				</div>
			) : (
				<div className="flex flex-col items-center gap-3 px-8 py-12 text-center">
					<ClockAlert className="size-10 text-muted-foreground" />
					<h2 className="font-bold text-[20px] text-foreground">
						This request has expired
					</h2>
					<p className="text-base text-muted-foreground">
						Ask the AI client to request access again.
					</p>
					<BusyButton
						look="plain"
						className="mt-2"
						onClick={() => onOpenChange(false)}
					>
						Done
					</BusyButton>
				</div>
			)}
		</Sheet>
	);
}
