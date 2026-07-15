import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

const inputCatalog = {
  root_dir: "data",
  videos: [
    {
      name: "match-a.mp4",
      path: "data/match-a.mp4",
      size_bytes: 1_024,
      modified_at: "2026-07-14T10:00:00Z",
    },
  ],
};

const previewDataUrl = `data:image/svg+xml;base64,${Buffer.from(
  '<svg xmlns="http://www.w3.org/2000/svg" width="1920" height="1080"><rect width="1920" height="1080" fill="#174f2a"/><path d="M100 100H1820V980H100Z" fill="none" stroke="white" stroke-width="8"/></svg>',
).toString("base64")}`;

const squarePreviewSvg =
  '<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="1000"><rect width="1000" height="1000" fill="#174f2a"/><path d="M80 80H920V920H80Z" fill="none" stroke="white" stroke-width="8"/></svg>';

function trialTrackCsv(counts: {
  detected: number;
  predicted: number;
  lost: number;
}) {
  const statuses = [
    ...Array.from({ length: counts.detected }, () => "Detected"),
    ...Array.from({ length: counts.predicted }, () => "Predicted"),
    ...Array.from({ length: counts.lost }, () => "Lost"),
  ];
  return [
    "Frame,X,Y,Confidence,Status",
    ...statuses.map(
      (status, frame) =>
        `${frame},${100 + frame},${200 + frame},${status === "Lost" ? 0 : 0.9},${status}`,
    ),
  ].join("\n");
}

const trialRawTrackCsv = trialTrackCsv({
  detected: 200,
  predicted: 50,
  lost: 50,
});
const trialCleanedTrackCsv = trialTrackCsv({
  detected: 210,
  predicted: 50,
  lost: 40,
});

const runtimeErrors = new WeakMap<Page, string[]>();
const allowedRuntimeErrors = new WeakMap<Page, RegExp[]>();

function allowRuntimeError(page: Page, pattern: RegExp) {
  allowedRuntimeErrors.set(page, [
    ...(allowedRuntimeErrors.get(page) ?? []),
    pattern,
  ]);
}

async function watchRuntimeErrors(page: Page) {
  const errors: string[] = [];
  runtimeErrors.set(page, errors);
  page.on("console", (message) => {
    if (message.type() === "error") {
      errors.push(`console.error: ${message.text()}`);
    }
  });
  page.on("pageerror", (error) => {
    errors.push(`pageerror: ${error.message}`);
  });
  await page.addInitScript(() => {
    window.addEventListener("unhandledrejection", (event) => {
      const reason =
        event.reason instanceof Error
          ? event.reason.message
          : String(event.reason ?? "unknown reason");
      console.error(`[unhandledrejection] ${reason}`);
    });
  });
}

async function createPlayableVideoFixture(page: Page) {
  return page.evaluate(async () => {
    const canvas = document.createElement("canvas");
    canvas.width = 64;
    canvas.height = 36;
    const context = canvas.getContext("2d");
    if (!context) throw new Error("Canvas 2D is unavailable");

    const preferredMime = ["video/webm;codecs=vp8", "video/webm"].find(
      (candidate) => MediaRecorder.isTypeSupported(candidate),
    );
    const stream = canvas.captureStream(10);
    const recorder = new MediaRecorder(
      stream,
      preferredMime ? { mimeType: preferredMime } : undefined,
    );
    const chunks: Blob[] = [];
    const stopped = new Promise<Blob>((resolve, reject) => {
      recorder.addEventListener("dataavailable", (event) => {
        if (event.data.size > 0) chunks.push(event.data);
      });
      recorder.addEventListener("error", () => {
        reject(new Error("Could not record the browser video fixture"));
      });
      recorder.addEventListener("stop", () => {
        resolve(new Blob(chunks, { type: recorder.mimeType || "video/webm" }));
      });
    });

    recorder.start();
    context.fillStyle = "#174f2a";
    context.fillRect(0, 0, canvas.width, canvas.height);
    context.strokeStyle = "#ffffff";
    context.strokeRect(4, 4, canvas.width - 8, canvas.height - 8);
    await new Promise((resolve) => window.setTimeout(resolve, 250));
    recorder.stop();
    const blob = await stopped;
    stream.getTracks().forEach((track) => track.stop());

    const dataUrl = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader();
      reader.addEventListener("load", () => resolve(String(reader.result)));
      reader.addEventListener("error", () => reject(reader.error));
      reader.readAsDataURL(blob);
    });
    return {
      bodyBase64: dataUrl.slice(dataUrl.indexOf(",") + 1),
      contentType: blob.type || "video/webm",
    };
  });
}

interface CanvasBox {
  x: number;
  y: number;
  width: number;
  height: number;
}

function sourcePointOnCanvas(
  box: CanvasBox,
  point: [number, number],
  source: { width: number; height: number },
) {
  const scale = Math.min(box.width / source.width, box.height / source.height);
  const offsetX = (box.width - source.width * scale) / 2;
  const offsetY = (box.height - source.height * scale) / 2;
  return {
    x: offsetX + point[0] * scale,
    y: offsetY + point[1] * scale,
  };
}

function chromiumMouseSourcePoint(
  box: CanvasBox,
  position: { x: number; y: number },
  source: { width: number; height: number },
): [number, number] {
  const scale = Math.min(box.width / source.width, box.height / source.height);
  const offsetX = (box.width - source.width * scale) / 2;
  const offsetY = (box.height - source.height * scale) / 2;
  const localX = Math.floor(box.x + position.x) - box.x;
  const localY = Math.floor(box.y + position.y) - box.y;
  return [
    Math.max(
      0,
      Math.min(source.width - 1, Math.round((localX - offsetX) / scale)),
    ),
    Math.max(
      0,
      Math.min(source.height - 1, Math.round((localY - offsetY) / scale)),
    ),
  ];
}

function draftWithApprovedPolygon() {
  const timestamp = "2026-07-14T12:00:00Z";
  return {
    schema_version: 3,
    workflow_id: "workflow-overlay-readiness",
    created_at: timestamp,
    updated_at: timestamp,
    status: "active",
    source: inputCatalog.videos[0],
    calibration: {
      source_resolution: { width: 1920, height: 1080 },
      suggestion: null,
      approved_polygon: [
        [100, 100],
        [1800, 100],
        [1800, 1000],
      ],
      exclusions: [],
      polygon_digest: "a".repeat(64),
      confirmed_frames: [],
    },
    trial: null,
    pending_config_confirmation: null,
    confirmed_config: null,
    full_run: null,
    verified_product: null,
  };
}

async function mockTrialDefaults(page: Page) {
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const key = `${request.method()} ${url.pathname}`;
    if (key === "GET /api/configs") {
      await route.fulfill({ json: [] });
      return;
    }
    if (key === "GET /api/health") {
      await route.fulfill({
        json: {
          status: "ok",
          active_run_id: null,
          config_count: 0,
          run_count: 0,
        },
      });
      return;
    }
    if (key === "GET /api/healthz") {
      await route.fulfill({
        json: {
          status: "ok",
          active_run_id: null,
          config_count: 0,
          run_count: 0,
        },
      });
      return;
    }
    if (key === "GET /api/runs") {
      await route.fulfill({ json: [] });
      return;
    }
    await route.fallback();
  });
}

type TrialRunStatus =
  | "queued"
  | "running"
  | "completed"
  | "failed"
  | "cancelled";

interface TrialScenarioOptions {
  activeRunId?: string | null;
  conflictOnCreate?: boolean;
  deferDerive?: boolean;
  loseCreateResponseOnce?: boolean;
  missingArtifact?: string;
  corruptMetrics?: boolean;
  omitVideo?: boolean;
}

function backendPathName(value: unknown): string {
  return (
    String(value ?? "")
      .replaceAll("\\", "/")
      .split("/")
      .at(-1) ?? ""
  );
}

function backendPathStem(value: unknown): string {
  const name = backendPathName(value);
  const suffixStart = name.lastIndexOf(".");
  return suffixStart > 0 ? name.slice(0, suffixStart) : name;
}

function backendMaterializedRunConfigName(
  baseConfigName: unknown,
  runId: string,
): string {
  return `generated/${backendPathStem(baseConfigName)}_field_setup_${runId}.yaml`;
}

async function installTrialScenario(
  page: Page,
  options: TrialScenarioOptions = {},
) {
  const videoFixture = options.omitVideo
    ? null
    : await createPlayableVideoFixture(page);
  const createBodies: Array<Record<string, unknown>> = [];
  const deriveBodies: Array<Record<string, unknown>> = [];
  const cancelIds: string[] = [];
  const configGetNames: string[] = [];
  const runs: Array<Record<string, unknown>> = [];
  const configs = new Map<string, Record<string, unknown>>();
  let externalActiveRunId = options.activeRunId ?? null;
  let configMode: "ok" | "missing" | "tampered" = "ok";
  let createResponseLost = false;
  let releaseDeriveGate: (() => void) | null = null;
  const deriveGate = options.deferDerive
    ? new Promise<void>((resolve) => {
        releaseDeriveGate = resolve;
      })
    : null;

  const artifactList = () =>
    [
      {
        name: "run_manifest.json",
        path: "run_manifest.json",
        kind: "json",
        exists: true,
        size_bytes: 100,
        content_type: "application/json",
      },
      {
        name: "metrics_report.json",
        path: "metrics_report.json",
        kind: "json",
        exists: true,
        size_bytes: 100,
        content_type: "application/json",
      },
      {
        name: "ball_track.csv",
        path: "ball_track.csv",
        kind: "csv",
        exists: true,
        size_bytes: Buffer.byteLength(trialRawTrackCsv),
        content_type: "text/csv",
      },
      {
        name: "ball_audit.json",
        path: "ball_audit.json",
        kind: "json",
        exists: true,
        size_bytes: 100,
        content_type: "application/json",
      },
      {
        name: "ball_track.cleaned.csv",
        path: "ball_track.cleaned.csv",
        kind: "csv",
        exists: true,
        size_bytes: Buffer.byteLength(trialCleanedTrackCsv),
        content_type: "text/csv",
      },
      ...(videoFixture
        ? [
            {
              name: "follow_cam.webm",
              path: "follow_cam.webm",
              kind: "video",
              exists: true,
              size_bytes: Buffer.byteLength(videoFixture.bodyBase64, "base64"),
              content_type: videoFixture.contentType,
            },
          ]
        : []),
    ].filter((item) => item.name !== options.missingArtifact);

  function activeRunId() {
    return (
      externalActiveRunId ??
      (runs.find((run) => run.status === "queued" || run.status === "running")
        ?.run_id as string | undefined) ??
      null
    );
  }

  function runRecord(
    runId: string,
    body: Record<string, unknown>,
  ): Record<string, unknown> {
    const configPatch = body.config_patch as Record<string, unknown> | null;
    const configName =
      configPatch && Object.keys(configPatch).length > 0
        ? backendMaterializedRunConfigName(body.config_name, runId)
        : String(body.config_name);
    return {
      run_id: runId,
      source: "api",
      status: "queued",
      created_at: "2026-07-15T12:00:00Z",
      started_at: null,
      completed_at: null,
      config_name: configName,
      config_path: `configs/${configName}`,
      input_video: body.input_video,
      parent_run_id: body.parent_run_id ?? null,
      output_dir: `outputs/${body.output_dir_name}`,
      modules_enabled: {
        postprocess: body.enable_postprocess,
        follow_cam: body.enable_follow_cam,
      },
      artifacts: [],
      stats: {},
      broadcast: null,
      progress: {
        stage: "queued",
        current_frame: 0,
        total_frames: body.max_frames,
        percent: 0,
      },
      notes: body.notes,
      error: null,
    };
  }

  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const method = request.method();
    const path = url.pathname;
    if (method === "GET" && path === "/api/configs") {
      await route.fulfill({
        json: [
          {
            name: "default.yaml",
            path: "configs/default.yaml",
            created_at: "2026-07-15T00:00:00Z",
            input_video: inputCatalog.videos[0].path,
            output_dir: null,
            detector_model_path: "models/ball.pt",
            postprocess_enabled: true,
            follow_cam_enabled: true,
            exists: { yaml: true },
          },
        ],
      });
      return;
    }
    if (method === "GET" && path === "/api/health") {
      await route.fulfill({
        json: {
          status: "ok",
          active_run_id: activeRunId(),
          config_count: configs.size + 1,
          run_count: runs.length,
        },
      });
      return;
    }
    if (method === "GET" && path === "/api/runs") {
      await route.fulfill({ json: runs });
      return;
    }
    if (method === "POST" && path === "/api/runs") {
      const body = request.postDataJSON() as Record<string, unknown>;
      createBodies.push(body);
      if (options.conflictOnCreate) {
        externalActiveRunId = "race-run";
        await route.fulfill({
          status: 409,
          json: { detail: "Another run is already active: race-run" },
        });
        return;
      }
      const runId = backendPathName(body.output_dir_name);
      if (!runId) throw new Error("Trial fixture requires output_dir_name");
      const created = runRecord(runId, body);
      runs.push(created);
      if (options.loseCreateResponseOnce && !createResponseLost) {
        createResponseLost = true;
        await route.abort("connectionreset");
        return;
      }
      await route.fulfill({ status: 201, json: created });
      return;
    }
    const cancelMatch = path.match(/^\/api\/runs\/([^/]+)\/cancel$/);
    if (method === "POST" && cancelMatch) {
      const run = runs.find(
        (item) => item.run_id === decodeURIComponent(cancelMatch[1]),
      );
      if (!run) {
        await route.fulfill({ status: 404, json: { detail: "missing" } });
        return;
      }
      cancelIds.push(String(run.run_id));
      run.status = "cancelled";
      run.progress = null;
      await route.fulfill({ json: run });
      return;
    }
    const artifactsMatch = path.match(/^\/api\/runs\/([^/]+)\/artifacts$/);
    if (method === "GET" && artifactsMatch) {
      await route.fulfill({ json: artifactList() });
      return;
    }
    const auditMatch = path.match(/^\/api\/runs\/([^/]+)\/ball-audit$/);
    if (method === "GET" && auditMatch) {
      await route.fulfill({
        json: {
          schema_version: "1.0",
          generated_at: "2026-07-15T12:05:00Z",
          summary: {
            frame_count: 300,
            source_count: 2,
            tracklet_count: 0,
            suspicious_tracklet_count: 0,
            review_event_count: 0,
            lost_gap_count: 0,
            max_step_px: 20,
          },
          sources: [
            {
              name: "raw",
              path: "ball_track.csv",
              row_count: 300,
              tracklet_count: 0,
            },
            {
              name: "cleaned",
              path: "ball_track.cleaned.csv",
              row_count: 300,
              tracklet_count: 0,
            },
          ],
          tracklets: [],
          review_events: [],
        },
      });
      return;
    }
    const artifactMatch = path.match(/^\/api\/runs\/([^/]+)\/artifacts\/(.+)$/);
    if (method === "GET" && artifactMatch) {
      const runId = decodeURIComponent(artifactMatch[1]);
      const name = decodeURIComponent(artifactMatch[2]);
      const run = runs.find((item) => item.run_id === runId);
      if (
        !run ||
        name === options.missingArtifact ||
        (options.omitVideo && name === "follow_cam.webm")
      ) {
        await route.fulfill({ status: 404, json: { detail: "missing" } });
        return;
      }
      if (name === "run_manifest.json") {
        await route.fulfill({
          json: {
            schema_version: "1.0",
            run_id: run.run_id,
            input_video: run.input_video,
            config_name: run.config_name,
            status: run.status,
            notes: run.notes,
          },
        });
        return;
      }
      if (name === "metrics_report.json") {
        if (options.corruptMetrics) {
          await route.fulfill({ contentType: "application/json", body: "[]" });
        } else {
          await route.fulfill({
            json: {
              schema_version: "1.0",
              generated_at: "2026-07-15T12:05:00Z",
              tracks: {
                raw: (run.stats as Record<string, unknown>).raw,
                cleaned: (run.stats as Record<string, unknown>).cleaned,
              },
              quality_gate: (run.stats as Record<string, unknown>).quality_gate,
            },
          });
        }
        return;
      }
      if (name.endsWith(".csv")) {
        await route.fulfill({
          contentType: "text/csv",
          body:
            name === "ball_track.cleaned.csv"
              ? trialCleanedTrackCsv
              : trialRawTrackCsv,
        });
        return;
      }
      if (name === "follow_cam.webm" && videoFixture) {
        await route.fulfill({
          status: 200,
          contentType: videoFixture.contentType,
          headers: { "Accept-Ranges": "bytes" },
          body: Buffer.from(videoFixture.bodyBase64, "base64"),
        });
        return;
      }
    }
    const runMatch = path.match(/^\/api\/runs\/([^/]+)$/);
    if (method === "GET" && runMatch) {
      const run = runs.find(
        (item) => item.run_id === decodeURIComponent(runMatch[1]),
      );
      if (!run) {
        await route.fulfill({ status: 404, json: { detail: "missing" } });
        return;
      }
      await route.fulfill({ json: run });
      return;
    }
    if (method === "POST" && path === "/api/configs/derive") {
      const body = request.postDataJSON() as Record<string, unknown>;
      deriveBodies.push(body);
      if (deriveGate) await deriveGate;
      const outputName = String(body.output_name);
      const name = `generated/${outputName}`;
      const detail = {
        name,
        path: `configs/${name}`,
        text: `input_video: ${inputCatalog.videos[0].path}\nname: ${name}\n`,
        raw: body.patch,
        resolved: body.patch,
        summary: {
          name,
          path: `configs/${name}`,
          created_at: "2026-07-15T12:10:00Z",
          input_video: inputCatalog.videos[0].path,
          output_dir: null,
          detector_model_path: "models/ball.pt",
          postprocess_enabled: true,
          follow_cam_enabled: false,
          exists: { yaml: true },
        },
      };
      configs.set(name, detail);
      configMode = "ok";
      await route.fulfill({ status: 201, json: detail });
      return;
    }
    const configMatch = path.match(/^\/api\/configs\/(.+)$/);
    if (method === "GET" && configMatch) {
      const name = decodeURIComponent(configMatch[1]);
      configGetNames.push(name);
      const detail = configs.get(name);
      if (!detail || configMode === "missing") {
        await route.fulfill({ status: 404, json: { detail: "missing" } });
        return;
      }
      await route.fulfill({
        json:
          configMode === "tampered"
            ? { ...detail, text: `${detail.text as string}tampered: true\n` }
            : detail,
      });
      return;
    }
    await route.fallback();
  });

  return {
    runs,
    createBodies,
    deriveBodies,
    cancelIds,
    configGetNames,
    runId(index = 0) {
      const runId = runs[index]?.run_id;
      if (typeof runId !== "string") throw new Error(`Unknown run ${index}`);
      return runId;
    },
    setStatus(runId: string, status: TrialRunStatus) {
      const run = runs.find((item) => item.run_id === runId);
      if (!run) throw new Error(`Unknown run ${runId}`);
      run.status = status;
      run.error = status === "failed" ? "trial failed" : null;
      run.completed_at =
        status === "completed" || status === "failed" || status === "cancelled"
          ? "2026-07-15T12:05:00Z"
          : null;
      run.artifacts = status === "completed" ? artifactList() : [];
      run.progress =
        status === "queued" || status === "running"
          ? {
              stage: status,
              current_frame: status === "running" ? 150 : 0,
              total_frames: 300,
              percent: status === "running" ? 50 : 0,
            }
          : null;
      run.stats =
        status === "completed"
          ? {
              raw: {
                frame_count: 300,
                detected: 200,
                predicted: 50,
                lost: 50,
                detected_ratio: 2 / 3,
                predicted_ratio: 1 / 6,
                lost_ratio: 1 / 6,
                longest_lost_streak: 4,
                false_positive_island_count: 1,
                max_step_px: 20,
              },
              cleaned: {
                frame_count: 300,
                detected: 210,
                predicted: 50,
                lost: 40,
                detected_ratio: 0.7,
                predicted_ratio: 1 / 6,
                lost_ratio: 2 / 15,
              },
              quality_gate: { status: "warn" },
            }
          : {};
    },
    setConfigMode(mode: "ok" | "missing" | "tampered") {
      configMode = mode;
    },
    setExternalActiveRun(runId: string | null) {
      externalActiveRunId = runId;
    },
    releaseDerive() {
      releaseDeriveGate?.();
    },
  };
}

function draftWithCompletedCalibration() {
  const draft = draftWithApprovedPolygon();
  const digest = draft.calibration.polygon_digest;
  return {
    ...draft,
    workflow_id: "workflow-completed-calibration",
    calibration: {
      ...draft.calibration,
      confirmed_frames: [10, 20, 30].map((frameIndex, index) => ({
        input_video: inputCatalog.videos[0].path,
        frame_index: frameIndex,
        frame_time_seconds: frameIndex / 25,
        sample_index: index + 1,
        source_resolution: { width: 1920, height: 1080 },
        polygon_digest: digest,
      })),
    },
  };
}

async function mockInputs(page: Page) {
  await page.route("**/api/inputs", async (route) => {
    await route.fulfill({ json: inputCatalog });
  });
  await page.route("**/api/inputs/field-preview", async (route) => {
    const body = route.request().postDataJSON() as { sample_index?: number };
    const sampleIndex = body.sample_index ?? 1;
    await route.fulfill({
      json: {
        input_video: inputCatalog.videos[0].path,
        preview_data_url: previewDataUrl,
        frame_width: 1920,
        frame_height: 1080,
        frame_index: sampleIndex * 10,
        frame_time_seconds: (sampleIndex * 10) / 25,
        sample_index: sampleIndex,
        sample_count: 3,
      },
    });
  });
  await page.route("**/api/inputs/field-suggestion", async (route) => {
    const body = route.request().postDataJSON() as { frame_index?: number };
    await route.fulfill({
      json: {
        input_video: inputCatalog.videos[0].path,
        preview_data_url: previewDataUrl,
        preview_bounds: [0, 0, 1919, 1079],
        frame_width: 1920,
        frame_height: 1080,
        frame_index: body.frame_index ?? 10,
        frame_time_seconds: (body.frame_index ?? 10) / 25,
        sample_index: Math.max(1, Math.round((body.frame_index ?? 10) / 10)),
        sample_count: 3,
        field_polygon: [
          [100, 100],
          [1800, 100],
          [1800, 1000],
          [100, 1000],
        ],
        expanded_polygon: [
          [80, 80],
          [1820, 80],
          [1820, 1020],
          [80, 1020],
        ],
        field_roi: [100, 100, 1800, 1000],
        expanded_roi: [80, 80, 1820, 1020],
        confidence: "detected",
        source: "system-detector",
        field_coverage: 0.78,
        config_patch: {},
      },
    });
  });
}

async function openCalibration(page: Page) {
  await page.goto("/production");
  await page.getByLabel("Original video").selectOption("data/match-a.mp4");
  await page.getByRole("button", { name: /^Next$/ }).click();
  await expect(
    page.getByRole("heading", { name: "Field calibration" }),
  ).toBeVisible();
  await expect(page.getByTestId("field-polygon-editor")).toBeVisible();
  await expect(page.getByAltText("Original source frame 10")).toBeVisible();
}

async function openTrialFromDraft(page: Page, language: "en" | "zh" = "en") {
  await page.addInitScript((draft) => {
    const key = "football-tracking.production-draft.v1";
    if (localStorage.getItem(key) === null) {
      localStorage.setItem(key, JSON.stringify(draft));
    }
  }, draftWithCompletedCalibration());
  await page.goto("/production");
  await expect(
    page.getByRole("heading", {
      name: language === "zh" ? "试跑调参" : "Trial and tuning",
    }),
  ).toBeVisible();
  await expect(
    page.getByLabel(language === "zh" ? "基础配置" : "Base configuration"),
  ).toHaveValue("default.yaml");
}

async function finishTrialForAcceptance(
  page: Page,
  scenario: Awaited<ReturnType<typeof installTrialScenario>>,
) {
  await page.getByRole("button", { name: "Start bounded trial" }).click();
  await expect.poll(() => scenario.createBodies.length).toBe(1);
  const runId = scenario.runId();
  scenario.setStatus(runId, "running");
  await expect(page.getByTestId("trial-run-status")).toHaveText("Running");
  scenario.setStatus(runId, "completed");
  const accept = page.getByRole("button", { name: "Accept this trial" });
  await expect(accept).toBeVisible({ timeout: 15_000 });
  await accept.click();
  await expect(page.getByText("Trial accepted")).toBeVisible();
}

test.beforeEach(async ({ page }) => {
  await watchRuntimeErrors(page);
  await mockInputs(page);
  await mockTrialDefaults(page);
});

test.afterEach(async ({ page }) => {
  const allowed = [...(allowedRuntimeErrors.get(page) ?? [])];
  const unexpected = (runtimeErrors.get(page) ?? []).filter((message) => {
    const match = allowed.findIndex((pattern) => pattern.test(message));
    if (match < 0) return true;
    allowed.splice(match, 1);
    return false;
  });
  expect(unexpected).toEqual([]);
});

test("selects an original video, advances, and restores after refresh", async ({
  page,
}) => {
  const mutationRequests: string[] = [];
  page.on("request", (request) => {
    if (request.url().includes("/api/") && request.method() !== "GET") {
      mutationRequests.push(`${request.method()} ${request.url()}`);
    }
  });

  await page.goto("/production");
  await expect(
    page.getByRole("heading", { name: "Choose the original video" }),
  ).toBeVisible();
  await expect(page.locator("nav").getByText("Match production")).toHaveCount(
    0,
  );

  await page.getByLabel("Original video").selectOption("data/match-a.mp4");
  await page.getByRole("button", { name: "Next" }).click();
  await expect(
    page.getByRole("heading", { name: "Field calibration" }),
  ).toBeVisible();
  await expect(page.getByText("Original video selected")).toBeVisible();

  await page.reload();
  await expect(
    page.getByText("Your unfinished production was restored."),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Field calibration" }),
  ).toBeVisible();
  expect(
    mutationRequests.every((request) =>
      request.includes("/api/inputs/field-preview"),
    ),
  ).toBe(true);
});

test("requires confirmation before starting over and has no serious accessibility findings", async ({
  page,
}) => {
  await page.goto("/production");
  await page.getByLabel("Original video").selectOption("data/match-a.mp4");
  const startNewButton = page.getByRole("button", {
    name: "Start new production",
  });
  const alertDialog = page.getByRole("alertdialog");
  await startNewButton.click();
  await expect(alertDialog).toBeVisible();
  await page.getByRole("button", { name: "Keep current production" }).click();
  await expect(alertDialog).toHaveCount(0);
  await expect(page.locator("main")).not.toHaveAttribute("aria-hidden", "true");
  await expect(startNewButton).toBeFocused();

  const results = await new AxeBuilder({ page })
    .include("main")
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    .analyze();
  const seriousFindings = results.violations.filter(
    (violation) =>
      violation.impact === "critical" || violation.impact === "serious",
  );
  expect(seriousFindings).toEqual([]);
});

test("renders the foundation flow in Chinese", async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("app-language", "zh"));
  await page.goto("/production");
  await expect(page.getByRole("heading", { name: "选择原片" })).toBeVisible();
  await expect(page.getByText("步骤 1/5 · 原片")).toBeVisible();
});

test("keeps production usable when the localStorage property is blocked", async ({
  page,
}) => {
  await page.addInitScript(() => {
    Object.defineProperty(window, "localStorage", {
      configurable: true,
      get() {
        throw new DOMException("blocked", "SecurityError");
      },
    });
  });

  await page.goto("/production");
  await expect(
    page.getByRole("heading", { name: "Choose the original video" }),
  ).toBeVisible();
  await expect(
    page.getByText(/changes are kept only for this browser session/i),
  ).toBeVisible();
  await page.getByLabel("Original video").selectOption("data/match-a.mp4");
  await expect(page.getByRole("button", { name: "Next" })).toBeEnabled();
});

test("restores the session draft after save and exit when storage is read-only", async ({
  page,
}) => {
  await page.addInitScript(() => {
    Storage.prototype.setItem = () => {
      throw new DOMException("read only", "SecurityError");
    };
  });

  await page.goto("/production");
  expect(await page.evaluate(() => localStorage.getItem("missing"))).toBeNull();

  const languageToggle = page.getByTestId("button-toggle-language").first();
  await languageToggle.click();
  await expect(page.getByRole("heading", { name: "选择原片" })).toBeVisible();
  await languageToggle.click();
  await expect(
    page.getByRole("heading", { name: "Choose the original video" }),
  ).toBeVisible();

  const themeToggle = page.getByTestId("button-toggle-theme").first();
  const wasDark = await page
    .locator("html")
    .evaluate((element) => element.classList.contains("dark"));
  await themeToggle.click();
  await expect
    .poll(() =>
      page
        .locator("html")
        .evaluate((element) => element.classList.contains("dark")),
    )
    .toBe(!wasDark);

  await page.getByLabel("Original video").selectOption("data/match-a.mp4");
  await expect(
    page.getByText(/changes are kept only for this browser session/i),
  ).toBeVisible();
  await page.getByRole("button", { name: "Save and exit" }).click();
  await expect(page).toHaveURL(/\/$/);

  await page.goBack();
  await expect(page).toHaveURL(/\/production$/);
  await expect(
    page.getByRole("heading", { name: "Field calibration" }),
  ).toBeVisible();
  await expect(page.getByText("data/match-a.mp4").first()).toBeVisible();
  await expect(
    page.getByText(/changes are kept only for this browser session/i),
  ).toBeVisible();
});

test("edits a real Konva polygon by click and drag, then deletes, undoes, and clears", async ({
  page,
}) => {
  await openCalibration(page);
  await page.getByRole("button", { name: "Request system suggestion" }).click();
  await expect(page.getByTestId("suggested-coordinates")).toContainText(
    "(100, 100)",
  );
  await expect(page.getByTestId("approved-coordinates")).toHaveText("—");
  await page.getByRole("button", { name: "Use this suggestion" }).click();
  await expect(page.getByTestId("approved-coordinates")).toContainText(
    "4. (100, 1000)",
  );

  const canvas = page
    .getByTestId("field-polygon-editor")
    .locator("canvas")
    .first();
  const box = await canvas.boundingBox();
  expect(box).not.toBeNull();
  if (!box) return;

  await canvas.click({ position: { x: box.width * 0.5, y: box.height * 0.5 } });
  await expect(page.getByTestId("approved-coordinates")).toContainText("5.");

  const draggedX = box.width * 0.5;
  const draggedY = (538 / 1080) * box.height;
  await page.mouse.move(box.x + draggedX, box.y + draggedY);
  await page.mouse.down();
  await page.mouse.move(box.x + draggedX + 45, box.y + draggedY + 30, {
    steps: 5,
  });
  await page.mouse.up();
  await expect(page.getByTestId("approved-coordinates")).not.toContainText(
    "5. (960, 538)",
  );

  await page.getByRole("button", { name: "Delete point 1" }).click();
  await expect(page.getByTestId("approved-coordinates")).not.toContainText(
    "5.",
  );
  await page.getByRole("button", { name: "Undo" }).click();
  await expect(page.getByTestId("approved-coordinates")).toContainText("5.");
  await page.getByRole("button", { name: "Clear" }).click();
  await expect(page.getByTestId("approved-coordinates")).toHaveText("—");
});

test("supports keyboard coordinates and completes three distinct frame confirmations", async ({
  page,
}) => {
  await openCalibration(page);
  for (let index = 0; index < 2; index += 1) {
    await page.getByRole("button", { name: "Add point" }).click();
  }
  await expect(page.getByText("Add at least three points.")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Confirm this frame" }),
  ).toBeDisabled();
  await page.getByRole("button", { name: "Add point" }).click();
  const pointOneX = page.getByLabel("Point 1 X coordinate");
  await pointOneX.fill("1920");
  await pointOneX.press("Enter");
  await expect(pointOneX).toHaveAttribute("aria-invalid", "true");
  await expect(
    page.getByRole("button", { name: "Confirm this frame" }),
  ).toBeDisabled();
  await pointOneX.fill("120");
  await pointOneX.press("Enter");
  await expect(pointOneX).toHaveAttribute("aria-invalid", "false");
  await expect(page.getByTestId("approved-coordinates")).toContainText(
    "1. (120,",
  );

  await expect(
    page.getByRole("button", { name: "Confirm this frame" }),
  ).toBeEnabled();
  await page.getByRole("button", { name: "Confirm this frame" }).click();
  await page.getByRole("button", { name: "Next frame" }).click();
  await expect(page.getByTestId("calibration-frame-meta")).toContainText(
    "source frame 20",
  );
  await expect(
    page.getByRole("button", { name: "Confirm this frame" }),
  ).toBeEnabled();
  await page.getByRole("button", { name: "Confirm this frame" }).click();
  await page.getByRole("button", { name: "Next frame" }).click();
  await expect(page.getByTestId("calibration-frame-meta")).toContainText(
    "source frame 30",
  );
  await page.getByRole("button", { name: "Confirm this frame" }).click();

  await expect(page.getByText("3 frames confirmed")).toBeVisible();
  await expect(page.getByRole("button", { name: /^Next$/ })).toBeEnabled();
  await expect(page.locator(".konvajs-content")).toHaveCount(1);
  await page.getByRole("button", { name: /^Next$/ }).click();
  await expect(
    page.getByRole("heading", { name: "Trial and tuning" }),
  ).toBeVisible();
  await expect(page.locator(".konvajs-content")).toHaveCount(0);
});

test("runs a bounded trial, reads evidence, explicitly accepts, freezes config, and enables Next", async ({
  page,
}, testInfo) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const scenario = await installTrialScenario(page);
  await openTrialFromDraft(page);
  await finishTrialForAcceptance(page, scenario);

  expect(scenario.createBodies[0]).toMatchObject({
    config_name: "default.yaml",
    input_video: inputCatalog.videos[0].path,
    parent_run_id: null,
    start_frame: 0,
    max_frames: 300,
    enable_postprocess: true,
    enable_follow_cam: true,
    pipeline_mode: "standard",
    config_patch: {
      input_video: inputCatalog.videos[0].path,
      filtering: { roi: [100, 100, 1800, 1000] },
      runtime: { start_frame: 0, max_frames: 300 },
    },
  });
  const trialRunId = scenario.runId();
  expect(trialRunId).toBe(scenario.createBodies[0].output_dir_name);
  const materializedTrialConfigName = backendMaterializedRunConfigName(
    scenario.createBodies[0].config_name,
    trialRunId,
  );
  expect(scenario.runs[0]).toMatchObject({
    run_id: trialRunId,
    config_name: materializedTrialConfigName,
    config_path: `configs/${materializedTrialConfigName}`,
  });
  await page.getByRole("button", { name: "Confirm configuration" }).click();
  await expect.poll(() => scenario.deriveBodies.length).toBe(1);
  await expect(page.getByText("Configuration snapshot verified")).toBeVisible();
  const next = page.getByRole("button", { name: /^Next$/ });
  await expect(next).toBeEnabled();
  const derive = scenario.deriveBodies[0];
  expect(derive.output_name).toMatch(
    /^production_workflow-completed-calibration_[0-9a-f-]+\.yaml$/,
  );
  const canonicalConfigName = `generated/${String(derive.output_name)}`;
  await expect
    .poll(() => scenario.configGetNames)
    .toContain(canonicalConfigName);
  expect(scenario.configGetNames).not.toContain(derive.output_name);
  await expect(page.getByText(canonicalConfigName)).toBeVisible();
  expect(derive.patch).toMatchObject({
    input_video: inputCatalog.videos[0].path,
    runtime: { start_frame: 0, max_frames: null },
    follow_cam: { enabled: false },
    metadata: {
      production_workflow: {
        workflow_id: "workflow-completed-calibration",
        accepted_trial_run_id: scenario.runId(),
      },
    },
  });
  await testInfo.attach("trial-config-verified-1440", {
    body: await page.screenshot(),
    contentType: "image/png",
  });
  await next.click();
  await expect(
    page.getByRole("heading", { name: "Full tracking and review" }),
  ).toBeVisible();

  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByTestId("completed-stage-trial")).toBeVisible();
  const results = await new AxeBuilder({ page })
    .include("main")
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    .analyze();
  expect(
    results.violations.filter(
      (violation) =>
        violation.impact === "critical" || violation.impact === "serious",
    ),
  ).toEqual([]);
});

test("keeps the trial workspace responsive, keyboard reachable, and accessible", async ({
  page,
}, testInfo) => {
  await installTrialScenario(page, { omitVideo: true });
  await openTrialFromDraft(page);

  for (const viewport of [
    { width: 1440, height: 900 },
    { width: 1280, height: 720 },
    { width: 390, height: 844 },
  ]) {
    await page.setViewportSize(viewport);
    await expect(page.getByTestId("production-trial-step")).toBeVisible();
    await expect
      .poll(() =>
        page.evaluate(
          () =>
            document.documentElement.scrollWidth <=
            document.documentElement.clientWidth,
        ),
      )
      .toBe(true);

    const baseConfig = page.getByLabel("Base configuration");
    const startFrame = page.getByLabel("Start frame");
    await baseConfig.focus();
    await expect(baseConfig).toBeFocused();
    await page.keyboard.press("Tab");
    await expect(startFrame).toBeFocused();
    await page.keyboard.press("Shift+Tab");
    await expect(baseConfig).toBeFocused();

    const results = await new AxeBuilder({ page })
      .include("main")
      .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
      .analyze();
    expect(
      results.violations.filter(
        (violation) =>
          violation.impact === "critical" || violation.impact === "serious",
      ),
    ).toEqual([]);
    await testInfo.attach(
      `trial-workspace-${viewport.width}x${viewport.height}`,
      {
        body: await page.screenshot(),
        contentType: "image/png",
      },
    );
  }
});

test("invalidates downstream evidence when the source changes and restores focus", async ({
  page,
}) => {
  const scenario = await installTrialScenario(page, { omitVideo: true });
  await openTrialFromDraft(page);
  await page.getByRole("button", { name: "Start bounded trial" }).click();
  await expect.poll(() => scenario.createBodies.length).toBe(1);
  scenario.setStatus(scenario.runId(), "failed");
  await expect(page.getByTestId("trial-run-status")).toHaveText("Failed");

  const changedSource = {
    ...inputCatalog.videos[0],
    size_bytes: 2_048,
    modified_at: "2026-07-15T14:00:00Z",
  };
  await page.unroute("**/api/inputs");
  await page.route("**/api/inputs", async (route) => {
    await route.fulfill({
      json: { ...inputCatalog, videos: [changedSource] },
    });
  });
  await page.reload();

  await expect(
    page.getByRole("heading", { name: "Choose the original video" }),
  ).toBeVisible();
  const sourceSelect = page.getByTestId("production-source-select");
  const useCurrentSource = page.getByRole("button", {
    name: "Use current file and reset downstream",
  });
  await useCurrentSource.click();
  await expect(page.getByRole("alertdialog")).toBeVisible();
  await page.getByRole("button", { name: "Keep current evidence" }).click();
  await expect(page.getByRole("alertdialog")).toHaveCount(0);
  await expect(sourceSelect).toBeFocused();

  await useCurrentSource.click();
  await page.getByRole("button", { name: "Invalidate and edit" }).click();
  await expect(page.getByRole("alertdialog")).toHaveCount(0);
  await expect(sourceSelect).toBeFocused();
  await expect(sourceSelect).toHaveValue(changedSource.path);
  await expect
    .poll(() =>
      page.evaluate(() => {
        const raw = localStorage.getItem(
          "football-tracking.production-draft.v1",
        );
        return raw ? JSON.parse(raw) : null;
      }),
    )
    .toMatchObject({
      source: {
        path: changedSource.path,
        size_bytes: changedSource.size_bytes,
        modified_at: changedSource.modified_at,
      },
      calibration: null,
      trial: null,
      pending_config_confirmation: null,
      confirmed_config: null,
    });
});

test("discards a delayed configuration response after trial evidence is invalidated", async ({
  page,
}) => {
  const scenario = await installTrialScenario(page, { deferDerive: true });
  await openTrialFromDraft(page);
  await finishTrialForAcceptance(page, scenario);

  await page.getByRole("button", { name: "Confirm configuration" }).click();
  await expect.poll(() => scenario.deriveBodies.length).toBe(1);
  await page.getByRole("button", { name: "Unlock trial settings" }).click();
  await page.getByRole("button", { name: "Unlock and invalidate" }).click();
  await expect(page.getByRole("alertdialog")).toHaveCount(0);
  await expect
    .poll(() =>
      page.evaluate(() => {
        const raw = localStorage.getItem(
          "football-tracking.production-draft.v1",
        );
        if (!raw) return null;
        const draft = JSON.parse(raw);
        return {
          accepted: draft.trial?.accepted ?? null,
          pending: draft.pending_config_confirmation ?? null,
          confirmed: draft.confirmed_config ?? null,
        };
      }),
    )
    .toEqual({ accepted: null, pending: null, confirmed: null });

  scenario.releaseDerive();
  await expect(page.getByText("Configuration snapshot verified")).toHaveCount(
    0,
  );
  await expect.poll(() => scenario.configGetNames.length).toBe(0);
  await expect(
    page.getByRole("button", { name: "Start bounded trial" }),
  ).toBeVisible();
});

test("does not duplicate a trial on double click or reload while active", async ({
  page,
}) => {
  const scenario = await installTrialScenario(page);
  await openTrialFromDraft(page);
  await page
    .getByRole("button", { name: "Start bounded trial" })
    .evaluate((button: HTMLButtonElement) => {
      button.click();
      button.click();
    });
  await expect.poll(() => scenario.createBodies.length).toBe(1);
  const runId = scenario.runId();
  expect(runId).toBe(scenario.createBodies[0].output_dir_name);
  scenario.setStatus(runId, "running");
  await expect(page.getByTestId("trial-run-status")).toHaveText("Running");
  await page.reload();
  await expect(page.getByTestId("trial-run-status")).toHaveText("Running");
  expect(scenario.createBodies).toHaveLength(1);
  await page.getByRole("button", { name: "Cancel trial" }).click();
  await expect.poll(() => scenario.cancelIds).toEqual([runId]);
  await expect(page.getByTestId("trial-run-status")).toHaveText("Stopped");
});

test("reconciles a lost create response after reload without another POST", async ({
  page,
}) => {
  const scenario = await installTrialScenario(page, {
    loseCreateResponseOnce: true,
    omitVideo: true,
  });
  allowRuntimeError(
    page,
    /^console\.error: Failed to load resource: net::ERR_CONNECTION_RESET$/,
  );
  await openTrialFromDraft(page);
  await page.getByRole("button", { name: "Start bounded trial" }).click();
  await expect.poll(() => scenario.createBodies.length).toBe(1);

  const runId = scenario.runId();
  const expectedConfigName = backendMaterializedRunConfigName(
    scenario.createBodies[0].config_name,
    runId,
  );
  expect(scenario.runs[0]).toMatchObject({
    run_id: runId,
    source: "api",
    config_name: expectedConfigName,
    input_video: scenario.createBodies[0].input_video,
    parent_run_id: scenario.createBodies[0].parent_run_id,
    modules_enabled: {
      postprocess: scenario.createBodies[0].enable_postprocess,
      follow_cam: scenario.createBodies[0].enable_follow_cam,
    },
    notes: scenario.createBodies[0].notes,
  });
  await expect(
    page
      .getByRole("paragraph")
      .filter({ hasText: /previous submission result is not confirmed/i }),
  ).toBeVisible();
  await expect
    .poll(() =>
      page.evaluate(() => {
        const raw = localStorage.getItem(
          "football-tracking.production-draft.v1",
        );
        return raw ? (JSON.parse(raw).trial?.pending_submission ?? null) : null;
      }),
    )
    .not.toBeNull();

  await page.reload();
  await expect(page.getByTestId("trial-run-status")).toHaveText("Queued");
  await expect
    .poll(() =>
      page.evaluate(() => {
        const raw = localStorage.getItem(
          "football-tracking.production-draft.v1",
        );
        if (!raw) return null;
        const trial = JSON.parse(raw).trial;
        return {
          pending: trial?.pending_submission ?? null,
          run_ids: (trial?.attempts ?? []).map(
            (attempt: { run_id: string }) => attempt.run_id,
          ),
        };
      }),
    )
    .toEqual({ pending: null, run_ids: [runId] });
  expect(scenario.createBodies).toHaveLength(1);
});

test("tunes after a failed trial, accepts the successful child, and preserves lineage", async ({
  page,
}) => {
  const scenario = await installTrialScenario(page);
  await openTrialFromDraft(page);
  await page.getByRole("button", { name: "Start bounded trial" }).click();
  await expect.poll(() => scenario.createBodies.length).toBe(1);
  const firstRunId = scenario.runId();
  scenario.setStatus(firstRunId, "failed");
  await expect(page.getByTestId("trial-run-status")).toHaveText("Failed");
  await page.getByLabel("Frame count").fill("120");
  await page.getByRole("button", { name: "Retry as a new trial" }).click();
  await expect.poll(() => scenario.createBodies.length).toBe(2);
  expect(scenario.createBodies[1]).toMatchObject({
    parent_run_id: firstRunId,
    max_frames: 120,
  });
  const secondRunId = scenario.runId(1);
  scenario.setStatus(secondRunId, "running");
  await expect(page.getByTestId("trial-run-status")).toHaveText("Running");
  scenario.setStatus(secondRunId, "completed");
  await expect(page.getByTestId("trial-evidence-ready")).toBeAttached({
    timeout: 15_000,
  });
  await page.getByRole("button", { name: "Accept this trial" }).click();
  await expect(page.getByText("Trial accepted")).toBeVisible();
  const attempts = page.getByRole("listitem");
  await expect(attempts.nth(0)).toContainText(firstRunId);
  await expect(attempts.nth(0)).toContainText("Failed");
  await expect(attempts.nth(1)).toContainText(secondRunId);
  await expect(attempts.nth(1)).toContainText(`Parent: ${firstRunId}`);
  await expect(attempts.nth(1)).toContainText("Completed");
});

test("shows the Chinese trial journey and preserves retry lineage", async ({
  page,
}) => {
  await page.addInitScript(() => localStorage.setItem("app-language", "zh"));
  const scenario = await installTrialScenario(page, { omitVideo: true });
  await openTrialFromDraft(page, "zh");
  await expect(page.getByText("试跑记录")).toHaveCount(0);

  await page.getByRole("button", { name: "开始有限试跑" }).click();
  await expect.poll(() => scenario.createBodies.length).toBe(1);
  const firstRunId = scenario.runId();
  scenario.setStatus(firstRunId, "failed");
  await expect(page.getByTestId("trial-run-status")).toHaveText("失败");
  await page.getByLabel("试跑帧数").fill("150");
  await page.getByRole("button", { name: "新建一次重试" }).click();
  await expect.poll(() => scenario.createBodies.length).toBe(2);
  expect(scenario.createBodies[1]).toMatchObject({
    parent_run_id: firstRunId,
    max_frames: 150,
  });
  await expect(page.getByText("试跑记录")).toBeVisible();
  await expect(page.getByText(`上一次试跑: ${firstRunId}`)).toBeVisible();
});

test("blocks missing evidence and preserves the draft for active-run conflicts", async ({
  page,
}) => {
  const missing = await installTrialScenario(page, {
    missingArtifact: "ball_audit.json",
    omitVideo: true,
  });
  await openTrialFromDraft(page);
  await page.getByRole("button", { name: "Start bounded trial" }).click();
  await expect.poll(() => missing.createBodies.length).toBe(1);
  missing.setStatus(missing.runId(), "completed");
  await expect(
    page.getByText("Required artifact is unavailable: ball_audit.json."),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Accept this trial" }),
  ).toHaveCount(0);

  await page.evaluate(() =>
    localStorage.removeItem("football-tracking.production-draft.v1"),
  );
  await page.reload();
  const conflict = await installTrialScenario(page, {
    activeRunId: "occupying-run",
  });
  await openTrialFromDraft(page);
  await page.getByRole("button", { name: "Start bounded trial" }).click();
  await expect(page.getByText(/occupying-run/)).toBeVisible();
  expect(conflict.createBodies).toHaveLength(0);
  await expect(page.getByLabel("Frame count")).toHaveValue("300");
});

test("detects config tampering and deletion, then re-confirms with a new UUID", async ({
  page,
}) => {
  const scenario = await installTrialScenario(page);
  await openTrialFromDraft(page);
  await finishTrialForAcceptance(page, scenario);
  await page.getByRole("button", { name: "Confirm configuration" }).click();
  await expect.poll(() => scenario.deriveBodies.length).toBe(1);
  await expect(page.getByText("Configuration snapshot verified")).toBeVisible();
  const firstName = scenario.deriveBodies[0].output_name;

  scenario.setConfigMode("tampered");
  await page.reload();
  await expect(
    page.getByText("The confirmed configuration text was modified."),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: /^Next$/ })).toBeDisabled();
  await page
    .getByRole("button", { name: "Re-confirm with a new snapshot" })
    .click();
  await expect.poll(() => scenario.deriveBodies.length).toBe(2);
  expect(scenario.deriveBodies[1].output_name).not.toBe(firstName);
  await expect(page.getByText("Configuration snapshot verified")).toBeVisible();

  scenario.setConfigMode("missing");
  // A missing canonical snapshot is deliberately represented by one 404.
  allowRuntimeError(
    page,
    /^console\.error: Failed to load resource: the server responded with a status of 404 \(Not Found\)$/,
  );
  await page.reload();
  await expect(
    page.getByText("The confirmed configuration was deleted."),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: /^Next$/ })).toBeDisabled();

  scenario.setConfigMode("ok");
  await page
    .getByRole("button", { name: "Re-confirm with a new snapshot" })
    .click();
  await expect.poll(() => scenario.deriveBodies.length).toBe(3);
  const thirdName = scenario.deriveBodies[2].output_name;
  expect(thirdName).not.toBe(firstName);
  expect(thirdName).not.toBe(scenario.deriveBodies[1].output_name);
  await expect(page.getByText("Configuration snapshot verified")).toBeVisible();
});

test("restores approved calibration and suggestion without persisting preview image data", async ({
  page,
}) => {
  await openCalibration(page);
  await page.getByRole("button", { name: "Request system suggestion" }).click();
  await page.getByRole("button", { name: "Use this suggestion" }).click();
  await expect
    .poll(() =>
      page.evaluate(() => {
        const raw = localStorage.getItem(
          "football-tracking.production-draft.v1",
        );
        return raw ? JSON.parse(raw).calibration?.polygon_digest : null;
      }),
    )
    .toMatch(/^[a-f\d]{64}$/);
  const raw = await page.evaluate(() =>
    localStorage.getItem("football-tracking.production-draft.v1"),
  );
  expect(raw).not.toContain("preview_data_url");

  await page.reload();
  await expect(page.getByTestId("approved-coordinates")).toContainText(
    "(100, 100)",
  );
  await expect(page.getByTestId("suggested-coordinates")).toContainText(
    "(100, 100)",
  );
  await expect(page.getByAltText("Original source frame 10")).toBeVisible();
});

test("blocks Next for unresolved restored coordinates and requires three fresh confirmations after an edit", async ({
  page,
}) => {
  await page.addInitScript((draft) => {
    localStorage.setItem(
      "football-tracking.production-draft.v1",
      JSON.stringify(draft),
    );
  }, draftWithCompletedCalibration());
  await page.goto("/production");
  await expect(
    page.getByRole("heading", { name: "Trial and tuning" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Back" }).click();
  await expect(
    page.getByRole("heading", { name: "Field calibration" }),
  ).toBeVisible();
  await expect(page.getByTestId("field-polygon-editor")).toBeVisible();

  const workspaceNext = page.getByRole("button", { name: /^Next$/ });
  await expect(workspaceNext).toBeEnabled();
  const pointOneX = page.getByLabel("Point 1 X coordinate");

  await pointOneX.fill("100.0");
  await expect(pointOneX).toHaveAttribute("aria-invalid", "false");
  await expect(workspaceNext).toBeDisabled();
  await pointOneX.press("Enter");
  await expect(pointOneX).toHaveValue("100");
  await expect(workspaceNext).toBeEnabled();
  const persistedAfterEquivalentCommit = await page.evaluate(() => {
    const raw = localStorage.getItem("football-tracking.production-draft.v1");
    return raw ? JSON.parse(raw).calibration : null;
  });
  expect(persistedAfterEquivalentCommit).toEqual(
    draftWithCompletedCalibration().calibration,
  );

  await pointOneX.fill("1920");
  await pointOneX.press("Enter");
  await expect(pointOneX).toHaveAttribute("aria-invalid", "true");
  await expect(
    page.getByText(/enter a value from 0 through 1919/i),
  ).toBeVisible();
  await expect(workspaceNext).toBeDisabled();
  const persistedWhileInvalid = await page.evaluate(() => {
    const raw = localStorage.getItem("football-tracking.production-draft.v1");
    const draft = raw ? JSON.parse(raw) : null;
    return {
      point: draft?.calibration?.approved_polygon?.[0],
      frames: draft?.calibration?.confirmed_frames?.length,
    };
  });
  expect(persistedWhileInvalid).toEqual({ point: [100, 100], frames: 3 });

  await pointOneX.fill("120");
  await expect(pointOneX).toHaveAttribute("aria-invalid", "false");
  await expect(workspaceNext).toBeDisabled();
  await pointOneX.press("Enter");
  await expect(page.getByTestId("approved-coordinates")).toContainText(
    "1. (120, 100)",
  );
  await expect(page.getByText("0 frames confirmed")).toBeVisible();
  await expect(workspaceNext).toBeDisabled();
  await expect
    .poll(() =>
      page.evaluate(() => {
        const raw = localStorage.getItem(
          "football-tracking.production-draft.v1",
        );
        return raw
          ? JSON.parse(raw).calibration?.confirmed_frames?.length
          : null;
      }),
    )
    .toBe(0);

  for (const frameIndex of [10, 20, 30]) {
    await expect(page.getByTestId("calibration-frame-meta")).toContainText(
      `source frame ${frameIndex}`,
    );
    const confirm = page.getByRole("button", { name: "Confirm this frame" });
    await expect(confirm).toBeEnabled();
    await confirm.click();
    if (frameIndex < 30) {
      await page.getByRole("button", { name: "Next frame" }).click();
    }
  }
  await expect(page.getByText("3 frames confirmed")).toBeVisible();
  await expect(workspaceNext).toBeEnabled();
});

test("maps real Chromium canvas coordinates at device scale factor 2", async ({
  browser,
}) => {
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 2,
  });
  const page = await context.newPage();
  try {
    await mockInputs(page);
    await openCalibration(page);
    expect(await page.evaluate(() => window.devicePixelRatio)).toBe(2);
    const canvas = page
      .getByTestId("field-polygon-editor")
      .locator("canvas")
      .first();
    const box = await canvas.boundingBox();
    expect(box).not.toBeNull();
    if (!box) return;

    const sourcePoint: [number, number] = [640, 360];
    const position = sourcePointOnCanvas(box, sourcePoint, {
      width: 1920,
      height: 1080,
    });
    const clickPosition = { x: position.x + 0.25, y: position.y + 0.25 };
    const expectedSourcePoint = chromiumMouseSourcePoint(box, clickPosition, {
      width: 1920,
      height: 1080,
    });
    await canvas.click({
      position: clickPosition,
    });
    await expect(page.getByTestId("approved-coordinates")).toHaveText(
      `1. (${expectedSourcePoint[0]}, ${expectedSourcePoint[1]})`,
    );
    expect(
      Math.abs(expectedSourcePoint[0] - sourcePoint[0]),
    ).toBeLessThanOrEqual(1);
    expect(
      Math.abs(expectedSourcePoint[1] - sourcePoint[1]),
    ).toBeLessThanOrEqual(1);
  } finally {
    await context.close();
  }
});

test("maps a letterboxed square source to and from display coordinates", async ({
  page,
}) => {
  await page.unroute("**/api/inputs/field-preview");
  await page.route("**/api/inputs/field-preview", async (route) => {
    await route.fulfill({
      json: {
        input_video: inputCatalog.videos[0].path,
        preview_data_url: `data:image/svg+xml;base64,${Buffer.from(squarePreviewSvg).toString("base64")}`,
        frame_width: 1000,
        frame_height: 1000,
        frame_index: 10,
        frame_time_seconds: 0.4,
        sample_index: 1,
        sample_count: 3,
      },
    });
  });
  await openCalibration(page);
  const canvas = page
    .getByTestId("field-polygon-editor")
    .locator("canvas")
    .first();
  const box = await canvas.boundingBox();
  expect(box).not.toBeNull();
  if (!box) return;

  await canvas.click({ position: { x: 5, y: box.height / 2 } });
  await expect(page.getByTestId("approved-coordinates")).toHaveText("—");

  const requested: [number, number] = [250, 750];
  const clickPosition = sourcePointOnCanvas(box, requested, {
    width: 1000,
    height: 1000,
  });
  const biasedClickPosition = clickPosition;
  const expectedSourcePoint = chromiumMouseSourcePoint(
    box,
    biasedClickPosition,
    { width: 1000, height: 1000 },
  );
  await canvas.click({ position: biasedClickPosition });
  await expect(page.getByTestId("approved-coordinates")).toHaveText(
    `1. (${expectedSourcePoint[0]}, ${expectedSourcePoint[1]})`,
  );
  const roundTrip = sourcePointOnCanvas(
    box,
    [
      Number(await page.getByLabel("Point 1 X coordinate").inputValue()),
      Number(await page.getByLabel("Point 1 Y coordinate").inputValue()),
    ],
    {
      width: 1000,
      height: 1000,
    },
  );
  expect(
    Math.hypot(
      roundTrip.x - biasedClickPosition.x,
      roundTrip.y - biasedClickPosition.y,
    ),
  ).toBeLessThanOrEqual(1);
});

test("keeps confirmation disabled until preview, image, and approved overlay are ready", async ({
  page,
}) => {
  let releasePreview!: () => void;
  let releaseImage!: () => void;
  let releaseOverlay!: () => void;
  const previewGate = new Promise<void>((resolve) => {
    releasePreview = resolve;
  });
  const imageGate = new Promise<void>((resolve) => {
    releaseImage = resolve;
  });
  const overlayGate = new Promise<void>((resolve) => {
    releaseOverlay = resolve;
  });
  await page.addInitScript((draft) => {
    localStorage.setItem(
      "football-tracking.production-draft.v1",
      JSON.stringify(draft),
    );
  }, draftWithApprovedPolygon());
  await page.unroute("**/api/inputs/field-preview");
  await page.route("**/api/inputs/field-preview", async (route) => {
    await previewGate;
    await route.fulfill({
      json: {
        input_video: inputCatalog.videos[0].path,
        preview_data_url: "http://127.0.0.1:5173/e2e-preview.svg",
        frame_width: 1920,
        frame_height: 1080,
        frame_index: 10,
        frame_time_seconds: 0.4,
        sample_index: 1,
        sample_count: 3,
      },
    });
  });
  await page.route("**/e2e-preview.svg", async (route) => {
    await imageGate;
    await route.fulfill({
      contentType: "image/svg+xml",
      body: squarePreviewSvg,
    });
  });
  await page.route(
    "**/src/components/production/FieldPolygonEditor.tsx*",
    async (route) => {
      await overlayGate;
      await route.continue();
    },
  );

  await page.goto("/production");
  const confirm = page.getByRole("button", { name: "Confirm this frame" });
  await expect(confirm).toBeDisabled();
  await expect(page.getByAltText("Original source frame 10")).toHaveCount(0);

  releasePreview();
  const image = page.getByAltText("Original source frame 10");
  await expect(image).toBeVisible();
  await expect
    .poll(() => image.evaluate((element: HTMLImageElement) => element.complete))
    .toBe(false);
  await expect(confirm).toBeDisabled();

  releaseImage();
  await expect
    .poll(() =>
      image.evaluate(
        (element: HTMLImageElement) =>
          element.complete && element.naturalWidth > 0,
      ),
    )
    .toBe(true);
  await expect(page.getByTestId("field-polygon-editor")).toHaveCount(0);
  await expect(confirm).toBeDisabled();

  releaseOverlay();
  await expect(page.getByTestId("field-polygon-editor")).toBeVisible();
  await expect(confirm).toBeEnabled();
});

test("keeps the overlay aligned at desktop and mobile sizes with accessible controls", async ({
  page,
}, testInfo) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await openCalibration(page);
  await page.getByRole("button", { name: "Request system suggestion" }).click();
  await expect(page.getByTestId("suggested-coordinates")).toContainText(
    "4. (100, 1000)",
  );
  await expect(page.getByTestId("approved-coordinates")).toHaveText("—");
  await testInfo.attach("calibration-1440-suggested", {
    body: await page.screenshot(),
    contentType: "image/png",
  });

  await page.getByRole("button", { name: "Use this suggestion" }).click();
  const pointOneX = page.getByLabel("Point 1 X coordinate");
  await pointOneX.fill("120");
  await pointOneX.press("Enter");
  await expect(page.getByTestId("approved-coordinates")).toContainText(
    "1. (120, 100)",
  );
  await expect(
    page.getByRole("button", { name: "Confirm this frame" }),
  ).toBeEnabled();
  await testInfo.attach("calibration-1440-editing", {
    body: await page.screenshot(),
    contentType: "image/png",
  });

  await page.getByRole("button", { name: "Confirm this frame" }).click();
  await expect(page.getByText("1 frames confirmed")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Frame already confirmed" }),
  ).toBeDisabled();
  await testInfo.attach("calibration-1440-confirmed", {
    body: await page.screenshot(),
    contentType: "image/png",
  });

  await page.setViewportSize({ width: 390, height: 844 });
  await expect
    .poll(async () => {
      const canvasBox = await page
        .getByTestId("field-polygon-editor")
        .locator("canvas")
        .first()
        .boundingBox();
      const containerBox = await page
        .getByTestId("field-polygon-editor")
        .boundingBox();
      return Boolean(
        canvasBox &&
        containerBox &&
        Math.abs(canvasBox.width - containerBox.width) <= 1 &&
        Math.abs(canvasBox.height - containerBox.height) <= 1,
      );
    })
    .toBe(true);
  const previewBox = await page
    .getByTestId("calibration-preview")
    .boundingBox();
  const imageBox = await page
    .getByAltText("Original source frame 10")
    .boundingBox();
  const editorBox = await page
    .getByTestId("field-polygon-editor")
    .boundingBox();
  expect(previewBox).not.toBeNull();
  expect(imageBox).not.toBeNull();
  expect(editorBox).not.toBeNull();
  expect(
    Math.abs((imageBox?.width ?? 0) - (editorBox?.width ?? 0)),
  ).toBeLessThanOrEqual(1);
  expect(
    Math.abs((imageBox?.height ?? 0) - (editorBox?.height ?? 0)),
  ).toBeLessThanOrEqual(1);
  expect(previewBox?.width ?? 0).toBeLessThanOrEqual(390);
  await expect(page.getByText("1 frames confirmed")).toBeVisible();
  await expect(page.getByTestId("approved-coordinates")).toContainText(
    "1. (120, 100)",
  );
  await testInfo.attach("calibration-mobile-confirmed", {
    body: await page.screenshot(),
    contentType: "image/png",
  });

  const mobileCanvas = page
    .getByTestId("field-polygon-editor")
    .locator("canvas")
    .first();
  const mobileBox = await mobileCanvas.boundingBox();
  expect(mobileBox).not.toBeNull();
  if (!mobileBox) return;
  const addedPoint: [number, number] = [960, 540];
  const addedPosition = sourcePointOnCanvas(mobileBox, addedPoint, {
    width: 1920,
    height: 1080,
  });
  const biasedAddedPosition = {
    x: addedPosition.x + 0.25,
    y: addedPosition.y + 0.25,
  };
  const expectedAddedPoint = chromiumMouseSourcePoint(
    mobileBox,
    biasedAddedPosition,
    { width: 1920, height: 1080 },
  );
  await mobileCanvas.click({ position: biasedAddedPosition });
  await expect(page.getByTestId("approved-coordinates")).toContainText(
    `5. (${expectedAddedPoint[0]}, ${expectedAddedPoint[1]})`,
  );

  const dragBox = await mobileCanvas.boundingBox();
  expect(dragBox).not.toBeNull();
  if (!dragBox) return;
  const dragStartPosition = sourcePointOnCanvas(dragBox, expectedAddedPoint, {
    width: 1920,
    height: 1080,
  });
  const draggedPoint: [number, number] = [1200, 650];
  const draggedPosition = sourcePointOnCanvas(dragBox, draggedPoint, {
    width: 1920,
    height: 1080,
  });
  const biasedDraggedPosition = {
    x: draggedPosition.x + 0.25,
    y: draggedPosition.y + 0.25,
  };
  const expectedDraggedPoint = chromiumMouseSourcePoint(
    dragBox,
    biasedDraggedPosition,
    { width: 1920, height: 1080 },
  );
  await page.mouse.move(
    dragBox.x + dragStartPosition.x,
    dragBox.y + dragStartPosition.y,
  );
  await page.mouse.down();
  await page.mouse.move(
    dragBox.x + biasedDraggedPosition.x,
    dragBox.y + biasedDraggedPosition.y,
    { steps: 6 },
  );
  await page.mouse.up();
  await expect(page.getByTestId("approved-coordinates")).toContainText(
    `5. (${expectedDraggedPoint[0]}, ${expectedDraggedPoint[1]})`,
  );

  const results = await new AxeBuilder({ page })
    .include("main")
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    .analyze();
  expect(
    results.violations.filter(
      (violation) =>
        violation.impact === "critical" || violation.impact === "serious",
    ),
  ).toEqual([]);
});

test("renders interactive calibration copy in Chinese", async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("app-language", "zh"));
  await page.goto("/production");
  await page.getByLabel("原片").selectOption("data/match-a.mp4");
  await page.getByRole("button", { name: "下一步" }).click();
  await expect(page.getByRole("heading", { name: "球场校准" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "系统建议" })).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "三帧校准确认" }),
  ).toBeVisible();
});
