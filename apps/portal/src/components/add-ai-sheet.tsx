// "Add AI": the box's remote MCP address to paste into an AI app, or the way to get one.

import { Button, buttonVariants } from "@openlolo/ui/components/button";
import { cn } from "@openlolo/ui/lib/utils";
import { Check, Copy, Globe } from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";

import { Sheet } from "@/components/dialogs";

const TAILSCALE_GUIDE =
	"https://openlolo.com/docs/remote-access/set-up-tailscale";

const STEPS = [
	"In Claude or ChatGPT, open connector settings and add a custom connector.",
	"Paste the address and connect.",
	"Approve the request that appears here on Access.",
];

/** navigator.clipboard needs a secure context; the box page is plain http on the LAN. */
async function copyText(value: string) {
	if (navigator.clipboard && window.isSecureContext) {
		await navigator.clipboard.writeText(value);
		return;
	}
	const field = document.createElement("textarea");
	field.value = value;
	field.setAttribute("readonly", "");
	field.style.position = "fixed";
	field.style.opacity = "0";
	document.body.appendChild(field);
	field.select();
	field.setSelectionRange(0, value.length);
	const ok = document.execCommand("copy");
	field.remove();
	if (!ok) throw new Error("copy failed");
}

export function AddAiSheet({
	mcpUrl,
	open,
	onOpenChange,
}: {
	mcpUrl: string | null | undefined;
	open: boolean;
	onOpenChange: (open: boolean) => void;
}) {
	const [copied, setCopied] = useState(false);
	useEffect(() => {
		if (open) setCopied(false);
	}, [open]);
	return (
		<Sheet open={open} onOpenChange={onOpenChange} title="Add AI">
			{mcpUrl ? (
				<div className="flex flex-col gap-5 pt-3 pb-2">
					<div className="flex flex-col gap-1.5 px-1">
						<h2 className="font-heading font-semibold text-[20px] text-foreground">
							Add AI
						</h2>
						<p className="text-[15px] text-muted-foreground leading-snug">
							Add this address to your AI app as a custom connector (remote MCP
							server).
						</p>
					</div>
					<div className="flex flex-col divide-y divide-border overflow-hidden rounded-2xl bg-card">
						<div className="flex flex-col gap-1 px-4 py-3.5">
							<span className="text-[13px] text-muted-foreground">
								MCP address
							</span>
							<span className="select-all break-all font-mono text-[15px] text-foreground">
								{mcpUrl}
							</span>
						</div>
						<Button
							variant="ghost"
							onClick={() => {
								copyText(mcpUrl).then(
									() => setCopied(true),
									() =>
										toast.error("Could not copy. Select the address instead."),
								);
							}}
							className={cn(
								"h-12 w-full gap-2 rounded-none font-semibold text-base",
								copied
									? "text-good hover:text-good"
									: "text-primary hover:text-primary",
							)}
						>
							{copied ? (
								<Check className="size-[18px]" strokeWidth={2.4} />
							) : (
								<Copy className="size-[18px]" />
							)}
							{copied ? "Copied" : "Copy address"}
						</Button>
					</div>
					<ol className="flex flex-col gap-3 px-1">
						{STEPS.map((step, index) => (
							<li key={step} className="flex items-start gap-3">
								<span className="flex size-6 shrink-0 items-center justify-center rounded-full bg-accent font-semibold text-[13px] text-accent-foreground">
									{index + 1}
								</span>
								<span className="text-[15px] text-foreground leading-snug">
									{step}
								</span>
							</li>
						))}
					</ol>
					<Button
						className="h-11 rounded-xl font-semibold text-[15px]"
						onClick={() => onOpenChange(false)}
					>
						Done
					</Button>
				</div>
			) : (
				<div className="flex flex-col gap-5 pt-5 pb-2">
					<div className="flex justify-center">
						<span className="flex size-14 items-center justify-center rounded-full bg-warn-tint text-warn">
							<Globe className="size-7" strokeWidth={1.8} />
						</span>
					</div>
					<div className="flex flex-col gap-1.5 px-1 text-center">
						<h2 className="font-heading font-semibold text-[20px] text-foreground">
							Set up remote access first
						</h2>
						<p className="text-[15px] text-muted-foreground leading-snug">
							Claude and ChatGPT reach your box over the internet. Turn on
							Tailscale on the box to get its MCP address, then come back here.
						</p>
					</div>
					<div className="flex flex-col gap-2">
						<a
							href={TAILSCALE_GUIDE}
							target="_blank"
							rel="noreferrer"
							className={cn(
								buttonVariants(),
								"h-11 rounded-xl font-semibold text-[15px]",
							)}
						>
							Open the Tailscale guide
						</a>
						<Button
							variant="secondary"
							className="h-11 rounded-xl bg-card font-semibold text-[15px]"
							onClick={() => onOpenChange(false)}
						>
							Close
						</Button>
					</div>
				</div>
			)}
		</Sheet>
	);
}
