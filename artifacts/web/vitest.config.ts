import path from "node:path";
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(import.meta.dirname, "src"),
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./vitest.setup.ts"],
    include: [
      "src/**/*.vitest.test.{ts,tsx}",
      "src/lib/productionWorkflow.test.ts",
      "src/lib/fieldGeometry.test.ts",
      "src/lib/productionCalibration.test.ts",
      "src/lib/productionTrial.test.ts",
      "src/lib/productionConfigFreeze.test.ts",
      "src/lib/productionBroadcast.test.ts",
      "src/lib/broadcastDelivery.test.ts",
      "src/lib/productionHistory.test.ts",
      "src/components/production/**/*.test.tsx",
      "src/components/history/**/*.test.tsx",
      "src/pages/production.test.tsx",
    ],
    coverage: {
      provider: "v8",
      reportsDirectory: path.resolve(
        import.meta.dirname,
        "../../tmp/coverage/web-production-workflow",
      ),
      include: [
        "src/lib/productionWorkflow.ts",
        "src/lib/fieldGeometry.ts",
        "src/lib/productionCalibration.ts",
        "src/lib/productionTrial.ts",
        "src/lib/productionConfigFreeze.ts",
        "src/lib/productionBroadcast.ts",
        "src/lib/broadcastDelivery.ts",
        "src/lib/productionHistory.ts",
        "src/components/broadcast/BroadcastReviewEvidenceStep.tsx",
        "src/components/broadcast/useBroadcastReviewEvidenceController.ts",
      ],
      thresholds: {
        branches: 90,
        functions: 90,
        lines: 90,
        statements: 90,
        "src/components/broadcast/BroadcastReviewEvidenceStep.tsx": {
          branches: 90,
          functions: 90,
          lines: 90,
          statements: 90,
        },
        "src/components/broadcast/useBroadcastReviewEvidenceController.ts": {
          branches: 90,
          functions: 90,
          lines: 90,
          statements: 90,
        },
        "src/lib/productionHistory.ts": {
          branches: 90,
          functions: 90,
          lines: 90,
          statements: 90,
        },
      },
    },
  },
});
