import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page, type Route } from "@playwright/test";
import { createHash } from "node:crypto";
import type {
  ArtifactSummary,
  BroadcastOperationResponse,
  BroadcastRenderRequest,
  BroadcastReviewEvidenceImportRequest,
  BroadcastReviewEvidenceRevokeResponse,
  BroadcastReviewEvidenceStateResponse,
  BroadcastReviewActionsRequest,
  BroadcastReviewWindowsResponse,
  BroadcastTerminalTailReviewRequest,
  BroadcastTrajectoryRecomputeRequest,
  ConfigDetail,
  ConfigListItem,
  CreateRunRequest,
  HealthResponse,
  RunRecord,
  TrialSignalGateV2,
} from "@workspace/api-client-react";

import {
  buildProductionConfigConfirmation,
  expectedProductionConfigName,
  finalizeProductionConfigConfirmation,
} from "../src/lib/productionConfigFreeze";
import type { ProductionCalibrationDraft } from "../src/lib/productionCalibration";
import {
  DETECTOR_PROBE_PROFILE_IDS,
  detectorProbeCatalogFixture as strictDetectorProbeCatalogFixture,
  detectorProbeJobFixture as strictDetectorProbeJobFixture,
} from "../src/test/detectorProbeFixtures";
import {
  acceptProductionTrial,
  appendProductionTrialAttempt,
  buildProductionTrialSubmission,
  canonicalJson,
  createProductionTrialState,
  setPendingProductionTrial,
  sha256Text,
} from "../src/lib/productionTrial";
import {
  createProductionDraft,
  PRODUCTION_DRAFT_STORAGE_KEY,
  updateConfirmedProductionConfig,
  updateProductionCalibration,
  updateProductionSource,
  updateProductionTrial,
  type ProductionDraft,
} from "../src/lib/productionWorkflow";
import { PRODUCTION_BROADCAST_DELIVERY_ARTIFACTS } from "../src/lib/broadcastDelivery";

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
const trialAllLostTrackCsv = trialTrackCsv({
  detected: 0,
  predicted: 0,
  lost: 300,
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
    if (key === "GET /api/production-trials/tuning-schema") {
      await route.fulfill({
        json: {
          schema_version: "1.0",
          patch_schema_version: "1.0",
          controls: [
            {
              path: "detector.confidence_threshold",
              section: "detector",
              kind: "number",
              minimum: 0.01,
              maximum: 0.9,
              step: 0.01,
              runtime_impact: "low",
              description:
                "Minimum detector confidence used for this bounded trial.",
              description_zh: "本次有限试跑使用的最低检测置信度。",
            },
          ],
          actions: [
            {
              action_code: "return_to_field_setup",
              target_step: "field_setup",
              reason_code: "field_geometry_requires_new_calibration",
              affected_paths: [
                "filtering.roi",
                "scene_bias.ground_zones",
                "scene_bias.negative_rois",
              ],
              lineage_constraint:
                "invalidate_trial_and_downstream_then_create_new_calibration_version",
            },
          ],
        },
      });
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
  allLost?: boolean;
  classRejected?: boolean;
  missingDiagnosticBudgets?: boolean;
  conflictOnCreate?: boolean;
  deferDerive?: boolean;
  loseCreateResponseOnce?: boolean;
  missingArtifact?: string;
  corruptMetrics?: boolean;
  omitVideo?: boolean;
  legacyConfigDigest?: boolean;
  diagnosticContractFailure?: boolean;
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
  const deriveResponseNames: string[] = [];
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
  const noTrack = options.allLost || options.classRejected;
  const rawTrackCsv = noTrack ? trialAllLostTrackCsv : trialRawTrackCsv;
  const cleanedTrackCsv = noTrack ? trialAllLostTrackCsv : trialCleanedTrackCsv;
  const auditTrackletCount = noTrack ? 0 : 1;

  function trialSignalGate(runStatus: TrialRunStatus = "completed") {
    const collectedCount = (value: number) => ({
      value,
      status: "collected",
    });
    const observation = (value: number, collected = true) => ({
      status: collected ? "collected" : "not_collected",
      value: collected ? value : null,
    });
    const allLost = options.allLost === true;
    const classRejected = options.classRejected === true;
    const failed = runStatus === "failed";
    const diagnosticContractFailure =
      options.diagnosticContractFailure === true;
    const trackLost = allLost || classRejected;
    const diagnosticsCollected = !failed;
    const budgetCollected =
      diagnosticsCollected && !options.missingDiagnosticBudgets;
    const trackDiagnostics = (cleaned: boolean) => ({
      status: diagnosticsCollected ? "collected" : "not_collected",
      frame_count: observation(300, diagnosticsCollected),
      detected: observation(
        trackLost ? 0 : cleaned ? 210 : 200,
        diagnosticsCollected,
      ),
      predicted: observation(trackLost ? 0 : 50, diagnosticsCollected),
      lost: observation(
        trackLost ? 300 : cleaned ? 40 : 50,
        diagnosticsCollected,
      ),
      detected_ratio: observation(
        trackLost ? 0 : cleaned ? 0.7 : 2 / 3,
        diagnosticsCollected,
      ),
      predicted_ratio: observation(trackLost ? 0 : 1 / 6, diagnosticsCollected),
      lost_ratio: observation(
        trackLost ? 1 : cleaned ? 2 / 15 : 1 / 6,
        diagnosticsCollected,
      ),
      longest_lost_streak: observation(
        trackLost ? 300 : 4,
        diagnosticsCollected,
      ),
      false_positive_island_count: observation(0, diagnosticsCollected),
      max_step_px: observation(trackLost ? 0 : 20, diagnosticsCollected),
    });
    return {
      schema_version: "2.0",
      status:
        failed || diagnosticContractFailure
          ? "insufficient_evidence"
          : trackLost
            ? "retune_required"
            : "acceptable",
      coverage_complete: !failed && !diagnosticContractFailure,
      evidence_available: !failed,
      trajectory_acceptable: !failed && !trackLost,
      signal_acceptable: !failed && !trackLost && !diagnosticContractFailure,
      acceptance_metrics_complete: !failed,
      acceptance_contract_complete: !failed && !trackLost,
      quality_acceptable: !failed && !trackLost && !diagnosticContractFailure,
      operator_confirmation_required: true,
      reason_codes: failed
        ? ["run_not_completed", "acceptance_contract_not_collected"]
        : diagnosticContractFailure
          ? [
              "trial_option_conflict:postprocess",
              "frame_exception",
              "stage_counter_not_collected:lost_frames",
              "filtered_candidate_count_exceeds_class_mapped",
              "rejection_reasons_not_collected",
            ]
          : classRejected
            ? [
                "all_candidates_class_rejected",
                "zero_tracklet",
                "all_lost",
                "acceptance_contract_not_collected",
              ]
            : allLost
              ? [
                  "zero_candidate",
                  "zero_tracklet",
                  "all_lost",
                  "acceptance_contract_not_collected",
                ]
              : ["quality_thresholds_passed"],
      failure_classification:
        failed || diagnosticContractFailure
          ? {
              code: "insufficient_evidence",
              severity: "blocking",
              summary: "Metrics are incomplete or inconsistent.",
              recommended_action: "Repair the failed run before acceptance.",
            }
          : classRejected
            ? {
                code: "all_candidates_class_rejected",
                severity: "blocking",
                summary:
                  "Every model output was rejected during class mapping.",
                recommended_action:
                  "Select the correct allowed labels and rerun.",
              }
            : allLost
              ? {
                  code: "no_raw_candidates",
                  severity: "blocking",
                  summary: "The detector produced no ball candidates.",
                  recommended_action:
                    "Adjust detector sensitivity or inference mode and rerun.",
                }
              : {
                  code: "acceptable",
                  severity: "none",
                  summary: "The signal thresholds pass.",
                  recommended_action:
                    "Inspect the playable evidence and explicitly confirm it.",
                },
      threshold_profile: {
        profile_id: "trial-signal-conservative",
        version: "1.1",
        algorithm_version: "trial-signal-gate-v2.1",
        matching_rules: {
          stage_counter_reconciliation:
            "all_required_counters_present_and_reconciled",
          track_metric_scope: "raw_and_cleaned_when_postprocess_enabled",
          follow_cam_scope: "motion_and_action_retention_when_enabled",
          required_visual_evidence: [
            "wide_context",
            "tight_crop",
            "follow_cam_when_enabled",
            "scale_strata",
            "lighting_strata",
            "attack_transition_windows",
          ],
          required_integrity: ["media_integrity", "identity_binding"],
          acceptance_contract: "server_verified_bundle_required",
        },
        sha256: "b".repeat(64),
        thresholds: {
          minimum_detected_ratio: 0.5,
          maximum_predicted_ratio: 0.35,
          maximum_lost_ratio: 0.25,
          maximum_longest_lost_streak: 30,
          maximum_false_positive_islands_per_100_frames: 8,
          maximum_suspicious_tracklet_ratio: 0.35,
          maximum_step_px: 600,
          maximum_follow_cam_pan_step_px: 90,
          maximum_follow_cam_pan_accel_px: 120,
          maximum_follow_cam_zoom_step_ratio: 0.1,
          maximum_ai_review_triggers_per_100_frames: 10,
          maximum_event_candidates_per_100_frames: 25,
        },
      },
      stage_counts: {
        schema_version: "2.0",
        coverage_status: failed
          ? "not_collected"
          : diagnosticContractFailure
            ? "invalid"
            : "complete",
        evaluated_frames: collectedCount(300),
        detected_frames: collectedCount(trackLost ? 0 : 200),
        predicted_frames: collectedCount(trackLost ? 0 : 50),
        lost_frames: diagnosticContractFailure
          ? { value: null, status: "not_collected" }
          : collectedCount(trackLost ? 300 : 50),
        raw_candidates: collectedCount(allLost ? 0 : classRejected ? 12 : 240),
        class_mapped_candidates: collectedCount(trackLost ? 0 : 230),
        filtered_candidates: collectedCount(trackLost ? 0 : 220),
        selected_candidates: collectedCount(trackLost ? 0 : 200),
        tracklets: collectedCount(trackLost ? 0 : 1),
        rejection_reasons: classRejected
          ? { "class_not_allowed:person": 12 }
          : {},
        reconciliation: {
          status: failed
            ? "not_collected"
            : diagnosticContractFailure
              ? "mismatch"
              : "reconciled",
          reason_codes: diagnosticContractFailure
            ? ["debug_frame_exception:1"]
            : [],
        },
      },
      trajectory: {
        evaluated_frames: 300,
        detected: trackLost ? 0 : 200,
        predicted: trackLost ? 0 : 50,
        lost: trackLost ? 300 : 50,
      },
      diagnostics: {
        raw_track: trackDiagnostics(false),
        cleaned_track: trackDiagnostics(true),
        rejection_reasons: {
          status: diagnosticsCollected ? "collected" : "not_collected",
          value: diagnosticsCollected
            ? classRejected
              ? { "class_not_allowed:person": 12 }
              : {}
            : null,
        },
        ai_review_trigger_count: observation(0, budgetCollected),
        ai_review_triggers_per_100_frames: observation(0, budgetCollected),
        event_candidate_count: observation(0, budgetCollected),
        event_candidates_per_100_frames: observation(0, budgetCollected),
        follow_cam: {
          status: budgetCollected ? "collected" : "not_collected",
          max_pan_step_px: observation(20, budgetCollected),
          max_pan_accel_px: observation(30, budgetCollected),
          max_zoom_step_ratio: observation(0.02, budgetCollected),
        },
      },
      evidence: {
        wide_context: failed ? "not_collected" : "available",
        tight_crop: failed ? "not_collected" : "available",
        follow_cam: failed ? "not_collected" : "available",
        follow_cam_action_retention: failed ? "not_collected" : "complete",
        scale_strata: failed ? "not_collected" : "complete",
        lighting_strata: failed ? "not_collected" : "complete",
        attack_transition_windows: failed ? "not_collected" : "complete",
        media_integrity: failed ? "not_collected" : "complete",
        identity_binding: failed ? "not_collected" : "complete",
      },
    };
  }

  const tuningSchema = {
    schema_version: "1.0",
    patch_schema_version: "1.0",
    controls: [
      {
        path: "detector.allowed_labels",
        section: "detector",
        kind: "multi_select",
        options: ["sports ball", "ball"],
        runtime_impact: "low",
        description: "Detector labels accepted as a football.",
        description_zh: "允许作为足球候选的检测类别。",
      },
      {
        path: "detector.confidence_threshold",
        section: "detector",
        kind: "number",
        minimum: 0.01,
        maximum: 0.9,
        step: 0.01,
        runtime_impact: "low",
        description: "Minimum detector confidence used for this bounded trial.",
        description_zh: "本次有限试跑使用的最低检测置信度。",
      },
      {
        path: "detector.inference_mode",
        section: "detector",
        kind: "select",
        options: ["direct_full_frame", "sahi"],
        runtime_impact: "high",
        description: "Choose full-frame or sliced small-object inference.",
        description_zh: "选择整帧或面向小目标的切片推理。",
      },
    ],
    actions: [
      {
        action_code: "return_to_field_setup",
        target_step: "field_setup",
        reason_code: "field_geometry_requires_new_calibration",
        affected_paths: [
          "filtering.roi",
          "scene_bias.ground_zones",
          "scene_bias.negative_rois",
        ],
        lineage_constraint:
          "invalidate_trial_and_downstream_then_create_new_calibration_version",
      },
    ],
  };

  const baseConfigDetail = {
    name: "default.yaml",
    path: "configs/default.yaml",
    text: "detector:\n  allowed_labels: [sports ball]\n  confidence_threshold: 0.25\n  inference_mode: direct_full_frame\n",
    raw: {
      detector: {
        allowed_labels: ["sports ball"],
        confidence_threshold: 0.25,
        inference_mode: "direct_full_frame",
      },
    },
    resolved: {
      detector: {
        allowed_labels: ["sports ball"],
        confidence_threshold: 0.25,
        inference_mode: "direct_full_frame",
      },
    },
    summary: {
      name: "default.yaml",
      path: "configs/default.yaml",
      input_video: inputCatalog.videos[0].path,
      detector_model_path: "models/ball.pt",
      postprocess_enabled: true,
      follow_cam_enabled: true,
      exists: { yaml: true },
    },
  };

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
        size_bytes: Buffer.byteLength(rawTrackCsv),
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
        size_bytes: Buffer.byteLength(cleanedTrackCsv),
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
      config_sha256: options.legacyConfigDigest ? null : "c".repeat(64),
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
    if (method === "GET" && path === "/api/production-trials/tuning-schema") {
      await route.fulfill({ json: tuningSchema });
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
            tracklet_count: auditTrackletCount,
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
              tracklet_count: auditTrackletCount,
            },
            {
              name: "cleaned",
              path: "ball_track.cleaned.csv",
              row_count: 300,
              tracklet_count: auditTrackletCount,
            },
          ],
          tracklets: noTrack
            ? []
            : [
                {
                  tracklet_id: "tracklet-1",
                  start_frame: 0,
                  end_frame: 299,
                  row_count: 300,
                  flags: [],
                },
              ],
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
              trial_signal_gate_v2: run.trial_signal_gate_v2,
            },
          });
        }
        return;
      }
      if (name.endsWith(".csv")) {
        await route.fulfill({
          contentType: "text/csv",
          body:
            name === "ball_track.cleaned.csv" ? cleanedTrackCsv : rawTrackCsv,
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
    const diagnosisMatch = path.match(
      /^\/api\/runs\/([^/]+)\/trial-diagnosis$/,
    );
    if (method === "GET" && diagnosisMatch) {
      const run = runs.find(
        (item) => item.run_id === decodeURIComponent(diagnosisMatch[1]),
      );
      if (!run || !run.trial_signal_gate_v2) {
        await route.fulfill({ status: 404, json: { detail: "missing" } });
        return;
      }
      await route.fulfill({
        json: {
          schema_version: "1.0",
          run_id: run.run_id,
          legacy_quality_gate_status: (
            (run.stats as Record<string, unknown>).quality_gate as
              | { status?: string }
              | undefined
          )?.status,
          trial_signal_gate_v2: run.trial_signal_gate_v2,
          tuning_schema_version: "1.0",
        },
      });
      return;
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
      deriveResponseNames.push(name);
      return;
    }
    const configMatch = path.match(/^\/api\/configs\/(.+)$/);
    if (method === "GET" && configMatch) {
      const name = decodeURIComponent(configMatch[1]);
      configGetNames.push(name);
      const detail =
        name === baseConfigDetail.name ? baseConfigDetail : configs.get(name);
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
    deriveResponseNames,
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
      const gate =
        status === "completed" || status === "failed"
          ? trialSignalGate(status)
          : null;
      run.status = status;
      run.trial_signal_gate_v2 = gate;
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
                detected: noTrack ? 0 : 200,
                predicted: noTrack ? 0 : 50,
                lost: noTrack ? 300 : 50,
                detected_ratio: noTrack ? 0 : 2 / 3,
                predicted_ratio: noTrack ? 0 : 1 / 6,
                lost_ratio: noTrack ? 1 : 1 / 6,
                longest_lost_streak: noTrack ? 300 : 4,
                false_positive_island_count: noTrack ? 0 : 1,
                max_step_px: noTrack ? 0 : 20,
              },
              cleaned: {
                frame_count: 300,
                detected: noTrack ? 0 : 210,
                predicted: noTrack ? 0 : 50,
                lost: noTrack ? 300 : 40,
                detected_ratio: noTrack ? 0 : 0.7,
                predicted_ratio: noTrack ? 0 : 1 / 6,
                lost_ratio: noTrack ? 1 : 2 / 15,
              },
              quality_gate: { status: noTrack ? "stable" : "warn" },
              trial_signal_gate_v2: gate,
            }
          : gate
            ? { trial_signal_gate_v2: gate }
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

const detectorProbeProfileIds = DETECTOR_PROBE_PROFILE_IDS;

function detectorProbeCatalogFixture() {
  const catalog = strictDetectorProbeCatalogFixture();
  catalog.models[0].descriptor.display_name = "Official YOLO11n";
  catalog.models[1].descriptor.display_name = "Official YOLO11s";
  return catalog;
}

function readyAllZeroDetectorProbe(
  jobId: string,
  parentTrialId: string,
  frameIndices: number[],
) {
  const job = strictDetectorProbeJobFixture(jobId, "ready", null, {
    frameIndices,
  });
  job.frozen_request.parent_trial_id = parentTrialId;
  if (job.report) job.report.lineage.parent_trial_id = parentTrialId;
  return job;
}

async function installDetectorProbeScenario(page: Page) {
  const createBodies: Array<Record<string, unknown>> = [];
  const artifactReads: string[] = [];
  const jobId = "probe-e2e-ready";
  let job: ReturnType<typeof readyAllZeroDetectorProbe> | null = null;

  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const method = request.method();
    if (method === "GET" && url.pathname === "/api/detector-models") {
      await route.fulfill({ json: detectorProbeCatalogFixture() });
      return;
    }
    if (method === "POST" && url.pathname === "/api/detector-probes") {
      const body = request.postDataJSON() as Record<string, unknown>;
      createBodies.push(body);
      job = readyAllZeroDetectorProbe(
        jobId,
        String(body.parent_trial_id),
        Array.isArray(body.frame_indices)
          ? [...(body.frame_indices as number[])]
          : [10, 20, 30, 40, 50, 60],
      );
      await route.fulfill({
        status: 202,
        json: {
          job_id: jobId,
          request_sha256: "e".repeat(64),
          status: "queued",
          status_url: `/api/v1/detector-probes/${jobId}`,
          cancel_url: `/api/v1/detector-probes/${jobId}/cancel`,
          retry_from_job_id: null,
        },
      });
      return;
    }
    if (
      method === "GET" &&
      url.pathname === `/api/detector-probes/${jobId}` &&
      job
    ) {
      await route.fulfill({ json: job });
      return;
    }
    if (
      method === "GET" &&
      url.pathname.startsWith(`/api/detector-probes/${jobId}/artifacts/`)
    ) {
      artifactReads.push(url.pathname);
      await route.fulfill({
        contentType: "image/svg+xml",
        body: squarePreviewSvg,
      });
      return;
    }
    await route.fallback();
  });

  return { artifactReads, createBodies };
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

async function confirmTrialVisualEvidence(page: Page) {
  const confirmation = page.getByRole("checkbox", {
    name: "I visually reviewed this evidence and confirm the ball remains usable across the trial.",
  });
  await expect(confirmation).toBeVisible({ timeout: 15_000 });
  await confirmation.click();
  await expect(confirmation).toBeChecked();
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
  await confirmTrialVisualEvidence(page);
  const accept = page.getByRole("button", { name: "Accept this trial" });
  await expect(accept).toBeVisible({ timeout: 15_000 });
  await accept.click();
  await expect(page.getByText("Trial accepted")).toBeVisible();
}

const BROADCAST_E2E_NOW = "2026-07-15T18:00:00.000Z";
const ACCEPTED_TRIAL_RUN_ID = "trial-broadcast-accepted";
const TRAJECTORY_GENERATION_ID = `trajectory-${"a".repeat(24)}`;
const BROADCAST_TRIAL_EVIDENCE_GENERATION = "e".repeat(64);
const BROADCAST_TRIAL_THRESHOLD_SHA256 = "b".repeat(64);

function acceptedBroadcastTrialGate(): TrialSignalGateV2 {
  const observation = (value: number) => ({
    status: "collected" as const,
    value,
  });
  const track = (cleaned: boolean) => ({
    status: "collected" as const,
    frame_count: observation(300),
    detected: observation(cleaned ? 210 : 200),
    predicted: observation(50),
    lost: observation(cleaned ? 40 : 50),
    detected_ratio: observation(cleaned ? 0.7 : 2 / 3),
    predicted_ratio: observation(1 / 6),
    lost_ratio: observation(cleaned ? 2 / 15 : 1 / 6),
    longest_lost_streak: observation(4),
    false_positive_island_count: observation(1),
    max_step_px: observation(20),
  });
  return {
    schema_version: "2.0",
    status: "acceptable",
    coverage_complete: true,
    evidence_available: true,
    trajectory_acceptable: true,
    signal_acceptable: true,
    acceptance_metrics_complete: true,
    acceptance_contract_complete: true,
    quality_acceptable: true,
    operator_confirmation_required: true,
    reason_codes: ["quality_thresholds_passed"],
    failure_classification: {
      code: "acceptable",
      severity: "none",
      summary: "The signal thresholds pass.",
      recommended_action: "Review and confirm the bound evidence.",
    },
    threshold_profile: {
      profile_id: "trial-signal-conservative",
      version: "1.1",
      algorithm_version: "trial-signal-gate-v2.1",
      matching_rules: {
        stage_counter_reconciliation:
          "all_required_counters_present_and_reconciled",
        track_metric_scope: "raw_and_cleaned_when_postprocess_enabled",
        follow_cam_scope: "motion_and_action_retention_when_enabled",
        required_visual_evidence: [
          "wide_context",
          "tight_crop",
          "follow_cam_when_enabled",
          "scale_strata",
          "lighting_strata",
          "attack_transition_windows",
        ],
        required_integrity: ["media_integrity", "identity_binding"],
        acceptance_contract: "server_verified_bundle_required",
      },
      sha256: BROADCAST_TRIAL_THRESHOLD_SHA256,
      thresholds: {
        minimum_detected_ratio: 0.5,
        maximum_predicted_ratio: 0.35,
        maximum_lost_ratio: 0.25,
        maximum_longest_lost_streak: 30,
        maximum_false_positive_islands_per_100_frames: 8,
        maximum_suspicious_tracklet_ratio: 0.35,
        maximum_step_px: 600,
        maximum_follow_cam_pan_step_px: 90,
        maximum_follow_cam_pan_accel_px: 120,
        maximum_follow_cam_zoom_step_ratio: 0.1,
        maximum_ai_review_triggers_per_100_frames: 10,
        maximum_event_candidates_per_100_frames: 25,
      },
    },
    stage_counts: {
      schema_version: "2.0",
      coverage_status: "complete",
      evaluated_frames: { value: 300, status: "collected" },
      detected_frames: { value: 200, status: "collected" },
      predicted_frames: { value: 50, status: "collected" },
      lost_frames: { value: 50, status: "collected" },
      raw_candidates: { value: 240, status: "collected" },
      class_mapped_candidates: { value: 230, status: "collected" },
      filtered_candidates: { value: 220, status: "collected" },
      selected_candidates: { value: 200, status: "collected" },
      tracklets: { value: 1, status: "collected" },
      rejection_reasons: { below_confidence: 10 },
      reconciliation: { status: "reconciled", reason_codes: [] },
    },
    trajectory: { evaluated_frames: 300 },
    diagnostics: {
      raw_track: track(false),
      cleaned_track: track(true),
      rejection_reasons: {
        status: "collected",
        value: { below_confidence: 10 },
      },
      ai_review_trigger_count: observation(0),
      ai_review_triggers_per_100_frames: observation(0),
      event_candidate_count: observation(0),
      event_candidates_per_100_frames: observation(0),
      follow_cam: {
        status: "collected",
        max_pan_step_px: observation(20),
        max_pan_accel_px: observation(30),
        max_zoom_step_ratio: observation(0.02),
      },
    },
    evidence: {
      wide_context: "available",
      tight_crop: "available",
      follow_cam: "available",
      follow_cam_action_retention: "complete",
      scale_strata: "complete",
      lighting_strata: "complete",
      attack_transition_windows: "complete",
      media_integrity: "complete",
      identity_binding: "complete",
    },
  };
}

interface ConfirmedBroadcastDraftFixture {
  draft: ProductionDraft;
  config: ConfigDetail;
  calibration: ProductionCalibrationDraft;
}

function cloneJson<T>(value: T): T {
  return structuredClone(value);
}

function sha256Bytes(value: Uint8Array | Buffer): string {
  return createHash("sha256").update(value).digest("hex");
}

async function buildConfirmedBroadcastDraft(): Promise<ConfirmedBroadcastDraftFixture> {
  const workflowId = "workflow-broadcast-e2e";
  const source = cloneJson(inputCatalog.videos[0]);
  const calibration = cloneJson(
    draftWithCompletedCalibration().calibration,
  ) as ProductionCalibrationDraft;
  const settings = {
    base_config_name: "default.yaml",
    start_frame: 125,
    max_frames: 300,
    enable_postprocess: true,
    enable_follow_cam: true,
    tuning_patch: {
      detector: { confidence: 0.2 },
      output: { dir: "trial-only", video_name: "trial.mp4" },
      runtime: { start_frame: 125, max_frames: 300 },
      follow_cam: { enabled: true, legacy_render: true },
    },
  };
  const trialSubmission = await buildProductionTrialSubmission({
    workflow_id: workflowId,
    source,
    calibration,
    settings,
    parent_run_id: null,
    submission_id: "broadcast-trial-submission",
    output_id: "broadcast-trial-output",
    generation: 1,
    created_at: BROADCAST_E2E_NOW,
  });
  const observedTrial = appendProductionTrialAttempt(
    setPendingProductionTrial(
      createProductionTrialState(settings),
      trialSubmission.pending,
    ),
    {
      run: { run_id: ACCEPTED_TRIAL_RUN_ID, status: "completed" },
      pending: trialSubmission.pending,
      observed_at: BROADCAST_E2E_NOW,
    },
  );
  const trialSignalGate = acceptedBroadcastTrialGate();
  const trial = acceptProductionTrial(observedTrial, {
    run: { run_id: ACCEPTED_TRIAL_RUN_ID, status: "completed" },
    current_intent_sha256: trialSubmission.pending.intent_sha256,
    readiness: {
      run_id: ACCEPTED_TRIAL_RUN_ID,
      request_sha256: trialSubmission.pending.request_sha256,
      evidence_generation: BROADCAST_TRIAL_EVIDENCE_GENERATION,
      verified_at: BROADCAST_E2E_NOW,
      video_artifact_name: "follow_cam.webm",
      artifact_names: [
        "run_manifest.json",
        "metrics_report.json",
        "ball_track.csv",
        "ball_audit.json",
        "ball_track.cleaned.csv",
        "follow_cam.webm",
      ],
      quality: {
        frame_count: 300,
        detected: 210,
        predicted: 50,
        lost: 40,
        detected_ratio: 0.7,
        predicted_ratio: 1 / 6,
        lost_ratio: 2 / 15,
        longest_lost_streak: 4,
        false_positive_island_count: 1,
        max_step_px: 20,
        audit_tracklet_count: 1,
        audit_suspicious_tracklet_count: 0,
        audit_review_event_count: 0,
        audit_lost_gap_count: 0,
        quality_gate_status: "warn",
        trial_signal_gate_v2: trialSignalGate,
      },
      operator_visual_confirmation: {
        confirmed: true,
        confirmed_at: BROADCAST_E2E_NOW,
        evidence_generation: BROADCAST_TRIAL_EVIDENCE_GENERATION,
        threshold_profile_sha256: BROADCAST_TRIAL_THRESHOLD_SHA256,
      },
    },
    accepted_at: BROADCAST_E2E_NOW,
  });
  const pendingConfig = await buildProductionConfigConfirmation({
    workflow_id: workflowId,
    source,
    calibration,
    trial,
    output_id: "11111111-1111-4111-8111-111111111111",
    generation: 1,
    confirmed_at: BROADCAST_E2E_NOW,
  });
  const configName = expectedProductionConfigName(pendingConfig.output_name);
  const config: ConfigDetail = {
    name: configName,
    path: `configs/${configName}`,
    text: "input_video: data/match-a.mp4\noutput:\n  save_tracking_contract: true\n",
    raw: pendingConfig.request.patch ?? {},
    resolved: pendingConfig.request.patch ?? {},
    summary: {
      name: configName,
      path: `configs/${configName}`,
      input_video: source.path,
      postprocess_enabled: true,
      follow_cam_enabled: false,
      exists: { yaml: true },
    },
  };
  const confirmedConfig = await finalizeProductionConfigConfirmation(
    pendingConfig,
    config,
  );
  let draft = createProductionDraft(BROADCAST_E2E_NOW, workflowId);
  draft = updateProductionSource(draft, source, BROADCAST_E2E_NOW);
  draft = updateProductionCalibration(draft, calibration, BROADCAST_E2E_NOW);
  draft = updateProductionTrial(draft, trial, BROADCAST_E2E_NOW);
  draft = updateConfirmedProductionConfig(
    draft,
    confirmedConfig,
    BROADCAST_E2E_NOW,
  );
  return { draft, config, calibration };
}

async function expectNoSeriousAccessibilityFindings(page: Page) {
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
}

interface BroadcastScenarioAudit {
  unhandledApi: string[];
  contractViolations: string[];
}

const broadcastScenarioAudits = new WeakMap<Page, BroadcastScenarioAudit>();

type ZeroReviewBroadcastPhase =
  | "tracking"
  | "needs_review"
  | "recomputing"
  | "trajectory_ready"
  | "rendering"
  | "ready"
  | "failed"
  | "cancelled";

interface ZeroReviewBroadcastScenario {
  audit: BroadcastScenarioAudit;
  createBodies: CreateRunRequest[];
  reviewBodies: BroadcastReviewActionsRequest[];
  terminalTailReviewBodies: BroadcastTerminalTailReviewRequest[];
  recomputeBodies: BroadcastTrajectoryRecomputeRequest[];
  renderBodies: BroadcastRenderRequest[];
  cancelIds: string[];
  healthActiveRunIds: Array<string | null>;
  configReads: Array<{ name: string; cacheControl: string }>;
  authoritativeReads: Array<{
    kind:
      | "config"
      | "run-list"
      | "run"
      | "artifact-list"
      | "artifact"
      | "review-evidence"
      | "review";
    path: string;
    cacheControl: string;
  }>;
  artifactReads: Array<{
    method: string;
    name: string;
    statusGeneration: string | null;
    range: string | null;
  }>;
  reviewWindowReads: Array<{
    method: string;
    runId: string;
    phase: ZeroReviewBroadcastPhase;
  }>;
  reviewEvidenceImportBodies: BroadcastReviewEvidenceImportRequest[];
  reviewEvidenceRevokes: Array<{
    generationId: string;
    queueSha256: string | null;
  }>;
  qualityStable: Record<string, unknown>;
  qualityBytes: Buffer;
  statusGeneration: string;
  flipReadyGeneration: () => string;
  parentRunId: () => string;
  phase: () => ZeroReviewBroadcastPhase;
  blockingReasons: () => string[];
  setConfigMode: (mode: "ok" | "tampered" | "missing") => void;
  usePopulatedReview: () => void;
  useMissingReviewQueue: () => void;
  requireTerminalTailReview: () => void;
  allowOneTrajectoryReadyReviewRead: () => void;
  setDeliveryMode: (mode: "ok" | "missing_video") => void;
  failNextCreateWithConflict: (runId: string) => void;
  failNextRenderWithConflict: (runId: string) => void;
  clearForeignBlocker: () => void;
  setRunning: () => void;
  setFailed: () => void;
  setNeedsReview: () => void;
  completeRecomputeChildOnly: () => void;
  publishTrajectoryReady: () => void;
  completeRecompute: () => void;
  completeRenderChildOnly: () => void;
  publishReady: () => void;
  completeRender: () => void;
}

function cloneRun(run: RunRecord): RunRecord {
  return cloneJson(run);
}

async function fulfillByteRange(
  route: Route,
  body: Buffer,
  contentType: string,
) {
  const requestedRange = route.request().headers()["range"] ?? null;
  if (!requestedRange) {
    await route.fulfill({
      status: 200,
      body,
      contentType,
      headers: {
        "Accept-Ranges": "bytes",
        "Content-Length": String(body.byteLength),
      },
    });
    return;
  }
  const match = /^bytes=(\d*)-(\d*)$/.exec(requestedRange);
  if (!match) {
    await route.fulfill({
      status: 416,
      headers: { "Content-Range": `bytes */${body.byteLength}` },
    });
    return;
  }
  const start = match[1] ? Number(match[1]) : 0;
  const requestedEnd = match[2] ? Number(match[2]) : body.byteLength - 1;
  const end = Math.min(requestedEnd, body.byteLength - 1);
  if (start < 0 || start >= body.byteLength || end < start) {
    await route.fulfill({
      status: 416,
      headers: { "Content-Range": `bytes */${body.byteLength}` },
    });
    return;
  }
  const chunk = body.subarray(start, end + 1);
  await route.fulfill({
    status: 206,
    body: chunk,
    contentType,
    headers: {
      "Accept-Ranges": "bytes",
      "Content-Length": String(chunk.byteLength),
      "Content-Range": `bytes ${start}-${end}/${body.byteLength}`,
    },
  });
}

async function installZeroReviewBroadcastScenario(
  page: Page,
  fixture: ConfirmedBroadcastDraftFixture,
): Promise<ZeroReviewBroadcastScenario> {
  const audit: BroadcastScenarioAudit = {
    unhandledApi: [],
    contractViolations: [],
  };
  broadcastScenarioAudits.set(page, audit);
  const videoFixture = await createPlayableVideoFixture(page);
  const videoBytes = Buffer.from(videoFixture.bodyBase64, "base64");
  const reviewDecisionBytes = Buffer.from(
    canonicalJson({ actions: [], queue_sha256: "1".repeat(64) }),
    "utf8",
  );
  const reviewDecisionSha256 = sha256Bytes(reviewDecisionBytes);
  const qualityStable: Record<string, unknown> = {
    schema_version: "1.0",
    artifact_type: "broadcast_quality_report",
    status: "ready",
    blocking_reasons: [],
    limitations: ["source_audio_not_preserved"],
    lineage: { sources: {} },
    artifacts: {
      "broadcast.mp4": {
        status: "available",
        path: "broadcast.mp4",
        sha256: sha256Bytes(videoBytes),
        size_bytes: videoBytes.byteLength,
      },
    },
    final_bindings: {},
    capabilities: {},
  };
  const statusGeneration = await sha256Text(canonicalJson(qualityStable));
  let enforcedStatusGeneration = statusGeneration;
  const qualityReport = {
    ...qualityStable,
    generated_at: BROADCAST_E2E_NOW,
    status_generation: statusGeneration,
  };
  const qualityBytes = Buffer.from(canonicalJson(qualityReport), "utf8");
  const artifactBodies = new Map<string, Buffer>([
    ["broadcast.mp4", videoBytes],
    ["broadcast_quality_report.json", qualityBytes],
    ["camera_target.csv", Buffer.from("frame,x,y\n0,960,540\n")],
    ["ball_track.v2.csv", Buffer.from("frame,x,y,status\n0,10,20,detected\n")],
    ["review_decisions.json", reviewDecisionBytes],
    ["action_track.csv", Buffer.from("frame,action\n0,hold\n")],
    ["candidate_classifications.jsonl", Buffer.from('{"candidate":null}\n')],
    ["ball_candidates.jsonl", Buffer.from('{"candidate":null}\n')],
  ]);
  const montageBodies = new Map<string, Buffer>([
    ["review/candidate-1.svg", Buffer.from(squarePreviewSvg, "utf8")],
    ["review/candidate-2.svg", Buffer.from(squarePreviewSvg, "utf8")],
  ]);
  const montageArtifacts: ArtifactSummary[] = [...montageBodies].map(
    ([name, body]) => ({
      name,
      path: name,
      kind: "image",
      exists: true,
      size_bytes: body.byteLength,
      content_type: "image/svg+xml",
    }),
  );
  const deliveryArtifacts: ArtifactSummary[] =
    PRODUCTION_BROADCAST_DELIVERY_ARTIFACTS.map((name) => ({
      name,
      path: `sealed/${name}`,
      kind: name === "broadcast.mp4" ? "video" : "file",
      exists: true,
      size_bytes: artifactBodies.get(name)!.byteLength,
      content_type:
        name === "broadcast.mp4"
          ? videoFixture.contentType
          : name.endsWith(".json") || name.endsWith(".jsonl")
            ? "application/json"
            : "text/csv",
    }));

  const createBodies: CreateRunRequest[] = [];
  const reviewBodies: BroadcastReviewActionsRequest[] = [];
  const terminalTailReviewBodies: BroadcastTerminalTailReviewRequest[] = [];
  const reviewEvidenceImportBodies: BroadcastReviewEvidenceImportRequest[] = [];
  const reviewEvidenceRevokes: ZeroReviewBroadcastScenario["reviewEvidenceRevokes"] =
    [];
  const recomputeBodies: BroadcastTrajectoryRecomputeRequest[] = [];
  const renderBodies: BroadcastRenderRequest[] = [];
  const cancelIds: string[] = [];
  const healthActiveRunIds: Array<string | null> = [];
  const configReads: Array<{ name: string; cacheControl: string }> = [];
  const authoritativeReads: ZeroReviewBroadcastScenario["authoritativeReads"] =
    [];
  const artifactReads: ZeroReviewBroadcastScenario["artifactReads"] = [];
  const reviewWindowReads: ZeroReviewBroadcastScenario["reviewWindowReads"] =
    [];
  const acceptedTrial = fixture.draft.trial!.accepted!;
  const acceptedAttempt = fixture.draft.trial!.attempts.find(
    (attempt) => attempt.run_id === acceptedTrial.run_id,
  )!;
  const acceptedTrialSignalGate =
    acceptedTrial.readiness.quality.trial_signal_gate_v2;
  if (!acceptedTrialSignalGate) {
    throw new Error("The accepted trial fixture must include its signal gate");
  }
  const tuningSchema = {
    schema_version: "1.0",
    patch_schema_version: "1.0",
    controls: [
      {
        path: "detector.allowed_labels",
        section: "detector",
        kind: "multi_select",
        options: ["sports ball", "ball"],
        runtime_impact: "low",
        description: "Detector labels accepted as a football.",
        description_zh: "允许作为足球候选的检测类别。",
      },
      {
        path: "detector.confidence_threshold",
        section: "detector",
        kind: "number",
        minimum: 0.01,
        maximum: 0.9,
        step: 0.01,
        runtime_impact: "low",
        description: "Minimum detector confidence used for this bounded trial.",
        description_zh: "本次有限试跑使用的最低检测置信度。",
      },
      {
        path: "detector.inference_mode",
        section: "detector",
        kind: "select",
        options: ["direct_full_frame", "sahi"],
        runtime_impact: "high",
        description: "Choose full-frame or sliced small-object inference.",
        description_zh: "选择整帧或面向小目标的切片推理。",
      },
    ],
    actions: [
      {
        action_code: "return_to_field_setup",
        target_step: "field_setup",
        reason_code: "field_geometry_requires_new_calibration",
        affected_paths: [
          "filtering.roi",
          "scene_bias.ground_zones",
          "scene_bias.negative_rois",
        ],
        lineage_constraint:
          "invalidate_trial_and_downstream_then_create_new_calibration_version",
      },
    ],
  };
  const trialArtifacts: ArtifactSummary[] = [
    ["run_manifest.json", 100, "application/json"],
    ["metrics_report.json", 100, "application/json"],
    ["ball_track.csv", Buffer.byteLength(trialRawTrackCsv), "text/csv"],
    ["ball_audit.json", 100, "application/json"],
    [
      "ball_track.cleaned.csv",
      Buffer.byteLength(trialCleanedTrackCsv),
      "text/csv",
    ],
    ["follow_cam.webm", videoBytes.byteLength, videoFixture.contentType],
  ].map(([name, size, contentType]) => ({
    name: String(name),
    path: String(name),
    kind: String(name).endsWith(".webm") ? "video" : "file",
    exists: true,
    size_bytes: Number(size),
    content_type: String(contentType),
  }));
  const trialRun: RunRecord = {
    run_id: acceptedTrial.run_id,
    source: "api",
    status: "completed",
    created_at: BROADCAST_E2E_NOW,
    completed_at: BROADCAST_E2E_NOW,
    config_name: backendMaterializedRunConfigName(
      acceptedAttempt.request.config_name,
      acceptedTrial.run_id,
    ),
    input_video: acceptedAttempt.request.input_video,
    parent_run_id: acceptedAttempt.request.parent_run_id,
    output_dir: `outputs/${acceptedTrial.run_id}`,
    modules_enabled: {
      postprocess: Boolean(acceptedAttempt.request.enable_postprocess),
      follow_cam: Boolean(acceptedAttempt.request.enable_follow_cam),
    },
    artifacts: trialArtifacts,
    stats: {},
    notes: acceptedAttempt.request.notes,
    error: null,
  };
  let currentPhase: ZeroReviewBroadcastPhase = "tracking";
  let configMode: "ok" | "tampered" | "missing" = "ok";
  let reviewMode: "zero" | "populated" | "missing" = "zero";
  let terminalTailReviewMode: "not_required" | "required" | "accepted" =
    "not_required";
  const terminalTailEvidence = {
    source_video_sha256: "1".repeat(64),
    tracking_contract_sha256: "2".repeat(64),
    action_signal_report_sha256: "3".repeat(64),
    temporal_chunks_report_sha256: "4".repeat(64),
    reported_frame_count: 5194,
    verified_frame_count: 5192,
    gap_frames: 2,
    gap_seconds: 0.1,
    evidence_sha256: "5".repeat(64),
  };
  const reviewEvidenceGenerationId = `review-evidence-${"7".repeat(24)}`;
  const reviewEvidenceQueueSha256 = "1".repeat(64);
  let reviewEvidenceRevoked = false;
  let deliveryMode: "ok" | "missing_video" = "ok";
  let allowedTrajectoryReadyReviewReads = 0;
  let submittedReviewDecisionSha256 = reviewDecisionSha256;
  let createConflictRunId: string | null = null;
  let renderConflictRunId: string | null = null;
  let foreignActiveRunId: string | null = null;
  let parent: RunRecord | null = null;
  const priorParents: RunRecord[] = [];
  let recomputeChild: RunRecord | null = null;
  let renderChild: RunRecord | null = null;
  const trajectoryGenerationId = TRAJECTORY_GENERATION_ID;

  function requireParent(): RunRecord {
    if (!parent)
      throw new Error("The full-production parent has not been created");
    return parent;
  }

  function runs(): RunRecord[] {
    return [
      ...(renderChild ? [cloneRun(renderChild)] : []),
      ...(recomputeChild ? [cloneRun(recomputeChild)] : []),
      ...(parent ? [cloneRun(parent)] : []),
      ...priorParents.map(cloneRun),
      cloneRun(trialRun),
    ];
  }

  function setConfigMode(mode: "ok" | "tampered" | "missing") {
    configMode = mode;
  }

  function usePopulatedReview() {
    reviewMode = "populated";
  }

  function useMissingReviewQueue() {
    reviewMode = "missing";
  }

  function requireTerminalTailReview() {
    terminalTailReviewMode = "required";
  }

  function allowOneTrajectoryReadyReviewRead() {
    allowedTrajectoryReadyReviewReads = 1;
  }

  function setDeliveryMode(mode: "ok" | "missing_video") {
    deliveryMode = mode;
  }

  function failNextCreateWithConflict(runId: string) {
    createConflictRunId = runId;
  }

  function failNextRenderWithConflict(runId: string) {
    renderConflictRunId = runId;
  }

  function clearForeignBlocker() {
    foreignActiveRunId = null;
  }

  function setRunning() {
    const run = requireParent();
    currentPhase = "tracking";
    run.status = "running";
    run.started_at = BROADCAST_E2E_NOW;
    run.progress = {
      stage: "tracking",
      current_frame: 500,
      total_frames: 1_000,
      percent: 50,
    };
    run.broadcast = { ...run.broadcast, status: "tracking" };
  }

  function setFailed() {
    const run = requireParent();
    currentPhase = "failed";
    run.status = "failed";
    run.completed_at = BROADCAST_E2E_NOW;
    run.progress = null;
    run.error = "full production failed";
    run.broadcast = { ...run.broadcast, status: "failed" };
  }

  function setNeedsReview() {
    const run = requireParent();
    currentPhase = "needs_review";
    run.status = "completed";
    run.completed_at = BROADCAST_E2E_NOW;
    run.progress = null;
    run.broadcast = {
      ...run.broadcast,
      status: "needs_review",
      blocking_reasons: [
        ...(terminalTailReviewMode === "required"
          ? ["terminal_decoder_shortfall_requires_operator_review"]
          : []),
        ...(reviewMode === "missing"
          ? ["missing_qualified_selective_review_queue"]
          : []),
      ],
      last_operation: undefined,
    };
  }

  function queueRecomputeChild() {
    const run = requireParent();
    currentPhase = "recomputing";
    recomputeChild = {
      run_id: "broadcast-recompute-zero-review",
      source: "broadcast_hybrid_recompute",
      status: "running",
      created_at: "2026-07-15T18:01:00.000Z",
      started_at: "2026-07-15T18:01:00.000Z",
      parent_run_id: run.run_id,
      output_dir: "outputs/broadcast-recompute-zero-review",
      progress: { stage: "recompute", percent: 40 },
      broadcast: {
        operation: "recompute",
        operation_status: "running",
        parent_run_id: run.run_id,
        request: { review_decisions_sha256: submittedReviewDecisionSha256 },
      },
    };
    run.broadcast = {
      ...run.broadcast,
      status: "needs_review",
      last_operation: {
        operation_run_id: recomputeChild.run_id,
        operation: "recompute",
        status: "running",
      },
    };
  }

  function completeRecomputeChildOnly() {
    if (!recomputeChild) throw new Error("Recompute was not queued");
    recomputeChild.status = "completed";
    recomputeChild.completed_at = "2026-07-15T18:02:00.000Z";
    recomputeChild.progress = null;
    recomputeChild.broadcast = {
      ...recomputeChild.broadcast,
      operation_status: "completed",
      result: { trajectory_generation_id: trajectoryGenerationId },
    };
  }

  function publishTrajectoryReady() {
    const run = requireParent();
    if (!recomputeChild || recomputeChild.status !== "completed") {
      throw new Error("Completed recompute evidence is unavailable");
    }
    currentPhase = "trajectory_ready";
    run.broadcast = {
      ...run.broadcast,
      status: "trajectory_ready",
      trajectory_generation_id: trajectoryGenerationId,
      last_operation: {
        operation_run_id: recomputeChild.run_id,
        operation: "recompute",
        status: "completed",
      },
    };
  }

  function completeRecompute() {
    completeRecomputeChildOnly();
    publishTrajectoryReady();
  }

  function queueRenderChild() {
    const run = requireParent();
    currentPhase = "rendering";
    renderChild = {
      run_id: "broadcast-render-zero-review",
      source: "broadcast_hybrid_render",
      status: "running",
      created_at: "2026-07-15T18:03:00.000Z",
      started_at: "2026-07-15T18:03:00.000Z",
      parent_run_id: run.run_id,
      output_dir: "outputs/broadcast-render-zero-review",
      progress: { stage: "render", percent: 35 },
      broadcast: {
        operation: "render",
        operation_status: "running",
        parent_run_id: run.run_id,
        request: { trajectory_generation_id: trajectoryGenerationId },
      },
    };
    run.broadcast = {
      ...run.broadcast,
      status: "trajectory_ready",
      last_operation: {
        operation_run_id: renderChild.run_id,
        operation: "render",
        status: "running",
      },
    };
  }

  function completeRenderChildOnly() {
    if (!renderChild) throw new Error("Render was not queued");
    renderChild.status = "completed";
    renderChild.completed_at = "2026-07-15T18:04:00.000Z";
    renderChild.progress = null;
    renderChild.broadcast = {
      ...renderChild.broadcast,
      operation_status: "completed",
      result: {
        trajectory_generation_id: trajectoryGenerationId,
        render_generation_id: "render-ready-zero-review",
      },
    };
  }

  function publishReady() {
    const run = requireParent();
    if (!renderChild || renderChild.status !== "completed") {
      throw new Error("Completed render evidence is unavailable");
    }
    currentPhase = "ready";
    run.artifacts =
      deliveryMode === "missing_video"
        ? deliveryArtifacts.filter(
            (artifact) => artifact.name !== "broadcast.mp4",
          )
        : deliveryArtifacts;
    run.broadcast = {
      ...run.broadcast,
      status: "ready",
      status_generation: statusGeneration,
      trajectory_generation_id: trajectoryGenerationId,
      limitations: ["source_audio_not_preserved"],
      last_operation: {
        operation_run_id: renderChild.run_id,
        operation: "render",
        status: "completed",
      },
    };
  }

  function completeRender() {
    completeRenderChildOnly();
    publishReady();
  }

  function captureAuthoritativeRead(
    kind: ZeroReviewBroadcastScenario["authoritativeReads"][number]["kind"],
    request: ReturnType<Route["request"]>,
  ) {
    authoritativeReads.push({
      kind,
      path: new URL(request.url()).pathname,
      cacheControl:
        request.headers()["cache-control"] ?? request.headers()["pragma"] ?? "",
    });
  }

  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const method = request.method();
    const path = url.pathname;
    const key = `${method} ${path}`;
    if (
      path === "/api/inputs" ||
      path === "/api/inputs/field-preview" ||
      path === "/api/inputs/field-suggestion"
    ) {
      await route.fallback();
      return;
    }
    if (method === "GET" && path === "/api/production-trials/tuning-schema") {
      await route.fulfill({ json: tuningSchema });
      return;
    }
    const diagnosisMatch = path.match(
      /^\/api\/runs\/([^/]+)\/trial-diagnosis$/,
    );
    if (method === "GET" && diagnosisMatch) {
      const runId = decodeURIComponent(diagnosisMatch[1]);
      if (runId !== trialRun.run_id) {
        audit.contractViolations.push(
          "trial diagnosis requested for a non-trial run",
        );
        await route.fulfill({ status: 404, json: { detail: "missing" } });
        return;
      }
      await route.fulfill({
        json: {
          schema_version: "1.0",
          run_id: runId,
          legacy_quality_gate_status: "warn",
          trial_signal_gate_v2: acceptedTrialSignalGate,
          tuning_schema_version: tuningSchema.schema_version,
        },
      });
      return;
    }
    if (method === "GET" && path === "/api/configs") {
      const summary: ConfigListItem = fixture.config.summary;
      await route.fulfill({ json: [summary] });
      return;
    }
    const configMatch = path.match(/^\/api\/configs\/(.+)$/);
    if (method === "GET" && configMatch) {
      captureAuthoritativeRead("config", request);
      const name = decodeURIComponent(configMatch[1]);
      configReads.push({
        name,
        cacheControl:
          request.headers()["cache-control"] ??
          request.headers()["pragma"] ??
          "",
      });
      if (name !== fixture.config.name) {
        audit.contractViolations.push(`unexpected config GET ${name}`);
        await route.fulfill({ status: 404, json: { detail: "missing" } });
        return;
      }
      if (configMode === "missing") {
        await route.fulfill({ status: 404, json: { detail: "missing" } });
        return;
      }
      await route.fulfill({
        json:
          configMode === "tampered"
            ? {
                ...fixture.config,
                text: `${fixture.config.text}tampered: true\n`,
              }
            : fixture.config,
        headers: { "Cache-Control": "no-store" },
      });
      return;
    }
    if (
      method === "GET" &&
      (path === "/api/health" || path === "/api/healthz")
    ) {
      const health: HealthResponse = {
        status: "ok",
        active_run_id:
          foreignActiveRunId ??
          (parent?.status === "queued" || parent?.status === "running"
            ? parent.run_id
            : null),
        config_count: 1,
        run_count: runs().length,
      };
      healthActiveRunIds.push(health.active_run_id ?? null);
      await route.fulfill({
        json: health,
        headers: { "Cache-Control": "no-store" },
      });
      return;
    }
    if (method === "GET" && path === "/api/runs") {
      captureAuthoritativeRead("run-list", request);
      await route.fulfill({
        json: runs(),
        headers: { "Cache-Control": "no-store" },
      });
      return;
    }
    if (method === "POST" && path === "/api/runs") {
      const body = request.postDataJSON() as CreateRunRequest;
      createBodies.push(cloneJson(body));
      if (createConflictRunId) {
        foreignActiveRunId = createConflictRunId;
        createConflictRunId = null;
        await route.fulfill({
          status: 409,
          json: {
            detail: `Another run is already active: ${foreignActiveRunId}`,
          },
        });
        return;
      }
      if (parent) {
        if (parent.status === "failed" || parent.status === "cancelled") {
          priorParents.push(cloneRun(parent));
        } else {
          audit.contractViolations.push("duplicate full-run create");
        }
      }
      parent = {
        run_id: String(body.output_dir_name),
        source: "broadcast_hybrid",
        status: "queued",
        created_at: BROADCAST_E2E_NOW,
        config_name: body.config_name,
        config_path: `configs/${body.config_name}`,
        input_video: body.input_video,
        parent_run_id: body.parent_run_id,
        output_dir: `outputs/${body.output_dir_name}`,
        modules_enabled: {
          postprocess: Boolean(body.enable_postprocess),
          follow_cam: Boolean(body.enable_follow_cam),
        },
        artifacts: [],
        stats: {},
        progress: { stage: "queued", percent: 0 },
        notes: body.notes,
        error: null,
        broadcast: {
          status: "tracking",
          quality_profile: "stable_broadcast",
          max_manual_review_windows: body.max_manual_review_windows,
        },
      };
      currentPhase = "tracking";
      await route.fulfill({ status: 201, json: cloneRun(parent) });
      return;
    }
    const cancelMatch = path.match(/^\/api\/runs\/([^/]+)\/cancel$/);
    if (method === "POST" && cancelMatch) {
      const runId = decodeURIComponent(cancelMatch[1]);
      cancelIds.push(runId);
      const run = requireParent();
      if (run.run_id !== runId) {
        audit.contractViolations.push("cancel targeted the wrong full parent");
        await route.fulfill({ status: 404, json: { detail: "missing" } });
        return;
      }
      currentPhase = "cancelled";
      run.status = "cancelled";
      run.completed_at = BROADCAST_E2E_NOW;
      run.progress = null;
      run.broadcast = { ...run.broadcast, status: "cancelled" };
      await route.fulfill({ json: cloneRun(run) });
      return;
    }
    const reviewEvidenceImportMatch = path.match(
      /^\/api\/runs\/([^/]+)\/broadcast\/review-evidence\/import$/,
    );
    if (method === "POST" && reviewEvidenceImportMatch) {
      const runId = decodeURIComponent(reviewEvidenceImportMatch[1]);
      const body =
        request.postDataJSON() as BroadcastReviewEvidenceImportRequest;
      reviewEvidenceImportBodies.push(cloneJson(body));
      if (runId !== requireParent().run_id) {
        audit.contractViolations.push(
          "review-evidence import targeted the wrong parent",
        );
      }
      reviewEvidenceRevoked = false;
      await route.fulfill({
        status: 202,
        json: {
          run_id: `review-evidence-import-${"8".repeat(24)}`,
          parent_run_id: requireParent().run_id,
          status: "queued",
          generation_id: reviewEvidenceGenerationId,
        } satisfies BroadcastOperationResponse,
      });
      return;
    }
    const reviewEvidenceRevokeMatch = path.match(
      /^\/api\/runs\/([^/]+)\/broadcast\/review-evidence\/([^/]+)$/,
    );
    if (method === "DELETE" && reviewEvidenceRevokeMatch) {
      const runId = decodeURIComponent(reviewEvidenceRevokeMatch[1]);
      const generationId = decodeURIComponent(reviewEvidenceRevokeMatch[2]);
      const queueSha256 = url.searchParams.get("queue_sha256");
      reviewEvidenceRevokes.push({ generationId, queueSha256 });
      if (
        runId !== requireParent().run_id ||
        generationId !== reviewEvidenceGenerationId ||
        queueSha256 !== reviewEvidenceQueueSha256
      ) {
        audit.contractViolations.push(
          "review-evidence revoke did not match the active generation",
        );
      }
      reviewEvidenceRevoked = true;
      await route.fulfill({
        json: {
          run_id: requireParent().run_id,
          status: "revoked",
          generation_id: reviewEvidenceGenerationId,
          queue_sha256: reviewEvidenceQueueSha256,
          revoked_at: BROADCAST_E2E_NOW,
        } satisfies BroadcastReviewEvidenceRevokeResponse,
      });
      return;
    }
    const reviewEvidenceMatch = path.match(
      /^\/api\/runs\/([^/]+)\/broadcast\/review-evidence$/,
    );
    if (method === "GET" && reviewEvidenceMatch) {
      captureAuthoritativeRead("review-evidence", request);
      const runId = decodeURIComponent(reviewEvidenceMatch[1]);
      if (runId !== requireParent().run_id) {
        audit.contractViolations.push(
          "review evidence requested for the wrong parent",
        );
      }
      const blocked = reviewMode === "missing" || reviewEvidenceRevoked;
      const response: BroadcastReviewEvidenceStateResponse = blocked
        ? {
            run_id: runId,
            status: "blocked",
            blocker_code: "review_evidence_bundle_not_available",
            recovery_action: "provision_qualified_review_evidence",
            retryable: false,
            can_cancel: false,
            bundles: [],
            blocking_reasons: ["missing_qualified_selective_review_queue"],
          }
        : {
            run_id: runId,
            status: "ready",
            generation_id: reviewEvidenceGenerationId,
            queue_sha256: reviewEvidenceQueueSha256,
            stage: "ready",
            progress_percent: 100,
            retryable: false,
            can_cancel: false,
            bundles: [],
          };
      await route.fulfill({
        json: response,
        headers: { "Cache-Control": "no-store" },
      });
      return;
    }
    const reviewMatch = path.match(
      /^\/api\/runs\/([^/]+)\/broadcast\/review-windows$/,
    );
    if (method === "GET" && reviewMatch) {
      captureAuthoritativeRead("review", request);
      const runId = decodeURIComponent(reviewMatch[1]);
      reviewWindowReads.push({ method, runId, phase: currentPhase });
      const exactTransitionRead =
        runId === requireParent().run_id &&
        currentPhase === "trajectory_ready" &&
        allowedTrajectoryReadyReviewReads > 0;
      if (exactTransitionRead) {
        allowedTrajectoryReadyReviewReads -= 1;
      } else if (
        runId !== requireParent().run_id ||
        (currentPhase !== "needs_review" && currentPhase !== "recomputing")
      ) {
        audit.contractViolations.push(
          `review windows requested during ${currentPhase}`,
        );
      }
      const candidates = [...montageBodies].map(
        ([montagePath, montageBody], index) => ({
          candidate_id: `candidate-${index + 1}`,
          candidate_fingerprint: String(index + 1).repeat(64),
          variant_id: "full",
          frame_index: 100 + index * 20,
          bbox: [10 + index, 20 + index, 30 + index, 40 + index] as [
            number,
            number,
            number,
            number,
          ],
          detector_source: "detector",
          detector_confidence: 0.8,
          predicted_label: index === 0 ? "match_ball" : "noise",
          prediction_confidence: 0.7,
          selective_decision: "abstain" as const,
          decision_reasons: ["manual_review_required"],
          review_kind: "policy_abstention",
          evidence: {
            sample_id: `sample-${index + 1}`,
            sha256: "a".repeat(64),
            dataset_version: "b".repeat(64),
            artifacts: {
              tight_tensor: {
                path: `review/candidate-${index + 1}-tight.npy`,
                sha256: "c".repeat(64),
                size_bytes: 10,
              },
              context_tensor: {
                path: `review/candidate-${index + 1}-context.npy`,
                sha256: "d".repeat(64),
                size_bytes: 20,
              },
              review_montage: {
                path: montagePath,
                sha256: sha256Bytes(montageBody),
                size_bytes: montageBody.byteLength,
              },
            },
          },
        }),
      );
      const response: BroadcastReviewWindowsResponse =
        reviewMode === "missing"
          ? {
              run_id: runId,
              status: "needs_review",
              reason: "missing_qualified_selective_review_queue",
              queue_sha256: null,
              review_item_count: 0,
              items: [],
            }
          : reviewMode === "populated"
            ? {
                run_id: runId,
                status: "ready",
                queue_sha256: "1".repeat(64),
                review_item_count: 1,
                items: [
                  {
                    review_item_id: "window-1",
                    variant_id: "full",
                    start_frame: 90,
                    end_frame: 140,
                    duration_seconds: 2,
                    compliance: "compliant",
                    priority: 1,
                    candidates,
                  },
                ],
              }
            : {
                run_id: runId,
                status: "ready",
                queue_sha256: "1".repeat(64),
                review_item_count: 0,
                items: [],
              };
      response.terminal_tail_review =
        terminalTailReviewMode === "not_required"
          ? { status: "not_required" }
          : terminalTailReviewMode === "required"
            ? {
                status: "required",
                reason: "terminal_decoder_shortfall_requires_operator_review",
                evidence: terminalTailEvidence,
              }
            : {
                status: "accepted",
                evidence: terminalTailEvidence,
                decision: "accept_terminal_shortfall",
                reviewer_id: "quality-lead",
                reviewed_at: BROADCAST_E2E_NOW,
                acknowledgement_sha256: "6".repeat(64),
              };
      await route.fulfill({
        json: response,
        headers: { "Cache-Control": "no-store" },
      });
      return;
    }
    const terminalTailReviewMatch = path.match(
      /^\/api\/runs\/([^/]+)\/broadcast\/terminal-tail-review$/,
    );
    if (method === "POST" && terminalTailReviewMatch) {
      const body = request.postDataJSON() as BroadcastTerminalTailReviewRequest;
      terminalTailReviewBodies.push(cloneJson(body));
      if (
        decodeURIComponent(terminalTailReviewMatch[1]) !==
          requireParent().run_id ||
        terminalTailReviewMode !== "required" ||
        body.decision !== "accept_terminal_shortfall" ||
        body.evidence_sha256 !== terminalTailEvidence.evidence_sha256 ||
        !body.reviewer_id.trim()
      ) {
        audit.contractViolations.push(
          "terminal-tail acknowledgement did not match current evidence",
        );
      }
      terminalTailReviewMode = "accepted";
      const parentRun = requireParent();
      parentRun.broadcast = {
        ...parentRun.broadcast,
        blocking_reasons: (parentRun.broadcast?.blocking_reasons ?? []).filter(
          (reason) =>
            reason !== "terminal_decoder_shortfall_requires_operator_review",
        ),
      };
      await route.fulfill({
        json: {
          run_id: requireParent().run_id,
          status: "completed",
          artifact: "terminal_tail_review.v1.json",
          details: { terminal_tail_review_sha256: "6".repeat(64) },
        } satisfies BroadcastOperationResponse,
      });
      return;
    }
    const reviewActionsMatch = path.match(
      /^\/api\/runs\/([^/]+)\/broadcast\/review-actions$/,
    );
    if (method === "POST" && reviewActionsMatch) {
      const body = request.postDataJSON() as BroadcastReviewActionsRequest;
      reviewBodies.push(cloneJson(body));
      submittedReviewDecisionSha256 = sha256Bytes(
        Buffer.from(canonicalJson(body), "utf8"),
      );
      if (
        decodeURIComponent(reviewActionsMatch[1]) !== requireParent().run_id
      ) {
        audit.contractViolations.push(
          "review actions targeted the wrong parent",
        );
      }
      if (body.queue_sha256 !== reviewEvidenceQueueSha256) {
        audit.contractViolations.push(
          "review actions did not bind the current review-evidence queue",
        );
      }
      await route.fulfill({
        json: {
          run_id: requireParent().run_id,
          status: "completed",
          artifact: "review_decisions.json",
          details: {
            review_decisions_sha256: submittedReviewDecisionSha256,
          },
        } satisfies BroadcastOperationResponse,
      });
      return;
    }
    const recomputeMatch = path.match(
      /^\/api\/runs\/([^/]+)\/broadcast\/trajectory-recompute$/,
    );
    if (method === "POST" && recomputeMatch) {
      const body =
        request.postDataJSON() as BroadcastTrajectoryRecomputeRequest;
      recomputeBodies.push(cloneJson(body));
      if (
        decodeURIComponent(recomputeMatch[1]) !== requireParent().run_id ||
        body.review_decisions_sha256 !== submittedReviewDecisionSha256
      ) {
        audit.contractViolations.push(
          "recompute lineage did not match review evidence",
        );
      }
      queueRecomputeChild();
      await route.fulfill({
        status: 202,
        json: {
          run_id: recomputeChild!.run_id,
          parent_run_id: requireParent().run_id,
          status: "queued",
        } satisfies BroadcastOperationResponse,
      });
      return;
    }
    const renderMatch = path.match(/^\/api\/runs\/([^/]+)\/broadcast\/render$/);
    if (method === "POST" && renderMatch) {
      const body = request.postDataJSON() as BroadcastRenderRequest;
      renderBodies.push(cloneJson(body));
      if (
        decodeURIComponent(renderMatch[1]) !== requireParent().run_id ||
        body.trajectory_generation_id !== trajectoryGenerationId
      ) {
        audit.contractViolations.push(
          "render lineage did not match the trajectory",
        );
      }
      if (renderConflictRunId) {
        const blocker = renderConflictRunId;
        renderConflictRunId = null;
        await route.fulfill({
          status: 409,
          json: { detail: `Another run is already active: ${blocker}` },
        });
        return;
      }
      queueRenderChild();
      await route.fulfill({
        status: 202,
        json: {
          run_id: renderChild!.run_id,
          parent_run_id: requireParent().run_id,
          status: "queued",
        } satisfies BroadcastOperationResponse,
      });
      return;
    }
    const artifactsMatch = path.match(/^\/api\/runs\/([^/]+)\/artifacts$/);
    if (method === "GET" && artifactsMatch) {
      const runId = decodeURIComponent(artifactsMatch[1]);
      captureAuthoritativeRead("artifact-list", request);
      if (runId === trialRun.run_id) {
        await route.fulfill({ json: trialArtifacts });
        return;
      }
      if (parent && runId === parent.run_id) {
        if (
          currentPhase === "ready" &&
          url.searchParams.get("status_generation") !== enforcedStatusGeneration
        ) {
          await route.fulfill({
            status: 409,
            json: { detail: "stale generation" },
          });
          return;
        }
        await route.fulfill({
          json:
            currentPhase === "ready"
              ? (parent.artifacts ?? [])
              : reviewMode === "populated" &&
                  (currentPhase === "needs_review" ||
                    currentPhase === "recomputing")
                ? montageArtifacts
                : [],
          headers: { "Cache-Control": "no-store" },
        });
        return;
      }
      await route.fulfill({ status: 404, json: { detail: "missing" } });
      return;
    }
    const auditMatch = path.match(/^\/api\/runs\/([^/]+)\/ball-audit$/);
    if (method === "GET" && auditMatch) {
      if (decodeURIComponent(auditMatch[1]) !== trialRun.run_id) {
        audit.contractViolations.push(
          "ball audit requested for a non-trial run",
        );
      }
      await route.fulfill({
        json: {
          schema_version: "1.0",
          generated_at: BROADCAST_E2E_NOW,
          summary: {
            frame_count: 300,
            source_count: 2,
            tracklet_count: 0,
            suspicious_tracklet_count: 0,
            review_event_count: 0,
            lost_gap_count: 0,
            max_step_px: 20,
          },
          sources: [],
          tracklets: [],
          review_events: [],
        },
      });
      return;
    }
    const artifactMatch = path.match(/^\/api\/runs\/([^/]+)\/artifacts\/(.+)$/);
    if ((method === "GET" || method === "HEAD") && artifactMatch) {
      const runId = decodeURIComponent(artifactMatch[1]);
      const name = decodeURIComponent(artifactMatch[2]);
      if (
        name !== "follow_cam.webm" &&
        name !== "broadcast.mp4" &&
        !name.endsWith(".svg")
      ) {
        captureAuthoritativeRead("artifact", request);
      }
      if (runId === trialRun.run_id) {
        if (name === "run_manifest.json") {
          await route.fulfill({ json: { run_id: trialRun.run_id } });
          return;
        }
        if (name === "metrics_report.json") {
          await route.fulfill({ json: { quality_gate: { status: "warn" } } });
          return;
        }
        if (name === "ball_track.csv" || name === "ball_track.cleaned.csv") {
          await route.fulfill({
            contentType: "text/csv",
            body: name.includes("cleaned")
              ? trialCleanedTrackCsv
              : trialRawTrackCsv,
          });
          return;
        }
        if (name === "follow_cam.webm") {
          await fulfillByteRange(route, videoBytes, videoFixture.contentType);
          return;
        }
      }
      if (parent && runId === parent.run_id && montageBodies.has(name)) {
        await route.fulfill({
          status: 200,
          body: montageBodies.get(name)!,
          contentType: "image/svg+xml",
          headers: { "Cache-Control": "no-store" },
        });
        return;
      }
      if (parent && runId === parent.run_id && artifactBodies.has(name)) {
        const generation = url.searchParams.get("status_generation");
        const range = request.headers()["range"] ?? null;
        artifactReads.push({
          method,
          name,
          statusGeneration: generation,
          range,
        });
        if (
          currentPhase === "ready" &&
          generation !== enforcedStatusGeneration
        ) {
          await route.fulfill({
            status: 409,
            json: { detail: "stale generation" },
          });
          return;
        }
        const bytes = artifactBodies.get(name)!;
        if (method === "HEAD") {
          await route.fulfill({
            status: 200,
            headers: {
              "Cache-Control": "no-store",
              "Content-Length": String(bytes.byteLength),
            },
          });
          return;
        }
        if (name === "broadcast.mp4") {
          await fulfillByteRange(route, bytes, videoFixture.contentType);
        } else {
          await route.fulfill({
            status: 200,
            body: bytes,
            contentType: name.endsWith(".json")
              ? "application/json"
              : "application/octet-stream",
            headers: { "Cache-Control": "no-store" },
          });
        }
        return;
      }
      await route.fulfill({ status: 404, json: { detail: "missing" } });
      return;
    }
    const runMatch = path.match(/^\/api\/runs\/([^/]+)$/);
    if (method === "GET" && runMatch) {
      captureAuthoritativeRead("run", request);
      const runId = decodeURIComponent(runMatch[1]);
      const run = runs().find((candidate) => candidate.run_id === runId);
      if (!run) {
        await route.fulfill({ status: 404, json: { detail: "missing" } });
        return;
      }
      await route.fulfill({
        json: run,
        headers: { "Cache-Control": "no-store" },
      });
      return;
    }
    audit.unhandledApi.push(key);
    await route.fulfill({
      status: 501,
      json: { detail: `Unhandled zero-review scenario request: ${key}` },
    });
  });

  return {
    audit,
    createBodies,
    reviewBodies,
    terminalTailReviewBodies,
    recomputeBodies,
    renderBodies,
    cancelIds,
    healthActiveRunIds,
    configReads,
    authoritativeReads,
    artifactReads,
    reviewWindowReads,
    reviewEvidenceImportBodies,
    reviewEvidenceRevokes,
    qualityStable,
    qualityBytes,
    statusGeneration,
    flipReadyGeneration: () => {
      enforcedStatusGeneration = "9".repeat(64);
      return enforcedStatusGeneration;
    },
    parentRunId: () => requireParent().run_id,
    phase: () => currentPhase,
    blockingReasons: () => [
      ...(requireParent().broadcast?.blocking_reasons ?? []),
    ],
    setConfigMode,
    usePopulatedReview,
    useMissingReviewQueue,
    requireTerminalTailReview,
    allowOneTrajectoryReadyReviewRead,
    setDeliveryMode,
    failNextCreateWithConflict,
    failNextRenderWithConflict,
    clearForeignBlocker,
    setRunning,
    setFailed,
    setNeedsReview,
    completeRecomputeChildOnly,
    publishTrajectoryReady,
    completeRecompute,
    completeRenderChildOnly,
    publishReady,
    completeRender,
  };
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

test("completes a zero-review full production and verifies the product", async ({
  page,
}) => {
  const fixture = await buildConfirmedBroadcastDraft();
  const scenario = await installZeroReviewBroadcastScenario(page, fixture);
  await page.addInitScript(
    ({ key, draft }) => {
      if (localStorage.getItem(key) === null) {
        localStorage.setItem(key, JSON.stringify(draft));
      }
    },
    { key: PRODUCTION_DRAFT_STORAGE_KEY, draft: fixture.draft },
  );

  await page.goto("/production");
  await expect(
    page.getByRole("heading", { name: "Trial and tuning" }),
  ).toBeVisible();
  const next = page.getByRole("button", { name: "Next" });
  await expect(next).toBeEnabled();
  await next.click();
  await expect(
    page.getByRole("heading", {
      name: "Full tracking and review",
      exact: true,
    }),
  ).toBeVisible();

  await page.getByTestId("production-start-full-run").dblclick();
  await expect.poll(() => scenario.createBodies.length).toBe(1);
  const create = scenario.createBodies[0];
  expect(create).toMatchObject({
    config_name: fixture.config.name,
    input_video: fixture.draft.source!.path,
    parent_run_id: ACCEPTED_TRIAL_RUN_ID,
    enable_postprocess: true,
    enable_follow_cam: false,
    start_frame: 0,
    max_frames: null,
    pipeline_mode: "broadcast_hybrid",
    quality_profile: "stable_broadcast",
    max_manual_review_windows: 30,
  });
  expect(create.output_dir_name).toMatch(
    /^production_full_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
  );
  expect("config_patch" in create).toBe(false);
  expect(create.calibration_confirmation?.confirmed_sample_frames).toHaveLength(
    3,
  );
  await expect(page).toHaveURL(
    new RegExp(`/production\\?run=${scenario.parentRunId()}$`),
  );
  expect(
    scenario.configReads.some((read) => read.name === fixture.config.name),
  ).toBe(true);
  expect(
    scenario.configReads
      .filter((read) => read.name === fixture.config.name)
      .some((read) => /no-cache|no-store/i.test(read.cacheControl)),
  ).toBe(true);

  scenario.setRunning();
  await page.reload();
  await expect(page.getByTestId("production-full-run-status")).toHaveText(
    "Tracking",
  );
  await expect(page).toHaveURL(
    new RegExp(`/production\\?run=${scenario.parentRunId()}$`),
  );
  expect(scenario.createBodies).toHaveLength(1);

  scenario.setNeedsReview();
  await expect(page.getByTestId("production-full-run-status")).toHaveText(
    "Needs review",
    { timeout: 15_000 },
  );
  const reviewStep = page.getByTestId("broadcast-review-step");
  await expect(reviewStep).toContainText("No review candidates were returned");
  await page
    .getByRole("button", { name: "Continue without candidate decisions" })
    .dblclick();
  await expect.poll(() => scenario.reviewBodies.length).toBe(1);
  await expect.poll(() => scenario.recomputeBodies.length).toBe(1);
  expect(scenario.reviewBodies[0]).toEqual({
    queue_sha256: "1".repeat(64),
    actions: [],
  });
  expect(scenario.phase()).toBe("recomputing");
  await expect(page.getByTestId("production-full-run-status")).toHaveText(
    "Recomputing trajectory",
  );

  scenario.completeRecompute();
  await expect(page.getByTestId("production-full-run-status")).toHaveText(
    "Trajectory ready",
    { timeout: 15_000 },
  );
  await page.getByRole("button", { name: "Render broadcast" }).dblclick();
  await expect.poll(() => scenario.renderBodies.length).toBe(1);
  expect(scenario.renderBodies[0]).toEqual({
    trajectory_generation_id: TRAJECTORY_GENERATION_ID,
    target_width: 1920,
    target_height: 1080,
  });
  expect(scenario.phase()).toBe("rendering");
  await expect(page.getByTestId("production-full-run-status")).toHaveText(
    "Rendering",
  );

  scenario.completeRender();
  const product = page.getByTestId("production-product-ready");
  await expect(product).toBeVisible({ timeout: 20_000 });
  await expect(page.getByTestId("production-full-run-status")).toHaveText(
    "Product ready",
  );
  await expect(product.getByText("Quality report verified")).toBeVisible();
  await expect(product.getByText("source_audio_not_preserved")).toBeVisible();
  const video = product.getByLabel("Broadcast video");
  await expect(video).toBeVisible();
  await expect
    .poll(async () =>
      video.evaluate((element: HTMLVideoElement) => ({
        readyState: element.readyState,
        duration: element.duration,
      })),
    )
    .toMatchObject({
      readyState: expect.any(Number),
      duration: expect.any(Number),
    });
  expect(
    await video.evaluate((element: HTMLVideoElement) => element.readyState),
  ).toBeGreaterThanOrEqual(3);

  const downloads = product.getByRole("link");
  await expect(downloads).toHaveCount(8);
  const downloadHrefs = await downloads.evaluateAll((links) =>
    links.map((link) => link.getAttribute("href")),
  );
  expect(downloadHrefs.sort()).toEqual(
    [...PRODUCTION_BROADCAST_DELIVERY_ARTIFACTS]
      .map(
        (name) =>
          `/api/runs/${scenario.parentRunId()}/artifacts/${name}?status_generation=${scenario.statusGeneration}`,
      )
      .sort(),
  );
  expect(await sha256Text(canonicalJson(scenario.qualityStable))).toBe(
    scenario.statusGeneration,
  );
  expect(JSON.parse(scenario.qualityBytes.toString("utf8"))).toMatchObject({
    status: "ready",
    status_generation: scenario.statusGeneration,
    blocking_reasons: [],
  });
  expect(
    scenario.artifactReads.some(
      (read) =>
        read.name === "broadcast_quality_report.json" &&
        read.statusGeneration === scenario.statusGeneration,
    ),
  ).toBe(true);
  expect(
    scenario.artifactReads.some(
      (read) =>
        read.name === "broadcast.mp4" &&
        read.statusGeneration === scenario.statusGeneration &&
        read.range?.startsWith("bytes=") === true,
    ),
  ).toBe(true);

  const requiredAuthoritativeKinds = [
    "config",
    "run-list",
    "run",
    "artifact-list",
    "artifact",
    "review",
  ] as const;
  for (const kind of requiredAuthoritativeKinds) {
    const reads = scenario.authoritativeReads.filter(
      (read) => read.kind === kind,
    );
    expect(reads.length, `missing authoritative ${kind} GET`).toBeGreaterThan(
      0,
    );
    expect(
      reads.some((read) => /no-cache|no-store/i.test(read.cacheControl)),
      `no fresh ${kind} GET bypassed the HTTP cache`,
    ).toBe(true);
  }

  const staleGeneration = scenario.statusGeneration;
  for (let index = 0; index < 12; index += 1) {
    allowRuntimeError(
      page,
      /^console\.error: Failed to load resource: the server responded with a status of 409 \(Conflict\)$/,
    );
  }
  const nextGeneration = scenario.flipReadyGeneration();
  expect(nextGeneration).not.toBe(staleGeneration);
  const staleStatuses = await page.evaluate(
    async ({ parentRunId, generation }) => {
      const base = `/api/runs/${parentRunId}/artifacts`;
      const query = `status_generation=${generation}`;
      const [list, get, head] = await Promise.all([
        fetch(`${base}?${query}`, { cache: "no-store" }),
        fetch(`${base}/broadcast_quality_report.json?${query}`, {
          cache: "no-store",
        }),
        fetch(`${base}/broadcast_quality_report.json?${query}`, {
          method: "HEAD",
          cache: "no-store",
        }),
      ]);
      return [list.status, get.status, head.status];
    },
    { parentRunId: scenario.parentRunId(), generation: staleGeneration },
  );
  expect(staleStatuses).toEqual([409, 409, 409]);

  await expectNoSeriousAccessibilityFindings(page);
  expect(scenario.createBodies).toHaveLength(1);
  expect(scenario.reviewBodies).toHaveLength(1);
  expect(scenario.recomputeBodies).toHaveLength(1);
  expect(scenario.renderBodies).toHaveLength(1);
  expect(scenario.audit.unhandledApi).toEqual([]);
  expect(scenario.audit.contractViolations).toEqual([]);
});

test("requires explicit terminal-tail acknowledgement before zero-candidate recompute", async ({
  page,
}) => {
  const fixture = await buildConfirmedBroadcastDraft();
  const scenario = await installZeroReviewBroadcastScenario(page, fixture);
  scenario.requireTerminalTailReview();
  await page.addInitScript(
    ({ key, draft }) => {
      localStorage.setItem(key, JSON.stringify(draft));
    },
    { key: PRODUCTION_DRAFT_STORAGE_KEY, draft: fixture.draft },
  );

  await page.goto("/production");
  await page.getByRole("button", { name: "Next" }).click();
  await page.getByTestId("production-start-full-run").click();
  await expect.poll(() => scenario.createBodies.length).toBe(1);
  scenario.setNeedsReview();

  const tailReview = page.getByTestId("production-terminal-tail-review");
  await expect(tailReview).toBeVisible({ timeout: 15_000 });
  await expect(tailReview).toContainText("reports 5194 frames");
  await expect(tailReview).toContainText("final 2 source frames (0.1s)");
  const continueButton = page.getByRole("button", {
    name: "Continue without candidate decisions",
  });
  await expect(continueButton).toBeDisabled();
  expect(scenario.terminalTailReviewBodies).toHaveLength(0);
  expect(scenario.reviewBodies).toHaveLength(0);
  expect(scenario.recomputeBodies).toHaveLength(0);

  const reviewer = page.getByLabel("Reviewer ID");
  await reviewer.fill("quality-lead");
  await page
    .getByLabel(
      "I reviewed and accept that the verified product excludes this damaged terminal tail.",
    )
    .check();
  await expect(continueButton).toBeEnabled();
  await continueButton.click();

  await expect.poll(() => scenario.terminalTailReviewBodies.length).toBe(1);
  await expect.poll(() => scenario.reviewBodies.length).toBe(1);
  await expect.poll(() => scenario.recomputeBodies.length).toBe(1);
  expect(scenario.terminalTailReviewBodies[0]).toEqual({
    decision: "accept_terminal_shortfall",
    reviewer_id: "quality-lead",
    evidence_sha256: "5".repeat(64),
  });
  expect(scenario.audit.unhandledApi).toEqual([]);
  expect(scenario.audit.contractViolations).toEqual([]);
});

test("acknowledges the terminal tail independently when the qualified review queue is missing", async ({
  page,
}) => {
  const fixture = await buildConfirmedBroadcastDraft();
  const scenario = await installZeroReviewBroadcastScenario(page, fixture);
  scenario.useMissingReviewQueue();
  scenario.requireTerminalTailReview();
  await page.addInitScript(
    ({ key, draft }) => {
      localStorage.setItem(key, JSON.stringify(draft));
    },
    { key: PRODUCTION_DRAFT_STORAGE_KEY, draft: fixture.draft },
  );

  await page.goto("/production");
  await page.getByRole("button", { name: "Next" }).click();
  await page.getByTestId("production-start-full-run").click();
  await expect.poll(() => scenario.createBodies.length).toBe(1);
  scenario.setNeedsReview();

  const tailReview = page.getByTestId("production-terminal-tail-review");
  await expect(tailReview).toBeVisible({ timeout: 15_000 });
  const evidence = page.getByTestId("broadcast-review-evidence-step");
  await expect(evidence).toBeVisible();
  await expect(
    evidence.getByText("review_evidence_bundle_not_available"),
  ).toBeVisible();
  await expect(evidence).toContainText(
    "Provision a qualified review-evidence bundle in the managed inbox.",
  );
  await expect(page.getByTestId("broadcast-review-step")).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Continue without candidate decisions" }),
  ).toHaveCount(0);
  const acknowledge = page.getByRole("button", {
    name: "Confirm terminal source limitation",
  });
  await expect(acknowledge).toBeDisabled();

  await page.getByLabel("Reviewer ID").fill("quality-lead");
  await page
    .getByLabel(
      "I reviewed and accept that the verified product excludes this damaged terminal tail.",
    )
    .check();
  await expect(acknowledge).toBeEnabled();
  await acknowledge.dblclick();

  await expect.poll(() => scenario.terminalTailReviewBodies.length).toBe(1);
  await expect(
    page.getByText("Terminal source limitation accepted"),
  ).toBeVisible();
  expect(scenario.terminalTailReviewBodies[0]).toEqual({
    decision: "accept_terminal_shortfall",
    reviewer_id: "quality-lead",
    evidence_sha256: "5".repeat(64),
  });
  expect(scenario.reviewBodies).toHaveLength(0);
  expect(scenario.recomputeBodies).toHaveLength(0);
  expect(scenario.blockingReasons()).toEqual([
    "missing_qualified_selective_review_queue",
  ]);
  expect(scenario.audit.unhandledApi).toEqual([]);
  expect(scenario.audit.contractViolations).toEqual([]);
});

test("[PR4 C2] blocks full production when the canonical config changes or disappears", async ({
  page,
}) => {
  allowRuntimeError(
    page,
    /^console\.error: Failed to load resource: the server responded with a status of 404 \(Not Found\)$/,
  );
  const fixture = await buildConfirmedBroadcastDraft();
  const scenario = await installZeroReviewBroadcastScenario(page, fixture);
  await page.addInitScript(
    ({ key, draft }) => {
      if (localStorage.getItem(key) === null) {
        localStorage.setItem(key, JSON.stringify(draft));
      }
    },
    { key: PRODUCTION_DRAFT_STORAGE_KEY, draft: fixture.draft },
  );
  await page.goto("/production");
  const next = page.getByRole("button", { name: "Next" });
  await expect(next).toBeEnabled();
  await next.click();
  const start = page.getByTestId("production-start-full-run");
  const initialConfigReads = scenario.configReads.length;
  const cases = [
    {
      mode: "tampered" as const,
      message: "The confirmed configuration no longer matches",
    },
    {
      mode: "missing" as const,
      message: "The confirmed configuration could not be freshly verified",
    },
  ];
  for (const configCase of cases) {
    scenario.setConfigMode(configCase.mode);
    await start.click();
    await expect(page.getByTestId("production-full-run-error")).toContainText(
      configCase.message,
    );
    expect(scenario.createBodies).toHaveLength(0);
  }
  expect(scenario.configReads).toHaveLength(initialConfigReads + cases.length);
  expect(
    scenario.configReads
      .slice(initialConfigReads)
      .every((read) => read.name === fixture.config.name),
  ).toBe(true);
  await expectNoSeriousAccessibilityFindings(page);
  expect(scenario.reviewBodies).toEqual([]);
  expect(scenario.recomputeBodies).toEqual([]);
  expect(scenario.renderBodies).toEqual([]);
  expect(scenario.cancelIds).toEqual([]);
  expect(scenario.audit.unhandledApi).toEqual([]);
  expect(scenario.audit.contractViolations).toEqual([]);
});

test("[PR4 C2] reconciles a create conflict before explicitly retrying with a new UUID", async ({
  page,
}) => {
  allowRuntimeError(
    page,
    /^console\.error: Failed to load resource: the server responded with a status of 409 \(Conflict\)$/,
  );
  const fixture = await buildConfirmedBroadcastDraft();
  const scenario = await installZeroReviewBroadcastScenario(page, fixture);
  await page.addInitScript(
    ({ key, draft }) => {
      if (localStorage.getItem(key) === null) {
        localStorage.setItem(key, JSON.stringify(draft));
      }
    },
    { key: PRODUCTION_DRAFT_STORAGE_KEY, draft: fixture.draft },
  );
  await page.goto("/production");
  const next = page.getByRole("button", { name: "Next" });
  await expect(next).toBeEnabled();
  await next.click();

  scenario.failNextCreateWithConflict("foreign-blocker");
  const healthReadStart = scenario.healthActiveRunIds.length;
  await page.getByTestId("production-start-full-run").dblclick();
  await expect.poll(() => scenario.createBodies.length).toBe(1);
  await expect(page.getByTestId("production-full-run-error")).toContainText(
    "foreign-blocker",
  );
  const firstRequest = scenario.createBodies[0];
  expect(scenario.healthActiveRunIds.slice(healthReadStart)).toEqual([
    null,
    "foreign-blocker",
  ]);
  const blockedDraft = await page.evaluate((key) => {
    const raw = localStorage.getItem(key);
    return raw ? JSON.parse(raw) : null;
  }, PRODUCTION_DRAFT_STORAGE_KEY);
  expect(blockedDraft).toMatchObject({
    source: fixture.draft.source,
    trial: { accepted: { run_id: ACCEPTED_TRIAL_RUN_ID } },
    confirmed_config: {
      name: fixture.config.name,
      sha256: fixture.draft.confirmed_config!.sha256,
    },
    full_run: {
      attempts: [],
      current_run_id: null,
      pending_submission: {
        expected_run_id: firstRequest.output_dir_name,
      },
    },
  });

  scenario.clearForeignBlocker();
  await page.getByTestId("production-retry-full-run").dblclick();
  await expect.poll(() => scenario.createBodies.length).toBe(2);
  const secondRequest = scenario.createBodies[1];
  expect(secondRequest.output_dir_name).not.toBe(firstRequest.output_dir_name);
  for (const body of scenario.createBodies) {
    expect(body).toMatchObject({
      config_name: fixture.config.name,
      input_video: fixture.draft.source!.path,
      parent_run_id: ACCEPTED_TRIAL_RUN_ID,
      pipeline_mode: "broadcast_hybrid",
      quality_profile: "stable_broadcast",
      start_frame: 0,
      max_frames: null,
      enable_follow_cam: false,
    });
    expect("config_patch" in body).toBe(false);
  }
  await expect(page).toHaveURL(
    new RegExp(`/production\\?run=${scenario.parentRunId()}$`),
  );
  await expectNoSeriousAccessibilityFindings(page);
  expect(scenario.reviewBodies).toEqual([]);
  expect(scenario.recomputeBodies).toEqual([]);
  expect(scenario.renderBodies).toEqual([]);
  expect(scenario.cancelIds).toEqual([]);
  expect(scenario.audit.unhandledApi).toEqual([]);
  expect(scenario.audit.contractViolations).toEqual([]);
});

test("[PR4 C2] cancels the tracking parent once and restores it without rebuilding", async ({
  page,
}) => {
  const fixture = await buildConfirmedBroadcastDraft();
  const scenario = await installZeroReviewBroadcastScenario(page, fixture);
  await page.addInitScript(
    ({ key, draft }) => {
      if (localStorage.getItem(key) === null) {
        localStorage.setItem(key, JSON.stringify(draft));
      }
    },
    { key: PRODUCTION_DRAFT_STORAGE_KEY, draft: fixture.draft },
  );
  await page.goto("/production");
  const next = page.getByRole("button", { name: "Next" });
  await expect(next).toBeEnabled();
  await next.click();
  await page.getByTestId("production-start-full-run").click();
  await expect.poll(() => scenario.createBodies.length).toBe(1);
  expect(scenario.createBodies[0]).toMatchObject({
    config_name: fixture.config.name,
    input_video: fixture.draft.source!.path,
    parent_run_id: ACCEPTED_TRIAL_RUN_ID,
    pipeline_mode: "broadcast_hybrid",
    quality_profile: "stable_broadcast",
    start_frame: 0,
    max_frames: null,
    enable_follow_cam: false,
  });
  expect("config_patch" in scenario.createBodies[0]).toBe(false);
  const parentRunId = scenario.parentRunId();
  scenario.setRunning();
  await expect(page.getByText("50.0%")).toBeVisible({ timeout: 15_000 });
  await page.getByRole("button", { name: "Cancel full tracking" }).dblclick();
  await expect.poll(() => scenario.cancelIds.length).toBe(1);
  expect(scenario.cancelIds).toEqual([parentRunId]);
  await expect(page.getByTestId("production-full-run-status")).toHaveText(
    "Cancelled",
    { timeout: 15_000 },
  );

  await page.reload();
  await expect(page.getByTestId("production-full-run-status")).toHaveText(
    "Cancelled",
  );
  expect(scenario.createBodies).toHaveLength(1);
  expect(scenario.cancelIds).toHaveLength(1);
  const restoredDraft = await page.evaluate((key) => {
    const raw = localStorage.getItem(key);
    return raw ? JSON.parse(raw) : null;
  }, PRODUCTION_DRAFT_STORAGE_KEY);
  expect(restoredDraft.full_run).toMatchObject({
    current_run_id: parentRunId,
    attempts: [
      {
        run_id: parentRunId,
        parent_trial_run_id: ACCEPTED_TRIAL_RUN_ID,
        last_observed: {
          run_status: "cancelled",
          workflow_state: "cancelled",
        },
      },
    ],
  });
  await expectNoSeriousAccessibilityFindings(page);
  expect(scenario.reviewBodies).toEqual([]);
  expect(scenario.recomputeBodies).toEqual([]);
  expect(scenario.renderBodies).toEqual([]);
  expect(scenario.audit.unhandledApi).toEqual([]);
  expect(scenario.audit.contractViolations).toEqual([]);
});

test("[PR4 C2] retries a failed restored parent with a new full-run UUID and preserved lineage", async ({
  page,
}) => {
  const fixture = await buildConfirmedBroadcastDraft();
  const scenario = await installZeroReviewBroadcastScenario(page, fixture);
  await page.addInitScript(
    ({ key, draft }) => {
      if (localStorage.getItem(key) === null) {
        localStorage.setItem(key, JSON.stringify(draft));
      }
    },
    { key: PRODUCTION_DRAFT_STORAGE_KEY, draft: fixture.draft },
  );
  await page.goto("/production");
  const next = page.getByRole("button", { name: "Next" });
  await expect(next).toBeEnabled();
  await next.click();
  await page.getByTestId("production-start-full-run").click();
  await expect.poll(() => scenario.createBodies.length).toBe(1);
  const firstRunId = scenario.parentRunId();
  scenario.setFailed();
  await page.reload();
  await expect(page.getByTestId("production-full-run-status")).toHaveText(
    "Failed",
  );

  await page.getByTestId("production-retry-full-run").click();
  await expect.poll(() => scenario.createBodies.length).toBe(2);
  const secondRunId = scenario.parentRunId();
  expect(secondRunId).not.toBe(firstRunId);
  expect(scenario.createBodies.map((body) => body.parent_run_id)).toEqual([
    ACCEPTED_TRIAL_RUN_ID,
    ACCEPTED_TRIAL_RUN_ID,
  ]);
  expect(scenario.createBodies.every((body) => !("config_patch" in body))).toBe(
    true,
  );
  for (const body of scenario.createBodies) {
    expect(body).toMatchObject({
      config_name: fixture.config.name,
      input_video: fixture.draft.source!.path,
      pipeline_mode: "broadcast_hybrid",
      quality_profile: "stable_broadcast",
      start_frame: 0,
      max_frames: null,
      enable_follow_cam: false,
    });
  }
  await expect(page).toHaveURL(new RegExp(`/production\\?run=${secondRunId}$`));
  await expect
    .poll(() =>
      page.evaluate((key) => {
        const raw = localStorage.getItem(key);
        if (!raw) return null;
        const draft = JSON.parse(raw);
        return {
          current: draft.full_run?.current_run_id,
          attempts: draft.full_run?.attempts?.map(
            (attempt: {
              run_id: string;
              parent_trial_run_id: string;
              last_observed: { workflow_state: string };
            }) => ({
              run_id: attempt.run_id,
              parent_trial_run_id: attempt.parent_trial_run_id,
              workflow_state: attempt.last_observed.workflow_state,
            }),
          ),
        };
      }, PRODUCTION_DRAFT_STORAGE_KEY),
    )
    .toEqual({
      current: secondRunId,
      attempts: [
        {
          run_id: firstRunId,
          parent_trial_run_id: ACCEPTED_TRIAL_RUN_ID,
          workflow_state: "failed",
        },
        {
          run_id: secondRunId,
          parent_trial_run_id: ACCEPTED_TRIAL_RUN_ID,
          workflow_state: "tracking",
        },
      ],
    });
  await expectNoSeriousAccessibilityFindings(page);
  expect(scenario.cancelIds).toEqual([]);
  expect(scenario.reviewBodies).toEqual([]);
  expect(scenario.recomputeBodies).toEqual([]);
  expect(scenario.renderBodies).toEqual([]);
  expect(scenario.audit.unhandledApi).toEqual([]);
  expect(scenario.audit.contractViolations).toEqual([]);
});

test("[PR4 C3] completes populated review through delayed child commits and verifies the product", async ({
  page,
}) => {
  allowRuntimeError(
    page,
    /^console\.error: Failed to load resource: the server responded with a status of 409 \(Conflict\)$/,
  );
  const conflictResponses: Array<{ method: string; url: string }> = [];
  page.on("response", (response) => {
    if (response.status() === 409) {
      conflictResponses.push({
        method: response.request().method(),
        url: response.url(),
      });
    }
  });
  const fixture = await buildConfirmedBroadcastDraft();
  const scenario = await installZeroReviewBroadcastScenario(page, fixture);
  scenario.usePopulatedReview();
  await page.addInitScript(
    ({ key, draft }) => {
      if (localStorage.getItem(key) === null) {
        localStorage.setItem(key, JSON.stringify(draft));
      }
    },
    { key: PRODUCTION_DRAFT_STORAGE_KEY, draft: fixture.draft },
  );
  await page.goto("/production");
  const next = page.getByRole("button", { name: "Next" });
  await expect(next).toBeEnabled();
  await next.click();
  await page.getByTestId("production-start-full-run").dblclick();
  await expect.poll(() => scenario.createBodies.length).toBe(1);
  const parentRunId = scenario.parentRunId();
  scenario.setNeedsReview();
  await expect(page.getByTestId("production-full-run-status")).toHaveText(
    "Needs review",
    { timeout: 15_000 },
  );

  const review = page.getByTestId("broadcast-review-step");
  const montages = review.getByAltText(/Verified candidate review montage/);
  await expect(montages).toHaveCount(2);
  await expect
    .poll(async () =>
      montages.evaluateAll((images: HTMLImageElement[]) =>
        images.every((image) => image.complete && image.naturalWidth > 0),
      ),
    )
    .toBe(true);
  await page.getByLabel("Reviewer id").fill("quality-lead");
  const candidates = review.locator("article");
  await candidates.nth(0).getByRole("radio", { name: "Confirm ball" }).click();
  await candidates
    .nth(1)
    .getByRole("radio", { name: "Reject as noise" })
    .click();
  await candidates
    .nth(1)
    .getByRole("combobox", { name: "Noise subtype" })
    .click();
  await page.getByRole("option", { name: "Field line or mark" }).click();
  await page
    .getByRole("button", { name: "Submit review decisions" })
    .dblclick();
  await expect.poll(() => scenario.reviewBodies.length).toBe(1);
  await expect.poll(() => scenario.recomputeBodies.length).toBe(1);
  const actions = scenario.reviewBodies[0].actions;
  expect(
    actions.map(({ created_at: _createdAt, ...action }) => action),
  ).toEqual([
    {
      action_id: "broadcast-review-0001",
      review_item_id: "window-1",
      candidate_id: "candidate-1",
      reviewer_id: "quality-lead",
      action: "confirm_ball",
    },
    {
      action_id: "broadcast-review-0002",
      review_item_id: "window-1",
      candidate_id: "candidate-2",
      reviewer_id: "quality-lead",
      action: "reject_noise",
      noise_subtype: "field_line_or_mark",
    },
  ]);
  expect(
    actions.every(
      (action) =>
        typeof action.created_at === "string" &&
        Number.isFinite(Date.parse(action.created_at)),
    ),
  ).toBe(true);
  expect(scenario.recomputeBodies[0]).toEqual({
    review_decisions_sha256: sha256Bytes(
      Buffer.from(canonicalJson(scenario.reviewBodies[0]), "utf8"),
    ),
  });
  expect(scenario.phase()).toBe("recomputing");
  await page.reload();
  await expect(page.getByTestId("production-full-run-status")).toHaveText(
    "Recomputing trajectory",
  );
  scenario.completeRecomputeChildOnly();
  await page.reload();
  await expect(page.getByTestId("production-full-run-status")).toHaveText(
    "Recomputing trajectory",
  );
  scenario.publishTrajectoryReady();
  await expect(page.getByTestId("production-full-run-status")).toHaveText(
    "Trajectory ready",
    { timeout: 15_000 },
  );

  await page.getByRole("button", { name: "Render broadcast" }).dblclick();
  await expect.poll(() => scenario.renderBodies.length).toBe(1);
  expect(scenario.phase()).toBe("rendering");
  await page.reload();
  await expect(page.getByTestId("production-full-run-status")).toHaveText(
    "Rendering",
  );
  scenario.completeRenderChildOnly();
  await page.reload();
  await expect(page.getByTestId("production-full-run-status")).toHaveText(
    "Rendering",
  );
  scenario.publishReady();
  const product = page.getByTestId("production-product-ready");
  await expect(product).toBeVisible({ timeout: 20_000 });
  await expect(product.getByLabel("Broadcast video")).toBeVisible();
  await expect(product.getByRole("link")).toHaveCount(8);
  await expect(page).toHaveURL(new RegExp(`/production\\?run=${parentRunId}$`));
  expect(scenario.createBodies).toHaveLength(1);
  expect(scenario.reviewBodies).toHaveLength(1);
  expect(scenario.recomputeBodies).toHaveLength(1);
  expect(scenario.renderBodies).toHaveLength(1);
  expect(scenario.cancelIds).toEqual([]);
  await expectNoSeriousAccessibilityFindings(page);
  expect(scenario.audit.unhandledApi).toEqual([]);
  expect(scenario.audit.contractViolations).toEqual([]);
  expect(conflictResponses.length).toBeLessThanOrEqual(1);
  expect(conflictResponses).toEqual(
    conflictResponses.length === 0
      ? []
      : [
          {
            method: "GET",
            url: `http://127.0.0.1:4173/api/runs/${parentRunId}/artifacts`,
          },
        ],
  );
});

test("[PR4 C3] keeps trajectory evidence safe after a render conflict and queues only an explicit retry", async ({
  page,
}) => {
  allowRuntimeError(
    page,
    /^console\.error: Failed to load resource: the server responded with a status of 409 \(Conflict\)$/,
  );
  const fixture = await buildConfirmedBroadcastDraft();
  const scenario = await installZeroReviewBroadcastScenario(page, fixture);
  scenario.allowOneTrajectoryReadyReviewRead();
  await page.addInitScript(
    ({ key, draft }) => {
      if (localStorage.getItem(key) === null) {
        localStorage.setItem(key, JSON.stringify(draft));
      }
    },
    { key: PRODUCTION_DRAFT_STORAGE_KEY, draft: fixture.draft },
  );
  await page.goto("/production");
  const next = page.getByRole("button", { name: "Next" });
  await expect(next).toBeEnabled();
  await next.click();
  await page.getByTestId("production-start-full-run").click();
  await expect.poll(() => scenario.createBodies.length).toBe(1);
  const parentRunId = scenario.parentRunId();
  scenario.setNeedsReview();
  await expect(page.getByTestId("broadcast-review-step")).toBeVisible({
    timeout: 15_000,
  });
  await page
    .getByRole("button", { name: "Continue without candidate decisions" })
    .click();
  await expect.poll(() => scenario.recomputeBodies.length).toBe(1);
  scenario.completeRecompute();
  const render = page.getByRole("button", { name: "Render broadcast" });
  await expect(render).toBeVisible({ timeout: 15_000 });
  scenario.failNextRenderWithConflict("foreign-render");
  await render.dblclick();
  await expect.poll(() => scenario.renderBodies.length).toBe(1);
  expect(scenario.phase()).toBe("trajectory_ready");
  await expect(render).toBeEnabled();
  await expect(page.getByTestId("production-product-ready")).toHaveCount(0);
  await expect(
    page.getByTestId("production-full-run-step").locator("video"),
  ).toHaveCount(0);
  await expect(
    page.getByTestId("production-full-run-step").getByRole("link"),
  ).toHaveCount(0);

  await render.dblclick();
  await expect.poll(() => scenario.renderBodies.length).toBe(2);
  expect(scenario.phase()).toBe("rendering");
  await expect(page.getByTestId("production-full-run-status")).toHaveText(
    "Rendering",
  );
  expect(scenario.renderBodies[0]).toEqual(scenario.renderBodies[1]);
  await expect(page).toHaveURL(new RegExp(`/production\\?run=${parentRunId}$`));
  expect(scenario.createBodies).toHaveLength(1);
  expect(scenario.reviewBodies).toHaveLength(1);
  expect(scenario.recomputeBodies).toHaveLength(1);
  expect(scenario.cancelIds).toEqual([]);
  await expectNoSeriousAccessibilityFindings(page);
  expect(scenario.audit.unhandledApi).toEqual([]);
  expect(scenario.audit.contractViolations).toEqual([]);
});

test("[PR4 C3] blocks a ready generation whose sealed delivery is missing broadcast video", async ({
  page,
}) => {
  allowRuntimeError(
    page,
    /^console\.error: Failed to load resource: the server responded with a status of 409 \(Conflict\)$/,
  );
  const conflictResponses: Array<{ method: string; url: string }> = [];
  page.on("response", (response) => {
    if (response.status() === 409) {
      conflictResponses.push({
        method: response.request().method(),
        url: response.url(),
      });
    }
  });
  const fixture = await buildConfirmedBroadcastDraft();
  const scenario = await installZeroReviewBroadcastScenario(page, fixture);
  scenario.setDeliveryMode("missing_video");
  scenario.allowOneTrajectoryReadyReviewRead();
  await page.addInitScript(
    ({ key, draft }) => {
      if (localStorage.getItem(key) === null) {
        localStorage.setItem(key, JSON.stringify(draft));
      }
    },
    { key: PRODUCTION_DRAFT_STORAGE_KEY, draft: fixture.draft },
  );
  await page.goto("/production");
  const next = page.getByRole("button", { name: "Next" });
  await expect(next).toBeEnabled();
  await next.click();
  await page.getByTestId("production-start-full-run").click();
  await expect.poll(() => scenario.createBodies.length).toBe(1);
  const parentRunId = scenario.parentRunId();
  scenario.setNeedsReview();
  await expect(page.getByTestId("broadcast-review-step")).toBeVisible({
    timeout: 15_000,
  });
  await page
    .getByRole("button", { name: "Continue without candidate decisions" })
    .click();
  await expect.poll(() => scenario.recomputeBodies.length).toBe(1);
  scenario.completeRecompute();
  await page.getByRole("button", { name: "Render broadcast" }).click();
  await expect.poll(() => scenario.renderBodies.length).toBe(1);
  scenario.completeRender();

  const blocked = page.getByTestId("production-delivery-blocked");
  await expect(blocked).toBeVisible({ timeout: 20_000 });
  await expect(blocked).toContainText("artifact_set_mismatch");
  expect(scenario.statusGeneration).toMatch(/^[0-9a-f]{64}$/);
  await expect(page.getByTestId("production-product-ready")).toHaveCount(0);
  const fullStep = page.getByTestId("production-full-run-step");
  await expect(fullStep.locator("video")).toHaveCount(0);
  await expect(fullStep.getByRole("link")).toHaveCount(0);
  const restored = await page.evaluate((key) => {
    const raw = localStorage.getItem(key);
    return raw ? JSON.parse(raw) : null;
  }, PRODUCTION_DRAFT_STORAGE_KEY);
  expect(restored.verified_product).toBeNull();
  await expect(page).toHaveURL(new RegExp(`/production\\?run=${parentRunId}$`));
  expect(scenario.createBodies).toHaveLength(1);
  expect(scenario.reviewBodies).toHaveLength(1);
  expect(scenario.recomputeBodies).toHaveLength(1);
  expect(scenario.renderBodies).toHaveLength(1);
  expect(scenario.cancelIds).toEqual([]);
  await expectNoSeriousAccessibilityFindings(page);
  const trajectoryReadyReviewReads = scenario.reviewWindowReads.filter(
    (read) => read.phase === "trajectory_ready",
  );
  expect(trajectoryReadyReviewReads.length).toBeLessThanOrEqual(1);
  expect(trajectoryReadyReviewReads).toEqual(
    trajectoryReadyReviewReads.length === 0
      ? []
      : [{ method: "GET", runId: parentRunId, phase: "trajectory_ready" }],
  );
  expect(scenario.audit.unhandledApi).toEqual([]);
  expect(scenario.audit.contractViolations).toEqual([]);
  expect(conflictResponses.length).toBeLessThanOrEqual(1);
  expect(conflictResponses).toEqual(
    conflictResponses.length === 0
      ? []
      : [
          {
            method: "GET",
            url: `http://127.0.0.1:4173/api/runs/${parentRunId}/artifacts`,
          },
        ],
  );
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
  await expect(page).toHaveURL(/\/history$/);

  await page.goBack();
  await expect(page).toHaveURL(/\/production$/);
  await expect(page.getByLabel("Original video")).toHaveValue(
    "data/match-a.mp4",
  );
  await page.getByRole("button", { name: "Next" }).click();
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

  const staleConfigName = `generated/${String(
    scenario.deriveBodies[0].output_name,
  )}`;
  scenario.releaseDerive();
  await expect
    .poll(() => scenario.deriveResponseNames)
    .toContain(staleConfigName);
  await expect(page.getByText("Configuration snapshot verified")).toHaveCount(
    0,
  );
  expect(scenario.configGetNames).not.toContain(staleConfigName);
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

test("blocks acceptance and exposes bounded tuning when every trial frame is lost", async ({
  page,
}) => {
  const scenario = await installTrialScenario(page, { allLost: true });
  const detectorScenario = await installDetectorProbeScenario(page);
  await openTrialFromDraft(page);

  await page.getByRole("button", { name: "Start bounded trial" }).click();
  await expect.poll(() => scenario.createBodies.length).toBe(1);
  const runId = scenario.runId();
  scenario.setStatus(runId, "completed");

  const diagnosis = page.getByTestId("trial-diagnosis");
  await expect(diagnosis).toContainText("Adjustment required");
  await expect(diagnosis).toContainText("No raw ball candidates were found.");
  await expect(
    diagnosis.getByText("Raw candidates", { exact: true }).locator(".."),
  ).toContainText("0 · Collected");
  await expect(
    diagnosis.getByText("Tracklets", { exact: true }).locator(".."),
  ).toContainText("0 · Collected");
  await expect(
    diagnosis.getByTestId("trial-debug-status-counts"),
  ).toContainText("Detected frames (debug)0 · Collected");
  await expect(
    diagnosis.getByTestId("trial-debug-status-counts"),
  ).toContainText("Lost frames (debug)300 · Collected");

  await expect(page.getByTestId("trial-evidence-ready")).toBeAttached({
    timeout: 15_000,
  });
  await expect(page.getByText("Quality gate: stable")).toBeVisible();
  expect(scenario.runs[0]).toMatchObject({
    stats: {
      quality_gate: { status: "stable" },
      trial_signal_gate_v2: {
        status: "retune_required",
        failure_classification: { code: "no_raw_candidates" },
        stage_counts: {
          raw_candidates: { value: 0 },
          tracklets: { value: 0 },
        },
      },
    },
  });

  await expect(
    page.getByRole("button", { name: "Accept this trial" }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Save and rerun" }),
  ).toBeEnabled();

  const detectorProbe = page.getByTestId("production-detector-probe-panel");
  await expect(detectorProbe).toBeVisible();
  await expect(
    detectorProbe.getByText("Official YOLO11n", { exact: true }),
  ).toBeVisible();
  await expect(
    detectorProbe.getByText("Official YOLO11s", { exact: true }),
  ).toBeVisible();
  await detectorProbe
    .getByRole("button", { name: "Run bounded comparison" })
    .click();
  await expect.poll(() => detectorScenario.createBodies.length).toBe(1);
  expect(detectorScenario.createBodies[0]).toEqual({
    parent_trial_id: runId,
    profile_ids: [...detectorProbeProfileIds],
    top_k: 5,
  });
  expect(JSON.stringify(detectorScenario.createBodies[0])).not.toContain(
    "model_path",
  );
  await expect(
    detectorProbe.getByText(
      "No selected profile produced retained candidate boxes in this bounded comparison.",
    ),
  ).toBeVisible();
  await expect(
    detectorProbe.getByText(/Next, start the 20–50-frame feasibility check/),
  ).toBeVisible();
  await expect(
    detectorProbe.getByRole("img", { name: "Source frame 10" }),
  ).toBeVisible();
  await expect(
    detectorProbe.getByRole("img", {
      name: `Raw detector overlay for ${detectorProbeProfileIds[0]} on frame 10`,
    }),
  ).toBeVisible();
  const frameArtifactPaths = (frameIndex: number) => [
    `/api/detector-probes/probe-e2e-ready/artifacts/source-${frameIndex}`,
    ...detectorProbeProfileIds.map(
      (profileId) =>
        `/api/detector-probes/probe-e2e-ready/artifacts/overlay-${frameIndex}-${profileId}`,
    ),
  ];
  await expect
    .poll(() => [...detectorScenario.artifactReads].sort())
    .toEqual(frameArtifactPaths(10).sort());
  for (const frameIndex of [20, 30, 40, 50, 60]) {
    await expect(
      detectorProbe.getByRole("img", { name: `Source frame ${frameIndex}` }),
    ).toHaveCount(0);
    expect(
      detectorScenario.artifactReads.some((path) =>
        path.includes(`-${frameIndex}`),
      ),
    ).toBe(false);
  }

  await detectorProbe.getByRole("button", { name: "Next frame" }).click();
  await expect(
    detectorProbe.getByRole("img", { name: "Source frame 20" }),
  ).toBeVisible();
  await expect
    .poll(() => [...detectorScenario.artifactReads].sort())
    .toEqual([...frameArtifactPaths(10), ...frameArtifactPaths(20)].sort());
  await expect(
    detectorProbe.getByRole("img", { name: "Source frame 10" }),
  ).toHaveCount(0);
  for (const frameIndex of [30, 40, 50, 60]) {
    await expect(
      detectorProbe.getByRole("img", { name: `Source frame ${frameIndex}` }),
    ).toHaveCount(0);
    expect(
      detectorScenario.artifactReads.some((path) =>
        path.includes(`-${frameIndex}`),
      ),
    ).toBe(false);
  }
  await expect(
    page.getByRole("button", { name: "Accept this trial" }),
  ).toHaveCount(0);

  await page.setViewportSize({ width: 375, height: 812 });
  await expect(detectorProbe).toBeVisible();
  const probeWidth = await detectorProbe.evaluate((element) => ({
    client: element.clientWidth,
    scroll: element.scrollWidth,
  }));
  expect(probeWidth.scroll).toBeLessThanOrEqual(probeWidth.client);

  await expectNoSeriousAccessibilityFindings(page);
  expect(runtimeErrors.get(page) ?? []).toEqual([]);
});

test("shows readable module, frame-exception, and stage-counter reconciliation failures", async ({
  page,
}) => {
  const scenario = await installTrialScenario(page, {
    diagnosticContractFailure: true,
  });
  await openTrialFromDraft(page);
  await page.getByRole("button", { name: "Start bounded trial" }).click();
  await expect.poll(() => scenario.createBodies.length).toBe(1);
  scenario.setStatus(scenario.runId(), "completed");

  const diagnosis = page.getByTestId("trial-diagnosis");
  await expect(diagnosis).toContainText(
    "The saved trial option conflicts with the executed module state: post-processing.",
  );
  await expect(diagnosis).toContainText(
    "At least one debug frame ended with an execution exception.",
  );
  await expect(diagnosis).toContainText(
    "Required stage counter was not collected: debug Lost frames.",
  );
  await expect(diagnosis).toContainText(
    "Filtered candidates exceed class-mapped candidates.",
  );
  await expect(diagnosis).toContainText(
    "Stage rejection-reason counters were not collected.",
  );
  await expect(diagnosis).toContainText("(trial_option_conflict:postprocess)");
});

test("restarts a terminal legacy trial as an explicit generation-one root", async ({
  page,
}) => {
  const scenario = await installTrialScenario(page, {
    allLost: true,
    legacyConfigDigest: true,
  });
  await openTrialFromDraft(page);
  await page.getByRole("button", { name: "Start bounded trial" }).click();
  await expect.poll(() => scenario.createBodies.length).toBe(1);
  const legacyRunId = scenario.runId();
  scenario.setStatus(legacyRunId, "completed");

  await expect(page.getByTestId("trial-diagnosis-code")).toContainText(
    "no_raw_candidates",
  );
  await expect(page.getByLabel("Base configuration")).toBeDisabled();
  await expect(
    page.getByText(
      "Locked to the verified base configuration of the current trial lineage.",
    ),
  ).toBeVisible();
  await page.getByRole("button", { name: "Save and rerun" }).click();
  await expect.poll(() => scenario.createBodies.length).toBe(2);

  const restart = scenario.createBodies[1];
  expect(restart.parent_run_id).toBeNull();
  expect(restart.output_dir_name).not.toBe(
    scenario.createBodies[0].output_dir_name,
  );
  expect(JSON.parse(String(restart.notes))).toMatchObject({
    generation: 1,
    legacy_restart_run_id: legacyRunId,
  });
});

test("shows class-mapping rejection, missing budgets, bounded labels, and the Step 2 action", async ({
  page,
}) => {
  const scenario = await installTrialScenario(page, {
    classRejected: true,
    missingDiagnosticBudgets: true,
  });
  await openTrialFromDraft(page);

  await page
    .getByLabel("detector.allowed_labels")
    .selectOption(["sports ball", "ball"]);
  await page.getByRole("button", { name: "Save adjustments" }).click();
  await expect(page.getByText(/Current patch version:/)).toBeVisible();

  await page.getByRole("button", { name: "Start bounded trial" }).click();
  await expect.poll(() => scenario.createBodies.length).toBe(1);
  expect(scenario.createBodies[0]).toMatchObject({
    config_patch: {
      detector: { allowed_labels: ["sports ball", "ball"] },
    },
  });
  const runId = scenario.runId();
  scenario.setStatus(runId, "completed");

  const diagnosis = page.getByTestId("trial-diagnosis");
  await expect(diagnosis.getByTestId("trial-diagnosis-code")).toContainText(
    "all_candidates_class_rejected",
  );
  await expect(
    diagnosis.getByTestId("trial-detection-stage-chain"),
  ).toContainText(
    /Raw candidates[\s\S]*12[\s\S]*Class-mapped candidates[\s\S]*0/,
  );
  await expect(
    diagnosis.getByTestId("trial-stage-rejection-reasons"),
  ).toContainText("Class not allowed: person: 12");
  const typedDiagnostics = diagnosis.getByTestId("trial-typed-diagnostics");
  await expect(typedDiagnostics).toContainText(
    "AI review triggers / 100 frames",
  );
  await expect(typedDiagnostics).toContainText(
    "Follow-camera maximum pan step",
  );
  await expect(typedDiagnostics).toContainText("— · Not collected");

  const fieldAction = page.getByTestId("trial-field-setup-action");
  await expect(fieldAction).toContainText(
    "invalidates this trial and every downstream result",
  );
  await fieldAction
    .getByRole("button", { name: "Return to field setup" })
    .click();
  await expect(
    page.getByText(/clears trial and configuration evidence from this draft/i),
  ).toBeVisible();
  await page.getByRole("button", { name: "Invalidate and edit" }).click();
  await expect(
    page.getByRole("heading", { name: "Field calibration" }),
  ).toBeVisible();
  await expect(page.locator("main [data-aria-hidden='true']")).toHaveCount(0);

  await expectNoSeriousAccessibilityFindings(page);
  expect(runtimeErrors.get(page) ?? []).toEqual([]);
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
  await expect(page.getByLabel("Base configuration")).toBeDisabled();
  await expect(
    page.getByText(
      "Locked to the verified base configuration of the current trial lineage.",
    ),
  ).toBeVisible();
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
  await confirmTrialVisualEvidence(page);
  await page.getByRole("button", { name: "Accept this trial" }).click();
  await expect(page.getByText("Trial accepted")).toBeVisible();
  const attempts = page
    .locator('section[aria-labelledby="trial-attempts-title"]')
    .getByRole("listitem");
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
