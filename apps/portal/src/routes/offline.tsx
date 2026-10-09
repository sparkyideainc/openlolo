// The page loaded but the box does not answer: usually the radio handoff, or the wrong Wi-Fi.

import { useQueryClient } from "@tanstack/react-query";
import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { WifiOff } from "lucide-react";
import { useEffect } from "react";

import { useSetupStatus } from "@/api/queries";
import { BusyButton } from "@/components/primitives";
import { Plain } from "@/components/shell";

export const Route = createFileRoute("/offline")({
	validateSearch: (search: Record<string, unknown>) => ({
		next:
			typeof search.next === "string" && search.next.startsWith("/")
				? search.next
				: undefined,
	}),
	component: Offline,
});

function Offline() {
	const { next } = Route.useSearch();
	const navigate = useNavigate();
	const queryClient = useQueryClient();
	const status = useSetupStatus(3000);
	useEffect(() => {
		if (status.isSuccess) void navigate({ to: next ?? "/", replace: true });
	}, [status.isSuccess, next, navigate]);
	return (
		<Plain className="items-center justify-center text-center">
			<WifiOff className="size-10 text-muted-foreground" />
			<h1 className="mt-4 font-bold font-heading text-[1.5rem] text-foreground">
				Can't reach the box
			</h1>
			<p className="mt-2 max-w-[320px] text-base text-muted-foreground">
				If the box is switching Wi-Fi, join the same network as the box and
				reopen this address. This page retries on its own.
			</p>
			<div className="mt-6 w-full max-w-xs">
				<BusyButton
					look="plain"
					busy={status.isFetching}
					onClick={() => void queryClient.invalidateQueries()}
				>
					Try again
				</BusyButton>
			</div>
		</Plain>
	);
}
