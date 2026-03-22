import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

const runtimeEnv = (
  globalThis as typeof globalThis & {
    process?: {
      env?: Record<string, string | undefined>;
    };
  }
).process?.env ?? {};

const devHost = runtimeEnv.FT_DEV_HOST ?? "127.0.0.1";
const devPort = Number(runtimeEnv.FT_FRONTEND_PORT ?? "5173");
const apiProxyTarget = runtimeEnv.VITE_API_PROXY_TARGET ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    setupFiles: "./src/test/setup.ts",
  },
  server: {
    host: devHost,
    port: devPort,
    strictPort: true,
    proxy: {
      "/api": {
        target: apiProxyTarget,
        changeOrigin: true,
      },
    },
  },
});
