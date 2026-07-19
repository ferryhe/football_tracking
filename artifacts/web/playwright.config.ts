import path from "node:path";
import { defineConfig, devices } from "@playwright/test";

const port = 4173;
const baseURL = `http://127.0.0.1:${port}`;

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 2 : 0,
  // Konva browser journeys allocate canvases and screenshots; cap local parallelism
  // so a full suite does not starve a page before React mounts. CI stays serial.
  workers: process.env.CI ? 1 : 4,
  reporter: process.env.CI
    ? [
        ["github"],
        [
          "html",
          { outputFolder: "../../tmp/playwright-report", open: "never" },
        ],
      ]
    : "list",
  outputDir: path.resolve(import.meta.dirname, "../../tmp/playwright-results"),
  use: {
    baseURL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  webServer: {
    command: "node node_modules/vite/bin/vite.js --config vite.config.ts",
    cwd: import.meta.dirname,
    env: {
      ...process.env,
      FT_FRONTEND_PORT: String(port),
      FT_DEV_HOST: "127.0.0.1",
    },
    url: baseURL,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
});
