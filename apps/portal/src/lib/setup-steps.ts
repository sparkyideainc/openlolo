// The Setting-up list (ADR 0010). Step 1 is the page itself being reachable.

export type StepState = "done" | "now" | "todo" | "bad";

export interface SetupStep {
	id: number;
	title: string;
	detail: string;
	state: StepState;
}

export interface SetupView {
	headline: string;
	subtitle: string;
	steps: SetupStep[];
}

export interface SetupInput {
	autopair: string;
	connection: string;
	setupRequired: boolean;
	boxName: string;
	hostname: string;
	error: string | null;
}

export function setupSteps({
	autopair,
	connection,
	setupRequired,
	boxName,
	hostname,
	error,
}: SetupInput): SetupView {
	const steps: SetupStep[] = [
		{
			id: 1,
			title: `Reachable at ${hostname}`,
			detail: "This page talks to the box over your Wi-Fi",
			state: "done",
		},
	];
	let headline = "Setting up";
	let subtitle = "The box reports each step as it finishes.";
	if (setupRequired) {
		let trust: SetupStep;
		switch (autopair) {
			case "pairing":
			case "trust_pending":
				trust = {
					id: 2,
					title: "Trust this box",
					detail: "Waiting for Trust on the iPhone",
					state: "now",
				};
				headline = "Tap Trust on the iPhone";
				subtitle = "Unlock it, tap Trust, then enter your passcode.";
				break;
			case "locked":
				trust = {
					id: 2,
					title: "Trust this box",
					detail: "The iPhone is locked",
					state: "now",
				};
				headline = "Unlock the iPhone";
				subtitle = "The Trust prompt appears once it is unlocked.";
				break;
			case "binding":
				trust = {
					id: 2,
					title: "Trust this box",
					detail: "Trusted · binding",
					state: "now",
				};
				headline = "Binding the iPhone";
				subtitle = "One moment.";
				break;
			case "denied":
				trust = {
					id: 2,
					title: "Trust this box",
					detail: "Trust was declined on the iPhone",
					state: "bad",
				};
				headline = "Trust was declined";
				subtitle =
					"Unplug the cable, plug it back in, then tap Trust when the prompt returns.";
				break;
			case "multiple_phones":
				trust = {
					id: 2,
					title: "Trust this box",
					detail: "Two phones are on the cable",
					state: "bad",
				};
				headline = "Two phones on the cable";
				subtitle = "Unplug the one you do not want to use.";
				break;
			case "no_phone":
				trust = {
					id: 2,
					title: "Trust this box",
					detail: "No iPhone on the box's cable",
					state: "now",
				};
				headline = "Plug the iPhone into the box";
				subtitle = "The box pairs the phone on its cable by itself.";
				break;
			case "failed":
			case "off":
				trust = {
					id: 2,
					title: "Trust this box",
					detail: error ?? "The box could not pair the phone",
					state: "bad",
				};
				headline = "The box could not pair the iPhone";
				subtitle = error ?? "Check the cable and try again from Home.";
				break;
			default:
				trust = {
					id: 2,
					title: "Trust this box",
					detail: "Waiting for the box",
					state: "now",
				};
				headline = "Plug the iPhone into the box";
				subtitle = "The box pairs the phone on its cable by itself.";
		}
		steps.push(trust);
		steps.push({
			id: 3,
			title: "Developer Mode",
			detail: "You turn this on in Settings",
			state: "todo",
		});
		steps.push({
			id: 4,
			title: "Developer image",
			detail: "The box installs it",
			state: "todo",
		});
		return { headline, subtitle, steps };
	}
	steps.push({
		id: 2,
		title: "Trusted and bound",
		detail: "USB now, Wi-Fi when unplugged",
		state: "done",
	});
	switch (connection) {
		case "developer_mode_required":
			steps.push({
				id: 3,
				title: "Developer Mode",
				detail:
					"Settings › Privacy & Security › Developer Mode, then confirm after the restart",
				state: "now",
			});
			steps.push({
				id: 4,
				title: "Developer image",
				detail: "Automatic, needs the box online",
				state: "todo",
			});
			headline = "Turn on Developer Mode";
			subtitle =
				"iOS requires you to do this yourself. The iPhone restarts once.";
			break;
		case "ddi_required":
		case "mounting_ddi":
			steps.push({
				id: 3,
				title: "Developer Mode",
				detail: "On",
				state: "done",
			});
			steps.push({
				id: 4,
				title: "Developer image",
				detail: "Installing · keep the iPhone unlocked",
				state: "now",
			});
			headline = "Installing the developer image";
			subtitle = "The box downloads it once; this needs the box online.";
			break;
		case "connected":
			steps.push({
				id: 3,
				title: "Developer Mode",
				detail: "On",
				state: "done",
			});
			steps.push({
				id: 4,
				title: "Developer image",
				detail: "Installed",
				state: "done",
			});
			headline = "Everything is working";
			subtitle = `${boxName} controls the iPhone. Approve an AI client when it asks, or pause from Home.`;
			break;
		case "trust_required":
		case "wifi_pairing_required":
			steps[1] = {
				id: 2,
				title: "Trust this box",
				detail: "The box lost trust; replug the cable",
				state: "bad",
			};
			steps.push({
				id: 3,
				title: "Developer Mode",
				detail: "You turn this on in Settings",
				state: "todo",
			});
			steps.push({
				id: 4,
				title: "Developer image",
				detail: "The box installs it",
				state: "todo",
			});
			headline = "Tap Trust on the iPhone";
			subtitle = "Unlock it, tap Trust, then enter your passcode.";
			break;
		default:
			steps.push({
				id: 3,
				title: "Developer Mode",
				detail: "Checking",
				state: "todo",
			});
			steps.push({
				id: 4,
				title: "Developer image",
				detail: "Checking",
				state: "todo",
			});
			headline = "Connecting to the iPhone";
			subtitle = "Keep it plugged in and unlocked.";
	}
	return { headline, subtitle, steps };
}
