// TanStack Query bindings. The box pushes nothing over HTTP, so screens poll at the rates the
// design sets; TanStack pauses intervals while the tab is hidden (refetchIntervalInBackground
// is off by default).

import {
	type UseQueryOptions,
	useInfiniteQuery,
	useQuery,
} from "@tanstack/react-query";

import { api } from "@/api";
import type { AppStatus, SetupStatus } from "@/api/types";

export const keys = {
	setupStatus: ["setup", "status"] as const,
	status: ["app", "status"] as const,
	info: ["app", "info"] as const,
	consents: ["app", "consents"] as const,
	clients: ["app", "clients"] as const,
	operations: ["app", "operations"] as const,
	network: ["app", "network"] as const,
	scan: ["app", "scan"] as const,
};

type Interval = number | false;

export function useSetupStatus(
	interval: Interval = 3000,
	options?: Partial<UseQueryOptions<SetupStatus>>,
) {
	return useQuery({
		queryKey: keys.setupStatus,
		queryFn: () => api.setupStatus(),
		refetchInterval: interval,
		retry: false,
		...options,
	});
}

export function useAppStatus(
	interval: Interval = 5000,
	options?: Partial<UseQueryOptions<AppStatus>>,
) {
	return useQuery({
		queryKey: keys.status,
		queryFn: () => api.status(),
		refetchInterval: interval,
		retry: false,
		...options,
	});
}

export function useInfo() {
	return useQuery({
		queryKey: keys.info,
		queryFn: () => api.info(),
		staleTime: 60_000,
	});
}

export function useConsents(interval: Interval = 5000) {
	return useQuery({
		queryKey: keys.consents,
		queryFn: () => api.consents(),
		refetchInterval: interval,
	});
}

export function useClients(interval: Interval = 15_000) {
	return useQuery({
		queryKey: keys.clients,
		queryFn: () => api.clients(),
		refetchInterval: interval,
	});
}

export function useOperations(limit: number, interval: Interval = 10_000) {
	return useInfiniteQuery({
		queryKey: [...keys.operations, limit],
		queryFn: ({ pageParam }) => api.operations(pageParam, limit),
		initialPageParam: null as string | null,
		getNextPageParam: (last) =>
			last.has_more ? (last.next_cursor ?? null) : null,
		refetchInterval: interval,
	});
}

export function useNetwork(interval: Interval = 5000) {
	return useQuery({
		queryKey: keys.network,
		queryFn: () => api.network(),
		refetchInterval: interval,
	});
}

export function useWifiScan(enabled = true) {
	return useQuery({
		queryKey: keys.scan,
		queryFn: () => api.scan(),
		enabled,
		staleTime: 20_000,
		refetchOnWindowFocus: false,
	});
}
