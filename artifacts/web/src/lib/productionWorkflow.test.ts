import { describe, expect, it } from "vitest";

import {
  PRODUCTION_DRAFT_SCHEMA_VERSION,
  PRODUCTION_DRAFT_STORAGE_KEY,
  canEnterProductionStage,
  clearProductionDraft,
  createProductionDraft,
  createProductionWorkflowId,
  deriveProductionWorkflow,
  invalidateProductionDraft,
  loadProductionDraft,
  productionHistoryOpenAction,
  productionTrialRequiresStop,
  requiresDraftReplacementConfirmation,
  saveProductionDraft,
  sourceSignaturesMatch,
  updateConfirmedProductionConfig,
  updatePendingConfigConfirmation,
  updateProductionCalibration,
  updateProductionFullRun,
  updateProductionSource,
  updateProductionTrial,
  updateVerifiedProductionProduct,
  type ProductionDraft,
  type ProductionWorkflowStage,
  type SourceSignature,
} from "./productionWorkflow";
import type { ProductionTrialState } from "./productionTrial";
import type {
  ProductionFullRunState,
  ProductionFullRunStatus,
} from "./productionBroadcast";
import { clearPendingProductionFullRun } from "./productionBroadcast";
import type { ProductionProductEvidence } from "./broadcastDelivery";

const NOW = "2026-07-14T12:00:00.000Z";
const LATER = "2026-07-14T12:05:00.000Z";
const SOURCE: SourceSignature = {
  path: "data/match-a.mp4",
  size_bytes: 1_024,
  modified_at: "2026-07-14T10:00:00Z",
};
const POLYGON_DIGEST = "c".repeat(64);
const INTENT_DIGEST = "d".repeat(64);
const REQUEST_DIGEST = "e".repeat(64);
const EVIDENCE_DIGEST = "f".repeat(64);

function completeCalibration() {
  return {
    source_resolution: { width: 1_920, height: 1_080 },
    suggestion: null,
    approved_polygon: [
      [0, 0],
      [1_919, 0],
      [1_919, 1_079],
    ] as [number, number][],
    exclusions: [],
    polygon_digest: POLYGON_DIGEST,
    confirmed_frames: [10, 20, 30].map((frame_index, sample_index) => ({
      input_video: SOURCE.path,
      frame_index,
      frame_time_seconds: frame_index / 25,
      sample_index,
      source_resolution: { width: 1_920, height: 1_080 },
      polygon_digest: POLYGON_DIGEST,
    })),
  };
}

function trialState(accepted = true): ProductionTrialState {
  const notes = JSON.stringify({
    schema_version: "1.0",
    purpose: "production_trial",
    workflow_id: "workflow-a",
    submission_id: "submission-a",
    output_id: "output-a",
    calibration_digest: POLYGON_DIGEST,
    intent_sha256: INTENT_DIGEST,
  });
  const attempt = {
    run_id: "trial-2",
    generation: 1,
    submission_id: "submission-a",
    parent_run_id: null,
    intent_sha256: INTENT_DIGEST,
    request_sha256: REQUEST_DIGEST,
    request: {
      config_name: "default.yaml",
      input_video: SOURCE.path,
      parent_run_id: null,
      output_dir_name: "production_trial_output-a",
      config_patch: {
        input_video: SOURCE.path,
        filtering: { roi: [0, 0, 1_919, 1_079] },
        scene_bias: {
          enabled: true,
          ground_zones: [
            {
              name: "production_field",
              points: completeCalibration().approved_polygon,
            },
          ],
          negative_rois: [],
        },
        postprocess: { enabled: true },
        follow_cam: { enabled: true },
        runtime: { start_frame: 0, max_frames: 300 },
        metadata: {
          production_workflow: {
            schema_version: "1.0",
            purpose: "production_trial",
            workflow_id: "workflow-a",
            submission_id: "submission-a",
            output_id: "output-a",
            calibration_digest: POLYGON_DIGEST,
            intent_sha256: INTENT_DIGEST,
            source_signature: SOURCE,
            output_dir_name: "production_trial_output-a",
          },
        },
      },
      enable_postprocess: true,
      enable_follow_cam: true,
      start_frame: 0,
      max_frames: 300,
      pipeline_mode: "standard" as const,
      notes,
    },
    created_at: NOW,
    last_observed: {
      status: "completed" as const,
      observed_at: NOW,
      evidence_generation: EVIDENCE_DIGEST,
    },
  };
  return {
    settings: {
      base_config_name: "default.yaml",
      start_frame: 0,
      max_frames: 300,
      enable_postprocess: true,
      enable_follow_cam: true,
      tuning_patch: {},
    },
    attempts: [attempt],
    active_run_id: null,
    pending_submission: null,
    accepted: accepted
      ? {
          run_id: attempt.run_id,
          intent_sha256: INTENT_DIGEST,
          request_sha256: REQUEST_DIGEST,
          accepted_at: NOW,
          readiness: {
            run_id: attempt.run_id,
            request_sha256: REQUEST_DIGEST,
            evidence_generation: EVIDENCE_DIGEST,
            verified_at: NOW,
            video_artifact_name: "follow_cam.mp4",
            artifact_names: [
              "run_manifest.json",
              "metrics_report.json",
              "ball_track.csv",
              "ball_audit.json",
              "ball_track.cleaned.csv",
              "follow_cam.mp4",
            ],
            quality: {
              frame_count: 300,
              detected: 200,
              predicted: 50,
              lost: 50,
              detected_ratio: 2 / 3,
              predicted_ratio: 1 / 6,
              lost_ratio: 1 / 6,
              longest_lost_streak: 5,
              false_positive_island_count: 1,
              max_step_px: 20,
              audit_tracklet_count: 3,
              audit_suspicious_tracklet_count: 1,
              audit_review_event_count: 1,
              audit_lost_gap_count: 2,
              quality_gate_status: null,
            },
          },
        }
      : null,
  };
}

function configExecutionPatch() {
  return {
    input_video: SOURCE.path,
    filtering: { roi: [0, 0, 1_919, 1_079] },
    scene_bias: {
      enabled: true,
      ground_zones: [
        {
          name: "production_field",
          points: completeCalibration().approved_polygon,
        },
      ],
      negative_rois: [],
    },
    postprocess: { enabled: true },
    runtime: { start_frame: 0, max_frames: null },
    follow_cam: { enabled: false },
    output: { save_tracking_contract: true },
    metadata: {
      production_workflow: {
        schema_version: "1.0",
        workflow_id: "workflow-a",
        base_config_name: "default.yaml",
        accepted_trial_run_id: "trial-2",
        calibration_digest: POLYGON_DIGEST,
        source_signature: SOURCE,
        trial_intent_sha256: INTENT_DIGEST,
        trial_request_sha256: REQUEST_DIGEST,
        trial_patch_sha256: "2".repeat(64),
        patch_sha256: "1".repeat(64),
        confirmed_at: NOW,
      },
    },
  };
}

function confirmedConfig() {
  return {
    name: "generated/production_workflow-a_11111111-1111-4111-8111-111111111111.yaml",
    sha256: "a".repeat(64),
    base_config_name: "default.yaml",
    patch: configExecutionPatch(),
    patch_sha256: "1".repeat(64),
    trial_patch_sha256: "2".repeat(64),
    workflow_id: "workflow-a",
    accepted_trial_run_id: "trial-2",
    trial_intent_sha256: INTENT_DIGEST,
    trial_request_sha256: REQUEST_DIGEST,
    calibration_digest: POLYGON_DIGEST,
    source_signature: SOURCE,
    confirmed_at: NOW,
  };
}

function pendingConfig() {
  const outputId = "11111111-1111-4111-8111-111111111111";
  const outputName = `production_workflow-a_${outputId}.yaml`;
  return {
    generation: 1,
    output_id: outputId,
    output_name: outputName,
    request: {
      base_config_name: "default.yaml",
      output_name: outputName,
      patch: configExecutionPatch(),
    },
    persistent_patch: configExecutionPatch(),
    patch_sha256: "1".repeat(64),
    trial_patch_sha256: "2".repeat(64),
    workflow_id: "workflow-a",
    base_config_name: "default.yaml",
    accepted_trial_run_id: "trial-2",
    trial_intent_sha256: INTENT_DIGEST,
    trial_request_sha256: REQUEST_DIGEST,
    calibration_digest: POLYGON_DIGEST,
    source_signature: SOURCE,
    confirmed_at: NOW,
  };
}

function calibrationSuggestion() {
  return {
    source_path: SOURCE.path,
    source: "system-detector",
    confidence: "detected" as const,
    field_coverage: 0.78,
    source_resolution: { width: 1_920, height: 1_080 },
    frame_index: 10,
    polygon: [
      [100, 100],
      [1_800, 100],
      [1_800, 1_000],
    ] as [number, number][],
  };
}

function fullRunState(
  status: ProductionFullRunStatus = "trajectory_ready",
  statusGeneration = "b".repeat(64),
): ProductionFullRunState {
  const outputId = "22222222-2222-4222-8222-222222222222";
  const runId = `production_full_${outputId}`;
  const notes = JSON.stringify({
    schema_version: "1.0",
    purpose: "production_full",
    workflow_id: "workflow-a",
    submission_id: "full-submission-a",
    output_id: outputId,
    generation: 1,
    accepted_trial_run_id: "trial-2",
    accepted_trial_request_sha256: REQUEST_DIGEST,
    confirmed_config_name: confirmedConfig().name,
    expected_config_sha256: confirmedConfig().sha256,
    config_patch_sha256: confirmedConfig().patch_sha256,
    calibration_digest: POLYGON_DIGEST,
    source_signature: SOURCE,
  });
  return {
    revision: 2,
    attempts: [
      {
        run_id: runId,
        generation: 1,
        submission_id: "full-submission-a",
        parent_trial_run_id: "trial-2",
        config_name: confirmedConfig().name,
        config_sha256: confirmedConfig().sha256,
        request_sha256: "3".repeat(64),
        request: {
          config_name: confirmedConfig().name,
          input_video: SOURCE.path,
          parent_run_id: "trial-2",
          output_dir_name: runId,
          enable_postprocess: true,
          enable_follow_cam: false,
          start_frame: 0,
          max_frames: null,
          pipeline_mode: "broadcast_hybrid",
          calibration_confirmation: {
            source_resolution: [1_920, 1_080],
            confirmed_sample_frames: [10, 20, 30],
            field_polygon: completeCalibration().approved_polygon,
            exclusion_polygons: [],
          },
          quality_profile: "stable_broadcast",
          max_manual_review_windows: 30,
          notes,
        },
        created_at: NOW,
        last_observed: {
          run_status:
            status === "failed"
              ? "failed"
              : status === "cancelled"
                ? "cancelled"
                : status === "ready"
                  ? "completed"
                  : "running",
          workflow_state: status,
          status_generation: status === "tracking" ? null : statusGeneration,
          trajectory_generation_id: null,
          operation: null,
          observed_at: NOW,
        },
      },
    ],
    pending_submission: null,
    current_run_id: runId,
  };
}

function productEvidence(
  fullRun = fullRunState("ready"),
): ProductionProductEvidence {
  return {
    run_id: fullRun.current_run_id!,
    artifact_name: "broadcast.mp4",
    artifact_size_bytes: 4_096,
    artifact_sha256: "4".repeat(64),
    quality_report_sha256: "5".repeat(64),
    status_generation:
      fullRun.attempts[0].last_observed.status_generation ?? "b".repeat(64),
    verified_at: NOW,
  };
}

function pendingFullRunState(): ProductionFullRunState {
  const fullRun = fullRunState("tracking");
  const attempt = fullRun.attempts[0];
  const note = JSON.parse(attempt.request.notes ?? "{}") as Record<
    string,
    unknown
  >;
  return {
    revision: 1,
    attempts: [],
    pending_submission: {
      generation: attempt.generation,
      submission_id: attempt.submission_id,
      output_id: String(note.output_id),
      expected_run_id: attempt.run_id,
      request: attempt.request,
      request_sha256: attempt.request_sha256,
      workflow_id: String(note.workflow_id),
      accepted_trial_run_id: attempt.parent_trial_run_id,
      accepted_trial_request_sha256: String(note.accepted_trial_request_sha256),
      config_name: attempt.config_name,
      config_sha256: attempt.config_sha256,
      config_patch_sha256: String(note.config_patch_sha256),
      calibration_digest: String(note.calibration_digest),
      source_signature: SOURCE,
      created_at: attempt.created_at,
    },
    current_run_id: null,
  };
}

function draftWithEvidence(): ProductionDraft {
  return {
    ...createProductionDraft(NOW, "workflow-a"),
    source: SOURCE,
    calibration: completeCalibration(),
    trial: trialState(),
    pending_config_confirmation: null,
    confirmed_config: confirmedConfig(),
    full_run: fullRunState(),
    verified_product: null,
  };
}

function memoryStorage(initial?: string): Storage {
  const values = new Map<string, string>();
  if (initial !== undefined) values.set(PRODUCTION_DRAFT_STORAGE_KEY, initial);

  return {
    get length() {
      return values.size;
    },
    clear: () => values.clear(),
    getItem: (key) => values.get(key) ?? null,
    key: (index) => [...values.keys()][index] ?? null,
    removeItem: (key) => void values.delete(key),
    setItem: (key, value) => void values.set(key, value),
  };
}

describe("production workflow stage derivation", () => {
  const cases: Array<{
    name: string;
    change: (draft: ProductionDraft) => void;
    expected: ProductionWorkflowStage;
    deliveryBlocked?: boolean;
  }> = [
    { name: "source", change: () => undefined, expected: "source" },
    {
      name: "calibration",
      change: (draft) => void (draft.source = SOURCE),
      expected: "calibration",
    },
    {
      name: "trial",
      change: (draft) => {
        draft.source = SOURCE;
        draft.calibration = completeCalibration();
      },
      expected: "trial",
    },
    {
      name: "configuration confirmation",
      change: (draft) => {
        Object.assign(draft, draftWithEvidence(), {
          confirmed_config: null,
          full_run: null,
        });
      },
      expected: "config_confirmation",
    },
    {
      name: "full tracking not started",
      change: (draft) =>
        Object.assign(draft, draftWithEvidence(), { full_run: null }),
      expected: "full_tracking",
    },
    {
      name: "queued full tracking",
      change: (draft) =>
        Object.assign(draft, draftWithEvidence(), {
          full_run: fullRunState("tracking"),
        }),
      expected: "full_tracking",
    },
    {
      name: "running full tracking",
      change: (draft) =>
        Object.assign(draft, draftWithEvidence(), {
          full_run: fullRunState("tracking"),
        }),
      expected: "full_tracking",
    },
    {
      name: "review",
      change: (draft) =>
        Object.assign(draft, draftWithEvidence(), {
          full_run: fullRunState("needs_review"),
        }),
      expected: "review",
    },
    {
      name: "recomputing",
      change: (draft) =>
        Object.assign(draft, draftWithEvidence(), {
          full_run: fullRunState("recomputing"),
        }),
      expected: "recomputing",
    },
    {
      name: "trajectory ready",
      change: (draft) => Object.assign(draft, draftWithEvidence()),
      expected: "trajectory_ready",
    },
    {
      name: "rendering",
      change: (draft) =>
        Object.assign(draft, draftWithEvidence(), {
          full_run: fullRunState("rendering"),
        }),
      expected: "rendering",
    },
    {
      name: "ready without a verified product",
      change: (draft) =>
        Object.assign(draft, draftWithEvidence(), {
          full_run: fullRunState("ready"),
        }),
      expected: "rendering",
      deliveryBlocked: true,
    },
    {
      name: "ready with a verified product",
      change: (draft) => {
        const fullRun = fullRunState("ready");
        Object.assign(draft, draftWithEvidence(), {
          status: "completed",
          full_run: fullRun,
          verified_product: productEvidence(fullRun),
        });
      },
      expected: "ready",
    },
    {
      name: "failed",
      change: (draft) =>
        Object.assign(draft, draftWithEvidence(), {
          full_run: fullRunState("failed"),
        }),
      expected: "failed",
    },
    {
      name: "cancelled",
      change: (draft) =>
        Object.assign(draft, draftWithEvidence(), {
          full_run: fullRunState("cancelled"),
        }),
      expected: "cancelled",
    },
  ];

  for (const testCase of cases) {
    it(`derives ${testCase.name}`, () => {
      const draft = createProductionDraft(NOW, "workflow-a");
      testCase.change(draft);
      const result = deriveProductionWorkflow(draft);
      expect(result.stage).toBe(testCase.expected);
      expect(result.delivery_blocked).toBe(testCase.deliveryBlocked ?? false);
    });
  }
});

describe("production workflow identifiers", () => {
  it("uses randomUUID when the runtime provides it", () => {
    expect(
      createProductionWorkflowId({
        randomUUID: () => "runtime-uuid",
        getRandomValues: (bytes) => bytes,
      }),
    ).toBe("runtime-uuid");
  });

  it("uses secure random bytes when randomUUID is unavailable", () => {
    const id = createProductionWorkflowId({
      getRandomValues: (bytes) => {
        bytes.forEach((_, index) => {
          bytes[index] = index;
        });
        return bytes;
      },
    });

    expect(id).toBe("00010203-0405-4607-8809-0a0b0c0d0e0f");
    expect(id).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
    );
  });
});

describe("production workflow guards", () => {
  it("requires an explicit stop only for active trial identities/statuses", () => {
    expect(productionTrialRequiresStop(null)).toBe(false);
    for (const status of ["completed", "failed", "cancelled"] as const) {
      const trial = trialState(false);
      trial.attempts[0].last_observed.status = status;
      trial.active_run_id = null;
      expect(productionTrialRequiresStop(trial)).toBe(false);
    }
    for (const status of ["queued", "running"] as const) {
      const trial = trialState(false);
      trial.attempts[0].last_observed.status = status;
      trial.active_run_id = "trial-2";
      expect(productionTrialRequiresStop(trial)).toBe(true);
    }
    const staleIdentity = trialState(false);
    staleIdentity.active_run_id = "trial-2";
    expect(productionTrialRequiresStop(staleIdentity)).toBe(true);

    const pending = trialState(false);
    pending.active_run_id = null;
    pending.attempts[0].last_observed.status = "completed";
    pending.pending_submission = {
      generation: 2,
      submission_id: "submission-b",
      output_id: "output-b",
      intent_sha256: "1".repeat(64),
      request_sha256: "2".repeat(64),
      request: { ...pending.attempts[0].request },
      created_at: LATER,
    };
    expect(productionTrialRequiresStop(pending)).toBe(true);
  });

  it("enforces each sequential prerequisite", () => {
    const empty = createProductionDraft(NOW, "workflow-a");
    expect(canEnterProductionStage(empty, "source")).toBe(true);
    expect(canEnterProductionStage(empty, "calibration")).toBe(false);

    const source = { ...empty, source: SOURCE };
    expect(canEnterProductionStage(source, "calibration")).toBe(true);
    expect(canEnterProductionStage(source, "trial")).toBe(false);

    const trial = draftWithEvidence();
    trial.trial = trialState(false);
    trial.confirmed_config = null;
    trial.full_run = null;
    expect(canEnterProductionStage(trial, "trial")).toBe(true);
    expect(canEnterProductionStage(trial, "config_confirmation")).toBe(false);

    trial.trial = trialState(true);
    expect(canEnterProductionStage(trial, "config_confirmation")).toBe(true);
    expect(canEnterProductionStage(trial, "full_tracking")).toBe(false);

    trial.confirmed_config = confirmedConfig();
    expect(canEnterProductionStage(trial, "full_tracking")).toBe(true);
    expect(canEnterProductionStage(trial, "review")).toBe(false);

    trial.full_run = fullRunState("needs_review");
    expect(canEnterProductionStage(trial, "review")).toBe(true);
    expect(canEnterProductionStage(trial, "recomputing")).toBe(false);

    trial.full_run = fullRunState("recomputing");
    expect(canEnterProductionStage(trial, "recomputing")).toBe(true);
    trial.full_run = fullRunState("trajectory_ready");
    expect(canEnterProductionStage(trial, "trajectory_ready")).toBe(true);
    trial.full_run = fullRunState("rendering");
    expect(canEnterProductionStage(trial, "rendering")).toBe(true);

    trial.full_run = fullRunState("ready");
    expect(canEnterProductionStage(trial, "rendering")).toBe(true);
    expect(canEnterProductionStage(trial, "ready")).toBe(false);
    trial.verified_product = productEvidence(trial.full_run);
    expect(canEnterProductionStage(trial, "ready")).toBe(true);

    trial.full_run = fullRunState("failed");
    expect(canEnterProductionStage(trial, "failed")).toBe(true);
    trial.full_run = fullRunState("cancelled");
    expect(canEnterProductionStage(trial, "cancelled")).toBe(true);
  });
});

describe("production full-run and delivery transitions", () => {
  it("accepts explicit exact-pending abandonment before retry", () => {
    const base = { ...draftWithEvidence(), full_run: null };
    const pending = updateProductionFullRun(
      base,
      pendingFullRunState(),
      0,
      LATER,
    );
    const clearedState = clearPendingProductionFullRun(
      pending.full_run!,
      pending.full_run!.pending_submission!,
    );
    const cleared = updateProductionFullRun(pending, clearedState, 1, LATER);
    expect(cleared.full_run).toMatchObject({
      revision: 2,
      attempts: [],
      pending_submission: null,
      current_run_id: null,
    });
  });

  it("accepts the exact pending -> attempt -> observation sequence with CAS", () => {
    const base = { ...draftWithEvidence(), full_run: null };
    const pendingState = pendingFullRunState();
    const pending = updateProductionFullRun(base, pendingState, 0, LATER);
    expect(pending.full_run).toEqual(pendingState);

    const trackingState = fullRunState("tracking");
    const tracking = updateProductionFullRun(pending, trackingState, 1, LATER);
    expect(tracking.full_run).toEqual(trackingState);
    expect(() =>
      updateProductionFullRun(pending, trackingState, 0, LATER),
    ).toThrow("revision conflict");

    const reviewState = fullRunState("needs_review");
    reviewState.revision = 3;
    const review = updateProductionFullRun(tracking, reviewState, 2, LATER);
    expect(deriveProductionWorkflow(review).stage).toBe("review");
  });

  it("rejects skipped revisions, rewritten attempt identity, and dangling state", () => {
    const current = draftWithEvidence();
    const skipped = fullRunState("rendering");
    skipped.revision = 4;
    expect(() => updateProductionFullRun(current, skipped, 2, LATER)).toThrow(
      "Invalid production full-run transition",
    );

    const rewritten = fullRunState("rendering");
    rewritten.revision = 3;
    rewritten.attempts[0].request_sha256 = "8".repeat(64);
    expect(() => updateProductionFullRun(current, rewritten, 2, LATER)).toThrow(
      "Invalid production full-run transition",
    );

    const dangling = fullRunState("rendering");
    dangling.current_run_id = "missing";
    expect(() => updateProductionFullRun(current, dangling, 2, LATER)).toThrow(
      "Invalid production full-run state",
    );
  });

  it("finalizes only exact ready-generation evidence and clears it when generation changes", () => {
    const tracking = {
      ...draftWithEvidence(),
      full_run: fullRunState("tracking"),
    };
    const readyState = fullRunState("ready");
    readyState.revision = 3;
    const ready = updateProductionFullRun(tracking, readyState, 2, LATER);
    const evidence = productEvidence(readyState);

    expect(() =>
      updateVerifiedProductionProduct(ready, evidence, 2, LATER),
    ).toThrow("revision conflict");
    expect(() =>
      updateVerifiedProductionProduct(
        ready,
        { ...evidence, status_generation: "9".repeat(64) },
        3,
        LATER,
      ),
    ).toThrow("does not match");

    const completed = updateVerifiedProductionProduct(
      ready,
      evidence,
      3,
      LATER,
    );
    expect(completed.status).toBe("completed");
    expect(deriveProductionWorkflow(completed)).toEqual({
      stage: "ready",
      user_stage: "ready",
      delivery_blocked: false,
    });

    const regenerated = fullRunState("ready", "9".repeat(64));
    regenerated.revision = 4;
    const invalidated = updateProductionFullRun(
      completed,
      regenerated,
      3,
      LATER,
    );
    expect(invalidated.status).toBe("active");
    expect(invalidated.verified_product).toBeNull();
    expect(deriveProductionWorkflow(invalidated).delivery_blocked).toBe(true);
  });
});

describe("production workflow invalidation", () => {
  it("invalidates all downstream evidence when the source changes", () => {
    const current = draftWithEvidence();
    const nextSource = { ...SOURCE, path: "data/match-b.mp4" };
    const updated = updateProductionSource(current, nextSource, LATER);

    expect(updated.source).toEqual(nextSource);
    expect(updated.calibration).toBeNull();
    expect(updated.trial).toBeNull();
    expect(updated.confirmed_config).toBeNull();
    expect(updated.full_run).toBeNull();
    expect(updated.verified_product).toBeNull();
    expect(updated.status).toBe("active");
    expect(updated.updated_at).toBe(LATER);
  });

  it.each([
    ["size", { ...SOURCE, size_bytes: SOURCE.size_bytes + 1 }],
    ["modified time", { ...SOURCE, modified_at: "2026-07-14T11:00:00Z" }],
  ])(
    "treats same-path source replacement by %s as a source change",
    (_, replacement) => {
      const updated = updateProductionSource(
        draftWithEvidence(),
        replacement,
        LATER,
      );
      expect(updated.source).toEqual(replacement);
      expect(updated.calibration).toBeNull();
      expect(deriveProductionWorkflow(updated).stage).toBe("calibration");
    },
  );

  it("does not invalidate evidence for an identical source signature", () => {
    const current = draftWithEvidence();
    expect(updateProductionSource(current, { ...SOURCE }, LATER)).toBe(current);
    expect(sourceSignaturesMatch(current.source, SOURCE)).toBe(true);
  });

  it("invalidates from the edited upstream stage only", () => {
    const fromCalibration = invalidateProductionDraft(
      draftWithEvidence(),
      "calibration",
      LATER,
    );
    expect(fromCalibration.source).toEqual(SOURCE);
    expect(fromCalibration.calibration).toBeNull();
    expect(fromCalibration.trial).toBeNull();

    const fromTrial = invalidateProductionDraft(
      draftWithEvidence(),
      "trial",
      LATER,
    );
    expect(fromTrial.calibration).not.toBeNull();
    expect(fromTrial.trial?.accepted).toBeNull();
    expect(fromTrial.trial?.attempts).toHaveLength(1);
    expect(fromTrial.confirmed_config).toBeNull();
    expect(fromTrial.full_run).toBeNull();

    const fromSource = invalidateProductionDraft(
      draftWithEvidence(),
      "source",
      LATER,
    );
    expect(fromSource.source).toBeNull();
    expect(fromSource.calibration).toBeNull();

    const fromConfig = invalidateProductionDraft(
      draftWithEvidence(),
      "config_confirmation",
      LATER,
    );
    expect(fromConfig.trial).not.toBeNull();
    expect(fromConfig.confirmed_config).toBeNull();

    const fromFullRun = invalidateProductionDraft(
      draftWithEvidence(),
      "full_tracking",
      LATER,
    );
    expect(fromFullRun.confirmed_config).not.toBeNull();
    expect(fromFullRun.full_run).toBeNull();
  });

  it("keeps suggestion-only updates but clears all downstream evidence on an approved polygon edit", () => {
    const current = draftWithEvidence();
    const suggestionOnly = updateProductionCalibration(
      current,
      {
        ...current.calibration!,
        suggestion: {
          source_path: SOURCE.path,
          source: "detector",
          confidence: "detected",
          field_coverage: 0.75,
          source_resolution: { width: 1_920, height: 1_080 },
          frame_index: 10,
          polygon: [
            [20, 20],
            [100, 20],
            [100, 100],
          ],
        },
      },
      LATER,
    );
    expect(suggestionOnly.trial).toEqual(current.trial);
    expect(suggestionOnly.calibration?.confirmed_frames).toHaveLength(3);

    const approvedEdit = updateProductionCalibration(
      current,
      {
        ...current.calibration!,
        approved_polygon: [
          [10, 10],
          [1_900, 10],
          [1_900, 1_000],
        ],
        polygon_digest: "d".repeat(64),
        confirmed_frames: [],
      },
      LATER,
    );
    expect(approvedEdit.calibration?.confirmed_frames).toEqual([]);
    expect(approvedEdit.trial).toBeNull();
    expect(approvedEdit.confirmed_config).toBeNull();
    expect(approvedEdit.full_run).toBeNull();
    expect(approvedEdit.verified_product).toBeNull();
  });

  it("validates trial/config transitions and clears only their downstream evidence", () => {
    const current = draftWithEvidence();
    expect(() => updateProductionTrial(current, {} as never, LATER)).toThrow(
      "Invalid production trial state",
    );
    expect(() =>
      updatePendingConfigConfirmation(current, {} as never, LATER),
    ).toThrow("Invalid pending configuration confirmation");
    expect(() =>
      updateConfirmedProductionConfig(current, {} as never, LATER),
    ).toThrow("Invalid confirmed configuration evidence");

    const pending = updatePendingConfigConfirmation(
      current,
      pendingConfig(),
      LATER,
    );
    expect(pending.pending_config_confirmation).toEqual(pendingConfig());
    expect(pending.confirmed_config).toBeNull();
    expect(pending.full_run).toBeNull();
    expect(pending.verified_product).toBeNull();

    const withoutPending = updatePendingConfigConfirmation(current, null);
    expect(withoutPending.confirmed_config).toEqual(current.confirmed_config);
    expect(withoutPending.full_run).toEqual(current.full_run);

    const confirmed = updateConfirmedProductionConfig(
      pending,
      confirmedConfig(),
    );
    expect(confirmed.pending_config_confirmation).toBeNull();
    expect(confirmed.confirmed_config).toEqual(confirmedConfig());
    expect(confirmed.full_run).toBeNull();
    expect(confirmed.verified_product).toBeNull();
  });
});

describe("production draft persistence", () => {
  it("serializes and restores a current draft", () => {
    const storage = memoryStorage();
    const draft = draftWithEvidence();
    expect(saveProductionDraft(storage, draft)).toEqual({ ok: true });
    expect(loadProductionDraft(storage)).toEqual({
      status: "restored",
      draft,
      migrated: false,
    });
  });

  it("returns an empty result when no draft exists", () => {
    expect(loadProductionDraft(memoryStorage())).toEqual({ status: "empty" });
  });

  it("fails safely for corrupt JSON and invalid current schemas", () => {
    expect(loadProductionDraft(memoryStorage("{not-json"))).toMatchObject({
      status: "corrupt",
    });
    expect(
      loadProductionDraft(
        memoryStorage(JSON.stringify({ schema_version: 1, workflow_id: 42 })),
      ),
    ).toMatchObject({ status: "corrupt" });
  });

  it.each([
    ["source", []],
    ["calibration", []],
    ["trial", []],
    ["confirmed_config", []],
    ["full_run", []],
    ["verified_product", []],
  ])("rejects an invalid %s evidence object", (field, invalidValue) => {
    const invalid = { ...draftWithEvidence(), [field]: invalidValue };
    expect(
      loadProductionDraft(memoryStorage(JSON.stringify(invalid))),
    ).toMatchObject({
      status: "corrupt",
    });
  });

  it("rejects non-object and versionless draft payloads", () => {
    expect(loadProductionDraft(memoryStorage("[]"))).toMatchObject({
      status: "corrupt",
    });
    expect(loadProductionDraft(memoryStorage("{}"))).toMatchObject({
      status: "corrupt",
    });
  });

  it("identifies unknown future versions without clearing them", () => {
    const storage = memoryStorage(JSON.stringify({ schema_version: 99 }));
    expect(loadProductionDraft(storage)).toEqual({
      status: "unsupported",
      version: 99,
    });
    expect(storage.getItem(PRODUCTION_DRAFT_STORAGE_KEY)).not.toBeNull();
  });

  it("migrates the defined version-zero draft", () => {
    const legacy = {
      schema_version: 0,
      workflow_id: "legacy-a",
      created_at: NOW,
      updated_at: NOW,
      source: SOURCE,
    };
    const result = loadProductionDraft(memoryStorage(JSON.stringify(legacy)));
    expect(result).toMatchObject({
      status: "restored",
      migrated: true,
      draft: {
        schema_version: PRODUCTION_DRAFT_SCHEMA_VERSION,
        workflow_id: "legacy-a",
        source: SOURCE,
        calibration: null,
        trial: null,
      },
    });
  });

  it("migrates v1 fail-closed by preserving the source and clearing calibration and downstream", () => {
    const legacy = {
      ...draftWithEvidence(),
      schema_version: 1,
      calibration: {
        polygon_digest: "legacy-unbound",
        confirmed_frame_ids: ["10", "20", "30"],
      },
    };
    const result = loadProductionDraft(memoryStorage(JSON.stringify(legacy)));
    expect(result).toMatchObject({
      status: "restored",
      migrated: true,
      draft: {
        schema_version: PRODUCTION_DRAFT_SCHEMA_VERSION,
        source: SOURCE,
        calibration: null,
        trial: null,
        confirmed_config: null,
        full_run: null,
        verified_product: null,
      },
    });
  });

  it.each([
    [
      "trial workflow",
      (draft: ProductionDraft) => {
        const attempt = draft.trial!.attempts[0];
        attempt.request.notes = JSON.stringify({
          ...JSON.parse(attempt.request.notes ?? "{}"),
          workflow_id: "workflow-other",
        });
      },
    ],
    [
      "trial source",
      (draft: ProductionDraft) => {
        draft.trial!.attempts[0].request.input_video = "data/other.mp4";
      },
    ],
    [
      "trial calibration",
      (draft: ProductionDraft) => {
        const attempt = draft.trial!.attempts[0];
        attempt.request.notes = JSON.stringify({
          ...JSON.parse(attempt.request.notes ?? "{}"),
          calibration_digest: "9".repeat(64),
        });
      },
    ],
    [
      "current settings",
      (draft: ProductionDraft) => {
        draft.trial!.settings.max_frames = 301;
      },
    ],
    [
      "dangling acceptance",
      (draft: ProductionDraft) => {
        draft.trial!.accepted!.run_id = "missing-run";
      },
    ],
    [
      "accepted artifact contract",
      (draft: ProductionDraft) => {
        draft.trial!.accepted!.readiness.artifact_names = [
          "run_manifest.json",
          "metrics_report.json",
          "ball_track.csv",
          "ball_audit.json",
          "follow_cam.mp4",
        ];
      },
    ],
    [
      "dangling active run",
      (draft: ProductionDraft) => {
        draft.trial!.accepted = null;
        draft.trial!.attempts[0].last_observed.status = "completed";
        draft.trial!.active_run_id = "missing-run";
      },
    ],
    [
      "pending config without acceptance",
      (draft: ProductionDraft) => {
        draft.trial!.accepted = null;
        draft.pending_config_confirmation = pendingConfig();
        draft.confirmed_config = null;
        draft.full_run = null;
      },
    ],
    [
      "confirmed config lineage",
      (draft: ProductionDraft) => {
        draft.confirmed_config!.accepted_trial_run_id = "other-run";
      },
    ],
    [
      "confirmed config empty patch",
      (draft: ProductionDraft) => {
        draft.confirmed_config!.patch = {};
      },
    ],
    [
      "confirmed config patch lineage",
      (draft: ProductionDraft) => {
        const metadata = draft.confirmed_config!.patch.metadata as Record<
          string,
          unknown
        >;
        (metadata.production_workflow as Record<string, unknown>).workflow_id =
          "workflow-other";
      },
    ],
    [
      "full-run calibration resolution",
      (draft: ProductionDraft) => {
        draft.full_run!.attempts[0].request.calibration_confirmation!.source_resolution =
          [1_280, 720];
      },
    ],
    [
      "full-run confirmed frame indices",
      (draft: ProductionDraft) => {
        draft.full_run!.attempts[0].request.calibration_confirmation!.confirmed_sample_frames =
          [10, 20, 31];
      },
    ],
    [
      "full-run field polygon",
      (draft: ProductionDraft) => {
        draft.full_run!.attempts[0].request.calibration_confirmation!.field_polygon =
          [
            [1, 0],
            [1_919, 0],
            [1_919, 1_079],
          ];
      },
    ],
    [
      "full-run exclusion polygons",
      (draft: ProductionDraft) => {
        draft.full_run!.attempts[0].request.calibration_confirmation!.exclusion_polygons =
          [
            [
              [100, 100],
              [200, 100],
              [200, 200],
            ],
          ];
      },
    ],
    [
      "pending config empty request patch",
      (draft: ProductionDraft) => {
        const pending = pendingConfig();
        (pending.request as { patch: Record<string, unknown> }).patch = {};
        draft.pending_config_confirmation = pending;
        draft.confirmed_config = null;
        draft.full_run = null;
      },
    ],
  ])("rejects v3 context corruption: %s", (_label, mutate) => {
    const invalid = structuredClone(draftWithEvidence());
    mutate(invalid);
    expect(
      loadProductionDraft(memoryStorage(JSON.stringify(invalid))),
    ).toMatchObject({ status: "corrupt" });
    expect(saveProductionDraft(memoryStorage(), invalid)).toMatchObject({
      ok: false,
    });
  });

  it("allows historical attempts to keep older tuning while current accepted tuning matches", () => {
    const draft = structuredClone(draftWithEvidence());
    const latest = draft.trial!.attempts[0];
    latest.generation = 2;
    const old = structuredClone(latest);
    old.run_id = "trial-1";
    old.generation = 1;
    old.submission_id = "submission-old";
    old.request_sha256 = "8".repeat(64);
    const oldNote = {
      ...JSON.parse(old.request.notes ?? "{}"),
      submission_id: "submission-old",
      output_id: "output-old",
    };
    old.request.notes = JSON.stringify(oldNote);
    old.request.output_dir_name = "production_trial_output-old";
    old.request.config_patch = {
      ...old.request.config_patch,
      detector: { confidence: 0.1 },
      metadata: {
        production_workflow: {
          ...oldNote,
          source_signature: SOURCE,
          output_dir_name: "production_trial_output-old",
        },
      },
    };
    latest.parent_run_id = old.run_id;
    latest.request.parent_run_id = old.run_id;
    draft.trial!.attempts = [old, latest];
    expect(
      loadProductionDraft(memoryStorage(JSON.stringify(draft))),
    ).toMatchObject({
      status: "restored",
      migrated: false,
    });
  });

  it("restores complete suggestion, pending-config, and verified-product shapes", () => {
    const ready = fullRunState("ready", "9".repeat(64));
    const withSuggestion = {
      ...draftWithEvidence(),
      status: "completed" as const,
      calibration: {
        ...completeCalibration(),
        suggestion: calibrationSuggestion(),
      },
      full_run: ready,
      verified_product: productEvidence(ready),
    };
    expect(
      loadProductionDraft(memoryStorage(JSON.stringify(withSuggestion))),
    ).toMatchObject({ status: "restored", migrated: false });

    const withPending = {
      ...draftWithEvidence(),
      pending_config_confirmation: pendingConfig(),
      confirmed_config: null,
      full_run: null,
      verified_product: null,
    };
    expect(
      loadProductionDraft(memoryStorage(JSON.stringify(withPending))),
    ).toMatchObject({ status: "restored", migrated: false });
  });

  it("rejects completed drafts without product evidence and active drafts with product evidence", () => {
    const completedWithoutProduct = {
      ...draftWithEvidence(),
      status: "completed" as const,
    };
    expect(
      loadProductionDraft(
        memoryStorage(JSON.stringify(completedWithoutProduct)),
      ),
    ).toMatchObject({ status: "corrupt" });

    const ready = fullRunState("ready");
    const activeWithProduct = {
      ...draftWithEvidence(),
      full_run: ready,
      verified_product: productEvidence(ready),
    };
    expect(
      loadProductionDraft(memoryStorage(JSON.stringify(activeWithProduct))),
    ).toMatchObject({ status: "corrupt" });
  });

  it.each([
    ["non-object", []],
    ["source path", { ...calibrationSuggestion(), source_path: "" }],
    ["source label", { ...calibrationSuggestion(), source: "" }],
    ["confidence", { ...calibrationSuggestion(), confidence: "unknown" }],
    ["negative coverage", { ...calibrationSuggestion(), field_coverage: -0.1 }],
    ["excess coverage", { ...calibrationSuggestion(), field_coverage: 1.1 }],
    [
      "resolution",
      {
        ...calibrationSuggestion(),
        source_resolution: { width: 0, height: 1_080 },
      },
    ],
    ["fractional frame", { ...calibrationSuggestion(), frame_index: 1.5 }],
    ["negative frame", { ...calibrationSuggestion(), frame_index: -1 }],
    ["polygon", { ...calibrationSuggestion(), polygon: [[100]] }],
  ])("rejects an invalid calibration suggestion: %s", (_label, suggestion) => {
    const invalid = {
      ...draftWithEvidence(),
      calibration: { ...completeCalibration(), suggestion },
    };
    expect(
      loadProductionDraft(memoryStorage(JSON.stringify(invalid))),
    ).toMatchObject({ status: "corrupt" });
  });

  it.each([
    ["run id", { run_id: "" }],
    ["artifact", { artifact_name: "other.mp4" }],
    ["size", { artifact_size_bytes: -1 }],
    ["artifact digest", { artifact_sha256: "invalid" }],
    ["quality digest", { quality_report_sha256: "invalid" }],
    ["generation", { status_generation: "invalid" }],
  ])("rejects invalid verified-product %s", (_label, override) => {
    const ready = fullRunState("ready");
    const invalid = {
      ...draftWithEvidence(),
      full_run: ready,
      verified_product: { ...productEvidence(ready), ...override },
    };
    expect(
      loadProductionDraft(memoryStorage(JSON.stringify(invalid))),
    ).toMatchObject({ status: "corrupt" });
  });

  it("migrates v2 by preserving a legal source/calibration and clearing insufficient downstream evidence", () => {
    const current = draftWithEvidence();
    const legacy = {
      ...current,
      schema_version: 2,
      trial: { latest_run_id: "legacy-trial", accepted_run_id: "legacy-trial" },
      confirmed_config: { name: "legacy.yaml", sha256: "a".repeat(64) },
    };
    const result = loadProductionDraft(memoryStorage(JSON.stringify(legacy)));
    expect(result).toMatchObject({
      status: "restored",
      migrated: true,
      draft: {
        schema_version: PRODUCTION_DRAFT_SCHEMA_VERSION,
        workflow_id: "workflow-a",
        status: "active",
        source: SOURCE,
        calibration: completeCalibration(),
        trial: null,
        pending_config_confirmation: null,
        confirmed_config: null,
        full_run: null,
        verified_product: null,
      },
    });
  });

  it("migrates v3 narrowly and clears legacy config/full-run evidence without the tracking contract", () => {
    const legacyConfig = structuredClone(confirmedConfig());
    delete (legacyConfig.patch as { output?: unknown }).output;
    const legacy = {
      ...draftWithEvidence(),
      schema_version: 3,
      status: "completed",
      confirmed_config: legacyConfig,
    };
    const result = loadProductionDraft(memoryStorage(JSON.stringify(legacy)));
    expect(result).toMatchObject({
      status: "restored",
      migrated: true,
      draft: {
        schema_version: PRODUCTION_DRAFT_SCHEMA_VERSION,
        status: "active",
        source: SOURCE,
        calibration: completeCalibration(),
        trial: trialState(),
        pending_config_confirmation: null,
        confirmed_config: null,
        full_run: null,
        verified_product: null,
      },
    });
    if (result.status === "restored") {
      expect(result.draft.confirmed_config).toBeNull();
    }
  });

  it("migrates v3 fail-closed when confirmed config retains trial-only render settings", () => {
    const legacyConfig = structuredClone(confirmedConfig());
    (legacyConfig.patch as Record<string, unknown>).follow_cam = {
      enabled: false,
      legacy_render: true,
    };
    const legacy = {
      ...draftWithEvidence(),
      schema_version: 3,
      status: "completed",
      confirmed_config: legacyConfig,
    };
    const result = loadProductionDraft(memoryStorage(JSON.stringify(legacy)));
    expect(result).toMatchObject({
      status: "restored",
      migrated: true,
      draft: {
        schema_version: PRODUCTION_DRAFT_SCHEMA_VERSION,
        status: "active",
        source: SOURCE,
        calibration: completeCalibration(),
        trial: trialState(),
        pending_config_confirmation: null,
        confirmed_config: null,
        full_run: null,
        verified_product: null,
      },
    });
  });

  it("migrates v3 by preserving already-valid confirmed config but never old run/product shapes", () => {
    const legacy = { ...draftWithEvidence(), schema_version: 3 };
    const result = loadProductionDraft(memoryStorage(JSON.stringify(legacy)));
    expect(result).toMatchObject({
      status: "restored",
      migrated: true,
      draft: {
        schema_version: PRODUCTION_DRAFT_SCHEMA_VERSION,
        source: SOURCE,
        calibration: completeCalibration(),
        trial: trialState(),
        confirmed_config: confirmedConfig(),
        full_run: null,
        verified_product: null,
      },
    });
  });

  it("never persists preview data URLs", () => {
    const storage = memoryStorage();
    const draft = draftWithEvidence() as ProductionDraft & {
      preview_data_url?: string;
    };
    draft.preview_data_url = "data:image/png;base64,large";
    expect(saveProductionDraft(storage, draft)).toEqual({ ok: true });
    expect(storage.getItem(PRODUCTION_DRAFT_STORAGE_KEY)).not.toContain(
      "preview_data_url",
    );
  });

  it("rejects malformed version-zero drafts", () => {
    expect(
      loadProductionDraft(
        memoryStorage(
          JSON.stringify({ schema_version: 0, workflow_id: "legacy-a" }),
        ),
      ),
    ).toMatchObject({ status: "corrupt" });
  });

  it("surfaces storage read and write failures", () => {
    const storage = memoryStorage();
    storage.getItem = () => {
      throw new Error("blocked");
    };
    expect(loadProductionDraft(storage)).toMatchObject({
      status: "unavailable",
    });

    const writeStorage = memoryStorage();
    writeStorage.setItem = () => {
      throw new Error("quota");
    };
    expect(
      saveProductionDraft(writeStorage, draftWithEvidence()),
    ).toMatchObject({
      ok: false,
    });

    const nonErrorStorage = memoryStorage();
    nonErrorStorage.setItem = () => {
      throw "blocked";
    };
    expect(saveProductionDraft(nonErrorStorage, draftWithEvidence())).toEqual({
      ok: false,
      message: "blocked",
    });
  });

  it("clears only the production draft key", () => {
    const storage = memoryStorage(JSON.stringify(draftWithEvidence()));
    storage.setItem("keep-me", "yes");
    expect(clearProductionDraft(storage)).toEqual({ ok: true });
    expect(storage.getItem(PRODUCTION_DRAFT_STORAGE_KEY)).toBeNull();
    expect(storage.getItem("keep-me")).toBe("yes");
  });

  it("surfaces storage clear failures", () => {
    const storage = memoryStorage();
    storage.removeItem = () => {
      throw new Error("blocked");
    };
    expect(clearProductionDraft(storage)).toEqual({
      ok: false,
      message: "blocked",
    });
  });
});

describe("draft replacement rules", () => {
  it("requires confirmation before replacing an unfinished production", () => {
    const current = {
      ...createProductionDraft(NOW, "workflow-a"),
      source: SOURCE,
    };
    expect(requiresDraftReplacementConfirmation(current, "workflow-b")).toBe(
      true,
    );
    expect(requiresDraftReplacementConfirmation(current, "workflow-a")).toBe(
      false,
    );
  });

  it("allows empty, completed, and archived drafts to be replaced", () => {
    expect(
      requiresDraftReplacementConfirmation(
        createProductionDraft(NOW, "workflow-a"),
        "workflow-b",
      ),
    ).toBe(false);

    for (const status of ["completed", "archived"] as const) {
      const current = { ...draftWithEvidence(), status };
      expect(requiresDraftReplacementConfirmation(current, "workflow-b")).toBe(
        false,
      );
    }
  });

  it("defines the history-open action through the unfinished replacement guard", () => {
    const unfinished = {
      ...createProductionDraft(NOW, "workflow-a"),
      source: SOURCE,
    };
    expect(productionHistoryOpenAction(unfinished, "workflow-a")).toBe(
      "resume_current",
    );
    expect(productionHistoryOpenAction(unfinished, "workflow-b")).toBe(
      "confirm_replace",
    );
    expect(
      productionHistoryOpenAction(
        { ...unfinished, status: "completed" },
        "workflow-b",
      ),
    ).toBe("open_requested");
  });
});
