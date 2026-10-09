// The cable is the key (ADR 0012): this screen waits until the box would admit this browser,
// then opens a session on its own. It also narrates USB pairing before any session exists.

import { useQueryClient } from "@tanstack/react-query";
import { createFileRoute, redirect, useNavigate } from "@tanstack/react-router";
import { Cable, Smartphone } from "lucide-react";
import { useEffect, useRef } from "react";

import { api } from "@/api";
import { keys, useSetupStatus } from "@/api/queries";
import { Mascot } from "@/components/primitives";
import { Plain } from "@/components/shell";
import { bootstrap } from "@/lib/session";
import { pairingText } from "@/lib/wifi";

export const Route = createFileRoute("/connect")({
	validateSearch: (search: Record<string, unknown>) => ({
		next:
			typeof search.next === "string" && search.next.startsWith("/")
				? search.next
				: undefined,
	}),
	beforeLoad: async ({ context, search }) => {
		const status = await bootstrap(context.queryClient);
		if (status.session || status.access.allowed) {
			if (!status.session) await api.openSession();
			throw redirect({ to: search.next ?? "/", replace: true });
		}
	},
	component: Connect,
});

function Connect() {
	const { next } = Route.useSearch();
	const navigate = useNavigate();
	const queryClient = useQueryClient();
	const status = useSetupStatus(3000);
	const opening = useRef(false);
	const data = status.data;
	const allowed = data?.access.allowed ?? false;
	useEffect(() => {
		if (!allowed || opening.current) return;
		opening.current = true;
		api
			.openSession()
			.then(async () => {
				await queryClient.invalidateQueries({ queryKey: keys.setupStatus });
				await navigate({ to: next ?? "/", replace: true });
			})
			.catch(() => {
				opening.current = false;
			});
	}, [allowed, next, navigate, queryClient]);

	const cable = data?.access.cable ?? false;
	const headline = !data
		? "Looking for the box"
		: !cable
			? "Plug the iPhone into the box"
			: "Open this page on the iPhone";
	const detail = !data
		? "One moment."
		: !cable
			? "Use the USB cable that came with it. The box pairs the phone on its cable by itself, and this page opens once it sees the phone."
			: data.bound
				? "Only the iPhone plugged into the box can use this page. Open http://" +
					data.hostname +
					" in Safari on that iPhone."
				: "Join the box's setup Wi-Fi from the card, then open this page on the iPhone plugged into the box.";
	return (
		<Plain className="justify-center">
			<div className="flex flex-col items-center gap-6 text-center">
				<Mascot size={96} radius={24} />
				<div className="flex items-center gap-3.5 text-foreground">
					<Smartphone className="size-9" strokeWidth={1.2} />
					<Cable className="size-6 text-primary" strokeWidth={1.6} />
					<span className="flex size-9 items-center justify-center rounded-lg border-[1.5px] border-foreground font-bold text-[11px]">
						O
					</span>
				</div>
				<div className="flex flex-col gap-2">
					<h1 className="font-bold font-heading text-[1.75rem] text-foreground leading-tight">
						{headline}
					</h1>
					<p className="max-w-[320px] text-base text-muted-foreground">
						{detail}
					</p>
				</div>
				{data?.usb_pairing && cable ? (
					<p className="max-w-[320px] rounded-2xl bg-card px-4 py-3 text-[15px] text-foreground">
						{pairingText(data.usb_pairing.state)}
					</p>
				) : null}
				<p className="text-[13px] text-muted-foreground">
					This page checks on its own every few seconds.
					{data ? ` Box: http://${data.hostname}` : ""}
				</p>
			</div>
		</Plain>
	);
}
