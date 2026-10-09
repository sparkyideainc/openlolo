// "Add to Home Screen" sheet: shown on every Safari visit on the iPhone, never in the Home
// Screen app. iOS has no install API, so the sheet shows the taps and gets out of the way. Steps
// follow Apple's iPhone User Guide for iOS 27, "Add a website icon to your Home Screen":
// https://support.apple.com/guide/iphone/bookmark-a-website-iph42ab2f3a7/ios

import { Menu, Share, SquarePlus } from "lucide-react";
import { createContext, type ReactNode, useContext } from "react";

import { Sheet, SheetBar } from "@/components/dialogs";
import { BusyButton, Mascot } from "@/components/primitives";
import { isSafari } from "@/lib/platform";

/** Opens the layout's sheet from any screen (the Finish step's "Add to Home Screen"). */
export const InstallSheetContext = createContext<() => void>(() => {});

export function useInstallSheet(): () => void {
	return useContext(InstallSheetContext);
}

/** The page's icon set (lucide), inline in the sentence the way the iPhone User Guide shows them. */
function Glyph({ kind }: { kind: "menu" | "share" | "add" }) {
	const className = "mx-0.5 inline size-[1.15em] align-[-0.2em]";
	switch (kind) {
		case "menu":
			return <Menu className={className} aria-hidden="true" />;
		case "share":
			return <Share className={className} aria-hidden="true" />;
		default:
			return <SquarePlus className={className} aria-hidden="true" />;
	}
}

function Step({
	n,
	title,
	detail,
}: {
	n: number;
	title: ReactNode;
	detail: ReactNode;
}) {
	return (
		<li className="flex items-start gap-2.5 px-4 py-3">
			<span className="w-5 shrink-0 text-right font-semibold text-base text-foreground tabular-nums">
				{n}.
			</span>
			<span className="flex min-w-0 flex-1 flex-col gap-0.5">
				<span className="font-semibold text-base text-foreground">{title}</span>
				<span className="text-[13px] text-muted-foreground">{detail}</span>
			</span>
		</li>
	);
}

export function InstallSheet({
	open,
	onOpenChange,
	hostname,
}: {
	open: boolean;
	onOpenChange: (open: boolean) => void;
	hostname: string;
}) {
	const safari = isSafari();
	return (
		<Sheet
			open={open}
			onOpenChange={onOpenChange}
			title="Add OpenLolo to your Home Screen"
		>
			<SheetBar
				right={
					<button type="button" onClick={() => onOpenChange(false)}>
						Done
					</button>
				}
			/>
			<div className="flex flex-col gap-5 pt-1 pb-2">
				<div className="flex flex-col items-center gap-3 text-center">
					<Mascot size={72} radius={18} />
					<div className="flex flex-col gap-1">
						<h2 className="font-bold text-[22px] text-foreground">
							Add OpenLolo to your Home Screen
						</h2>
						<p className="text-base text-muted-foreground">
							{safari
								? "Opens like an app, full screen, one tap away."
								: `Open http://${hostname} in Safari, then add it to your Home Screen from there.`}
						</p>
					</div>
				</div>
				{safari ? (
					<ol className="flex flex-col divide-y divide-border overflow-hidden rounded-2xl bg-card">
						<Step
							n={1}
							title={
								<>
									Tap <Glyph kind="menu" />, then tap Share.
								</>
							}
							detail={
								<>
									If your Tabs layout is Bottom or Top, tap{" "}
									<Glyph kind="share" />.
								</>
							}
						/>
						<Step
							n={2}
							title="Scroll down the list of options, then tap Add to Home Screen."
							detail={
								<>
									Don't see it? Scroll to the bottom, tap Edit Actions, then tap{" "}
									<Glyph kind="add" /> Add to Home Screen.
								</>
							}
						/>
						<Step
							n={3}
							title="Tap Add."
							detail="Choose Open as Web App if offered. The icon appears only on this iPhone."
						/>
					</ol>
				) : null}
				<BusyButton look="plain" onClick={() => onOpenChange(false)}>
					Maybe later
				</BusyButton>
			</div>
		</Sheet>
	);
}
