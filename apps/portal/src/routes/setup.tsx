// Onboarding layout: a session is needed (free on the setup access point), no tab bar. Safari on
// the iPhone gets the "Add to Home Screen" sheet on every visit, before Welcome.

import { createFileRoute, Outlet } from "@tanstack/react-router";
import { useState } from "react";

import { InstallSheet, InstallSheetContext } from "@/components/install-sheet";
import { Plain } from "@/components/shell";
import { needsInstall } from "@/lib/platform";
import { ensureSession } from "@/lib/session";

export const Route = createFileRoute("/setup")({
	beforeLoad: async ({ context, location }) => {
		const status = await ensureSession(context.queryClient, location.pathname);
		return { hostname: status.hostname, device: status.device };
	},
	component: SetupLayout,
});

function SetupLayout() {
	const { hostname } = Route.useRouteContext();
	const [install, setInstall] = useState(() => needsInstall());
	return (
		<InstallSheetContext.Provider value={() => setInstall(true)}>
			<Plain>
				<Outlet />
				<InstallSheet
					open={install}
					hostname={hostname}
					onOpenChange={setInstall}
				/>
			</Plain>
		</InstallSheetContext.Provider>
	);
}
