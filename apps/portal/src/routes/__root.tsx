import { Toaster } from "@openlolo/ui/components/sonner";
import type { QueryClient } from "@tanstack/react-query";
import {
	createRootRouteWithContext,
	Link,
	Outlet,
	useRouter,
} from "@tanstack/react-router";

import { BusyButton } from "@/components/primitives";
import { Plain } from "@/components/shell";

export interface RouterContext {
	queryClient: QueryClient;
}

export const Route = createRootRouteWithContext<RouterContext>()({
	component: RootComponent,
	notFoundComponent: NotFound,
	errorComponent: ErrorScreen,
});

function RootComponent() {
	return (
		<>
			<Outlet />
			<Toaster richColors position="top-center" />
		</>
	);
}

function NotFound() {
	return (
		<Plain className="items-center justify-center text-center">
			<h1 className="font-bold font-heading text-[2rem] text-foreground">
				Page not found
			</h1>
			<p className="mt-2 text-base text-muted-foreground">
				This address is not part of the box page.
			</p>
			<Link to="/" className="mt-6 font-semibold text-[15px] text-primary">
				Go home
			</Link>
		</Plain>
	);
}

function ErrorScreen({ error, reset }: { error: unknown; reset: () => void }) {
	console.error(error);
	const router = useRouter();
	return (
		<Plain className="items-center justify-center text-center">
			<h1 className="font-bold font-heading text-[1.5rem] text-foreground">
				This page didn't load
			</h1>
			<p className="mt-2 text-base text-muted-foreground">
				{error instanceof Error ? error.message : "Something went wrong."}
			</p>
			<div className="mt-6 w-full max-w-xs">
				<BusyButton
					onClick={() => {
						router.invalidate();
						reset();
					}}
				>
					Try again
				</BusyButton>
			</div>
		</Plain>
	);
}
