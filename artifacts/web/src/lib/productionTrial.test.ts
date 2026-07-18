import { describe, expect, it } from "vitest";
import type {
  ArtifactSummary,
  BallAuditReport,
  RunRecord,
  TrialTuningControl,
} from "@workspace/api-client-react";

import type { ProductionCalibrationDraft } from "./productionCalibration";
import {
  acceptProductionTrial,
  appendProductionTrialAttempt,
  assessProductionTrialEvidence,
  buildVersionedProductionTuningPatch,
  buildProductionTrialIntent,
  buildProductionTrialSubmission,
  canonicalJson,
  createProductionTrialState,
  invalidateProductionTrialAcceptance,
  isProductionTrialSettings,
  isProductionTrialState,
  isTrialSignalGateV2,
  materializedProductionTrialConfigName,
  observeProductionTrialRun,
  productionTrialArtifactContract,
  productionTrialEvidenceGeneration,
  productionTrialEvidenceSnapshotIdentity,
  productionTrialSignalGateAcceptable,
  productionTrialSubmissionLineage,
  productionTrialTuningSchema,
  productionTuningDraft,
  productionTuningHistory,
  productionTuningVersion,
  reconcilePendingProductionTrial,
  selectProductionTrialVideo,
  setPendingProductionTrial,
  sha256Text,
  type ProductionTrialSignalGateV2,
  type ProductionTrialTuningControl,
  type ProductionTrialPendingSubmission,
  type ProductionTrialReadinessSummary,
  type ProductionTrialSettings,
  type TrialDiagnosticObservation,
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
  tuning_patch: { detector: { confidence_threshold: 0.2 } },
};
const CREATED_AT = "2026-07-15T12:00:00.000Z";

const collected = (value: number): TrialDiagnosticObservation => ({
  status: "collected",
  value,
});

const trackDiagnostics = (input: {
  detected: number;
  predicted: number;
  lost: number;
}) => {
  const frameCount = input.detected + input.predicted + input.lost;
  return {
    status: "collected" as const,
    frame_count: collected(frameCount),
    detected: collected(input.detected),
    predicted: collected(input.predicted),
    lost: collected(input.lost),
    detected_ratio: collected(input.detected / frameCount),
    predicted_ratio: collected(input.predicted / frameCount),
    lost_ratio: collected(input.lost / frameCount),
    longest_lost_streak: collected(4),
    false_positive_island_count: collected(1),
    max_step_px: collected(20),
  };
};

const ACCEPTABLE_GATE: ProductionTrialSignalGateV2 = {
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
    recommended_action: "Inspect the playable evidence.",
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
    sha256: "a".repeat(64),
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
    evaluated_frames: { value: 240, status: "collected" },
    detected_frames: { value: 150, status: "collected" },
    predicted_frames: { value: 50, status: "collected" },
    lost_frames: { value: 40, status: "collected" },
    raw_candidates: { value: 180, status: "collected" },
    class_mapped_candidates: { value: 180, status: "collected" },
    filtered_candidates: { value: 175, status: "collected" },
    selected_candidates: { value: 150, status: "collected" },
    tracklets: { value: 4, status: "collected" },
    rejection_reasons: { below_confidence: 5 },
    reconciliation: { status: "reconciled", reason_codes: [] },
  },
  trajectory: { raw: { frame_count: 240 } },
  diagnostics: {
    raw_track: trackDiagnostics({ detected: 150, predicted: 50, lost: 40 }),
    cleaned_track: trackDiagnostics({
      detected: 160,
      predicted: 50,
      lost: 30,
    }),
    rejection_reasons: {
      status: "collected",
      value: { below_confidence: 5 },
    },
    ai_review_trigger_count: collected(1),
    ai_review_triggers_per_100_frames: collected(1 / 2.4),
    event_candidate_count: collected(2),
    event_candidates_per_100_frames: collected(2 / 2.4),
    follow_cam: {
      status: "collected",
      max_pan_step_px: collected(20),
      max_pan_accel_px: collected(30),
      max_zoom_step_ratio: collected(0.02),
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

function visualConfirmation(evidenceGeneration: string) {
  return {
    confirmed: true as const,
    confirmed_at: CREATED_AT,
    evidence_generation: evidenceGeneration,
    threshold_profile_sha256: ACCEPTABLE_GATE.threshold_profile.sha256,
  };
}

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
  trial_signal_gate_v2: ACCEPTABLE_GATE,
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
  trial_signal_gate_v2: ACCEPTABLE_GATE,
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

describe("bounded production tuning patches", () => {
  const controls: TrialTuningControl[] = [
    {
      path: "detector.confidence_threshold",
      section: "detector",
      kind: "number",
      minimum: 0.01,
      maximum: 0.9,
      step: 0.01,
      runtime_impact: "low",
      description: "Detector confidence",
      description_zh: "检测置信度",
    },
    {
      path: "detector.image_size",
      section: "detector",
      kind: "integer",
      minimum: 640,
      maximum: 2560,
      step: 32,
      runtime_impact: "high",
      description: "Inference size",
      description_zh: "推理尺寸",
    },
    {
      path: "detector.inference_mode",
      section: "detector",
      kind: "select",
      options: ["direct_full_frame", "sahi"],
      runtime_impact: "high",
      description: "Inference mode",
      description_zh: "推理模式",
    },
  ];
  const baseConfig = {
    detector: {
      confidence_threshold: 0.25,
      image_size: 1280,
      inference_mode: "direct_full_frame",
    },
  };

  it("accepts only bounded multi-select labels and the versioned field action", async () => {
    const labelControl: ProductionTrialTuningControl = {
      path: "detector.allowed_labels",
      section: "detector",
      kind: "multi_select",
      options: ["sports ball", "ball"],
      runtime_impact: "low",
      description: "Allowed ball labels",
      description_zh: "允许的足球类别",
    };
    const schema = productionTrialTuningSchema({
      schema_version: "1.0",
      patch_schema_version: "1.0",
      controls: [labelControl],
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
    });
    expect(schema?.actions[0].target_step).toBe("field_setup");

    const result = await buildVersionedProductionTuningPatch({
      base_config: { detector: { allowed_labels: ["sports ball"] } },
      previous_patch: {},
      controls: [labelControl],
      values: { "detector.allowed_labels": ["sports ball", "ball"] },
      version_id: "labels-1",
      created_at: CREATED_AT,
    });
    expect(result.patch).toMatchObject({
      detector: { allowed_labels: ["sports ball", "ball"] },
    });
    await expect(
      buildVersionedProductionTuningPatch({
        base_config: { detector: { allowed_labels: ["sports ball"] } },
        previous_patch: {},
        controls: [labelControl],
        values: { "detector.allowed_labels": ["person"] },
        version_id: "labels-invalid",
        created_at: CREATED_AT,
      }),
    ).rejects.toThrow("Invalid tuning value");
  });

  it("creates immutable, digest-bound versions with minimal diffs and history", async () => {
    const first = await buildVersionedProductionTuningPatch({
      base_config: baseConfig,
      previous_patch: {},
      controls,
      values: {
        "detector.confidence_threshold": 0.15,
        "detector.image_size": 1280,
        "detector.inference_mode": "sahi",
      },
      version_id: "tune-1",
      created_at: CREATED_AT,
    });
    expect(first.patch).toMatchObject({
      detector: { confidence_threshold: 0.15, inference_mode: "sahi" },
      metadata: {
        production_tuning: {
          version_id: "tune-1",
          parent_version_id: null,
        },
      },
    });
    expect((first.patch.detector as Record<string, unknown>).image_size).toBe(
      undefined,
    );
    expect(first.diff.map((item) => item.path)).toEqual([
      "detector.confidence_threshold",
      "detector.inference_mode",
    ]);
    expect(
      productionTuningDraft({
        base_config: baseConfig,
        patch: first.patch,
        controls,
      }),
    ).toEqual(first.version.values);

    const second = await buildVersionedProductionTuningPatch({
      base_config: baseConfig,
      previous_patch: first.patch,
      controls,
      values: {
        ...first.version.values,
        "detector.image_size": 1536,
      },
      version_id: "tune-2",
      created_at: "2026-07-15T12:01:00.000Z",
    });
    expect(second.version.parent_version_id).toBe("tune-1");
    expect(second.diff).toEqual([
      {
        path: "detector.image_size",
        previous_value: 1280,
        next_value: 1536,
      },
    ]);
    expect(productionTuningHistory(second.patch)).toHaveLength(1);
    expect(productionTuningHistory(second.patch)[0]).toMatchObject({
      version_id: "tune-1",
      values: first.version.values,
    });
    expect(productionTuningVersion(second.patch)).toEqual(second.version);
    expect(first.version.history).toEqual([]);
  });

  it("submits the current draft while preserving the two prior tuning versions", async () => {
    const v1 = await buildVersionedProductionTuningPatch({
      base_config: baseConfig,
      previous_patch: {},
      controls,
      values: {
        "detector.confidence_threshold": 0.2,
        "detector.image_size": 1280,
        "detector.inference_mode": "direct_full_frame",
      },
      version_id: "tune-v1",
      created_at: CREATED_AT,
    });
    const v2 = await buildVersionedProductionTuningPatch({
      base_config: baseConfig,
      previous_patch: v1.patch,
      controls,
      values: {
        ...v1.version.values,
        "detector.confidence_threshold": 0.15,
      },
      version_id: "tune-v2",
      created_at: "2026-07-15T12:01:00.000Z",
    });
    const v3 = await buildVersionedProductionTuningPatch({
      base_config: baseConfig,
      previous_patch: v2.patch,
      controls,
      values: {
        ...v2.version.values,
        "detector.inference_mode": "sahi",
      },
      version_id: "tune-v3",
      created_at: "2026-07-15T12:02:00.000Z",
    });

    const rerun = await buildProductionTrialSubmission({
      workflow_id: "workflow-a",
      source: SOURCE,
      calibration: CALIBRATION,
      settings: {
        ...SETTINGS,
        tuning_patch: v3.patch,
      },
      parent_run_id: "trial-parent",
      submission_id: "submission-v3",
      output_id: "output-v3",
      generation: 2,
      created_at: "2026-07-15T12:03:00.000Z",
    });
    const submittedVersion = productionTuningVersion(
      rerun.pending.request.config_patch,
    );

    expect(submittedVersion?.version_id).toBe("tune-v3");
    expect(submittedVersion?.parent_version_id).toBe("tune-v2");
    expect(
      submittedVersion?.history.map(({ version_id }) => version_id),
    ).toEqual(["tune-v1", "tune-v2"]);
    expect(v1.version.history).toEqual([]);
    expect(v2.version.history.map(({ version_id }) => version_id)).toEqual([
      "tune-v1",
    ]);
  });

  it.each([
    ["arbitrary path", { "detector.model_path": "foreign.pt" }],
    [
      "out of range",
      {
        "detector.confidence_threshold": 0,
        "detector.image_size": 1280,
        "detector.inference_mode": "sahi",
      },
    ],
    [
      "wrong step",
      {
        "detector.confidence_threshold": 0.155,
        "detector.image_size": 1280,
        "detector.inference_mode": "sahi",
      },
    ],
    [
      "invalid option",
      {
        "detector.confidence_threshold": 0.15,
        "detector.image_size": 1280,
        "detector.inference_mode": "remote",
      },
    ],
  ])("rejects %s", async (_label, values) => {
    await expect(
      buildVersionedProductionTuningPatch({
        base_config: baseConfig,
        previous_patch: {},
        controls,
        values,
        version_id: "invalid",
        created_at: CREATED_AT,
      }),
    ).rejects.toThrow();
  });

  it("rejects duplicate or prototype-polluting schema paths", async () => {
    for (const candidateControls of [
      [...controls, controls[0]],
      [
        {
          ...controls[0],
          path: "detector.__proto__.polluted",
        },
      ],
    ]) {
      await expect(
        buildVersionedProductionTuningPatch({
          base_config: baseConfig,
          previous_patch: {},
          controls: candidateControls,
          values: Object.fromEntries(
            candidateControls.map((control) => [
              control.path,
              control.kind === "number" ? 0.25 : 1280,
            ]),
          ),
          version_id: "invalid-schema",
          created_at: CREATED_AT,
        }),
      ).rejects.toThrow(/safe paths/);
    }
    expect(({} as Record<string, unknown>).polluted).toBeUndefined();
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
        detector: { confidence_threshold: 0.2 },
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

  it("keeps a hash-bound failed run as parent and restarts a digest-less legacy run explicitly", async () => {
    const { pending } = await submission(null, 1);
    const completed = appendProductionTrialAttempt(
      setPendingProductionTrial(createProductionTrialState(SETTINGS), pending),
      {
        run: { run_id: "trial-legacy-or-modern", status: "completed" },
        pending,
        observed_at: CREATED_AT,
      },
    );
    const failed = observeProductionTrialRun(completed, {
      run_id: "trial-legacy-or-modern",
      status: "failed",
      observed_at: "later",
    });

    const modern = productionTrialSubmissionLineage(failed, {
      run_id: "trial-legacy-or-modern",
      status: "failed",
      config_sha256: "a".repeat(64),
    });
    expect(modern).toMatchObject({
      state: failed,
      parent_run_id: "trial-legacy-or-modern",
      generation: 2,
      legacy_restart_run_id: null,
      base_config_locked: true,
    });

    const legacy = productionTrialSubmissionLineage(completed, {
      run_id: "trial-legacy-or-modern",
      status: "completed",
      config_sha256: null,
    });
    expect(legacy).not.toBeNull();
    expect(legacy).toMatchObject({
      parent_run_id: null,
      generation: 1,
      legacy_restart_run_id: "trial-legacy-or-modern",
      base_config_locked: true,
    });
    expect(legacy?.state.attempts).toEqual([]);
    expect(legacy?.state.settings).toEqual(completed.settings);

    const restart = await buildProductionTrialSubmission({
      workflow_id: "workflow-a",
      source: SOURCE,
      calibration: CALIBRATION,
      settings: legacy!.state.settings,
      parent_run_id: legacy!.parent_run_id,
      legacy_restart_run_id: legacy!.legacy_restart_run_id,
      submission_id: "restart-submission",
      output_id: "restart-output",
      generation: legacy!.generation,
      created_at: CREATED_AT,
    });
    expect(restart.pending.request.parent_run_id).toBeNull();
    expect(JSON.parse(String(restart.pending.request.notes))).toMatchObject({
      generation: 1,
      legacy_restart_run_id: "trial-legacy-or-modern",
    });
  });

  it("rejects generation drift for modern and legacy roots", async () => {
    await expect(
      buildProductionTrialSubmission({
        workflow_id: "workflow-a",
        source: SOURCE,
        calibration: CALIBRATION,
        settings: SETTINGS,
        parent_run_id: null,
        submission_id: "bad-root-submission",
        output_id: "bad-root-output",
        generation: 2,
        created_at: CREATED_AT,
      }),
    ).rejects.toThrow(/generation 1/i);
  });

  it.each(["queued", "running", "completed"] as const)(
    "reconciles one materialized %s run only for the current generation",
    async (status) => {
      const { pending } = await submission(null, 1);
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
          expected_generation: 2,
          runs: [run],
          observed_at: CREATED_AT,
        }),
      ).toBe(state);
      const recovered = reconcilePendingProductionTrial(state, {
        workflow_id: "workflow-a",
        expected_generation: 1,
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
          expected_generation: 1,
          runs: [run, { ...run }],
          observed_at: CREATED_AT,
        }),
      ).toBe(state);
    },
  );

  it("rejects base configs and every foreign recovery identity", async () => {
    const { pending } = await submission(null, 1);
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
          expected_generation: 1,
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
  it("accepts only a complete v2 signal gate and rejects fail-open variants", () => {
    expect(isTrialSignalGateV2(ACCEPTABLE_GATE)).toBe(true);
    expect(productionTrialSignalGateAcceptable(ACCEPTABLE_GATE)).toBe(true);
    for (const gate of [
      { ...ACCEPTABLE_GATE, status: "retune_required" },
      { ...ACCEPTABLE_GATE, coverage_complete: false },
      { ...ACCEPTABLE_GATE, evidence_available: false },
      { ...ACCEPTABLE_GATE, acceptance_metrics_complete: false },
      { ...ACCEPTABLE_GATE, acceptance_contract_complete: false },
      { ...ACCEPTABLE_GATE, quality_acceptable: false },
      {
        ...ACCEPTABLE_GATE,
        stage_counts: {
          ...ACCEPTABLE_GATE.stage_counts!,
          raw_candidates: { value: 0, status: "collected" as const },
        },
      },
      {
        ...ACCEPTABLE_GATE,
        stage_counts: {
          ...ACCEPTABLE_GATE.stage_counts!,
          detected_frames: { value: null, status: "not_collected" as const },
        },
      },
      {
        ...ACCEPTABLE_GATE,
        stage_counts: {
          ...ACCEPTABLE_GATE.stage_counts!,
          lost_frames: { value: 41, status: "collected" as const },
        },
      },
      {
        ...ACCEPTABLE_GATE,
        stage_counts: {
          ...ACCEPTABLE_GATE.stage_counts!,
          selected_candidates: { value: 149, status: "collected" as const },
        },
      },
      {
        ...ACCEPTABLE_GATE,
        stage_counts: {
          ...ACCEPTABLE_GATE.stage_counts!,
          filtered_candidates: { value: 181, status: "collected" as const },
        },
      },
      {
        ...ACCEPTABLE_GATE,
        evidence: {
          ...ACCEPTABLE_GATE.evidence,
          identity_binding: "not_collected",
        },
      },
      {
        ...ACCEPTABLE_GATE,
        evidence: {
          ...ACCEPTABLE_GATE.evidence,
          scale_strata: "not_collected",
        },
      },
    ]) {
      expect(productionTrialSignalGateAcceptable(gate)).toBe(false);
    }
    expect(
      isTrialSignalGateV2({
        ...ACCEPTABLE_GATE,
        threshold_profile: {
          ...ACCEPTABLE_GATE.threshold_profile,
          sha256: "not-a-digest",
        },
      }),
    ).toBe(false);
    const withoutDebugStatusCount = structuredClone(ACCEPTABLE_GATE) as Record<
      string,
      unknown
    >;
    delete (withoutDebugStatusCount.stage_counts as Record<string, unknown>)
      .detected_frames;
    expect(isTrialSignalGateV2(withoutDebugStatusCount)).toBe(false);
    expect(
      isTrialSignalGateV2({
        ...ACCEPTABLE_GATE,
        threshold_profile: {
          ...ACCEPTABLE_GATE.threshold_profile,
          algorithm_version: "",
        },
      }),
    ).toBe(false);
    expect(
      isTrialSignalGateV2({
        ...ACCEPTABLE_GATE,
        threshold_profile: {
          ...ACCEPTABLE_GATE.threshold_profile,
          matching_rules: {},
        },
      }),
    ).toBe(false);
    expect(
      isTrialSignalGateV2({
        ...ACCEPTABLE_GATE,
        stage_counts: {
          ...ACCEPTABLE_GATE.stage_counts,
          schema_version: "1.0",
        },
      }),
    ).toBe(false);
    expect(
      isTrialSignalGateV2({
        ...ACCEPTABLE_GATE,
        stage_counts: {
          ...ACCEPTABLE_GATE.stage_counts,
          class_mapped_candidates: { value: 180, status: "inferred" },
        },
      }),
    ).toBe(false);
    const withoutDiagnostics: Record<string, unknown> = {
      ...ACCEPTABLE_GATE,
    };
    delete withoutDiagnostics.diagnostics;
    expect(isTrialSignalGateV2(withoutDiagnostics)).toBe(false);
  });

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
      trial_signal_gate_v2: ACCEPTABLE_GATE,
    });
  });

  it("keeps readable artifacts visible but clears a missing or mismatched signal gate", () => {
    const run = completedRun();
    const common = {
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
    };
    const mismatched = assessProductionTrialEvidence({
      ...common,
      trial_signal_gate_v2: {
        ...ACCEPTABLE_GATE,
        status: "retune_required",
        quality_acceptable: false,
      },
    });
    expect(mismatched.ready).toBe(true);
    if (!mismatched.ready) throw new Error("expected readable evidence");
    expect(mismatched.quality.trial_signal_gate_v2).toBeNull();

    const legacy = assessProductionTrialEvidence({
      ...common,
      run: completedRun({ stats: { raw: STATS.raw, cleaned: STATS.cleaned } }),
      metrics: {
        schema_version: "1.0",
        generated_at: CREATED_AT,
        tracks: { raw: STATS.raw, cleaned: STATS.cleaned },
      },
    });
    expect(legacy.ready).toBe(true);
    if (!legacy.ready) throw new Error("expected readable legacy evidence");
    expect(legacy.quality.trial_signal_gate_v2).toBeNull();
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
        trial_signal_gate_v2: ACCEPTABLE_GATE,
      },
      operator_visual_confirmation: visualConfirmation(generation),
    };
    expect(() =>
      acceptProductionTrial(state, {
        run: { run_id: "trial-1", status: "completed" },
        current_intent_sha256: pending.intent_sha256,
        readiness: {
          ...readiness,
          operator_visual_confirmation: undefined,
        },
        accepted_at: CREATED_AT,
      }),
    ).toThrow("not eligible");
    expect(() =>
      acceptProductionTrial(state, {
        run: { run_id: "trial-1", status: "completed" },
        current_intent_sha256: pending.intent_sha256,
        readiness: {
          ...readiness,
          quality: {
            ...readiness.quality,
            trial_signal_gate_v2: {
              ...ACCEPTABLE_GATE,
              status: "retune_required",
              quality_acceptable: false,
            },
          },
        },
        accepted_at: CREATED_AT,
      }),
    ).toThrow("not eligible");
    expect(() =>
      acceptProductionTrial(state, {
        run: { run_id: "trial-1", status: "completed" },
        current_intent_sha256: pending.intent_sha256,
        readiness: {
          ...readiness,
          operator_visual_confirmation: {
            ...readiness.operator_visual_confirmation!,
            evidence_generation: "0".repeat(64),
          },
        },
        accepted_at: CREATED_AT,
      }),
    ).toThrow("not eligible");
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
        trial_signal_gate_v2: ACCEPTABLE_GATE,
      },
      operator_visual_confirmation: visualConfirmation("f".repeat(64)),
    };
    const accepted = acceptProductionTrial(observed, {
      run: { run_id: "trial-1", status: "completed" },
      current_intent_sha256: pending.intent_sha256,
      readiness,
      accepted_at: CREATED_AT,
    });
    expect(isProductionTrialState(accepted)).toBe(true);
    const legacyAccepted = structuredClone(accepted);
    delete legacyAccepted.accepted!.readiness.operator_visual_confirmation;
    delete (
      legacyAccepted.accepted!.readiness.quality as unknown as Record<
        string,
        unknown
      >
    ).trial_signal_gate_v2;
    expect(isProductionTrialState(legacyAccepted)).toBe(false);

    const missingVisual = structuredClone(accepted);
    delete missingVisual.accepted!.readiness.operator_visual_confirmation;
    expect(isProductionTrialState(missingVisual)).toBe(false);

    const retuneAccepted = structuredClone(accepted);
    retuneAccepted.accepted!.readiness.quality.trial_signal_gate_v2 = {
      ...ACCEPTABLE_GATE,
      status: "retune_required",
      quality_acceptable: false,
    };
    expect(isProductionTrialState(retuneAccepted)).toBe(false);

    const retry = await submission("trial-1", 2);
    const withLatestRetry = appendProductionTrialAttempt(
      setPendingProductionTrial(
        invalidateProductionTrialAcceptance(accepted),
        retry.pending,
      ),
      {
        run: { run_id: "trial-2", status: "completed" },
        pending: retry.pending,
        observed_at: CREATED_AT,
      },
    );
    const olderAttemptAccepted = {
      ...withLatestRetry,
      accepted: accepted.accepted,
    };
    expect(isProductionTrialState(olderAttemptAccepted)).toBe(false);
    expect(() =>
      acceptProductionTrial(withLatestRetry, {
        run: { run_id: "trial-1", status: "completed" },
        current_intent_sha256: pending.intent_sha256,
        readiness,
        accepted_at: CREATED_AT,
      }),
    ).toThrow("not eligible");
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
      (value: ProductionTrialState) => {
        value.accepted!.readiness.operator_visual_confirmation!.evidence_generation =
          "0".repeat(64);
      },
      (value: ProductionTrialState) => {
        value.accepted!.readiness.operator_visual_confirmation!.threshold_profile_sha256 =
          "0".repeat(64);
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
