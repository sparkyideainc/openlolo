// The guided setup, one page: the box reports cable, trust, Developer Mode and Wi-Fi every few
// seconds and the card advances by itself. Nothing to tap until the Wi-Fi step; the faded
// primary button at the bottom is the one "waiting" signal (ADR 0011 amendment, design A).

import { keepPreviousData } from "@tanstack/react-query";
import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { Loader2, RefreshCw, Smartphone } from "lucide-react";
import { useState } from "react";

import {
	useAppStatus,
	useNetwork,
	useSetupStatus,
	useWifiScan,
} from "@/api/queries";
import type { WifiNetwork } from "@/api/types";
import { useInstallSheet } from "@/components/install-sheet";
import { BusyButton, Mascot } from "@/components/primitives";
import { TrackBar } from "@/components/track-bar";
import {
	HiddenDialog,
	JoinDialog,
	NetworkList,
	useJoinWifi,
} from "@/components/wifi";
import { type FlowView, setupFlow } from "@/lib/setup-flow";
import { setOnboardingComplete } from "@/lib/store";
import { currentSsid } from "@/lib/wifi";

export const Route = createFileRoute("/setup/steps")({
	component: SetupSteps,
});

function SetupSteps() {
	const { hostname } = Route.useRouteContext();
	const setup = useSetupStatus(3000, { placeholderData: keepPreviousData });
	const app = useAppStatus(5000, {
		placeholderData: keepPreviousData,
		enabled: Boolean(setup.data?.bound),
	});
	const [joiningSsid, setJoiningSsid] = useState<string | null>(null);
	const view = setupFlow({
		setup: setup.data,
		app: app.data,
		unreachable: setup.isError,
		joiningSsid,
	});
	return (
		<div className="flex flex-1 flex-col gap-4">
			<header className="flex items-center gap-3">
				<Mascot size={44} radius={12} />
				<div className="flex flex-col gap-0.5">
					<h1 className="font-bold font-heading text-[22px] text-foreground leading-tight">
						Setting up
					</h1>
					<p className="text-[13px] text-muted-foreground">
						{view.step === "finish"
							? "All four steps done"
							: `Step ${view.stepNumber} of 4`}
					</p>
				</div>
			</header>
			<TrackBar states={view.track} />
			{view.step === "finish" ? (
				<Finish view={view} hostname={hostname} />
			) : view.step === "wifi" && !view.switchingTo ? (
				<WifiStep view={view} onJoinStarted={setJoiningSsid} />
			) : (
				<StepCard view={view} hostname={hostname} />
			)}
		</div>
	);
}

function Card({ children }: { children: React.ReactNode }) {
	return (
		<section className="flex flex-col gap-3.5 rounded-[20px] bg-card p-[18px]">
			{children}
		</section>
	);
}

function Headline({ view }: { view: FlowView }) {
	return (
		<>
			<h2 className="font-bold font-heading text-[24px] text-foreground leading-tight">
				{view.headline}
			</h2>
			<p className="text-base text-muted-foreground">{view.description}</p>
		</>
	);
}

/** The faded primary at the bottom: the one thing the page is waiting for. */
function WaitingButton({ view }: { view: FlowView }) {
	if (view.error) {
		return (
			<p className="mt-auto rounded-2xl bg-destructive-tint px-4 py-3 text-[15px] text-destructive">
				{view.error}
			</p>
		);
	}
	if (!view.waiting) return null;
	return (
		<BusyButton
			disabled
			aria-disabled="true"
			className="mt-auto opacity-55 disabled:opacity-55"
			icon={<Loader2 className="size-5 animate-spin" />}
		>
			{view.waiting}
		</BusyButton>
	);
}

function StepCard({ view, hostname }: { view: FlowView; hostname: string }) {
	return (
		<>
			<Card>
				<Headline view={view} />
				{view.step === "cable" ? <CableArt /> : null}
				{view.step === "trust" && view.track[1] === "now" ? (
					<TrustPrompt />
				) : null}
				{view.step === "devmode" &&
				view.track[2] === "now" &&
				view.waiting === "Waiting for Developer Mode" ? (
					<DeveloperModeSteps />
				) : null}
				{view.switchingTo ? (
					<SwitchingSteps ssid={view.switchingTo} hostname={hostname} />
				) : null}
			</Card>
			{view.switchingTo ? (
				<a
					href={`http://${hostname}/setup/steps`}
					className="mt-auto flex h-12 items-center justify-center rounded-xl border border-border bg-card font-semibold text-[15px] text-primary"
				>
					Open {hostname} now
				</a>
			) : (
				<WaitingButton view={view} />
			)}
		</>
	);
}

function CableArt() {
	return (
		<div className="flex items-center justify-center gap-3.5 py-2 text-foreground">
			<Smartphone className="size-11" strokeWidth={1.2} />
			<svg
				width="74"
				height="14"
				viewBox="0 0 74 14"
				fill="none"
				stroke="currentColor"
				strokeWidth="2"
				strokeLinecap="round"
				className="text-primary"
				aria-hidden="true"
			>
				<line x1="2" y1="7" x2="28" y2="7" />
				<rect x="28" y="1" width="18" height="12" rx="3" />
				<line x1="46" y1="7" x2="72" y2="7" />
			</svg>
			<span className="flex size-9 items-center justify-center rounded-lg border-[1.5px] border-foreground font-bold text-[11px]">
				O
			</span>
		</div>
	);
}

/** What iOS shows, drawn so the owner recognises it. */
function TrustPrompt() {
	return (
		<div className="flex flex-col items-center gap-2 rounded-2xl border-[1.5px] border-border p-3.5 text-center">
			<p className="font-semibold text-[15px] text-foreground">
				Trust This Computer?
			</p>
			<p className="max-w-[240px] text-[12.5px] text-muted-foreground">
				Your settings and data will be accessible from this computer when
				connected.
			</p>
			<div className="mt-1 flex w-full gap-2">
				<span className="flex-1 rounded-[10px] border-[1.5px] border-border py-2 text-[14px] text-muted-foreground">
					Don't Trust
				</span>
				<span className="flex-1 rounded-[10px] border-[1.5px] border-primary py-2 font-semibold text-[14px] text-primary">
					Trust
				</span>
			</div>
			<p className="text-[11.5px] text-muted-foreground">
				What the iPhone shows
			</p>
		</div>
	);
}

function Numbered({ items }: { items: React.ReactNode[] }) {
	return (
		<ol className="flex flex-col gap-2 text-[14px] text-foreground">
			{items.map((item, i) => (
				<li key={i} className="flex items-start gap-2.5">
					<span className="flex size-[22px] shrink-0 items-center justify-center rounded-full bg-accent font-bold text-[11px] text-primary">
						{i + 1}
					</span>
					<span>{item}</span>
				</li>
			))}
		</ol>
	);
}

function DeveloperModeSteps() {
	return (
		<Numbered
			items={[
				<strong key="path">
					Settings › Privacy &amp; Security › Developer Mode
				</strong>,
				<span key="on">
					Turn it on, tap <strong>Restart</strong>
				</span>,
				<span key="after">
					After the restart: unlock, tap <strong>Turn On</strong>
				</span>,
			]}
		/>
	);
}

function SwitchingSteps({
	ssid,
	hostname,
}: {
	ssid: string;
	hostname: string;
}) {
	return (
		<>
			<Numbered
				items={[
					<span key="join">
						On this iPhone, join <strong>{ssid}</strong> if it has not switched
						by itself. The setup Wi-Fi is gone now, that is expected.
					</span>,
					<span key="reopen">
						Keep this page open. It reopens <strong>{hostname}</strong> on its
						own once the box answers there.
					</span>,
				]}
			/>
			<p className="flex items-center gap-2.5 font-semibold text-[15px] text-foreground">
				<Loader2 className="size-4 animate-spin text-primary" /> Looking for the
				box on {ssid}
			</p>
		</>
	);
}

function WifiStep({
	view,
	onJoinStarted,
}: {
	view: FlowView;
	onJoinStarted: (ssid: string) => void;
}) {
	const network = useNetwork(5000);
	const scan = useWifiScan();
	const [picked, setPicked] = useState<WifiNetwork | null>(null);
	const [manual, setManual] = useState<"hidden" | "hotspot" | null>(null);
	const join = useJoinWifi(onJoinStarted);
	const current = currentSsid(network.data);
	const busy = join.isPending || Boolean(view.waiting);
	return (
		<>
			<Card>
				<Headline view={view} />
				<div className="flex items-center justify-between gap-3">
					<span className="font-semibold text-[12px] text-muted-foreground uppercase tracking-[0.06em]">
						Networks nearby
					</span>
					<button
						type="button"
						onClick={() => void scan.refetch()}
						disabled={scan.isFetching}
						className="flex min-h-8 items-center gap-1.5 rounded-full bg-accent px-3 py-1.5 font-semibold text-[14px] text-primary disabled:opacity-60"
					>
						{scan.isFetching ? (
							<Loader2 className="size-[15px] animate-spin" />
						) : (
							<RefreshCw className="size-[15px]" strokeWidth={2.2} />
						)}
						{scan.isFetching ? "Scanning…" : "Scan again"}
					</button>
				</div>
				<NetworkList
					networks={scan.data ?? []}
					scanning={false}
					currentSsid={current}
					onPick={(n) => !busy && setPicked(n)}
					onHidden={() => setManual("hidden")}
				/>
			</Card>
			{view.waiting || view.error ? (
				<WaitingButton view={view} />
			) : (
				<div className="mt-auto flex flex-col gap-2.5">
					<BusyButton onClick={() => setManual("hidden")}>
						Join a hidden network
					</BusyButton>
					<BusyButton look="plain" onClick={() => setManual("hotspot")}>
						No Wi-Fi? Use hotspot instead
					</BusyButton>
				</div>
			)}
			<JoinDialog
				network={picked}
				onClose={() => setPicked(null)}
				joining={join.isPending}
				onJoin={(ssid, password) => {
					setPicked(null);
					join.mutate({ ssid, password });
				}}
			/>
			<HiddenDialog
				open={manual !== null}
				onClose={() => setManual(null)}
				onJoin={(ssid, password) => {
					setManual(null);
					join.mutate({ ssid, password });
				}}
			/>
		</>
	);
}

function Finish({ view, hostname }: { view: FlowView; hostname: string }) {
	const navigate = useNavigate();
	const openInstall = useInstallSheet();
	const network = useNetwork(false);
	const ssid = currentSsid(network.data);
	return (
		<>
			<Card>
				<div className="flex flex-col items-center gap-3 pt-1.5 text-center">
					<Mascot size={120} radius={30} />
					<h2 className="font-bold font-heading text-[28px] text-foreground leading-tight">
						{view.headline}
					</h2>
					<p className="max-w-[300px] text-base text-muted-foreground">
						{view.description}
					</p>
				</div>
				<div className="mt-1 grid grid-cols-2 gap-2.5">
					<div className="flex flex-col gap-0.5 rounded-2xl bg-good-tint p-3 text-good">
						<span className="font-semibold text-[12px] uppercase tracking-[0.06em]">
							Wi-Fi
						</span>
						<span className="font-semibold">
							{ssid ? `Connected to ${ssid}` : `Online at ${hostname}`}
						</span>
					</div>
					<div className="flex flex-col gap-0.5 rounded-2xl bg-good-tint p-3 text-good">
						<span className="font-semibold text-[12px] uppercase tracking-[0.06em]">
							iPhone
						</span>
						<span className="font-semibold">Paired with your iPhone</span>
					</div>
				</div>
			</Card>
			<div className="mt-auto flex flex-col gap-2.5">
				<BusyButton
					onClick={() => {
						setOnboardingComplete(true);
						void navigate({ to: "/home", replace: true });
					}}
				>
					Enjoy with Lolo!
				</BusyButton>
				<BusyButton look="plain" onClick={openInstall}>
					Add to Home Screen
				</BusyButton>
			</div>
		</>
	);
}
