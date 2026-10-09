import tailwindcss from "@tailwindcss/vite";
import { tanstackRouter } from "@tanstack/router-plugin/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// The box page is built into the device package and served by the box on port 80. In
// development, Vite proxies the box API to a running box (real or simulated).
const target = process.env.PORTAL_PROXY_TARGET ?? "http://127.0.0.1:8088";

export default defineConfig({
	server: {
		port: 3002,
		proxy: {
			"/setup": { target, changeOrigin: false },
			"/app": { target, changeOrigin: false },
		},
	},
	resolve: {
		tsconfigPaths: true,
	},
	build: {
		outDir: "../device/src/openlolo/web/portal",
		emptyOutDir: true,
		assetsInlineLimit: 0,
		sourcemap: false,
	},
	plugins: [
		tailwindcss(),
		tanstackRouter({
			target: "react",
			autoCodeSplitting: true,
		}),
		react(),
	],
	test: {
		environment: "node",
		include: ["src/**/*.test.ts"],
	},
});
