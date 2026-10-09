// The tabbed owner surface: Home, Access, Settings (and the screens pushed from them).

import { createFileRoute, Outlet } from "@tanstack/react-router";
import { useState } from "react";
import { useConsents } from "@/api/queries";
import { InstallSheet, InstallSheetContext } from "@/components/install-sheet";
import { Shell } from "@/components/shell";
import { needsInstall } from "@/lib/platform";
import { ensureSession } from "@/lib/session";

export const Route = createFileRoute("/_shell")({
	beforeLoad: async ({ context, location }) => {
		const status = await ensureSession(context.queryClient, location.pathname);
		return { hostname: status.hostname, device: status.device };
	},
	component: ShellRoute,
});

function ShellRoute() {
	const { hostname } = Route.useRouteContext();
	const consents = useConsents(5000);
	// Safari on the iPhone gets the "Add to Home Screen" sheet on every visit; the Home Screen app never.
	const [install, setInstall] = useState(() => needsInstall());
	return (
		<InstallSheetContext.Provider value={() => setInstall(true)}>
			<Shell badge={consents.data?.length ?? 0}>
				<Outlet />
				<InstallSheet
					open={install}
					hostname={hostname}
					onOpenChange={setInstall}
				/>
			</Shell>
		</InstallSheetContext.Provider>
	);
}
