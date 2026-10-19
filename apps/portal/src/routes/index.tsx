// Entry: send the owner to the connect screen, the setup flow, or Home.

import { createFileRoute, redirect } from "@tanstack/react-router";

import { api } from "@/api";
import { ensureSession } from "@/lib/session";
import { onboardingComplete, setOnboardingComplete } from "@/lib/store";

export const Route = createFileRoute("/")({
	beforeLoad: async ({ context }) => {
		const setup = await ensureSession(context.queryClient, "/");
		// The box decides: with no phone bound (or in Setup mode) the browser's remembered
		// "onboarding done" flag is stale (the box was reset) and setup opens. Local mode (own
		// Wi-Fi, phone saved) is not setup once onboarding is done.
		if (
			!setup.bound ||
			setup.mode === "setup" ||
			(setup.setup_mode && !onboardingComplete())
		) {
			setOnboardingComplete(false);
			throw redirect({ to: "/setup", replace: true });
		}
		if (onboardingComplete()) throw redirect({ to: "/home", replace: true });
		try {
			const status = await api.status();
			if (status.connection?.state === "connected" && !status.setup_required) {
				setOnboardingComplete(true);
				throw redirect({ to: "/home", replace: true });
			}
		} catch (error) {
			if (error instanceof Error && !("to" in (error as object))) {
				// Could not read the status: let the setup flow explain.
			} else {
				throw error;
			}
		}
		throw redirect({ to: "/setup", replace: true });
	},
	component: () => null,
});
