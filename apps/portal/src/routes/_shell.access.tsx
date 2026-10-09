// Access: requests waiting for the owner, then every connected client. `?r=<handle>` opens a
// request straight away (the consent page's QR links here).

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";

import { api } from "@/api";
import { keys, useClients, useConsents } from "@/api/queries";
import type { Client } from "@/api/types";
import { ClientDialog } from "@/components/client-dialog";
import { ConfirmDialog } from "@/components/dialogs";
import { ButtonRow, Page, PageHeader, Row, Section } from "@/components/list";
import { Avatar } from "@/components/primitives";
import { RequestDialog } from "@/components/request-dialog";
import {
	clientName,
	clientSubtitle,
	expiresText,
	relativeText,
} from "@/lib/format";

export const Route = createFileRoute("/_shell/access")({
	validateSearch: (search: Record<string, unknown>) => ({
		r: typeof search.r === "string" && search.r !== "" ? search.r : undefined,
	}),
	component: Access,
});

function Access() {
	const { r } = Route.useSearch();
	const navigate = useNavigate();
	const queryClient = useQueryClient();
	const consents = useConsents(5000);
	const clients = useClients(15_000);
	const [request, setRequest] = useState<string | null>(null);
	const [selected, setSelected] = useState<Client | null>(null);
	const [revoking, setRevoking] = useState<Client | null>(null);
	useEffect(() => {
		if (r) {
			setRequest(r);
			void navigate({ to: "/access", search: { r: undefined }, replace: true });
		}
	}, [r, navigate]);
	const revoke = useMutation({
		mutationFn: (client: Client) => api.revoke(client.client_id),
		onSuccess: () => {
			void queryClient.invalidateQueries({ queryKey: keys.clients });
			void queryClient.invalidateQueries({ queryKey: keys.status });
		},
		onError: (error) => toast.error(error.message),
	});
	const ai = (clients.data ?? []).filter((c) => !c.self);
	const pendingConsent = request
		? consents.data?.find((c) => c.handle === request)
		: undefined;
	return (
		<Page>
			<PageHeader title="Access" />
			{(consents.data?.length ?? 0) > 0 ? (
				<Section title="Waiting for you">
					{consents.data?.map((consent) => (
						<ButtonRow
							key={consent.handle}
							minHeight={56}
							title={clientName(consent)}
							subtitle={`Asked ${relativeText(consent.created).toLowerCase()} · ${expiresText(consent.expires_in).toLowerCase()}`}
							accessory="chevron"
							leading={<Avatar name={clientName(consent)} />}
							onClick={() => setRequest(consent.handle)}
						/>
					))}
				</Section>
			) : null}
			<Section
				title="Connected"
				footer={
					ai.length > 0
						? "Tap the bin on a client to end its access early."
						: undefined
				}
			>
				{ai.length === 0 ? (
					<Row
						minHeight={56}
						title="No AI clients yet"
						subtitle="Approve a request to connect one"
					/>
				) : null}
				{ai.map((client) => (
					<div key={client.client_id} className="flex items-center">
						<ButtonRow
							minHeight={56}
							lines={1}
							title={clientName(client)}
							subtitle={clientSubtitle(client)}
							accessory="chevron"
							leading={<Avatar name={clientName(client)} />}
							onClick={() => setSelected(client)}
							className="min-w-0 flex-1"
						/>
						<button
							type="button"
							onClick={() => setRevoking(client)}
							className="flex size-11 shrink-0 items-center justify-center text-destructive"
							aria-label={`Remove ${clientName(client)}`}
						>
							<Trash2 className="size-5" />
						</button>
					</div>
				))}
			</Section>
			<RequestDialog
				consent={request ? pendingConsent : null}
				open={request !== null}
				onOpenChange={(open) => {
					if (!open) setRequest(null);
				}}
			/>
			<ClientDialog
				client={selected}
				open={selected !== null}
				onOpenChange={(open) => {
					if (!open) setSelected(null);
				}}
				onEndAccess={setRevoking}
			/>
			<ConfirmDialog
				open={revoking !== null}
				onOpenChange={(open) => {
					if (!open) setRevoking(null);
				}}
				title={`End access for ${revoking ? clientName(revoking) : "this client"}?`}
				message="The client must ask again before it can see or control your iPhone."
				choices={[
					{
						label: "End access",
						destructive: true,
						onSelect: () => {
							if (revoking) revoke.mutate(revoking);
						},
					},
				]}
			/>
		</Page>
	);
}
