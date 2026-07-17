import { expect, test, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

const GENERATION_A = "a".repeat(64);
const GENERATION_B = "b".repeat(64);
const GENERATION_C = "c".repeat(64);
const CONFIG_DIGEST =
  "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824";

function trialMachineNote(): string {
  return JSON.stringify({
    schema_version: "1.0",
    purpose: "production_trial",
    workflow_id: "workflow-a",
    submission_id: "submission-trial",
    output_id: "accepted",
    generation: 1,
    calibration_digest: GENERATION_A,
    intent_sha256: GENERATION_B,
    start_frame: 10,
    max_frames: 300,
    enable_postprocess: true,
    enable_follow_cam: false,
  });
}

function fullMachineNote(): string {
  return JSON.stringify({
    schema_version: "1.0",
    purpose: "production_full",
    workflow_id: "workflow-a",
    submission_id: "submission-historical",
    output_id: "historical",
    generation: 1,
    accepted_trial_run_id: "production_trial_accepted",
    accepted_trial_request_sha256: GENERATION_A,
    confirmed_config_name: "confirmed.yaml",
    expected_config_sha256: CONFIG_DIGEST,
    config_patch_sha256: GENERATION_B,
    calibration_digest: GENERATION_A,
    source_signature: {
      path: "C:/videos/match.mp4",
      size_bytes: 1_000,
      modified_at: "2026-07-14T09:00:00Z",
    },
  });
}

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
  const acceptedTrial = run("production_trial_accepted", {
    notes: trialMachineNote(),
  });
  const historicalFull = run("production_full_historical", {
    source: "broadcast_hybrid",
    parent_run_id: acceptedTrial.run_id,
    config_name: "confirmed.yaml",
    config_path: "config/confirmed.yaml",
    notes: fullMachineNote(),
    broadcast: { status: "trajectory_ready" },
  });
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
    progress: {
      stage: "render",
      percent: 42,
      elapsed_seconds: 12,
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
      run_count: 7,
      config_count: 2,
      output_count: 7,
      runs: [
        readyProduct("product-one", firstGeneration),
        readyProduct("product-two", GENERATION_B),
        acceptedTrial,
        historicalFull,
        parent,
        child,
        run("leaf-output"),
      ],
      configs: [
        {
          name: "default.yaml",
          path: "config/default.yaml",
          created_at: "2026-07-14T09:30:00Z",
          input_video: "C:/videos/match.mp4",
          output_dir: "C:/outputs/default",
          detector_model_path: "models/ball.pt",
          postprocess_enabled: true,
          follow_cam_enabled: true,
          exists: { config: true, input_video: true },
        },
        {
          name: "confirmed.yaml",
          path: "config/confirmed.yaml",
          created_at: "2026-07-14T09:40:00Z",
          input_video: "C:/videos/match.mp4",
          output_dir: "C:/outputs/confirmed",
          detector_model_path: "models/ball.pt",
          postprocess_enabled: true,
          follow_cam_enabled: false,
          exists: { config: true, input_video: true },
        },
      ],
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
    {
      group_id: "config-only",
      title: "config-only.mp4",
      input_video: {
        name: "config-only.mp4",
        path: "C:/videos/config-only.mp4",
        size_bytes: 2_000,
        modified_at: "2026-07-12T09:00:00Z",
      },
      last_activity_at: "2026-07-12T09:30:00Z",
      run_count: 0,
      config_count: 1,
      output_count: 0,
      runs: [],
      configs: [
        {
          name: "config-only.yaml",
          path: "config/config-only.yaml",
          created_at: "2026-07-12T09:30:00Z",
          input_video: "C:/videos/config-only.mp4",
          output_dir: "C:/outputs/config-only",
          detector_model_path: "models/ball.pt",
          postprocess_enabled: true,
          follow_cam_enabled: false,
          exists: { config: true, input_video: true },
        },
      ],
      outputs: [],
      is_unbound: false,
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
  const configReads: string[] = [];
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
    if (url.pathname === "/api/configs/confirmed.yaml") {
      configReads.push("confirmed.yaml");
      await route.fulfill({
        status: 200,
        json: {
          name: "confirmed.yaml",
          path: "config/confirmed.yaml",
          text: "hello",
          raw: {
            metadata: {
              production_workflow: {
                schema_version: "1.0",
                workflow_id: "workflow-a",
                accepted_trial_run_id: "production_trial_accepted",
                calibration_digest: GENERATION_A,
                source_signature: {
                  path: "C:/videos/match.mp4",
                  size_bytes: 1_000,
                  modified_at: "2026-07-14T09:00:00Z",
                },
                trial_request_sha256: GENERATION_A,
                trial_intent_sha256: GENERATION_B,
                patch_sha256: GENERATION_B,
              },
            },
          },
          resolved: {},
          summary: {
            name: "confirmed.yaml",
            path: "config/confirmed.yaml",
            input_video: "C:/videos/match.mp4",
            postprocess_enabled: true,
            follow_cam_enabled: false,
            exists: { config: true, input_video: true },
          },
        },
      });
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
  await expect(page.getByTestId("asset-group-config-only")).toBeVisible();
  expect(artifactReads).toEqual([]);
  expect(qualityReads).toEqual([]);
  expect(configReads).toEqual([]);

  await page.getByTestId("asset-group-toggle-config-only").click();
  await expect(
    page.getByTestId("group-config-snapshots-config-only"),
  ).toContainText("config/config-only.yaml");
  expect(artifactReads).toEqual([]);
  expect(configReads).toEqual([]);
  await page.getByTestId("asset-group-toggle-config-only").click();

  await page.getByTestId("asset-group-toggle-unbound-legacy").click();
  await expect(page.getByTestId("timeline-run-legacy-failed")).toBeVisible();
  await expect(page.getByTestId("timeline-run-legacy-cancelled")).toBeVisible();
  await page.getByTestId("asset-group-toggle-unbound-legacy").click();

  await page.getByTestId("asset-group-toggle-match").click();
  await expect(page.getByTestId("group-source-metadata-match")).toContainText(
    "match.mp4",
  );
  await expect(page.getByTestId("group-config-snapshots-match")).toContainText(
    "config/default.yaml",
  );
  expect(artifactReads).toEqual([]);
  expect(qualityReads).toEqual([]);
  expect(configReads).toEqual([]);

  await page.getByTestId("timeline-toggle-production_full_historical").click();
  await expect(
    page.getByTestId("current-config-status-production_full_historical"),
  ).toContainText("Current saved configuration verified");
  expect(configReads).toEqual(["confirmed.yaml"]);
  await page.getByTestId("timeline-toggle-production_full_historical").click();

  await page.getByTestId("timeline-toggle-product-one").click();
  await expect(page.getByTestId("verified-product-product-one")).toBeVisible();
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
  expect(artifactReads).toEqual([`product-one:${GENERATION_A}`]);
  await expect(page.getByTestId("group-products-verified-match")).toContainText(
    "1",
  );
  await expect(page.getByTestId("group-products-ready-match")).toContainText(
    "Ready candidates: 2",
  );
  await expect(
    page.getByTestId("group-products-unverified-match"),
  ).toContainText("1");
  await expect(
    page.getByTestId("group-products-unavailable-match"),
  ).toContainText("0");

  await page.getByTestId("timeline-toggle-product-two").click();
  await expect(page.getByTestId("verified-product-product-two")).toBeVisible();
  expect(artifactReads.sort()).toEqual(
    [`product-one:${GENERATION_A}`, `product-two:${GENERATION_B}`].sort(),
  );

  const readsBeforeReopen = artifactReads.length;
  await page.getByTestId("asset-group-toggle-match").click();
  await page.getByTestId("asset-group-toggle-match").click();
  expect(artifactReads).toHaveLength(readsBeforeReopen);
  await page.getByTestId("timeline-toggle-product-one").click();
  await expect(page.getByTestId("verified-product-product-one")).toBeVisible();
  expect(artifactReads).toHaveLength(readsBeforeReopen);
  await page.getByTestId("timeline-toggle-product-one").click();

  await page.getByTestId("timeline-toggle-full-active").click();
  await expect(page.getByTestId("group-delete-full-active")).toBeDisabled();
  await expect(
    page.getByTestId("group-delete-blocker-full-active"),
  ).toContainText(/child output/i);
  await page.getByTestId("timeline-toggle-production_full_historical").click();
  await expect(
    page.getByTestId("current-config-status-production_full_historical"),
  ).toContainText("Current saved configuration verified");
  await expect(
    page.getByTestId("current-config-status-production_full_historical"),
  ).toHaveAttribute("role", "status");
  await expect(
    page.getByTestId("current-config-status-production_full_historical"),
  ).toHaveAttribute("aria-live", "polite");
  expect(configReads).toEqual(["confirmed.yaml", "confirmed.yaml"]);
  const accessibility = await new AxeBuilder({ page })
    .include('[data-testid="group-detail-match"]')
    .analyze();
  expect(
    accessibility.violations.filter(
      (violation) =>
        violation.id === "aria-progressbar-name" &&
        ["serious", "critical"].includes(violation.impact ?? ""),
    ),
  ).toEqual([]);

  firstGeneration = GENERATION_C;
  await page.getByTestId("group-cancel-full-active").click();
  await page.getByTestId("group-confirm-cancel-full-active").click();
  await expect.poll(() => cancelled).toEqual(["render-active"]);
  await expect(
    page.getByTestId("group-products-unverified-match"),
  ).toContainText("1");
  expect(
    artifactReads.filter((read) => read === `product-one:${GENERATION_C}`),
  ).toEqual([]);
  await page.getByTestId("timeline-toggle-product-one").click();
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

test("a 1,000-product group fetches only the explicitly expanded row", async ({
  page,
}) => {
  const artifactReads: string[] = [];
  const configReads: string[] = [];
  await page.route("**/api/**", async (route) => {
    const requestUrl = new URL(route.request().url());
    if (requestUrl.pathname.startsWith("/api/configs/")) {
      configReads.push(requestUrl.pathname);
      await route.fulfill({
        status: 500,
        json: { detail: "Unexpected config read" },
      });
      return;
    }
    if (requestUrl.pathname === "/api/runs/asset-groups") {
      await route.fulfill({
        status: 200,
        json: [
          {
            group_id: "large-match",
            title: "large-match.mp4",
            input_video: {
              name: "large-match.mp4",
              path: "C:/videos/large-match.mp4",
              size_bytes: 2_000,
              modified_at: "2026-07-14T09:00:00Z",
            },
            last_activity_at: "2026-07-14T10:02:00Z",
            runs: Array.from({ length: 1_000 }, (_, index) =>
              readyProduct(`large-product-${index}`, GENERATION_A),
            ).map((candidate) => ({
              ...candidate,
              input_video: "C:/videos/large-match.mp4",
            })),
            configs: [],
            outputs: [],
            is_unbound: false,
          },
        ],
      });
      return;
    }
    const match = requestUrl.pathname.match(
      /^\/api\/runs\/([^/]+)\/artifacts$/,
    );
    if (match) {
      const runId = decodeURIComponent(match[1]);
      artifactReads.push(runId);
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
        ],
      });
      return;
    }
    await route.fulfill({
      status: 404,
      json: { detail: "Unhandled test route" },
    });
  });

  await page.goto("/history");
  await page.getByTestId("asset-group-toggle-large-match").click();
  expect(artifactReads).toEqual([]);
  expect(configReads).toEqual([]);
  await page.getByTestId("timeline-toggle-large-product-999").click();
  await expect(
    page.getByTestId("verified-product-large-product-999"),
  ).toBeVisible();
  expect(artifactReads).toEqual(["large-product-999"]);
  expect(configReads).toEqual([]);
});
