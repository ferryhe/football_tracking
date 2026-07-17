import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

const GENERATION = "a".repeat(64);

async function monitorBrowserFailures(page: Page): Promise<string[]> {
  const failures: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") failures.push(`console: ${message.text()}`);
  });
  page.on("pageerror", (error) => {
    failures.push(`pageerror: ${error.message}`);
  });
  await page.addInitScript(() => {
    window.addEventListener("unhandledrejection", (event) => {
      const reason =
        event.reason instanceof Error
          ? `${event.reason.name}: ${event.reason.message}`
          : String(event.reason);
      console.error(`[unhandledrejection] ${reason}`);
    });
  });
  return failures;
}

function cutoverRun() {
  return {
    run_id: "cutover-ready",
    source: "broadcast_hybrid",
    status: "completed",
    created_at: "2026-07-17T12:00:00Z",
    started_at: "2026-07-17T12:00:01Z",
    completed_at: "2026-07-17T12:01:00Z",
    config_name: "confirmed.yaml",
    config_path: "config/confirmed.yaml",
    input_video: "C:/videos/cutover.mp4",
    parent_run_id: null,
    output_dir: "C:/outputs/cutover-ready",
    modules_enabled: {},
    artifacts: [
      {
        name: "event_candidates.json",
        path: "C:/outputs/cutover-ready/event_candidates.json",
        kind: "report",
        exists: true,
        size_bytes: 100,
        content_type: "application/json",
      },
    ],
    stats: { event_candidates: { candidate_count: 1 } },
    broadcast: {
      status: "ready",
      status_generation: GENERATION,
      limitations: ["Independent visual release review is still required."],
    },
    ai_candidate_lifecycle: {},
    progress: null,
    notes: null,
    error: null,
  };
}

function cutoverGroup() {
  return {
    group_id: "cutover",
    title: "cutover.mp4",
    input_video: {
      name: "cutover.mp4",
      path: "C:/videos/cutover.mp4",
      size_bytes: 1_000,
      modified_at: "2026-07-17T11:00:00Z",
    },
    last_activity_at: "2026-07-17T12:01:00Z",
    run_count: 1,
    config_count: 0,
    output_count: 1,
    runs: [cutoverRun()],
    configs: [],
    outputs: [],
    is_unbound: false,
  };
}

test("cutover journey opens Production, reopens the exact product, and preserves advanced run context", async ({
  page,
}, testInfo) => {
  const failures = await monitorBrowserFailures(page);
  await page.setViewportSize({ width: 1280, height: 720 });
  await page.addInitScript(() => {
    localStorage.setItem("app-theme", "light");
    localStorage.setItem("app-language", "en");
  });
  let assetGroupReads = 0;
  let artifactListReads = 0;

  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === "/api/inputs") {
      await route.fulfill({
        status: 200,
        json: {
          root_dir: "data",
          videos: [
            {
              name: "cutover.mp4",
              path: "C:/videos/cutover.mp4",
              size_bytes: 1_000,
              modified_at: "2026-07-17T11:00:00Z",
            },
          ],
        },
      });
      return;
    }
    if (url.pathname === "/api/inputs/field-preview") {
      await route.fulfill({
        status: 200,
        json: {
          input_video: "C:/videos/cutover.mp4",
          preview_data_url:
            "data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs=",
          frame_width: 1,
          frame_height: 1,
          frame_index: 0,
          frame_time_seconds: 0,
          sample_index: 0,
          sample_count: 1,
        },
      });
      return;
    }
    if (url.pathname === "/api/runs/asset-groups") {
      assetGroupReads += 1;
      await route.fulfill({ status: 200, json: [cutoverGroup()] });
      return;
    }
    if (url.pathname === "/api/runs") {
      await route.fulfill({ status: 200, json: [cutoverRun()] });
      return;
    }
    if (url.pathname === "/api/configs") {
      await route.fulfill({ status: 200, json: [] });
      return;
    }
    if (url.pathname === "/api/configs/confirmed.yaml") {
      await route.fulfill({
        status: 200,
        json: {
          name: "confirmed.yaml",
          path: "config/confirmed.yaml",
          text: "tracking: {}\n",
          raw: {},
          resolved: {},
          summary: {
            name: "confirmed.yaml",
            path: "config/confirmed.yaml",
            input_video: "C:/videos/cutover.mp4",
            postprocess_enabled: true,
            follow_cam_enabled: true,
            exists: { config: true, input_video: true },
          },
        },
      });
      return;
    }
    if (url.pathname === "/api/runs/cutover-ready/artifacts") {
      artifactListReads += 1;
      await route.fulfill({
        status: 200,
        json: [
          {
            name: "broadcast.mp4",
            path: "C:/outputs/cutover-ready/broadcast.mp4",
            kind: "video",
            exists: true,
            size_bytes: 1_000,
            content_type: "video/mp4",
          },
          {
            name: "broadcast_quality_report.json",
            path: "C:/outputs/cutover-ready/broadcast_quality_report.json",
            kind: "report",
            exists: true,
            size_bytes: 100,
            content_type: "application/json",
          },
        ],
      });
      return;
    }
    if (
      url.pathname ===
      "/api/runs/cutover-ready/artifacts/broadcast_quality_report.json"
    ) {
      await route.fulfill({
        status: 200,
        json: {
          overall_status: "pass",
          run_id: "cutover-ready",
          status_generation: GENERATION,
        },
      });
      return;
    }
    if (
      url.pathname === "/api/runs/cutover-ready/artifacts/broadcast.mp4"
    ) {
      await route.fulfill({
        status: 200,
        contentType: "video/mp4",
        body: Buffer.from("not-a-real-video"),
      });
      return;
    }
    if (url.pathname === "/api/runs/cutover-ready/ai-improvement-status") {
      await route.fulfill({
        status: 200,
        json: {
          run_id: "cutover-ready",
          output_dir: "C:/outputs/cutover-ready",
          artifacts: [],
          items_by_problem_type: {},
          final_manifest_status: {
            status: "unavailable",
            artifact_status: "unavailable",
          },
          final_selected_artifacts: [],
          final_selected_artifact_candidate_ids: [],
        },
      });
      return;
    }
    if (url.pathname === "/api/runs/cutover-ready/event-candidates") {
      await route.fulfill({
        status: 200,
        json: {
          run_id: "cutover-ready",
          summary: { candidate_count: 0 },
          candidates: [],
        },
      });
      return;
    }
    failures.push(`unhandled-api: ${url.pathname}${url.search}`);
    await route.fulfill({ status: 404, json: { detail: "not mocked" } });
  });

  await page.goto("/");
  await expect(page).toHaveURL(/\/production$/);
  await expect(
    page.getByRole("heading", { name: "Choose the original video" }),
  ).toBeVisible();
  const primaryNav = page.getByRole("navigation", {
    name: "Primary navigation",
  });
  await expect(primaryNav.getByRole("link")).toHaveCount(2);
  await expect(primaryNav.getByRole("link").nth(0)).toHaveText("Production");
  await expect(primaryNav.getByRole("link").nth(1)).toHaveText(
    "Production History",
  );

  const productionA11y = await new AxeBuilder({ page }).analyze();
  expect(
    productionA11y.violations.filter((violation) =>
      ["critical", "serious"].includes(violation.impact ?? ""),
    ),
  ).toEqual([]);
  await testInfo.attach("cutover-1280x720-light-en", {
    body: await page.screenshot({ animations: "disabled" }),
    contentType: "image/png",
  });

  await page.goto("/baseline");
  await expect(page).toHaveURL(/\/production\?from=baseline$/);
  await expect(page.getByRole("status")).toContainText(
    "original video and calibration prerequisites are still required",
  );
  await expect(
    page.getByRole("heading", { name: "Choose the original video" }),
  ).toBeVisible();

  await page.goto("/broadcast?run=cutover-ready");
  await expect(page).toHaveURL(
    /\/history\?run=cutover-ready&from=broadcast$/,
  );
  await expect(page.getByTestId("group-detail-cutover")).toBeVisible();
  await expect(page.getByTestId("timeline-toggle-cutover-ready")).toHaveAttribute(
    "aria-expanded",
    "true",
  );
  await expect(page.getByTestId("verified-product-cutover-ready")).toBeVisible();
  expect(assetGroupReads).toBe(1);
  expect(artifactListReads).toBe(1);

  const historyA11y = await new AxeBuilder({ page }).analyze();
  expect(
    historyA11y.violations.filter((violation) =>
      ["critical", "serious"].includes(violation.impact ?? ""),
    ),
  ).toEqual([]);

  await page.getByRole("link", { name: "Open AI review" }).click();
  await expect(page).toHaveURL(/\/ai\?run=cutover-ready$/);
  await expect(page.getByTestId("trigger-ai-run")).toContainText(
    "cutover-ready",
  );

  await page.goto("/history?run=cutover-ready");
  await page.getByRole("link", { name: "Open highlight tools" }).click();
  await expect(page).toHaveURL(/\/deliverable\?run=cutover-ready$/);
  await expect(page.getByTestId("trigger-highlight-run")).toContainText(
    "cutover-ready",
  );
  expect(failures).toEqual([]);
});

test("mobile navigation closes after navigation and follows focus", async ({
  page,
}, testInfo) => {
  const failures = await monitorBrowserFailures(page);
  await page.addInitScript(() => {
    localStorage.setItem("app-theme", "light");
    localStorage.setItem("app-language", "en");
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === "/api/inputs") {
      await route.fulfill({ status: 200, json: { root_dir: "data", videos: [] } });
      return;
    }
    if (url.pathname === "/api/runs/asset-groups") {
      await route.fulfill({ status: 200, json: [] });
      return;
    }
    await route.fulfill({ status: 404, json: { detail: "not mocked" } });
  });
  await page.goto("/");
  await expect(page).toHaveURL(/\/production$/);
  await page.getByRole("button", { name: "Open navigation" }).click();
  await page
    .getByRole("dialog", { name: "Primary navigation" })
    .getByRole("link", { name: "Production", exact: true })
    .click();
  await expect(page).toHaveURL(/\/production$/);
  await expect(page.getByTestId("button-close-sidebar")).toHaveCount(0);
  await expect(page.getByRole("main")).toBeFocused();

  await page.getByRole("button", { name: "Open navigation" }).click();
  await page
    .getByRole("navigation", { name: "Primary navigation" })
    .getByRole("link", { name: "Production History" })
    .click();
  await expect(page).toHaveURL(/\/history$/);
  await expect(page.getByTestId("button-close-sidebar")).toHaveCount(0);
  await expect(page.getByRole("main")).toBeFocused();
  await page.getByRole("button", { name: "Switch to dark mode" }).click();
  await expect(page.locator("html")).toHaveClass(/dark/);
  const darkHistoryA11y = await new AxeBuilder({ page }).analyze();
  expect(
    darkHistoryA11y.violations.filter((violation) =>
      ["critical", "serious"].includes(violation.impact ?? ""),
    ),
  ).toEqual([]);
  await page.getByRole("button", { name: "中文" }).click();
  await expect(page.getByRole("heading", { name: "成品历史" })).toBeVisible();
  await testInfo.attach("cutover-390x844-dark-zh", {
    body: await page.screenshot({ animations: "disabled" }),
    contentType: "image/png",
  });
  expect(failures).toEqual([]);
});
