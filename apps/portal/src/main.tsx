import {
	QueryCache,
	QueryClient,
	QueryClientProvider,
} from "@tanstack/react-query";
import { createRouter, RouterProvider } from "@tanstack/react-router";
import ReactDOM from "react-dom/client";

import { isApiError } from "@/api";
import { Splash } from "@/components/splash";
import { installViewportGap } from "@/lib/viewport-gap";
import { routeTree } from "./routeTree.gen";

import "./index.css";

// The app follows the system appearance; the design tokens live under `.dark` (globals.css).
const scheme = window.matchMedia("(prefers-color-scheme: dark)");
function applyScheme() {
	document.documentElement.classList.toggle("dark", scheme.matches);
}
applyScheme();
scheme.addEventListener("change", applyScheme);
installViewportGap();

const queryClient = new QueryClient({
	queryCache: new QueryCache({
		onError: (error) => {
			// The box restarted or the session expired: go back through the connect screen.
			if (
				isApiError(error) &&
				error.status === 401 &&
				!location.pathname.startsWith("/connect")
			) {
				void router.navigate({
					to: "/connect",
					search: { next: location.pathname },
					replace: true,
				});
			}
		},
	}),
	defaultOptions: {
		queries: { retry: 1, refetchOnWindowFocus: true, staleTime: 1000 },
	},
});

const router = createRouter({
	routeTree,
	defaultPreload: "intent",
	scrollRestoration: true,
	defaultPendingComponent: () => <Splash status="Reaching the box…" />,
	// The static launch screen in index.html is already on screen; keep showing it, not a blank.
	defaultPendingMs: 0,
	defaultPendingMinMs: 300,
	context: { queryClient },
});

declare module "@tanstack/react-router" {
	interface Register {
		router: typeof router;
	}
}

const rootElement = document.getElementById("app");
if (!rootElement) throw new Error("Root element not found");

ReactDOM.createRoot(rootElement).render(
	<QueryClientProvider client={queryClient}>
		<RouterProvider router={router} />
	</QueryClientProvider>,
);
