// Local mode (top-button hold on a box with a saved phone): the box broadcasts its own Wi-Fi so
// the phone can reach it without a router. One line on every tab, with the way out.

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Loader2, Radio } from "lucide-react";
import { toast } from "sonner";

import { api } from "@/api";
import { keys, useSetupStatus } from "@/api/queries";

export function LocalModeBanner() {
	const setup = useSetupStatus(5000);
	const queryClient = useQueryClient();
	const exit = useMutation({
		mutationFn: () => api.setupMode(false),
		onSuccess: () =>
			void queryClient.invalidateQueries({ queryKey: keys.setupStatus }),
		onError: (error) => toast.error(error.message),
	});
	if (setup.data?.mode !== "local") return null;
	return (
		<section
			role="status"
			className="mb-4 flex items-center gap-3 rounded-2xl bg-accent px-4 py-3 text-[14px] text-foreground"
		>
			<Radio className="size-5 shrink-0 text-primary" strokeWidth={2} />
			<p className="flex-1">
				<strong>Local mode.</strong> The box is broadcasting{" "}
				<strong>{setup.data.device ?? "its own Wi-Fi"}</strong>. Press the top
				button once to end it.
			</p>
			<button
				type="button"
				onClick={() => exit.mutate()}
				disabled={exit.isPending}
				className="flex min-h-8 shrink-0 items-center gap-1.5 rounded-full bg-card px-3 py-1.5 font-semibold text-[14px] text-primary disabled:opacity-60"
			>
				{exit.isPending ? <Loader2 className="size-4 animate-spin" /> : null}
				End
			</button>
		</section>
	);
}
