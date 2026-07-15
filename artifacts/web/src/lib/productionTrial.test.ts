import { describe, expect, it } from "vitest";
import type {
  ArtifactSummary,
  BallAuditReport,
  RunRecord,
} from "@workspace/api-client-react";

import type { ProductionCalibrationDraft } from "./productionCalibration";
import {
  acceptProductionTrial,
  appendProductionTrialAttempt,
  assessProductionTrialEvidence,
  buildProductionTrialIntent,
  buildProductionTrialSubmission,
  canonicalJson,
  createProductionTrialState,
  invalidateProductionTrialAcceptance,
  isProductionTrialSettings,
  isProductionTrialState,
  materializedProductionTrialConfigName,
  observeProductionTrialRun,
  productionTrialArtifactContract,
  productionTrialEvidenceGeneration,
  productionTrialEvidenceSnapshotIdentity,
  reconcilePendingProductionTrial,
  selectProductionTrialVideo,
  setPendingProductionTrial,
  sha256Text,
  type ProductionTrialPendingSubmission,
  type ProductionTrialReadinessSummary,
  type ProductionTrialSettings,
} from "./productionTrial";
import type { SourceSignature } from "./productionWorkflow";

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
    [100, 200],
    [1_800, 150],
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
  confirmed_frames: [10, 20, 30].map((frame_index, sample_index) => ({
    input_video: SOURCE.path,
    frame_index,
    frame_time_seconds: frame_index / 25,
    sample_index,
    source_resolution: { width: 1_920, height: 1_080 },
    polygon_digest: CALIBRATION_DIGEST,
  })),
};
const SETTINGS: ProductionTrialSettings = {
  base_config_name: "configs/base.yaml",
  start_frame: 25,
  max_frames: 240,
  enable_postprocess: true,
  enable_follow_cam: true,
  tuning_patch: { detector: { confidence: 0.2 } },
};
const CREATED_AT = "2026-07-15T12:00:00.000Z";

async function submission(parent_run_id: string | null = null, generation = 1) {
  return buildProductionTrialSubmission({
    workflow_id: "workflow-a",
    source: SOURCE,
    calibration: CALIBRATION,
    settings: SETTINGS,
    parent_run_id,
    submission_id: `submission-${generation}`,
    output_id: `output-${generation}`,
    generation,
    created_at: CREATED_AT,
  });
}

function artifacts(
  overrides: Partial<ArtifactSummary>[] = [],
): ArtifactSummary[] {
  const base: ArtifactSummary[] = [
    {
      name: "run_manifest.json",
      path: "run_manifest.json",
      kind: "json",
      exists: true,
      size_bytes: 120,
    },
    {
      name: "metrics_report.json",
      path: "metrics_report.json",
      kind: "json",
      exists: true,
      size_bytes: 130,
    },
    {
      name: "ball_track.csv",
      path: "ball_track.csv",
      kind: "csv",
      exists: true,
      size_bytes: 140,
    },
    {
      name: "ball_audit.json",
      path: "ball_audit.json",
      kind: "json",
      exists: true,
      size_bytes: 150,
    },
    {
      name: "ball_track.cleaned.csv",
      path: "ball_track.cleaned.csv",
      kind: "csv",
      exists: true,
      size_bytes: 160,
    },
    {
      name: "follow_cam.mp4",
      path: "follow_cam.mp4",
      kind: "video",
      exists: true,
      size_bytes: 1_000,
      content_type: "video/mp4",
    },
  ];
  for (const override of overrides) {
    const index = base.findIndex((item) => item.name === override.name);
    if (index >= 0) base[index] = { ...base[index], ...override };
    else base.push(override as ArtifactSummary);
  }
  return base;
}

const STATS = {
  raw: {
    frame_count: 240,
    detected: 150,
    predicted: 60,
    lost: 30,
    detected_ratio: 0.625,
    predicted_ratio: 0.25,
    lost_ratio: 0.125,
    longest_lost_streak: 5,
    false_positive_island_count: 2,
    max_step_px: 32.5,
  },
  cleaned: {
    frame_count: 240,
    detected: 160,
    predicted: 60,
    lost: 20,
    detected_ratio: 0.6667,
    predicted_ratio: 0.25,
    lost_ratio: 0.0833,
  },
  quality_gate: { status: "warn" },
};

const AUDIT: BallAuditReport = {
  schema_version: "1.0",
  generated_at: CREATED_AT,
  summary: {
    frame_count: 240,
    source_count: 2,
    tracklet_count: 4,
    suspicious_tracklet_count: 1,
    review_event_count: 2,
    lost_gap_count: 1,
    max_step_px: 32.5,
  },
  sources: [
    { name: "raw", path: "ball_track.csv", row_count: 240, tracklet_count: 2 },
    {
      name: "cleaned",
      path: "ball_track.cleaned.csv",
      row_count: 240,
      tracklet_count: 2,
    },
  ],
  tracklets: Array.from({ length: 4 }, (_, index) => ({
    id: `raw:${index}`,
    source: index < 2 ? "raw" : "cleaned",
    start_frame: index,
    end_frame: index,
    length: 1,
    status_counts: { Detected: 1 },
    mean_confidence: 0.8,
    start_point: { x: 1, y: 1 },
    end_point: { x: 1, y: 1 },
    max_step_px: 0,
    flags: index === 0 ? ["short_tracklet"] : [],
    suspicion_score: index === 0 ? 1 : 0,
  })),
  review_events: [
    {
      source: "raw",
      type: "lost_gap",
      severity: "warn",
      frame_count: 3,
      reason: "gap",
    },
    {
      source: "raw",
      type: "short_tracklet",
      severity: "warn",
      frame_count: 1,
      reason: "short",
    },
  ],
};

const METRICS = {
  schema_version: "1.0",
  generated_at: CREATED_AT,
  tracks: { raw: STATS.raw, cleaned: STATS.cleaned },
  quality_gate: STATS.quality_gate,
};
function trackCsv(stats: {
  detected: number;
  predicted: number;
  lost: number;
}) {
  const statuses = [
    ...Array.from({ length: stats.detected }, () => "Detected"),
    ...Array.from({ length: stats.predicted }, () => "Predicted"),
    ...Array.from({ length: stats.lost }, () => "Lost"),
  ];
  return [
    "Frame,X,Y,Confidence,Status",
    ...statuses.map((status, frame) =>
      status === "Lost"
        ? `${frame},,,0,Lost`
        : `${frame},${frame + 1},${frame + 2},0.9,${status}`,
    ),
  ].join("\n");
}
const TRACK_CSV = trackCsv(STATS.raw);
const CLEANED_TRACK_CSV = trackCsv(STATS.cleaned);

function completedRun(changes: Partial<RunRecord> = {}): RunRecord {
  return {
    run_id: "trial-1",
    source: "api",
    status: "completed",
    created_at: CREATED_AT,
    completed_at: CREATED_AT,
    config_name: SETTINGS.base_config_name,
    input_video: SOURCE.path,
    parent_run_id: null,
    output_dir: "outputs/production_trial_output-1",
    artifacts: artifacts(),
    stats: STATS,
    ...changes,
  };
}

describe("canonical production JSON", () => {
  it("sorts object keys, preserves arrays, and normalizes negative zero", () => {
    expect(canonicalJson({ z: -0, a: [3, { y: 2, x: 1 }] })).toBe(
      '{"a":[3,{"x":1,"y":2}],"z":0}',
    );
  });

  it.each([
    { value: undefined },
    { value: Number.NaN },
    { value: Number.POSITIVE_INFINITY },
    { value: BigInt(1) },
  ])("rejects non-JSON value %#", (input) => {
    expect(() => canonicalJson(input)).toThrow();
  });

  it("hashes UTF-8 text deterministically", async () => {
    expect(await sha256Text("hello")).toBe(
      "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
    );
  });
});

describe("production trial artifact contract", () => {
  const core = [
    "run_manifest.json",
    "metrics_report.json",
    "ball_track.csv",
    "ball_audit.json",
  ];

  it("returns and accepts the exact postprocess-on and postprocess-off lists", () => {
    const off = productionTrialArtifactContract({
      enable_postprocess: false,
      video_artifact_name: "annotated.mp4",
    }).required_names;
    expect(off).toEqual([...core, "annotated.mp4"]);
    expect(
      productionTrialArtifactContract({
        enable_postprocess: false,
        video_artifact_name: "annotated.mp4",
        artifact_names: [...off].reverse(),
      }).matches,
    ).toBe(true);
    const on = productionTrialArtifactContract({
      enable_postprocess: true,
      video_artifact_name: "follow_cam.mp4",
    }).required_names;
    expect(on).toEqual([...core, "ball_track.cleaned.csv", "follow_cam.mp4"]);
    expect(
      productionTrialArtifactContract({
        enable_postprocess: true,
        video_artifact_name: "follow_cam.mp4",
        artifact_names: on,
      }).matches,
    ).toBe(true);
  });

  it.each([
    ["on missing cleaned", true, "follow_cam.mp4", [...core, "follow_cam.mp4"]],
    ...core.map((missing) => [
      `off missing ${missing}`,
      false,
      "annotated.mp4",
      [...core.filter((name) => name !== missing), "annotated.mp4"],
    ]),
    ["video mismatch", false, "annotated.mp4", [...core, "follow_cam.mp4"]],
    [
      "duplicate",
      false,
      "annotated.mp4",
      [...core, "annotated.mp4", "annotated.mp4"],
    ],
    ["empty", false, "annotated.mp4", [...core, ""]],
    [
      "untrusted extra",
      false,
      "annotated.mp4",
      [...core, "annotated.mp4", "debug.json"],
    ],
  ])("rejects %s", (_label, enablePostprocess, videoName, names) => {
    expect(
      productionTrialArtifactContract({
        enable_postprocess: Boolean(enablePostprocess),
        video_artifact_name: String(videoName),
        artifact_names: names as string[],
      }).matches,
    ).toBe(false);
  });
});

describe("production trial request", () => {
  it.each([
    ["zero frames", { ...CALIBRATION, confirmed_frames: [] }],
    [
      "two frames",
      {
        ...CALIBRATION,
        confirmed_frames: CALIBRATION.confirmed_frames.slice(0, 2),
      },
    ],
    [
      "duplicate frame",
      {
        ...CALIBRATION,
        confirmed_frames: [
          CALIBRATION.confirmed_frames[0],
          { ...CALIBRATION.confirmed_frames[0] },
          CALIBRATION.confirmed_frames[2],
        ],
      },
    ],
    [
      "wrong source",
      {
        ...CALIBRATION,
        confirmed_frames: CALIBRATION.confirmed_frames.map((frame, index) =>
          index === 1 ? { ...frame, input_video: "data/other.mp4" } : frame,
        ),
      },
    ],
    [
      "wrong digest",
      {
        ...CALIBRATION,
        confirmed_frames: CALIBRATION.confirmed_frames.map((frame, index) =>
          index === 1 ? { ...frame, polygon_digest: "d".repeat(64) } : frame,
        ),
      },
    ],
    [
      "wrong resolution",
      {
        ...CALIBRATION,
        confirmed_frames: CALIBRATION.confirmed_frames.map((frame, index) =>
          index === 1
            ? { ...frame, source_resolution: { width: 1_280, height: 720 } }
            : frame,
        ),
      },
    ],
  ])(
    "rejects incomplete calibration evidence: %s",
    async (_label, calibration) => {
      await expect(
        buildProductionTrialSubmission({
          workflow_id: "workflow-a",
          source: SOURCE,
          calibration,
          settings: SETTINGS,
          parent_run_id: null,
          submission_id: "submission-invalid",
          output_id: "output-invalid",
          generation: 1,
          created_at: CREATED_AT,
        }),
      ).rejects.toThrow(/calibration/i);
    },
  );

  it("builds the exact bounded request, protected calibration patch, bbox, and machine note", async () => {
    const result = await submission("trial-previous");
    const intentDigest = await sha256Text(canonicalJson(result.intent));
    const machineNote = {
      schema_version: "1.0",
      purpose: "production_trial",
      workflow_id: "workflow-a",
      submission_id: "submission-1",
      output_id: "output-1",
      generation: 1,
      calibration_digest: CALIBRATION_DIGEST,
      intent_sha256: intentDigest,
      start_frame: SETTINGS.start_frame,
      max_frames: SETTINGS.max_frames,
      enable_postprocess: SETTINGS.enable_postprocess,
      enable_follow_cam: SETTINGS.enable_follow_cam,
    };
    expect(result.pending.request).toEqual({
      config_name: "configs/base.yaml",
      input_video: SOURCE.path,
      parent_run_id: "trial-previous",
      output_dir_name: "production_trial_output-1",
      config_patch: {
        detector: { confidence: 0.2 },
        input_video: SOURCE.path,
        filtering: { roi: [100, 150, 1_800, 950] },
        scene_bias: {
          enabled: true,
          ground_zones: [
            {
              name: "production_field",
              points: CALIBRATION.approved_polygon,
            },
          ],
          negative_rois: [
            {
              name: "production_exclusion_1",
              points: CALIBRATION.exclusions[0],
            },
          ],
        },
        postprocess: { enabled: true },
        follow_cam: { enabled: true },
        runtime: { start_frame: 25, max_frames: 240 },
        metadata: {
          production_workflow: {
            ...machineNote,
            source_signature: SOURCE,
            output_dir_name: "production_trial_output-1",
          },
        },
      },
      enable_postprocess: true,
      enable_follow_cam: true,
      start_frame: 25,
      max_frames: 240,
      pipeline_mode: "standard",
      notes: canonicalJson(machineNote),
    });
    expect(result.pending.intent_sha256).toBe(intentDigest);
    expect(result.pending.request_sha256).toBe(
      await sha256Text(canonicalJson(result.pending.request)),
    );
  });

  it("deep copies polygon, exclusions, and tuning and keeps intent independent of lineage", async () => {
    const one = await submission(null, 1);
    const two = await buildProductionTrialSubmission({
      workflow_id: "workflow-a",
      source: SOURCE,
      calibration: CALIBRATION,
      settings: SETTINGS,
      parent_run_id: "trial-1",
      submission_id: "submission-2",
      output_id: "output-2",
      generation: 2,
      created_at: CREATED_AT,
    });
    expect(one.pending.intent_sha256).toBe(two.pending.intent_sha256);
    expect(one.pending.request_sha256).not.toBe(two.pending.request_sha256);
    expect(one.intent.approved_polygon).not.toBe(CALIBRATION.approved_polygon);
    expect(one.intent.exclusions[0]).not.toBe(CALIBRATION.exclusions[0]);
    expect(one.intent.tuning_patch).not.toBe(SETTINGS.tuning_patch);
  });

  it.each([
    [{ ...SETTINGS, start_frame: -1 }, "start"],
    [{ ...SETTINGS, start_frame: 1.2 }, "start"],
    [{ ...SETTINGS, max_frames: 0 }, "max"],
    [{ ...SETTINGS, max_frames: Number.NaN }, "max"],
  ])("rejects invalid frame settings without clamping %#", (settings) => {
    expect(isProductionTrialSettings(settings)).toBe(false);
    expect(() =>
      buildProductionTrialIntent({
        workflow_id: "workflow-a",
        source: SOURCE,
        calibration: CALIBRATION,
        settings: settings as ProductionTrialSettings,
      }),
    ).toThrow();
  });

  it("protects execution/calibration keys from tuning patch overrides", async () => {
    const result = await buildProductionTrialSubmission({
      workflow_id: "workflow-a",
      source: SOURCE,
      calibration: CALIBRATION,
      settings: {
        ...SETTINGS,
        tuning_patch: {
          input_video: "data/wrong.mp4",
          runtime: { start_frame: 999, worker_threads: 4 },
          filtering: { roi: [0, 0, 1, 1], legacy_filter: true },
          scene_bias: { enabled: false, legacy_scene: true },
          postprocess: { enabled: false, legacy_cleanup: true },
          follow_cam: { enabled: false, legacy_render: true },
          metadata: {
            old: true,
            production_workflow: { workflow_id: "wrong" },
          },
          detector: { confidence: 0.3 },
        },
      },
      parent_run_id: null,
      submission_id: "submission-protected",
      output_id: "output-protected",
      generation: 1,
      created_at: CREATED_AT,
    });
    expect(result.pending.request.config_patch).toMatchObject({
      input_video: SOURCE.path,
      runtime: { start_frame: 25, max_frames: 240, worker_threads: 4 },
      filtering: { roi: [100, 150, 1_800, 950], legacy_filter: true },
      scene_bias: { enabled: true, legacy_scene: true },
      postprocess: { enabled: true, legacy_cleanup: true },
      follow_cam: { enabled: true, legacy_render: true },
      metadata: {
        old: true,
        production_workflow: {
          workflow_id: "workflow-a",
          source_signature: SOURCE,
        },
      },
      detector: { confidence: 0.3 },
    });
  });
});

describe("production trial append-only recovery", () => {
  it("mirrors the backend materialized field-setup config name", () => {
    expect(
      materializedProductionTrialConfigName("default.yaml", "run_demo1234"),
    ).toBe("generated/default_field_setup_run_demo1234.yaml");
    expect(
      materializedProductionTrialConfigName(
        "configs/base.trial.yaml",
        "production_trial_output-2",
      ),
    ).toBe("generated/base.trial_field_setup_production_trial_output-2.yaml");
  });

  it("persists pending before append, appends once, and tracks active terminal status", async () => {
    const { pending } = await submission();
    const pendingState = setPendingProductionTrial(
      createProductionTrialState(SETTINGS),
      pending,
    );
    const queued = appendProductionTrialAttempt(pendingState, {
      run: { run_id: "trial-1", status: "queued" },
      pending,
      observed_at: CREATED_AT,
    });
    expect(queued.attempts).toHaveLength(1);
    expect(queued.active_run_id).toBe("trial-1");
    expect(queued.pending_submission).toBeNull();
    expect(
      appendProductionTrialAttempt(queued, {
        run: { run_id: "trial-1", status: "queued" },
        pending,
        observed_at: CREATED_AT,
      }).attempts,
    ).toHaveLength(1);

    const failed = observeProductionTrialRun(queued, {
      run_id: "trial-1",
      status: "failed",
      observed_at: "later",
    });
    expect(failed.active_run_id).toBeNull();
    expect(failed.attempts[0].last_observed.status).toBe("failed");
  });

  it.each(["queued", "running", "completed"] as const)(
    "reconciles one materialized %s run only for the current generation",
    async (status) => {
      const { pending } = await submission(null, 2);
      const state = setPendingProductionTrial(
        createProductionTrialState(SETTINGS),
        pending,
      );
      const run: RunRecord = {
        run_id: `production_trial_${pending.output_id}`,
        source: "api",
        status,
        created_at: CREATED_AT,
        notes: pending.request.notes,
        config_name: materializedProductionTrialConfigName(
          pending.request.config_name,
          `production_trial_${pending.output_id}`,
        ),
        input_video: pending.request.input_video,
        parent_run_id: pending.request.parent_run_id,
        output_dir: `outputs/production_trial_${pending.output_id}`,
        modules_enabled: {
          postprocess: pending.request.enable_postprocess,
          follow_cam: pending.request.enable_follow_cam,
        },
      };
      expect(
        reconcilePendingProductionTrial(state, {
          workflow_id: "workflow-a",
          expected_generation: 1,
          runs: [run],
          observed_at: CREATED_AT,
        }),
      ).toBe(state);
      const recovered = reconcilePendingProductionTrial(state, {
        workflow_id: "workflow-a",
        expected_generation: 2,
        runs: [run],
        observed_at: CREATED_AT,
      });
      expect(recovered.attempts.map((attempt) => attempt.run_id)).toEqual([
        `production_trial_${pending.output_id}`,
      ]);
      expect(recovered.active_run_id).toBe(
        status === "queued" || status === "running"
          ? `production_trial_${pending.output_id}`
          : null,
      );
      expect(
        reconcilePendingProductionTrial(state, {
          workflow_id: "workflow-a",
          expected_generation: 2,
          runs: [run, { ...run }],
          observed_at: CREATED_AT,
        }),
      ).toBe(state);
    },
  );

  it("rejects base configs and every foreign recovery identity", async () => {
    const { pending } = await submission(null, 2);
    const state = setPendingProductionTrial(
      createProductionTrialState(SETTINGS),
      pending,
    );
    const run: RunRecord = {
      run_id: `production_trial_${pending.output_id}`,
      source: "api",
      status: "running",
      created_at: CREATED_AT,
      notes: pending.request.notes,
      config_name: materializedProductionTrialConfigName(
        pending.request.config_name,
        `production_trial_${pending.output_id}`,
      ),
      input_video: pending.request.input_video,
      parent_run_id: pending.request.parent_run_id,
      output_dir: `outputs/production_trial_${pending.output_id}`,
      modules_enabled: { postprocess: true, follow_cam: true },
    };
    const changedNote = (changes: Record<string, unknown>): string =>
      canonicalJson({
        ...(JSON.parse(String(pending.request.notes)) as Record<
          string,
          unknown
        >),
        ...changes,
      });
    const foreignRuns: RunRecord[] = [
      { ...run, run_id: "production_trial_foreign" },
      { ...run, source: "cli" },
      { ...run, config_name: pending.request.config_name },
      { ...run, config_name: "generated/foreign_field_setup_foreign.yaml" },
      { ...run, notes: changedNote({ workflow_id: "workflow-b" }) },
      { ...run, notes: changedNote({ submission_id: "submission-foreign" }) },
      { ...run, notes: changedNote({ output_id: "output-foreign" }) },
      { ...run, notes: changedNote({ generation: 99 }) },
      { ...run, notes: changedNote({ intent_sha256: "0".repeat(64) }) },
      { ...run, notes: changedNote({ calibration_digest: "0".repeat(64) }) },
      { ...run, input_video: "data/other.mp4" },
      { ...run, parent_run_id: "parent-foreign" },
      {
        ...run,
        modules_enabled: { postprocess: false, follow_cam: true },
      },
      {
        ...run,
        modules_enabled: { postprocess: true, follow_cam: false },
      },
    ];
    for (const foreign of foreignRuns) {
      expect(
        reconcilePendingProductionTrial(state, {
          workflow_id: "workflow-a",
          expected_generation: 2,
          runs: [foreign],
          observed_at: CREATED_AT,
        }),
      ).toBe(state);
    }
  });

  it("rejects conflicting duplicate run or submission identities", async () => {
    const one = await submission(null, 1);
    const two = await submission("trial-1", 2);
    const state = appendProductionTrialAttempt(
      setPendingProductionTrial(
        createProductionTrialState(SETTINGS),
        one.pending,
      ),
      {
        run: { run_id: "trial-1", status: "completed" },
        pending: one.pending,
        observed_at: CREATED_AT,
      },
    );
    expect(() =>
      appendProductionTrialAttempt(state, {
        run: { run_id: "trial-1", status: "completed" },
        pending: two.pending,
        observed_at: CREATED_AT,
      }),
    ).toThrow("Conflicting");
  });
});

describe("production trial evidence and acceptance", () => {
  it("requires unique readable evidence, valid raw/cleaned stats, and a loaded main video", () => {
    const run = completedRun();
    const result = assessProductionTrialEvidence({
      run,
      artifacts: run.artifacts ?? [],
      manifest: {
        schema_version: "1.0",
        run_id: run.run_id,
        input_video: run.input_video,
        config_name: run.config_name,
        status: run.status,
        notes: null,
      },
      metrics: METRICS,
      audit: AUDIT,
      raw_csv: TRACK_CSV,
      cleaned_csv: CLEANED_TRACK_CSV,
      readable_artifact_names: [
        "run_manifest.json",
        "metrics_report.json",
        "ball_track.csv",
        "ball_audit.json",
        "ball_track.cleaned.csv",
      ],
      enable_postprocess: true,
      enable_follow_cam: true,
      video_loaded: true,
    });
    expect(result.ready).toBe(true);
    if (!result.ready) throw new Error("expected ready evidence");
    expect(result.video.name).toBe("follow_cam.mp4");
    expect(result.quality).toMatchObject({
      frame_count: 240,
      detected_ratio: 0.625,
      longest_lost_streak: 5,
      false_positive_island_count: 2,
      audit_tracklet_count: 4,
      quality_gate_status: "warn",
    });
  });

  it.each([
    [
      "missing",
      artifacts([{ name: "ball_audit.json", exists: false }]),
      "artifact_unavailable:ball_audit.json",
    ],
    [
      "zero",
      artifacts([{ name: "metrics_report.json", size_bytes: 0 }]),
      "artifact_unavailable:metrics_report.json",
    ],
    [
      "duplicate",
      [...artifacts(), { ...artifacts()[0] }],
      "artifact_unavailable:run_manifest.json",
    ],
  ])("blocks %s required artifacts", (_, list, reason) => {
    const result = assessProductionTrialEvidence({
      run: completedRun({ artifacts: list }),
      artifacts: list,
      manifest: {},
      metrics: {},
      audit: AUDIT,
      raw_csv: TRACK_CSV,
      cleaned_csv: CLEANED_TRACK_CSV,
      readable_artifact_names: [
        "run_manifest.json",
        "metrics_report.json",
        "ball_track.csv",
        "ball_audit.json",
        "ball_track.cleaned.csv",
      ],
      enable_postprocess: true,
      enable_follow_cam: true,
      video_loaded: true,
    });
    expect(result).toMatchObject({
      ready: false,
      reasons: expect.arrayContaining([reason]),
    });
  });

  it("blocks corrupt manifest, metrics, audit, stats, cleaned stats, and unloaded video", () => {
    const result = assessProductionTrialEvidence({
      run: completedRun({ stats: { raw: { frame_count: Number.NaN } } }),
      artifacts: artifacts(),
      manifest: { run_id: "wrong" },
      metrics: [],
      audit: { ...AUDIT, summary: { ...AUDIT.summary, frame_count: 0 } },
      raw_csv: TRACK_CSV,
      cleaned_csv: CLEANED_TRACK_CSV,
      readable_artifact_names: [
        "run_manifest.json",
        "metrics_report.json",
        "ball_track.csv",
        "ball_audit.json",
        "ball_track.cleaned.csv",
      ],
      enable_postprocess: true,
      enable_follow_cam: true,
      video_loaded: false,
    });
    expect(result).toMatchObject({
      ready: false,
      reasons: expect.arrayContaining([
        "manifest_mismatch",
        "metrics_unreadable",
        "audit_unreadable",
        "raw_stats_unreadable",
        "cleaned_stats_unreadable",
        "video_not_loaded",
      ]),
    });
  });

  it.each([
    [
      "metric counts",
      {
        metrics: {
          ...METRICS,
          tracks: { ...METRICS.tracks, raw: { ...STATS.raw, detected: 149 } },
        },
      },
      "metrics_unreadable",
    ],
    [
      "raw ratios",
      {
        run: completedRun({
          stats: { ...STATS, raw: { ...STATS.raw, detected_ratio: 0.5 } },
        }),
      },
      "raw_stats_unreadable",
    ],
    [
      "audit counts",
      {
        audit: { ...AUDIT, summary: { ...AUDIT.summary, tracklet_count: 99 } },
      },
      "audit_unreadable",
    ],
    ["raw CSV header", { raw_csv: "frame,x,y\n0,1,1\n" }, "raw_csv_unreadable"],
    [
      "raw CSV malformed row",
      { raw_csv: "Frame,X,Y,Confidence,Status\ngarbage" },
      "raw_csv_unreadable",
    ],
    [
      "raw CSV row count",
      { raw_csv: TRACK_CSV.split("\n").slice(0, -1).join("\n") },
      "raw_csv_unreadable",
    ],
    [
      "raw CSV status",
      { raw_csv: TRACK_CSV.replace(",Detected", ",Unknown") },
      "raw_csv_unreadable",
    ],
    [
      "raw CSV duplicate frame",
      { raw_csv: TRACK_CSV.replace(/^1,/m, "0,") },
      "raw_csv_unreadable",
    ],
    [
      "cleaned CSV header",
      { cleaned_csv: "frame,x,y\n0,1,1\n" },
      "cleaned_csv_unreadable",
    ],
    [
      "manifest notes",
      {
        manifest: {
          schema_version: "1.0",
          run_id: "trial-1",
          input_video: SOURCE.path,
          config_name: SETTINGS.base_config_name,
          status: "completed",
          notes: "unexpected",
        },
      },
      "manifest_mismatch",
    ],
  ])("blocks schema-corrupt %s", (_label, change, reason) => {
    const run = (change as { run?: RunRecord }).run ?? completedRun();
    const result = assessProductionTrialEvidence({
      run,
      artifacts: artifacts(),
      manifest: (change as { manifest?: unknown }).manifest ?? {
        schema_version: "1.0",
        run_id: run.run_id,
        input_video: run.input_video,
        config_name: run.config_name,
        status: run.status,
        notes: null,
      },
      metrics: (change as { metrics?: unknown }).metrics ?? METRICS,
      audit: ((change as { audit?: unknown }).audit ??
        AUDIT) as BallAuditReport,
      raw_csv: (change as { raw_csv?: unknown }).raw_csv ?? TRACK_CSV,
      cleaned_csv:
        (change as { cleaned_csv?: unknown }).cleaned_csv ?? CLEANED_TRACK_CSV,
      readable_artifact_names: [
        "run_manifest.json",
        "metrics_report.json",
        "ball_track.csv",
        "ball_audit.json",
        "ball_track.cleaned.csv",
      ],
      enable_postprocess: true,
      enable_follow_cam: true,
      video_loaded: true,
    });
    expect(result).toMatchObject({
      ready: false,
      reasons: expect.arrayContaining([reason]),
    });
  });

  it("hashes every acceptance body while ignoring artifact order and unrelated artifacts", async () => {
    const selectedVideo = artifacts().find(
      (item) => item.name === "follow_cam.mp4",
    )!;
    const base = {
      run_id: "trial-1",
      intent_sha256: "d".repeat(64),
      request_sha256: "e".repeat(64),
      artifacts: artifacts(),
      stats: STATS,
      manifest: {
        schema_version: "1.0",
        run_id: "trial-1",
        input_video: SOURCE.path,
        config_name: SETTINGS.base_config_name,
        status: "completed",
        notes: null,
      },
      metrics: METRICS,
      audit: AUDIT,
      raw_csv: TRACK_CSV,
      cleaned_csv: CLEANED_TRACK_CSV,
      selected_video: selectedVideo,
      video_metadata: { duration: 10, width: 1_920, height: 1_080 },
    };
    const baseline = await productionTrialEvidenceGeneration(base);
    expect(
      await productionTrialEvidenceGeneration({
        ...base,
        artifacts: [
          ...[...base.artifacts].reverse(),
          {
            name: "debug.json",
            path: "debug.json",
            kind: "json",
            exists: true,
            size_bytes: 9,
          },
        ],
      }),
    ).toBe(baseline);
    const changes = [
      { ...base, stats: { ...STATS, quality_gate: { status: "pass" } } },
      { ...base, manifest: { ...base.manifest, completed_at: CREATED_AT } },
      { ...base, metrics: { ...METRICS, generated_at: "later" } },
      { ...base, audit: { ...AUDIT, generated_at: "later" } },
      { ...base, raw_csv: `${TRACK_CSV}1,2,2,0.8,Predicted\n` },
      { ...base, cleaned_csv: `${CLEANED_TRACK_CSV}\n999,2,2,0.8,Predicted` },
      { ...base, selected_video: { ...selectedVideo, size_bytes: 1_001 } },
      { ...base, video_metadata: { ...base.video_metadata, duration: 11 } },
    ];
    for (const changed of changes) {
      expect(await productionTrialEvidenceGeneration(changed)).not.toBe(
        baseline,
      );
    }
  });

  it("uses only compact query revisions for synchronous readiness invalidation", () => {
    const identityInput = {
      run_id: "trial-1",
      intent_sha256: "d".repeat(64),
      request_sha256: "e".repeat(64),
      query_revisions: {
        run: 1,
        artifacts: 1,
        manifest: 1,
        metrics: 1,
        audit: 1,
        raw_csv: 1,
        cleaned_csv: 1,
      },
      raw_csv_length: 20_000_000,
      cleaned_csv_length: 18_000_000,
      selected_video: {
        name: "follow_cam.mp4",
        path: "follow_cam.mp4",
        size_bytes: 1_000,
      },
      video_metadata: { duration: 10, width: 1_920, height: 1_080 },
    };
    const baseline = productionTrialEvidenceSnapshotIdentity(identityInput);
    expect(baseline).not.toContain("Frame,X,Y,Confidence,Status");
    expect(
      productionTrialEvidenceSnapshotIdentity({
        ...identityInput,
        query_revisions: { ...identityInput.query_revisions, raw_csv: 2 },
      }),
    ).not.toBe(baseline);
  });

  it("selects ordered top-level videos and excludes nested or ambiguous fallback videos", () => {
    const list = artifacts([
      {
        name: "candidate/chunk.mp4",
        path: "candidate/chunk.mp4",
        kind: "video",
        exists: true,
        size_bytes: 99,
      },
      {
        name: "annotated.mp4",
        path: "annotated.mp4",
        kind: "video",
        exists: true,
        size_bytes: 999,
      },
    ]).filter((item) => item.name !== "follow_cam.mp4");
    expect(selectProductionTrialVideo(list, true)?.name).toBe("annotated.mp4");
    const custom = list.filter((item) => item.name !== "annotated.mp4");
    custom.push({
      name: "custom.mp4",
      path: "custom.mp4",
      kind: "video",
      exists: true,
      size_bytes: 50,
    });
    expect(selectProductionTrialVideo(custom, false)?.name).toBe("custom.mp4");
    custom.push({
      name: "other.mp4",
      path: "other.mp4",
      kind: "video",
      exists: true,
      size_bytes: 50,
    });
    expect(selectProductionTrialVideo(custom, false)).toBeNull();
  });

  it("accepts only a completed current-intent attempt with current evidence generation", async () => {
    const { pending } = await submission();
    let state = appendProductionTrialAttempt(
      setPendingProductionTrial(createProductionTrialState(SETTINGS), pending),
      {
        run: { run_id: "trial-1", status: "completed" },
        pending,
        observed_at: CREATED_AT,
      },
    );
    const generation = await productionTrialEvidenceGeneration({
      run_id: "trial-1",
      intent_sha256: pending.intent_sha256,
      request_sha256: pending.request_sha256,
      artifacts: artifacts(),
      stats: STATS,
      manifest: {
        schema_version: "1.0",
        run_id: "trial-1",
        input_video: SOURCE.path,
        config_name: SETTINGS.base_config_name,
        status: "completed",
        notes: null,
      },
      metrics: METRICS,
      audit: AUDIT,
      raw_csv: TRACK_CSV,
      cleaned_csv: CLEANED_TRACK_CSV,
      selected_video: artifacts().find(
        (item) => item.name === "follow_cam.mp4",
      )!,
      video_metadata: { duration: 1, width: 1_920, height: 1_080 },
    });
    const readiness: ProductionTrialReadinessSummary = {
      run_id: "trial-1",
      request_sha256: pending.request_sha256,
      evidence_generation: generation,
      verified_at: CREATED_AT,
      video_artifact_name: "follow_cam.mp4",
      artifact_names: artifacts().map((item) => item.name),
      quality: {
        frame_count: 240,
        detected: 150,
        predicted: 60,
        lost: 30,
        detected_ratio: 0.625,
        predicted_ratio: 0.25,
        lost_ratio: 0.125,
        longest_lost_streak: 5,
        false_positive_island_count: 2,
        max_step_px: 32.5,
        audit_tracklet_count: 4,
        audit_suspicious_tracklet_count: 1,
        audit_review_event_count: 2,
        audit_lost_gap_count: 3,
        quality_gate_status: "warn",
      },
    };
    state = acceptProductionTrial(state, {
      run: { run_id: "trial-1", status: "completed" },
      current_intent_sha256: pending.intent_sha256,
      readiness,
      accepted_at: CREATED_AT,
    });
    expect(state.accepted?.run_id).toBe("trial-1");
    expect(invalidateProductionTrialAcceptance(state).accepted).toBeNull();
    expect(isProductionTrialState(state)).toBe(true);

    expect(() =>
      acceptProductionTrial(invalidateProductionTrialAcceptance(state), {
        run: { run_id: "trial-1", status: "failed" },
        current_intent_sha256: pending.intent_sha256,
        readiness,
        accepted_at: CREATED_AT,
      }),
    ).toThrow();
    expect(() =>
      acceptProductionTrial(invalidateProductionTrialAcceptance(state), {
        run: { run_id: "trial-1", status: "completed" },
        current_intent_sha256: "f".repeat(64),
        readiness,
        accepted_at: CREATED_AT,
      }),
    ).toThrow();
  });
});

describe("production trial state validation", () => {
  it("binds each attempt parent to its request and the append-only retry chain", async () => {
    const first = await submission(null, 1);
    let state = appendProductionTrialAttempt(
      setPendingProductionTrial(
        createProductionTrialState(SETTINGS),
        first.pending,
      ),
      {
        run: { run_id: "trial-1", status: "failed" },
        pending: first.pending,
        observed_at: CREATED_AT,
      },
    );
    const second = await submission("trial-1", 2);
    state = appendProductionTrialAttempt(
      setPendingProductionTrial(state, second.pending),
      {
        run: { run_id: "trial-2", status: "completed" },
        pending: second.pending,
        observed_at: CREATED_AT,
      },
    );
    expect(isProductionTrialState(state)).toBe(true);

    const detachedAttempt = structuredClone(state);
    detachedAttempt.attempts[1].parent_run_id = "foreign-parent";
    expect(isProductionTrialState(detachedAttempt)).toBe(false);

    const detachedRequest = structuredClone(state);
    detachedRequest.attempts[1].parent_run_id = null;
    detachedRequest.attempts[1].request.parent_run_id = null;
    expect(isProductionTrialState(detachedRequest)).toBe(false);

    const externalFirstParent = structuredClone(state);
    externalFirstParent.attempts[0].parent_run_id = "external-parent";
    externalFirstParent.attempts[0].request.parent_run_id = "external-parent";
    expect(isProductionTrialState(externalFirstParent)).toBe(false);
  });

  it("rejects each malformed accepted-evidence identity field", async () => {
    const { pending } = await submission();
    const observed = appendProductionTrialAttempt(
      setPendingProductionTrial(createProductionTrialState(SETTINGS), pending),
      {
        run: { run_id: "trial-1", status: "completed" },
        pending,
        observed_at: CREATED_AT,
      },
    );
    const readiness: ProductionTrialReadinessSummary = {
      run_id: "trial-1",
      request_sha256: pending.request_sha256,
      evidence_generation: "f".repeat(64),
      verified_at: CREATED_AT,
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
        frame_count: 240,
        detected: 150,
        predicted: 60,
        lost: 30,
        detected_ratio: 0.625,
        predicted_ratio: 0.25,
        lost_ratio: 0.125,
        longest_lost_streak: 5,
        false_positive_island_count: 2,
        max_step_px: 32.5,
        audit_tracklet_count: 4,
        audit_suspicious_tracklet_count: 1,
        audit_review_event_count: 2,
        audit_lost_gap_count: 1,
        quality_gate_status: "warn",
      },
    };
    const accepted = acceptProductionTrial(observed, {
      run: { run_id: "trial-1", status: "completed" },
      current_intent_sha256: pending.intent_sha256,
      readiness,
      accepted_at: CREATED_AT,
    });
    expect(isProductionTrialState(accepted)).toBe(true);
    const corruptions = [
      (value: ProductionTrialState) => {
        value.accepted!.intent_sha256 = "0".repeat(64);
      },
      (value: ProductionTrialState) => {
        value.accepted!.request_sha256 = "0".repeat(64);
      },
      (value: ProductionTrialState) => {
        value.accepted!.readiness.run_id = "other";
      },
      (value: ProductionTrialState) => {
        value.accepted!.readiness.request_sha256 = "0".repeat(64);
      },
      (value: ProductionTrialState) => {
        value.accepted!.readiness.video_artifact_name = "";
      },
      (value: ProductionTrialState) => {
        value.accepted!.readiness.artifact_names = ["same", "same"];
      },
    ];
    for (const corrupt of corruptions) {
      const candidate = structuredClone(accepted);
      corrupt(candidate);
      expect(isProductionTrialState(candidate)).toBe(false);
    }
    const missingCleaned = structuredClone(accepted);
    missingCleaned.accepted!.readiness.artifact_names =
      missingCleaned.accepted!.readiness.artifact_names.filter(
        (name) => name !== "ball_track.cleaned.csv",
      );
    expect(isProductionTrialState(missingCleaned)).toBe(false);

    const offSettings = { ...SETTINGS, enable_postprocess: false };
    const offSubmission = await buildProductionTrialSubmission({
      workflow_id: "workflow-a",
      source: SOURCE,
      calibration: CALIBRATION,
      settings: offSettings,
      parent_run_id: null,
      submission_id: "submission-off",
      output_id: "output-off",
      generation: 1,
      created_at: CREATED_AT,
    });
    const offObserved = appendProductionTrialAttempt(
      setPendingProductionTrial(
        createProductionTrialState(offSettings),
        offSubmission.pending,
      ),
      {
        run: { run_id: "trial-off", status: "completed" },
        pending: offSubmission.pending,
        observed_at: CREATED_AT,
      },
    );
    const offAccepted = acceptProductionTrial(offObserved, {
      run: { run_id: "trial-off", status: "completed" },
      current_intent_sha256: offSubmission.pending.intent_sha256,
      readiness: {
        ...readiness,
        run_id: "trial-off",
        request_sha256: offSubmission.pending.request_sha256,
        artifact_names: productionTrialArtifactContract({
          enable_postprocess: false,
          video_artifact_name: "annotated.mp4",
        }).required_names,
        video_artifact_name: "annotated.mp4",
      },
      accepted_at: CREATED_AT,
    });
    expect(isProductionTrialState(offAccepted)).toBe(true);
    for (const name of [
      "run_manifest.json",
      "metrics_report.json",
      "ball_track.csv",
      "ball_audit.json",
    ]) {
      const missing = structuredClone(offAccepted);
      missing.accepted!.readiness.artifact_names =
        missing.accepted!.readiness.artifact_names.filter(
          (candidate) => candidate !== name,
        );
      expect(isProductionTrialState(missing)).toBe(false);
    }
  });

  it("rejects dangling active/accepted references and duplicate identities", async () => {
    const { pending } = await submission();
    const state = appendProductionTrialAttempt(
      setPendingProductionTrial(createProductionTrialState(SETTINGS), pending),
      {
        run: { run_id: "trial-1", status: "completed" },
        pending,
        observed_at: CREATED_AT,
      },
    );
    expect(isProductionTrialState({ ...state, active_run_id: "missing" })).toBe(
      false,
    );
    expect(
      isProductionTrialState({
        ...state,
        attempts: [...state.attempts, state.attempts[0]],
      }),
    ).toBe(false);
    expect(
      isProductionTrialState({
        ...state,
        accepted: {
          run_id: "missing",
          intent_sha256: pending.intent_sha256,
          request_sha256: pending.request_sha256,
          accepted_at: CREATED_AT,
          readiness: {},
        },
      }),
    ).toBe(false);
  });

  it("rejects pending references that duplicate an appended submission", async () => {
    const { pending } = await submission();
    const state = appendProductionTrialAttempt(
      setPendingProductionTrial(createProductionTrialState(SETTINGS), pending),
      {
        run: { run_id: "trial-1", status: "completed" },
        pending,
        observed_at: CREATED_AT,
      },
    );
    expect(
      isProductionTrialState({
        ...state,
        pending_submission: pending as ProductionTrialPendingSubmission,
      }),
    ).toBe(false);
  });
});
