import { expect, test, type Page } from "@playwright/test";

const GENERATION_A = "a".repeat(64);
const GENERATION_B = "b".repeat(64);
const GENERATION_C = "c".repeat(64);

function run(
  runId: string,
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    run_id: runId,
    source: "api",
    status: "completed",
    created_at: "2026-07-14T10:00:00Z",
    started_at: "2026-07-14T10:01:00Z",
    completed_at: "2026-07-14T10:02:00Z",
    config_name: "default.yaml",
    config_path: "config/default.yaml",
    input_video: "C:/videos/match.mp4",
    parent_run_id: null,
    output_dir: `C:/outputs/${runId}`,
    modules_enabled: {},
    artifacts: [],
    stats: {},
    broadcast: {},
    ai_candidate_lifecycle: {},
    progress: null,
    notes: null,
    error: null,
    ...overrides,
  };
}

function readyProduct(runId: string, generation: string) {
  return run(runId, {
    source: "broadcast_hybrid",
    broadcast: {
      status: "ready",
      status_generation: generation,
      limitations: [`${runId} requires an independent visual release review.`],
    },
  });
}

function assetGroups(firstGeneration: string) {
  const parent = run("full-active", {
    source: "broadcast_hybrid",
    broadcast: {
      status: "trajectory_ready",
      last_operation: {
        operation_run_id: "render-active",
        operation: "render",
        status: "running",
      },
    },
  });
  const child = run("render-active", {
    source: "broadcast_operation",
    status: "running",
    completed_at: null,
    parent_run_id: "full-active",
    broadcast: {
      parent_run_id: "full-active",
      operation: "render",
      operation_status: "running",
    },
  });
  return [
    {
      group_id: "match",
      title: "match.mp4",
      input_video: {
        name: "match.mp4",
        path: "C:/videos/match.mp4",
        size_bytes: 1_000,
        modified_at: "2026-07-14T09:00:00Z",
      },
      last_activity_at: "2026-07-14T10:02:00Z",
      run_count: 5,
      config_count: 0,
      output_count: 5,
      runs: [
        readyProduct("product-one", firstGeneration),
        readyProduct("product-two", GENERATION_B),
        parent,
        child,
        run("leaf-output"),
      ],
      configs: [],
      outputs: [],
      is_unbound: false,
    },
    {
      group_id: "unbound-legacy",
      title: "Unbound / Legacy",
      input_video: null,
      last_activity_at: "2026-07-13T10:00:00Z",
      run_count: 2,
      config_count: 0,
      output_count: 2,
      runs: [
        run("legacy-failed", {
          input_video: null,
          status: "failed",
          error: "legacy failure",
        }),
        run("legacy-cancelled", {
          input_video: null,
          status: "cancelled",
        }),
      ],
      configs: [],
      outputs: [],
      is_unbound: true,
    },
  ];
}

function monitorBrowserFailures(page: Page) {
  const failures: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") failures.push(`console: ${message.text()}`);
  });
  page.on("pageerror", (error) => failures.push(`pageerror: ${error.message}`));
  return failures;
}

test("grouped history lazily verifies versioned products and keeps actions safe", async ({
  page,
}) => {
  const failures = monitorBrowserFailures(page);
  let firstGeneration = GENERATION_A;
  const artifactReads: string[] = [];
  const qualityReads: string[] = [];
  const cancelled: string[] = [];
  const deleted: string[] = [];

  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const artifactMatch = url.pathname.match(
      /^\/api\/runs\/([^/]+)\/artifacts(?:\/(.+))?$/,
    );
    if (url.pathname === "/api/runs/asset-groups") {
      await route.fulfill({ status: 200, json: assetGroups(firstGeneration) });
      return;
    }
    if (artifactMatch) {
      const runId = decodeURIComponent(artifactMatch[1]);
      const artifactName = artifactMatch[2]
        ? decodeURIComponent(artifactMatch[2])
        : null;
      const generation = url.searchParams.get("status_generation") ?? "";
      if (artifactName === "broadcast_quality_report.json") {
        qualityReads.push(`${runId}:${generation}`);
        await route.fulfill({
          status: 200,
          json: {
            overall_status: "pass",
            run_id: runId,
            status_generation: generation,
          },
        });
        return;
      }
      if (artifactName === "broadcast.mp4") {
        await route.fulfill({
          status: 200,
          contentType: "video/mp4",
          body: Buffer.from("not-a-real-video"),
        });
        return;
      }
      if (artifactName === null) {
        artifactReads.push(`${runId}:${generation}`);
        await route.fulfill({
          status: 200,
          json: [
            {
              name: "broadcast.mp4",
              path: `C:/outputs/${runId}/broadcast.mp4`,
              kind: "video",
              exists: true,
              size_bytes: 1_000,
              content_type: "video/mp4",
            },
            {
              name: "broadcast_quality_report.json",
              path: `C:/outputs/${runId}/broadcast_quality_report.json`,
              kind: "report",
              exists: true,
              size_bytes: 100,
              content_type: "application/json",
            },
          ],
        });
        return;
      }
    }
    const cancelMatch = url.pathname.match(/^\/api\/runs\/([^/]+)\/cancel$/);
    if (cancelMatch && request.method() === "POST") {
      const runId = decodeURIComponent(cancelMatch[1]);
      cancelled.push(runId);
      await route.fulfill({
        status: 200,
        json: run(runId, { status: "cancelled" }),
      });
      return;
    }
    if (url.pathname === "/api/runs" && request.method() === "DELETE") {
      const runId = url.searchParams.get("run_id") ?? "";
      deleted.push(runId);
      await route.fulfill({
        status: 200,
        json: { name: runId, path: `C:/outputs/${runId}`, deleted: true },
      });
      return;
    }
    await route.fulfill({
      status: 404,
      json: { detail: `Unhandled ${url.pathname}` },
    });
  });

  await page.goto("/history");
  await expect(page.getByTestId("asset-group-match")).toBeVisible();
  await expect(page.getByTestId("asset-group-unbound-legacy")).toBeVisible();
  expect(artifactReads).toEqual([]);
  expect(qualityReads).toEqual([]);

  await page.getByTestId("asset-group-toggle-unbound-legacy").click();
  await expect(page.getByTestId("timeline-run-legacy-failed")).toBeVisible();
  await expect(page.getByTestId("timeline-run-legacy-cancelled")).toBeVisible();
  await page.getByTestId("asset-group-toggle-unbound-legacy").click();

  await page.getByTestId("asset-group-toggle-match").click();
  await expect(page.getByTestId("verified-product-product-one")).toBeVisible();
  await expect(page.getByTestId("verified-product-product-two")).toBeVisible();
  await expect(page.getByTestId("product-preview-product-one")).toBeVisible();
  await expect(page.getByTestId("product-quality-product-one")).toContainText(
    "pass",
  );
  await expect(
    page.getByText(/independent visual release review/i).first(),
  ).toBeVisible();
  await expect(
    page.getByText(/artifact verification is not release approval/i).first(),
  ).toBeVisible();
  expect(artifactReads.sort()).toEqual(
    [`product-one:${GENERATION_A}`, `product-two:${GENERATION_B}`].sort(),
  );

  const readsBeforeReopen = artifactReads.length;
  await page.getByTestId("asset-group-toggle-match").click();
  await page.getByTestId("asset-group-toggle-match").click();
  await expect(page.getByTestId("verified-product-product-one")).toBeVisible();
  expect(artifactReads).toHaveLength(readsBeforeReopen);

  await page.getByTestId("timeline-toggle-full-active").click();
  await expect(page.getByTestId("group-delete-full-active")).toBeDisabled();
  await expect(
    page.getByTestId("group-delete-blocker-full-active"),
  ).toContainText(/child output/i);

  firstGeneration = GENERATION_C;
  await page.getByTestId("group-cancel-full-active").click();
  await page.getByTestId("group-confirm-cancel-full-active").click();
  await expect.poll(() => cancelled).toEqual(["render-active"]);
  await expect
    .poll(
      () =>
        artifactReads.filter((read) => read === `product-one:${GENERATION_C}`)
          .length,
    )
    .toBe(1);

  await page.getByTestId("timeline-toggle-leaf-output").click();
  await page.getByTestId("group-delete-leaf-output").click();
  await page.getByTestId("group-confirm-delete-leaf-output").click();
  await expect.poll(() => deleted).toEqual(["leaf-output"]);

  expect(failures).toEqual([]);
});
