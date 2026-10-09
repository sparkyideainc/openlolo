// Picks the live box or the demo box once, from the page URL. `?demo=1` (optionally with
// `?screen=<name>`) fills every screen with seed data and sends nothing to a box.

import type { BoxApi } from "@/api/client";
import { liveApi } from "@/api/client";
import { createDemoApi, type DemoScreen } from "@/api/demo";

const params = new URLSearchParams(window.location.search);

export const DEMO =
	params.get("demo") === "1" || import.meta.env.VITE_DEMO === "1";
export const DEMO_SCREEN = (params.get("screen") as DemoScreen | null) ?? null;

export const api: BoxApi = DEMO ? createDemoApi(DEMO_SCREEN) : liveApi;

export { ApiError, isApiError, OfflineError } from "@/api/client";
