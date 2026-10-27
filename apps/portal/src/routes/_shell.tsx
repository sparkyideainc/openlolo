// The tabbed owner surface: Home, Access, Settings (and the screens pushed from them).

import {
	createFileRoute,
	Outlet,
	useLocation,
	useNavigate,
} from "@tanstack/react-router";
import { useEffect, useState } from "react";
import { useConsents, useUpdate } from "@/api/queries";
import { InstallSheet, InstallSheetContext } from "@/components/install-sheet";
import { LocalModeBanner } from "@/components/local-mode-banner";
import { Shell } from "@/components/shell";
import { UpdateDialog } from "@/components/update-dialog";
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
	const update = useUpdate();
	const navigate = useNavigate();
	const onUpdatePage = useLocation({
		select: (location) => location.pathname === "/settings/update",
	});
	const updating = update.data?.attempt?.result === "running";
	// A page opened mid-update goes straight to the progress screen; the tabs stay locked there.
	useEffect(() => {
		if (updating && !onUpdatePage) void navigate({ to: "/settings/update" });
	}, [updating, onUpdatePage, navigate]);
	// Safari on the iPhone gets the "Add to Home Screen" sheet on every visit; the Home Screen app never.
	const [install, setInstall] = useState(() => needsInstall());
	return (
		<InstallSheetContext.Provider value={() => setInstall(true)}>
			<Shell
				badge={consents.data?.length ?? 0}
				settingsDot={update.data?.available === true && !updating}
				locked={updating}
			>
				<LocalModeBanner />
				<Outlet />
				<UpdateDialog
					view={update.data}
					suppressed={onUpdatePage || updating}
				/>
				<InstallSheet
					open={install}
					hostname={hostname}
					onOpenChange={setInstall}
				/>
			</Shell>
		</InstallSheetContext.Provider>
	);
}
