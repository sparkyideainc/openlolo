// The update alert (canvas "Home · update dialog"): asks once per version, on whichever tab is
// open, when the box knows of a newer release. Later hides it for that version only.

import {
	AlertDialog,
	AlertDialogCancel,
	AlertDialogContent,
	AlertDialogDescription,
	AlertDialogFooter,
	AlertDialogHeader,
	AlertDialogTitle,
} from "@openlolo/ui/components/alert-dialog";
import { Button } from "@openlolo/ui/components/button";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { ExternalLink } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";

import { api, isApiError } from "@/api";
import { keys, useAppStatus } from "@/api/queries";
import type { UpdateView } from "@/api/types";
import { Mascot } from "@/components/primitives";
import { promptedUpdate, setPromptedUpdate } from "@/lib/store";
import { refusalText, shouldPrompt, versionText } from "@/lib/update";

/** Start installing `version`, show the attempt at once, and open the Software update page. */
export function useStartUpdate() {
	const queryClient = useQueryClient();
	const navigate = useNavigate();
	return useMutation({
		mutationFn: (version: string) => api.updateInstall(version),
		onSuccess: async (attempt) => {
			queryClient.setQueryData<UpdateView>(keys.update, (view) =>
				view ? { ...view, attempt } : view,
			);
			await navigate({ to: "/settings/update" });
		},
		onError: (error) =>
			toast.error(
				isApiError(error)
					? refusalText(error.code, error.message)
					: error.message,
			),
	});
}

/** "Claude is using the iPhone": someone other than this page holds the control lease. */
export function useAiInControl(enabled = true): boolean {
	const control = useAppStatus(enabled ? 10_000 : false, { enabled }).data
		?.control;
	return control?.active === true && control.owned !== true;
}

export function UpdateDialog({
	view,
	suppressed,
}: {
	view: UpdateView | undefined;
	/** The Software update page is open: it shows the same offer. */
	suppressed: boolean;
}) {
	const [open, setOpen] = useState(false);
	const start = useStartUpdate();
	const aiInControl = useAiInControl(open);
	const latest = view?.latest ?? null;
	const updateNow = useRef<HTMLButtonElement>(null);

	useEffect(() => {
		if (!suppressed && shouldPrompt(view, promptedUpdate())) setOpen(true);
	}, [view, suppressed]);

	const close = () => {
		if (latest) setPromptedUpdate(latest);
		setOpen(false);
	};
	if (!view || !latest) return null;
	return (
		<AlertDialog
			open={open && !suppressed}
			onOpenChange={(next) => (next ? setOpen(true) : close())}
		>
			<AlertDialogContent
				className="gap-4 rounded-2xl p-6"
				initialFocus={updateNow}
			>
				<AlertDialogHeader className="place-items-center gap-3 text-center sm:place-items-center sm:text-center">
					<Mascot size={56} radius={14} />
					<AlertDialogTitle className="font-semibold text-[17px] leading-[22px]">
						Update to OpenLolo {versionText(latest)}?
					</AlertDialogTitle>
					<AlertDialogDescription className="text-[13px] leading-[18px]">
						You have {versionText(view.installed)}. The update takes about 3
						minutes, and phone control stops while the box restarts.
						{aiInControl
							? " An AI client is using the iPhone now; updating stops it."
							: null}
					</AlertDialogDescription>
				</AlertDialogHeader>
				{view.changelog_url ? (
					<a
						href={view.changelog_url}
						target="_blank"
						rel="noreferrer"
						className="-mt-1 flex min-h-8 items-center justify-center gap-1 font-semibold text-[14px] text-primary"
					>
						View changelog
						<ExternalLink className="size-3.5" strokeWidth={2.2} />
					</a>
				) : null}
				<AlertDialogFooter className="m-0 flex-col gap-2 border-0 bg-transparent p-0 sm:flex-col">
					<Button
						ref={updateNow}
						className="h-11 w-full rounded-xl font-semibold text-[15px]"
						disabled={start.isPending}
						onClick={() => {
							close();
							start.mutate(latest);
						}}
					>
						Update now
					</Button>
					<AlertDialogCancel className="h-11 w-full rounded-xl font-semibold text-[15px]">
						Later
					</AlertDialogCancel>
				</AlertDialogFooter>
			</AlertDialogContent>
		</AlertDialog>
	);
}
