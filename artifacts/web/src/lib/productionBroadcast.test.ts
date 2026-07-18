import { describe, expect, it } from "vitest";
import type {
  ArtifactSummary,
  ConfigDetail,
  CreateRunRequest,
  InputCatalogResponse,
  RunRecord,
} from "@workspace/api-client-react";

import type { ProductionCalibrationDraft } from "./productionCalibration";
import {
  buildProductionConfigConfirmation,
  expectedProductionConfigName,
  finalizeProductionConfigConfirmation,
  type ProductionConfigEvidence,
} from "./productionConfigFreeze";
import {
  appendProductionFullRunAttempt,
  buildProductionFullRunSubmission,
  clearPendingProductionFullRun,
  createProductionFullRunState,
  isProductionFullRunPendingSubmission,
  isProductionFullRunState,
  nextProductionFullRunGeneration,
  observeProductionFullRun,
  productionFullRunAuthoritativeContextMatches,
  productionFullRunMatchesContext,
  productionFullRunRequestHashesMatch,
  productionFullRunRequiresStop,
  reconcilePendingProductionFullRun,
  setPendingProductionFullRun,
} from "./productionBroadcast";
import {
  appendProductionTrialAttempt,
  buildProductionTrialSubmission,
  createProductionTrialState,
  setPendingProductionTrial,
  materializedProductionTrialConfigName,
  type ProductionTrialState,
} from "./productionTrial";
import type { SourceSignature } from "./productionWorkflow";
import { ACCEPTABLE_TRIAL_SIGNAL_GATE } from "../test/productionTrialFixtures";

const NOW = "2026-07-15T16:00:00.000Z";
const SOURCE: SourceSignature = {
  path: "data/match-a.mp4",
  size_bytes: 12_345,
  modified_at: "2026-07-15T00:00:00Z",
};
const CALIBRATION_DIGEST = "c".repeat(64);
const CALIBRATION: ProductionCalibrationDraft = {
  source_resolution: { width: 1_920, height: 1_080 },
  suggestion: null,
  approved_polygon: [
    [100, 100],
    [1_800, 100],
    [1_700, 950],
    [200, 900],
  ],
  exclusions: [
    [
      [300, 300],
      [400, 300],
      [400, 400],
    ],
  ],
  polygon_digest: CALIBRATION_DIGEST,
  confirmed_frames: [30, 10, 20].map((frame_index, sample_index) => ({
    input_video: SOURCE.path,
    frame_index,
    frame_time_seconds: frame_index / 25,
    sample_index,
    source_resolution: { width: 1_920, height: 1_080 },
    polygon_digest: CALIBRATION_DIGEST,
  })),
};

async function acceptedTrial(): Promise<ProductionTrialState> {
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
  const built = await buildProductionTrialSubmission({
    workflow_id: "workflow-a",
    source: SOURCE,
    calibration: CALIBRATION,
    settings,
    parent_run_id: null,
    submission_id: "trial-submission",
    output_id: "trial-output",
    generation: 1,
    created_at: NOW,
  });
  const observed = appendProductionTrialAttempt(
    setPendingProductionTrial(
      createProductionTrialState(settings),
      built.pending,
    ),
    {
      run: { run_id: "trial-accepted", status: "completed" },
      pending: built.pending,
      observed_at: NOW,
    },
  );
  return {
    ...observed,
    attempts: observed.attempts.map((attempt) => ({
      ...attempt,
      last_observed: {
        ...attempt.last_observed,
        evidence_generation: "e".repeat(64),
      },
    })),
    accepted: {
      run_id: "trial-accepted",
      intent_sha256: built.pending.intent_sha256,
      request_sha256: built.pending.request_sha256,
      accepted_at: NOW,
      readiness: {
        run_id: "trial-accepted",
        request_sha256: built.pending.request_sha256,
        evidence_generation: "e".repeat(64),
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
          longest_lost_streak: 4,
          false_positive_island_count: 1,
          max_step_px: 20,
          audit_tracklet_count: 0,
          audit_suspicious_tracklet_count: 0,
          audit_review_event_count: 0,
          audit_lost_gap_count: 0,
          quality_gate_status: "warn",
          trial_signal_gate_v2: ACCEPTABLE_TRIAL_SIGNAL_GATE,
        },
        operator_visual_confirmation: {
          confirmed: true,
          confirmed_at: NOW,
          evidence_generation: "e".repeat(64),
          threshold_profile_sha256:
            ACCEPTABLE_TRIAL_SIGNAL_GATE.threshold_profile.sha256,
        },
      },
    },
  };
}

async function context() {
  const trial = await acceptedTrial();
  const pending = await buildProductionConfigConfirmation({
    workflow_id: "workflow-a",
    source: SOURCE,
    calibration: CALIBRATION,
    trial,
    output_id: "11111111-1111-4111-8111-111111111111",
    generation: 1,
    confirmed_at: NOW,
  });
  const detail: ConfigDetail = {
    name: expectedProductionConfigName(pending.output_name),
    path: `configs/${expectedProductionConfigName(pending.output_name)}`,
    text: "verified production config\n",
    raw: pending.request.patch ?? {},
    resolved: pending.request.patch ?? {},
    summary: {
      name: expectedProductionConfigName(pending.output_name),
      path: `configs/${expectedProductionConfigName(pending.output_name)}`,
      input_video: SOURCE.path,
      postprocess_enabled: true,
      follow_cam_enabled: false,
      exists: { yaml: true },
    },
  };
  const confirmed_config = await finalizeProductionConfigConfirmation(
    pending,
    detail,
  );
  return {
    workflow_id: "workflow-a",
    source: SOURCE,
    calibration: CALIBRATION,
    trial,
    confirmed_config,
    config_verification: {
      status: "verified" as const,
      sha256: confirmed_config.sha256,
    },
  };
}

async function submission(
  changes: Partial<Parameters<typeof buildProductionFullRunSubmission>[0]> = {},
) {
  return buildProductionFullRunSubmission({
    ...(await context()),
    submission_id: "full-submission-1",
    output_id: "22222222-2222-4222-8222-222222222222",
    generation: 1,
    created_at: NOW,
    ...changes,
  });
}

function runFor(
  pending: Awaited<ReturnType<typeof submission>>["pending"],
  changes: Partial<RunRecord> = {},
): RunRecord {
  return {
    run_id: pending.expected_run_id,
    source: "broadcast_hybrid",
    status: "queued",
    created_at: NOW,
    config_name: pending.config_name,
    config_path: `configs/${pending.config_name}`,
    input_video: pending.source_signature.path,
    parent_run_id: pending.accepted_trial_run_id,
    output_dir: `outputs/${pending.expected_run_id}`,
    modules_enabled: { postprocess: true, follow_cam: false },
    artifacts: [],
    stats: {},
    broadcast: {
      status: "tracking",
      quality_profile: "stable_broadcast",
      max_manual_review_windows: 30,
    },
    progress: { stage: "queued", percent: 0 },
    notes: pending.request.notes,
    error: null,
    ...changes,
  };
}

function authoritativeTrialRun(trial: ProductionTrialState): RunRecord {
  const accepted = trial.accepted!;
  const attempt = trial.attempts.find(
    (candidate) => candidate.run_id === accepted.run_id,
  )!;
  return {
    run_id: attempt.run_id,
    source: "api",
    status: "completed",
    created_at: NOW,
    config_name: materializedProductionTrialConfigName(
      attempt.request.config_name!,
      attempt.run_id,
    ),
    input_video: attempt.request.input_video,
    parent_run_id: attempt.request.parent_run_id,
    output_dir: `outputs/${attempt.run_id}`,
    modules_enabled: {
      postprocess: attempt.request.enable_postprocess!,
      follow_cam: attempt.request.enable_follow_cam!,
    },
    notes: attempt.request.notes,
  };
}

function authoritativeTrialArtifacts(
  trial: ProductionTrialState,
): ArtifactSummary[] {
  return trial.accepted!.readiness.artifact_names.map((name) => ({
    name,
    path: `outputs/trial-accepted/${name}`,
    kind: name.endsWith(".mp4") ? "video" : "file",
    exists: true,
    size_bytes: 100,
  }));
}

function authoritativeInputs(): InputCatalogResponse {
  return {
    root_dir: "data",
    videos: [{ name: "match-a.mp4", ...SOURCE }],
  };
}

describe("production broadcast request adapter", () => {
  it("builds a patch-free full-video request from the verified snapshot", async () => {
    const built = await submission();
    const request = built.pending.request;
    expect(request).toMatchObject({
      config_name: (await context()).confirmed_config.name,
      input_video: SOURCE.path,
      parent_run_id: "trial-accepted",
      output_dir_name: "production_full_22222222-2222-4222-8222-222222222222",
      start_frame: 0,
      max_frames: null,
      enable_postprocess: true,
      enable_follow_cam: false,
      pipeline_mode: "broadcast_hybrid",
      quality_profile: "stable_broadcast",
      max_manual_review_windows: 30,
      calibration_confirmation: {
        source_resolution: [1_920, 1_080],
        confirmed_sample_frames: [10, 20, 30],
        field_polygon: CALIBRATION.approved_polygon,
        exclusion_polygons: CALIBRATION.exclusions,
      },
    });
    expect(request).not.toHaveProperty("config_patch");
    expect(request).not.toHaveProperty("approved_action_ids");
    const note = JSON.parse(String(request.notes));
    expect(note).toMatchObject({
      purpose: "production_full",
      workflow_id: "workflow-a",
      accepted_trial_run_id: "trial-accepted",
      confirmed_config_name: (await context()).confirmed_config.name,
      expected_config_sha256: built.pending.config_sha256,
      calibration_digest: CALIBRATION_DIGEST,
      source_signature: SOURCE,
    });
    expect(isProductionFullRunPendingSubmission(built.pending)).toBe(true);
  });

  it.each([
    [
      "missing verification",
      { config_verification: { status: "missing" as const } },
    ],
    [
      "wrong verified digest",
      {
        config_verification: {
          status: "verified" as const,
          sha256: "0".repeat(64),
        },
      },
    ],
    ["unsafe output id", { output_id: "../full" }],
    ["invalid review limit", { max_manual_review_windows: 31 }],
  ])("rejects %s", async (_label, changes) => {
    await expect(submission(changes as never)).rejects.toThrow();
  });

  it("rejects configuration or calibration evidence from another context", async () => {
    const ready = await context();
    const changedConfig: ProductionConfigEvidence = {
      ...ready.confirmed_config,
      source_signature: { ...SOURCE, size_bytes: SOURCE.size_bytes + 1 },
    };
    await expect(
      submission({ confirmed_config: changedConfig }),
    ).rejects.toThrow();
    await expect(
      submission({
        calibration: {
          ...CALIBRATION,
          confirmed_frames: CALIBRATION.confirmed_frames.slice(0, 2),
        },
      }),
    ).rejects.toThrow();
  });
});

describe("production full-run authoritative prerequisites", () => {
  it("accepts the exact current source, completed accepted trial, request, and artifacts", async () => {
    const trial = await acceptedTrial();
    await expect(
      productionFullRunAuthoritativeContextMatches({
        source: SOURCE,
        trial,
        input_catalog: authoritativeInputs(),
        accepted_trial_run: authoritativeTrialRun(trial),
        accepted_trial_artifacts: authoritativeTrialArtifacts(trial),
      }),
    ).resolves.toBe(true);
  });

  it.each([
    [
      "replaced source",
      (trial: ProductionTrialState) => ({
        input_catalog: {
          ...authoritativeInputs(),
          videos: [
            {
              name: "match-a.mp4",
              ...SOURCE,
              modified_at: "2026-07-15T01:00:00Z",
            },
          ],
        },
        accepted_trial_run: authoritativeTrialRun(trial),
        accepted_trial_artifacts: authoritativeTrialArtifacts(trial),
      }),
    ],
    [
      "missing accepted trial",
      (trial: ProductionTrialState) => ({
        input_catalog: authoritativeInputs(),
        accepted_trial_run: {
          ...authoritativeTrialRun(trial),
          run_id: "missing",
        },
        accepted_trial_artifacts: authoritativeTrialArtifacts(trial),
      }),
    ],
    [
      "non-completed accepted trial",
      (trial: ProductionTrialState) => ({
        input_catalog: authoritativeInputs(),
        accepted_trial_run: {
          ...authoritativeTrialRun(trial),
          status: "failed" as const,
        },
        accepted_trial_artifacts: authoritativeTrialArtifacts(trial),
      }),
    ],
    [
      "missing accepted artifact",
      (trial: ProductionTrialState) => ({
        input_catalog: authoritativeInputs(),
        accepted_trial_run: authoritativeTrialRun(trial),
        accepted_trial_artifacts: authoritativeTrialArtifacts(trial).slice(1),
      }),
    ],
  ])("rejects %s", async (_label, changes) => {
    const trial = await acceptedTrial();
    await expect(
      productionFullRunAuthoritativeContextMatches({
        source: SOURCE,
        trial,
        ...changes(trial),
      }),
    ).resolves.toBe(false);
  });
});

describe("production full-run transitions and recovery", () => {
  it("validates every persisted pending and attempt request hash", async () => {
    const pending = (await submission()).pending;
    const waiting = setPendingProductionFullRun(
      createProductionFullRunState(),
      pending,
    );
    expect(await productionFullRunRequestHashesMatch(waiting)).toBe(true);

    const observed = appendProductionFullRunAttempt(waiting, {
      run: runFor(pending),
      pending,
      observed_at: NOW,
    });
    expect(await productionFullRunRequestHashesMatch(observed)).toBe(true);
    expect(
      await productionFullRunRequestHashesMatch({
        ...observed,
        attempts: observed.attempts.map((attempt) => ({
          ...attempt,
          request_sha256: "0".repeat(64),
        })),
      }),
    ).toBe(false);
  });

  it("persists pending before appending an exact backend response", async () => {
    const pending = (await submission()).pending;
    const empty = createProductionFullRunState();
    expect(nextProductionFullRunGeneration(empty)).toBe(1);
    const waiting = setPendingProductionFullRun(empty, pending);
    expect(waiting.revision).toBe(1);
    expect(productionFullRunRequiresStop(waiting)).toBe(true);
    const observed = appendProductionFullRunAttempt(waiting, {
      run: runFor(pending),
      pending,
      observed_at: NOW,
    });
    expect(observed.pending_submission).toBeNull();
    expect(observed.current_run_id).toBe(pending.expected_run_id);
    expect(observed.attempts).toHaveLength(1);
    expect(observed.attempts[0].last_observed.workflow_state).toBe("tracking");
    expect(nextProductionFullRunGeneration(observed)).toBe(2);
    expect(isProductionFullRunState(observed)).toBe(true);
    expect(productionFullRunMatchesContext(observed, await context())).toBe(
      true,
    );
  });

  it("clears only the exact pending identity before a new-UUID retry", async () => {
    const pending = (await submission()).pending;
    const waiting = setPendingProductionFullRun(
      createProductionFullRunState(),
      pending,
    );
    expect(clearPendingProductionFullRun(waiting, pending)).toEqual({
      ...waiting,
      revision: waiting.revision + 1,
      pending_submission: null,
    });
    expect(() =>
      clearPendingProductionFullRun(waiting, {
        ...pending,
        submission_id: "other",
      }),
    ).toThrow("does not match");
  });

  it("reconciles a lost create response exactly once and never guesses", async () => {
    const pending = (await submission()).pending;
    const waiting = setPendingProductionFullRun(
      createProductionFullRunState(),
      pending,
    );
    expect(
      await reconcilePendingProductionFullRun(waiting, {
        runs: [runFor(pending, { notes: "{}" })],
        observed_at: NOW,
      }),
    ).toBe(waiting);
    expect(
      await reconcilePendingProductionFullRun(waiting, {
        runs: [runFor(pending), runFor(pending)],
        observed_at: NOW,
      }),
    ).toBe(waiting);
    const recovered = await reconcilePendingProductionFullRun(waiting, {
      runs: [runFor(pending)],
      observed_at: NOW,
    });
    expect(recovered.attempts).toHaveLength(1);
    expect(recovered.pending_submission).toBeNull();
  });

  it("recomputes the pending request digest before lost-response reconciliation", async () => {
    const pending = (await submission()).pending;
    const waiting = setPendingProductionFullRun(
      createProductionFullRunState(),
      pending,
    );
    const tampered = {
      ...waiting,
      pending_submission: {
        ...pending,
        request_sha256: "0".repeat(64),
      },
    };
    expect(
      await reconcilePendingProductionFullRun(tampered, {
        runs: [runFor(pending)],
        observed_at: NOW,
      }),
    ).toBe(tampered);
  });

  it("refuses a response whose source, config, trial parent, or modules differ", async () => {
    const pending = (await submission()).pending;
    const waiting = setPendingProductionFullRun(
      createProductionFullRunState(),
      pending,
    );
    for (const changes of [
      { source: "api" },
      { config_name: "generated/other.yaml" },
      { input_video: "data/other.mp4" },
      { parent_run_id: "trial-other" },
      { modules_enabled: { postprocess: true, follow_cam: true } },
    ] satisfies Partial<RunRecord>[]) {
      expect(() =>
        appendProductionFullRunAttempt(waiting, {
          run: runFor(pending, changes),
          pending,
          observed_at: NOW,
        }),
      ).toThrow();
    }
  });

  it("observes authoritative workflow changes without rewriting identity", async () => {
    const pending = (await submission()).pending;
    const waiting = setPendingProductionFullRun(
      createProductionFullRunState(),
      pending,
    );
    const initial = appendProductionFullRunAttempt(waiting, {
      run: runFor(pending),
      pending,
      observed_at: NOW,
    });
    const trajectory = await observeProductionFullRun(initial, {
      run: runFor(pending, {
        status: "completed",
        broadcast: {
          status: "trajectory_ready",
          quality_profile: "stable_broadcast",
          max_manual_review_windows: 30,
          trajectory_generation_id: `trajectory-${"a".repeat(24)}`,
        },
      }),
      observed_at: "2026-07-15T16:05:00.000Z",
    });
    expect(trajectory.revision).toBe(initial.revision + 1);
    expect(trajectory.attempts[0]).toMatchObject({
      run_id: pending.expected_run_id,
      request_sha256: pending.request_sha256,
      last_observed: {
        workflow_state: "trajectory_ready",
        trajectory_generation_id: `trajectory-${"a".repeat(24)}`,
      },
    });
    expect(productionFullRunRequiresStop(trajectory)).toBe(false);
  });

  it("recomputes an attempt request digest before accepting remote observations", async () => {
    const pending = (await submission()).pending;
    const initial = appendProductionFullRunAttempt(
      setPendingProductionFullRun(createProductionFullRunState(), pending),
      { run: runFor(pending), pending, observed_at: NOW },
    );
    const tampered = {
      ...initial,
      attempts: initial.attempts.map((attempt) => ({
        ...attempt,
        request_sha256: "0".repeat(64),
      })),
    };
    expect(
      await observeProductionFullRun(tampered, {
        run: runFor(pending, {
          status: "completed",
          broadcast: {
            status: "trajectory_ready",
            quality_profile: "stable_broadcast",
            max_manual_review_windows: 30,
          },
        }),
        observed_at: "2026-07-15T16:05:00.000Z",
      }),
    ).toBe(tampered);
  });

  it.each([
    [
      "resolution",
      (
        confirmation: NonNullable<CreateRunRequest["calibration_confirmation"]>,
      ) => void (confirmation.source_resolution = [1_280, 720]),
    ],
    [
      "confirmed frame indices",
      (
        confirmation: NonNullable<CreateRunRequest["calibration_confirmation"]>,
      ) => void (confirmation.confirmed_sample_frames = [10, 20, 31]),
    ],
    [
      "field polygon",
      (
        confirmation: NonNullable<CreateRunRequest["calibration_confirmation"]>,
      ) =>
        void (confirmation.field_polygon = [
          [101, 100],
          ...CALIBRATION.approved_polygon.slice(1),
        ]),
    ],
    [
      "exclusion polygons",
      (
        confirmation: NonNullable<CreateRunRequest["calibration_confirmation"]>,
      ) => void (confirmation.exclusion_polygons = []),
    ],
  ])("rejects persisted full-run %s drift", async (_label, mutate) => {
    const ready = await context();
    const pending = (await submission()).pending;
    const initial = appendProductionFullRunAttempt(
      setPendingProductionFullRun(createProductionFullRunState(), pending),
      { run: runFor(pending), pending, observed_at: NOW },
    );
    const changed = structuredClone(initial);
    mutate(changed.attempts[0].request.calibration_confirmation!);
    expect(productionFullRunMatchesContext(changed, ready)).toBe(false);
  });

  it("persists only an exact active child recovered before the parent facade catches up", async () => {
    const pending = (await submission()).pending;
    const initial = appendProductionFullRunAttempt(
      setPendingProductionFullRun(createProductionFullRunState(), pending),
      {
        run: runFor(pending, {
          status: "completed",
          broadcast: {
            status: "trajectory_ready",
            quality_profile: "stable_broadcast",
            max_manual_review_windows: 30,
            trajectory_generation_id: `trajectory-${"a".repeat(24)}`,
          },
        }),
        pending,
        observed_at: NOW,
      },
    );
    const parent = runFor(pending, {
      status: "completed",
      broadcast: {
        status: "trajectory_ready",
        quality_profile: "stable_broadcast",
        max_manual_review_windows: 30,
        trajectory_generation_id: `trajectory-${"a".repeat(24)}`,
      },
    });
    const child: RunRecord = {
      run_id: "render-child",
      source: "broadcast_hybrid_render",
      status: "running",
      created_at: NOW,
      parent_run_id: parent.run_id,
      broadcast: {
        operation: "render",
        operation_status: "running",
        parent_run_id: parent.run_id,
      },
    };
    const recovered = await observeProductionFullRun(initial, {
      run: parent,
      observed_at: "2026-07-15T16:06:00.000Z",
      recovery: { state: "rendering", operation_run: child },
    });
    expect(recovered.attempts[0].last_observed).toMatchObject({
      workflow_state: "rendering",
      operation: { run_id: "render-child", kind: "render", status: "running" },
    });
    expect(productionFullRunRequiresStop(recovered)).toBe(true);

    for (const invalid of [
      { ...child, parent_run_id: "other-parent" },
      { ...child, source: "broadcast_hybrid_recompute" },
      { ...child, broadcast: { ...child.broadcast, operation: "recompute" } },
    ]) {
      expect(
        await observeProductionFullRun(initial, {
          run: parent,
          observed_at: "2026-07-15T16:06:00.000Z",
          recovery: { state: "rendering", operation_run: invalid },
        }),
      ).toBe(initial);
    }
  });

  it("builds every retry with a new UUID and the accepted trial as parent", async () => {
    const first = (await submission()).pending;
    const firstState = appendProductionFullRunAttempt(
      setPendingProductionFullRun(createProductionFullRunState(), first),
      {
        run: runFor(first, { status: "failed", error: "failed" }),
        pending: first,
        observed_at: NOW,
      },
    );
    const retry = await buildProductionFullRunSubmission({
      ...(await context()),
      submission_id: "full-submission-2",
      output_id: "33333333-3333-4333-8333-333333333333",
      generation: nextProductionFullRunGeneration(firstState),
      created_at: "2026-07-15T16:10:00.000Z",
    });
    expect(retry.pending.expected_run_id).not.toBe(first.expected_run_id);
    expect(retry.pending.request.parent_run_id).toBe("trial-accepted");
    expect(retry.pending.request.parent_run_id).not.toBe(first.expected_run_id);
  });

  it("rejects malformed or dangling persisted full-run state", async () => {
    const pending = (await submission()).pending;
    const waiting = setPendingProductionFullRun(
      createProductionFullRunState(),
      pending,
    );
    expect(
      isProductionFullRunState({ ...waiting, current_run_id: "missing" }),
    ).toBe(false);
    expect(
      isProductionFullRunState({
        ...waiting,
        pending_submission: {
          ...pending,
          request: { ...pending.request, config_patch: { runtime: {} } },
        },
      }),
    ).toBe(false);
  });
});
