// Confirmations (the app's `.alert`) and sheets (the app's `.sheet`): AlertDialog for the
// former, a Dialog that becomes a bottom sheet on phones for the latter.

import {
	AlertDialog,
	AlertDialogAction,
	AlertDialogCancel,
	AlertDialogContent,
	AlertDialogDescription,
	AlertDialogFooter,
	AlertDialogHeader,
	AlertDialogTitle,
} from "@openlolo/ui/components/alert-dialog";
import {
	Dialog,
	DialogContent,
	DialogDescription,
	DialogTitle,
} from "@openlolo/ui/components/dialog";
import { cn } from "@openlolo/ui/lib/utils";
import type { ReactNode } from "react";

export interface ConfirmChoice {
	label: string;
	destructive?: boolean;
	/** Greyed out until the dialog's own fields allow it (type-to-confirm). */
	disabled?: boolean;
	onSelect: () => void;
}

export function ConfirmDialog({
	open,
	onOpenChange,
	title,
	message,
	choices,
	cancel = "Cancel",
	children,
}: {
	open: boolean;
	onOpenChange: (open: boolean) => void;
	title: string;
	message?: ReactNode;
	choices: ConfirmChoice[];
	cancel?: string;
	/** Optional fields rendered between the message and the buttons (the app's alert text fields). */
	children?: ReactNode;
}) {
	return (
		<AlertDialog open={open} onOpenChange={onOpenChange}>
			<AlertDialogContent className="rounded-2xl">
				<AlertDialogHeader>
					<AlertDialogTitle className="text-[17px]">{title}</AlertDialogTitle>
					{message ? (
						<AlertDialogDescription className="text-[13px]">
							{message}
						</AlertDialogDescription>
					) : null}
				</AlertDialogHeader>
				{children}
				<AlertDialogFooter className="flex-col-reverse gap-2 sm:flex-col-reverse">
					<AlertDialogCancel className="h-11 w-full rounded-xl font-semibold text-[15px]">
						{cancel}
					</AlertDialogCancel>
					{choices.map((choice) => (
						<AlertDialogAction
							key={choice.label}
							disabled={choice.disabled}
							onClick={() => {
								onOpenChange(false);
								choice.onSelect();
							}}
							className={cn(
								"h-11 w-full rounded-xl font-semibold text-[15px]",
								choice.destructive
									? "bg-destructive text-destructive-foreground hover:bg-destructive/90"
									: "bg-primary text-primary-foreground hover:bg-primary/90",
							)}
						>
							{choice.label}
						</AlertDialogAction>
					))}
				</AlertDialogFooter>
			</AlertDialogContent>
		</AlertDialog>
	);
}

/** A sheet from the bottom on phones, a centred dialog on wider screens. */
export function Sheet({
	open,
	onOpenChange,
	title,
	description,
	children,
}: {
	open: boolean;
	onOpenChange: (open: boolean) => void;
	title: string;
	description?: string;
	children: ReactNode;
}) {
	return (
		<Dialog open={open} onOpenChange={onOpenChange}>
			<DialogContent
				showCloseButton={false}
				className={cn(
					"flex max-h-[92dvh] flex-col gap-0 overflow-hidden bg-background p-0 text-base",
					"max-sm:top-auto max-sm:bottom-0 max-sm:left-0 max-sm:max-w-full max-sm:translate-x-0 max-sm:translate-y-0 max-sm:rounded-t-2xl max-sm:rounded-b-none",
					"max-sm:data-closed:slide-out-to-bottom max-sm:data-open:slide-in-from-bottom max-sm:data-open:zoom-in-100 max-sm:data-closed:zoom-out-100",
					"sm:max-w-md sm:rounded-2xl",
				)}
			>
				<div
					className="flex shrink-0 justify-center pt-2 sm:hidden"
					aria-hidden="true"
				>
					<span className="h-1 w-9 rounded-full bg-chevron" />
				</div>
				<DialogTitle className="sr-only">{title}</DialogTitle>
				<DialogDescription className="sr-only">
					{description ?? title}
				</DialogDescription>
				<div className="flex min-h-0 flex-1 flex-col overflow-y-auto px-4 pb-[max(1rem,env(safe-area-inset-bottom))]">
					{children}
				</div>
			</DialogContent>
		</Dialog>
	);
}

export function SheetBar({
	left,
	right,
	title,
}: {
	left?: ReactNode;
	right?: ReactNode;
	title?: string;
}) {
	return (
		<div className="flex min-h-11 shrink-0 items-center justify-between gap-2 py-1">
			<span className="min-w-16 text-[15px] text-primary">{left}</span>
			<span className="truncate font-semibold text-[17px] text-foreground">
				{title}
			</span>
			<span className="min-w-16 text-right font-semibold text-[15px] text-primary">
				{right}
			</span>
		</div>
	);
}
