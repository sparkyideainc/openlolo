// Auto sleep: how long a PiSugar3 box on battery stays on with no phone and nothing using it.
// The box turns itself off (a clean shutdown, then the PiSugar cuts power); the power button
// turns it back on.

import { Button } from "@openlolo/ui/components/button";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { createFileRoute } from "@tanstack/react-router";
import { BatteryMedium, Moon, Plug } from "lucide-react";
import { toast } from "sonner";

import { api } from "@/api";
import { keys, usePower } from "@/api/queries";
import type { PowerStatus } from "@/api/types";
import { ButtonRow, Page, PageHeader, Row, Section } from "@/components/list";
import { IconBadge } from "@/components/primitives";
import { sleepLabel } from "@/lib/power";

export const Route = createFileRoute("/_shell/settings/power")({
	component: AutoSleep,
});

const FALLBACK_CHOICES = [0, 30, 60, 120, 180, 240, 300];

function AutoSleep() {
	const queryClient = useQueryClient();
	const power = usePower(2000);
	const data = power.data;
	const update = (next: PowerStatus) =>
		queryClient.setQueryData(keys.power, next);
	const choose = useMutation({
		mutationFn: (seconds: number) => api.setAutoSleep(seconds),
		onSuccess: update,
		onError: (error) => toast.error(error.message),
	});
	const keepOn = useMutation({
		mutationFn: () => api.keepOn(),
		onSuccess: update,
		onError: (error) => toast.error(error.message),
	});
	const supported = data?.supported ?? false;
	const choices = data?.choices ?? FALLBACK_CHOICES;

	return (
		<Page>
			<PageHeader title="Auto sleep" back="/settings" inline />
			{data && !supported ? (
				<Section>
					<Row
						minHeight={60}
						title="No battery board found"
						subtitle="Needs a PiSugar3. Stays on."
						leading={<IconBadge icon={<Plug />} fill="grey" />}
					/>
				</Section>
			) : null}
			{data?.state === "warning" ? (
				<section
					aria-live="polite"
					className="flex flex-col gap-3 rounded-2xl bg-warn-tint p-4"
				>
					<div className="flex items-center gap-3">
						<IconBadge icon={<Moon />} fill="warn" />
						<span className="flex min-w-0 flex-1 flex-col">
							<span className="font-semibold text-base text-foreground">
								Turning off in {data.seconds_left ?? 0} s
							</span>
							<span className="text-[13px] text-muted-foreground leading-snug">
								Idle for {sleepLabel(data.auto_sleep_seconds)}
							</span>
						</span>
					</div>
					<Button
						size="lg"
						disabled={keepOn.isPending}
						onClick={() => keepOn.mutate()}
					>
						Keep on
					</Button>
				</section>
			) : data && supported ? (
				<Section>
					<Row
						minHeight={60}
						title={statusTitle(data)}
						subtitle={statusDetail(data)}
						leading={
							data.on_battery ? (
								<IconBadge icon={<BatteryMedium />} fill="good" />
							) : (
								<IconBadge icon={<Plug />} fill="info" />
							)
						}
					/>
				</Section>
			) : null}
			{data ? (
				<Section
					title="Turn off after idle"
					footer={
						supported ? "Press the power button to turn it back on." : undefined
					}
				>
					{choices.map((seconds) => (
						<ButtonRow
							key={seconds}
							title={sleepLabel(seconds)}
							accessory={
								supported && data?.auto_sleep_seconds === seconds
									? "check"
									: "none"
							}
							disabled={!supported || choose.isPending}
							onClick={() => choose.mutate(seconds)}
						/>
					))}
				</Section>
			) : null}
		</Page>
	);
}

function statusTitle(power: PowerStatus): string {
	if (!power.on_battery) return "On USB power";
	return power.battery_percent === null
		? "On battery"
		: `On battery · ${power.battery_percent}%`;
}

function statusDetail(power: PowerStatus): string {
	switch (power.state) {
		case "off":
			return "Auto sleep is off";
		case "plugged_in":
			return "Stays on while plugged in";
		case "shutting_down":
			return "Turning off";
		default:
			return `Turns off after idle for ${sleepLabel(power.auto_sleep_seconds)}`;
	}
}
