// Route guards. Every owner screen needs a session; on the setup access point the box issues
// one silently (cable + setup Wi-Fi, or cable + the paired phone itself); nobody types anything.

import type { QueryClient } from "@tanstack/react-query";
import { redirect } from "@tanstack/react-router";

import { api, OfflineError } from "@/api";
import { keys } from "@/api/queries";
import type { SetupStatus } from "@/api/types";

export async function bootstrap(
	queryClient: QueryClient,
): Promise<SetupStatus> {
	return queryClient.fetchQuery({
		queryKey: keys.setupStatus,
		queryFn: () => api.setupStatus(),
		staleTime: 2000,
		retry: false,
	});
}

/** Returns the bootstrap status once a session exists; a browser the box would admit (the
 * paired phone on its cable, or the setup Wi-Fi with a phone on the cable) gets one silently;
 * anyone else goes to the connect screen. */
export async function ensureSession(
	queryClient: QueryClient,
	next: string,
): Promise<SetupStatus> {
	let status: SetupStatus;
	try {
		status = await bootstrap(queryClient);
	} catch (error) {
		if (error instanceof OfflineError)
			throw redirect({ to: "/offline", search: { next } });
		throw error;
	}
	if (status.session) return status;
	if (status.access.allowed) {
		await api.openSession();
		await queryClient.invalidateQueries({ queryKey: keys.setupStatus });
		return { ...status, session: true };
	}
	throw redirect({ to: "/connect", search: { next }, replace: true });
}
