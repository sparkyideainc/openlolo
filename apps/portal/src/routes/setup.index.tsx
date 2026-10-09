// Welcome: the app's first screen, one task per screen.

import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { Cable } from "lucide-react";

import { useSetupStatus } from "@/api/queries";
import { BusyButton, Mascot } from "@/components/primitives";

export const Route = createFileRoute("/setup/")({
	component: Welcome,
});

function Welcome() {
	const navigate = useNavigate();
	const status = useSetupStatus(5000);
	const onSetupAp = status.data?.access.setup_ap ?? false;
	return (
		<div className="flex flex-1 flex-col">
			<div className="flex flex-1 flex-col items-center justify-center gap-5 text-center">
				<Mascot size={168} radius={40} />
				<h1 className="font-bold font-heading text-[2.5rem] text-foreground leading-none">
					Let's set up your box
				</h1>
				<p className="max-w-[300px] text-base text-muted-foreground">
					You need your iPhone and its USB cable. About three minutes.
				</p>
			</div>
			<div className="flex flex-col gap-3 pt-6 text-center">
				<BusyButton
					icon={<Cable className="size-5" />}
					onClick={() => void navigate({ to: "/setup/steps" })}
				>
					Start setup
				</BusyButton>
				{onSetupAp ? (
					<p className="text-[13px] text-muted-foreground">
						You're on the box's temporary Wi-Fi. That's expected.
					</p>
				) : null}
			</div>
		</div>
	);
}
