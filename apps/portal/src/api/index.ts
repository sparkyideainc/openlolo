// Picks the live box or the demo box once. The demo box fills every screen with seed data and
// sends nothing to a box. It exists only in development (`vite dev`), where it is the default:
// `?demo=0` (or VITE_DEMO=0) talks to the box behind the dev proxy instead, and `?screen=<name>`
// opens a seeded screen. Production builds drop the demo code and always use the live box.

import type { BoxApi } from "@/api/client";
import { liveApi } from "@/api/client";
import { createDemoApi, type DemoScreen } from "@/api/demo";

const params = new URLSearchParams(window.location.search);

export const DEMO =
	import.meta.env.DEV &&
	(params.get("demo") ?? import.meta.env.VITE_DEMO ?? "1") !== "0";
export const DEMO_SCREEN = (params.get("screen") as DemoScreen | null) ?? null;

export const api: BoxApi = DEMO ? createDemoApi(DEMO_SCREEN) : liveApi;

export { ApiError, isApiError, OfflineError } from "@/api/client";
