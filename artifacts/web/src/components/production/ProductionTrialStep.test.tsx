import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import userEvent from "@testing-library/user-event";
import { useRef, useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type {
  BallAuditReport,
  RunRecord,
  TrialDiagnosisResponse,
} from "@workspace/api-client-react";

import { LanguageProvider } from "@/contexts/LanguageContext";
import { translations } from "@/lib/i18n";
import type { ProductionCalibrationDraft } from "@/lib/productionCalibration";
import {
  buildProductionConfigConfirmation,
  expectedProductionConfigName,
  finalizeProductionConfigConfirmation,
  type ProductionConfigEvidence,
  type ProductionPendingConfigConfirmation,
} from "@/lib/productionConfigFreeze";
import {
  assessProductionTrialEvidence,
  appendProductionTrialAttempt,
  buildProductionTrialSubmission,
  createProductionTrialState,
  materializedProductionTrialConfigName,
  productionTrialEvidenceGeneration,
  selectProductionTrialVideo,
  setPendingProductionTrial,
  type ProductionTrialSignalGateV2,
  type ProductionTrialState,
  type ProductionTrialTuningSchema,
  type TrialDiagnosticObservation,
} from "@/lib/productionTrial";
import type { SourceSignature } from "@/lib/productionWorkflow";

const createMutate = vi.fn();
const cancelMutate = vi.fn();
const deriveMutate = vi.fn();
const healthRefetch = vi.fn();
const runsRefetch = vi.fn();
const runRefetch = vi.fn();
const artifactsRefetch = vi.fn();
const artifactRefetch = vi.fn();
const diagnosisRefetch = vi.fn();
const artifactQueryRequests: Array<{
  name: string;
  request: Record<string, unknown> | undefined;
}> = [];
const auditRefetch = vi.fn();
let runsData: RunRecord[] = [];
let runData: RunRecord | undefined;
let runQueryPending = false;
let runQueryError: unknown = null;
let artifactsData: Array<Record<string, unknown>> = [];
let artifactBodies: Record<string, unknown> = {};
let auditData: BallAuditReport | undefined;
let configData: Record<string, unknown> | undefined;
let configError: unknown = null;
let configPending = false;
type TrialDiagnosisFixture = Omit<
  TrialDiagnosisResponse,
  "trial_signal_gate_v2"
> & { trial_signal_gate_v2: ProductionTrialSignalGateV2 };

let diagnosisData: TrialDiagnosisFixture | undefined;
let tuningSchemaData: ProductionTrialTuningSchema;
let tuningBaseConfigData: Record<string, unknown> | undefined;

const collected = (value: number): TrialDiagnosticObservation => ({
  status: "collected",
  value,
});

const notCollected = (): TrialDiagnosticObservation => ({
  status: "not_collected",
  value: null,
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
    summary: "Ball signal is usable.",
    recommended_action: "Review the videos and confirm.",
  },
  threshold_profile: {
    profile_id: "tiny-ball-trial",
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
    evaluated_frames: { value: 300, status: "collected" },
    detected_frames: { value: 200, status: "collected" },
    predicted_frames: { value: 50, status: "collected" },
    lost_frames: { value: 50, status: "collected" },
    raw_candidates: { value: 240, status: "collected" },
    class_mapped_candidates: { value: 240, status: "collected" },
    filtered_candidates: { value: 220, status: "collected" },
    selected_candidates: { value: 200, status: "collected" },
    tracklets: { value: 2, status: "collected" },
    rejection_reasons: { too_small: 20 },
    reconciliation: { status: "reconciled", reason_codes: [] },
  },
  trajectory: { raw_lost_ratio: 1 / 6 },
  diagnostics: {
    raw_track: trackDiagnostics({ detected: 200, predicted: 50, lost: 50 }),
    cleaned_track: trackDiagnostics({
      detected: 210,
      predicted: 50,
      lost: 40,
    }),
    rejection_reasons: {
      status: "collected",
      value: { too_small: 20 },
    },
    ai_review_trigger_count: collected(1),
    ai_review_triggers_per_100_frames: collected(1 / 3),
    event_candidate_count: collected(2),
    event_candidates_per_100_frames: collected(2 / 3),
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

const ALL_LOST_GATE: ProductionTrialSignalGateV2 = {
  ...ACCEPTABLE_GATE,
  status: "retune_required",
  trajectory_acceptable: false,
  signal_acceptable: false,
  acceptance_contract_complete: false,
  quality_acceptable: false,
  reason_codes: [
    "zero_candidate",
    "zero_tracklet",
    "all_lost",
    "acceptance_contract_not_collected",
  ],
  failure_classification: {
    code: "no_raw_candidates",
    severity: "blocking",
    summary: "No raw ball candidates were found.",
    recommended_action: "Lower the detector threshold and rerun.",
  },
  stage_counts: {
    ...ACCEPTABLE_GATE.stage_counts!,
    detected_frames: { value: 0, status: "collected" },
    predicted_frames: { value: 0, status: "collected" },
    lost_frames: { value: 300, status: "collected" },
    raw_candidates: { value: 0, status: "collected" },
    class_mapped_candidates: { value: 0, status: "collected" },
    filtered_candidates: { value: 0, status: "collected" },
    selected_candidates: { value: 0, status: "collected" },
    tracklets: { value: 0, status: "collected" },
  },
  diagnostics: {
    ...ACCEPTABLE_GATE.diagnostics,
    raw_track: trackDiagnostics({ detected: 0, predicted: 0, lost: 300 }),
    cleaned_track: trackDiagnostics({ detected: 0, predicted: 0, lost: 300 }),
  },
};

const queryBase = {
  dataUpdatedAt: 1,
  isPending: false,
  isLoading: false,
  isFetching: false,
  isError: false,
  error: null,
  data: undefined,
  refetch: vi.fn(),
};

vi.mock("@workspace/api-client-react", () => ({
  getGetArtifactQueryKey: (runId: string, name: string) => [runId, name],
  getGetArtifactUrl: (runId: string, name: string) =>
    `/api/runs/${runId}/artifacts/${name}`,
  getGetBallAuditReportQueryKey: (runId: string) => [runId, "audit"],
  getGetConfigQueryKey: (name: string) => ["config", name],
  getGetConfigQueryOptions: (name: string) => ({
    queryKey: ["config", name],
    queryFn: async () => {
      if (configError) throw configError;
      return configData;
    },
    retry: false,
    staleTime: 0,
  }),
  getGetHealthQueryKey: () => ["health"],
  getGetRunQueryKey: (runId: string) => ["run", runId],
  getGetTrialDiagnosisQueryKey: (runId: string) => [runId, "diagnosis"],
  getGetProductionTrialTuningSchemaQueryKey: () => ["trial-tuning-schema"],
  getListArtifactsQueryKey: (runId: string) => ["artifacts", runId],
  getListRunsQueryKey: () => ["runs"],
  useListConfigs: () => ({
    ...queryBase,
    data: [
      {
        name: "default.yaml",
        path: "configs/default.yaml",
        postprocess_enabled: true,
        follow_cam_enabled: true,
        exists: { yaml: true },
      },
      {
        name: "alternate.yaml",
        path: "configs/alternate.yaml",
        postprocess_enabled: true,
        follow_cam_enabled: true,
        exists: { yaml: true },
      },
    ],
  }),
  useGetHealth: () => ({
    ...queryBase,
    data: {
      status: "ok",
      active_run_id: null,
      config_count: 1,
      run_count: runsData.length,
    },
    refetch: healthRefetch,
  }),
  useListRuns: () => ({ ...queryBase, data: runsData, refetch: runsRefetch }),
  useGetRun: () => ({
    ...queryBase,
    data: runQueryPending || runQueryError ? undefined : runData,
    isPending: runQueryPending,
    isError: runQueryError !== null,
    error: runQueryError,
    refetch: runRefetch,
  }),
  useGetTrialDiagnosis: () => ({
    ...queryBase,
    data: diagnosisData,
    refetch: diagnosisRefetch,
  }),
  useGetProductionTrialTuningSchema: () => ({
    ...queryBase,
    data: tuningSchemaData,
  }),
  useListArtifacts: () => ({
    ...queryBase,
    data: artifactsData,
    refetch: artifactsRefetch,
  }),
  useGetArtifact: (
    _runId: string,
    name: string,
    _params: unknown,
    options?: { request?: Record<string, unknown> },
  ) => {
    artifactQueryRequests.push({ name, request: options?.request });
    return {
      ...queryBase,
      data: artifactBodies[name],
      refetch: () => artifactRefetch(name, options?.request),
    };
  },
  useGetBallAuditReport: () => ({
    ...queryBase,
    data: auditData,
    refetch: auditRefetch,
  }),
  useGetConfig: (name: string) => {
    const tuningBase = name === "default.yaml";
    return {
      ...queryBase,
      data: tuningBase ? tuningBaseConfigData : configData,
      isPending: tuningBase ? false : configPending,
      isError: tuningBase ? false : configError !== null,
      error: tuningBase ? null : configError,
    };
  },
  useCreateRun: () => ({ isPending: false, mutateAsync: createMutate }),
  useCancelRun: () => ({ isPending: false, mutateAsync: cancelMutate }),
  useDeriveConfig: () => ({ isPending: false, mutateAsync: deriveMutate }),
}));

import { ProductionTrialStep } from "./ProductionTrialStep";

const SOURCE: SourceSignature = {
  path: "data/match-a.mp4",
  size_bytes: 1_024,
  modified_at: "2026-07-15T00:00:00Z",
};
const CALIBRATION: ProductionCalibrationDraft = {
  source_resolution: { width: 1_920, height: 1_080 },
  suggestion: null,
  approved_polygon: [
    [100, 100],
    [1_800, 100],
    [1_800, 900],
  ],
  exclusions: [],
  polygon_digest: "c".repeat(64),
  confirmed_frames: [10, 20, 30].map((frame_index, sample_index) => ({
    input_video: SOURCE.path,
    frame_index,
    frame_time_seconds: frame_index / 25,
    sample_index,
    source_resolution: { width: 1_920, height: 1_080 },
    polygon_digest: "c".repeat(64),
  })),
};
const NOW = "2026-07-15T12:00:00.000Z";

function run(
  status: RunRecord["status"],
  changes: Partial<RunRecord> = {},
): RunRecord {
  return {
    run_id: "trial-1",
    source: "api",
    status,
    created_at: NOW,
    config_name: "default.yaml",
    input_video: SOURCE.path,
    parent_run_id: null,
    output_dir: "outputs/trial-1",
    notes: null,
    ...changes,
  };
}

function recoveredRun(
  pending: NonNullable<ProductionTrialState["pending_submission"]>,
  status: RunRecord["status"] = "queued",
): RunRecord {
  const runId = `production_trial_${pending.output_id}`;
  return run(status, {
    run_id: runId,
    source: "api",
    notes: pending.request.notes,
    config_name: materializedProductionTrialConfigName(
      pending.request.config_name ?? "",
      runId,
    ),
    input_video: pending.request.input_video,
    parent_run_id: pending.request.parent_run_id,
    output_dir: `outputs/${runId}`,
    modules_enabled: {
      postprocess: Boolean(pending.request.enable_postprocess),
      follow_cam: Boolean(pending.request.enable_follow_cam),
    },
  });
}

async function trialWithAttempt(status: RunRecord["status"] = "running") {
  const settings = {
    base_config_name: "default.yaml",
    start_frame: 0,
    max_frames: 300,
    enable_postprocess: true,
    enable_follow_cam: true,
    tuning_patch: {},
  };
  const submission = await buildProductionTrialSubmission({
    workflow_id: "workflow-a",
    source: SOURCE,
    calibration: CALIBRATION,
    settings,
    parent_run_id: null,
    submission_id: "submission-a",
    output_id: "output-a",
    generation: 1,
    created_at: NOW,
  });
  const state = appendProductionTrialAttempt(
    setPendingProductionTrial(
      createProductionTrialState(settings),
      submission.pending,
    ),
    {
      run: { run_id: "trial-1", status },
      pending: submission.pending,
      observed_at: NOW,
    },
  );
  return { state, submission };
}

async function acceptedTrial(): Promise<ProductionTrialState> {
  const { state, submission } = await trialWithAttempt("completed");
  installReadableEvidence();
  const attempt = state.attempts[0];
  const evidence = assessProductionTrialEvidence({
    run: runData!,
    artifacts: artifactsData as never,
    manifest: artifactBodies["run_manifest.json"],
    metrics: artifactBodies["metrics_report.json"],
    audit: auditData ?? null,
    raw_csv: artifactBodies["ball_track.csv"],
    cleaned_csv: artifactBodies["ball_track.cleaned.csv"],
    readable_artifact_names: [
      "run_manifest.json",
      "metrics_report.json",
      "ball_track.csv",
      "ball_track.cleaned.csv",
      "ball_audit.json",
    ],
    enable_postprocess: true,
    enable_follow_cam: true,
    video_loaded: true,
  });
  if (!evidence.ready || !evidence.video)
    throw new Error("fixture evidence invalid");
  const evidenceGeneration = await productionTrialEvidenceGeneration({
    run_id: "trial-1",
    intent_sha256: attempt.intent_sha256,
    request_sha256: attempt.request_sha256,
    artifacts: artifactsData as never,
    stats: runData?.stats ?? null,
    manifest: artifactBodies["run_manifest.json"],
    metrics: artifactBodies["metrics_report.json"],
    audit: auditData!,
    raw_csv: String(artifactBodies["ball_track.csv"]),
    cleaned_csv: String(artifactBodies["ball_track.cleaned.csv"]),
    selected_video: selectProductionTrialVideo(artifactsData as never, true)!,
    video_metadata: { duration: null, width: 0, height: 0 },
  });
  return {
    ...state,
    attempts: state.attempts.map((attempt) => ({
      ...attempt,
      last_observed: {
        ...attempt.last_observed,
        evidence_generation: evidenceGeneration,
      },
    })),
    accepted: {
      run_id: "trial-1",
      intent_sha256: submission.pending.intent_sha256,
      request_sha256: submission.pending.request_sha256,
      accepted_at: NOW,
      readiness: {
        run_id: "trial-1",
        request_sha256: submission.pending.request_sha256,
        evidence_generation: evidenceGeneration,
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
          audit_tracklet_count: 2,
          audit_suspicious_tracklet_count: 1,
          audit_review_event_count: 1,
          audit_lost_gap_count: 1,
          quality_gate_status: null,
          trial_signal_gate_v2: ACCEPTABLE_GATE,
        },
        operator_visual_confirmation: {
          confirmed: true,
          confirmed_at: NOW,
          evidence_generation: evidenceGeneration,
          threshold_profile_sha256: ACCEPTABLE_GATE.threshold_profile.sha256,
        },
      },
    },
  };
}

function renderStep(
  changes: Partial<React.ComponentProps<typeof ProductionTrialStep>> = {},
) {
  const onTrialChange = vi.fn(
    (_trial: ProductionTrialState, _expected: ProductionTrialState | null) =>
      true,
  );
  const onPendingConfigChange = vi.fn(
    (_pending: ProductionPendingConfigConfirmation | null) => true,
  );
  const onConfirmedConfigChange = vi.fn(
    (_confirmed: ProductionConfigEvidence) => true,
  );
  const onReturnToFieldSetup = vi.fn();
  const onUsabilityChange = vi.fn((_usable: boolean) => undefined);
  const props: React.ComponentProps<typeof ProductionTrialStep> = {
    workflowId: "workflow-a",
    source: SOURCE,
    calibration: CALIBRATION,
    trial: null,
    pendingConfig: null,
    confirmedConfig: null,
    onTrialChange,
    onPendingConfigChange,
    onConfirmedConfigChange,
    onReturnToFieldSetup,
    onUsabilityChange,
    ...changes,
  };
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const renderTree = (nextProps: typeof props) => (
    <QueryClientProvider client={queryClient}>
      <LanguageProvider>
        <ProductionTrialStep {...nextProps} />
      </LanguageProvider>
    </QueryClientProvider>
  );
  const view = render(renderTree(props));
  return {
    user: userEvent.setup(),
    ...view,
    rerenderStep(nextChanges: Partial<typeof props>) {
      Object.assign(props, nextChanges);
      view.rerender(renderTree(props));
    },
    onTrialChange,
    onPendingConfigChange,
    onConfirmedConfigChange,
    onReturnToFieldSetup,
    onUsabilityChange,
  };
}

beforeEach(() => {
  localStorage.removeItem("app-language");
  createMutate.mockReset();
  cancelMutate.mockReset();
  deriveMutate.mockReset();
  healthRefetch.mockReset().mockResolvedValue({
    data: { status: "ok", active_run_id: null, config_count: 1, run_count: 0 },
  });
  runsRefetch.mockReset().mockResolvedValue({ data: [] });
  runRefetch.mockReset().mockImplementation(async () => ({
    data: runData,
    isError: false,
    dataUpdatedAt: 2,
  }));
  artifactsRefetch.mockReset().mockImplementation(async () => ({
    data: artifactsData,
    isError: false,
    dataUpdatedAt: 2,
  }));
  artifactQueryRequests.length = 0;
  artifactRefetch.mockReset().mockImplementation(async (name: string) => ({
    data: artifactBodies[name],
    isError: false,
    dataUpdatedAt: 2,
  }));
  auditRefetch.mockReset().mockImplementation(async () => ({
    data: auditData,
    isError: false,
    dataUpdatedAt: 2,
  }));
  diagnosisRefetch.mockReset().mockImplementation(async () => ({
    data: diagnosisData,
    isError: false,
    dataUpdatedAt: 2,
  }));
  runsData = [];
  runData = undefined;
  runQueryPending = false;
  runQueryError = null;
  artifactsData = [];
  artifactBodies = {};
  auditData = undefined;
  configData = undefined;
  configError = null;
  configPending = false;
  diagnosisData = undefined;
  tuningSchemaData = {
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
        description: "Minimum detector confidence.",
        description_zh: "检测器最低置信度。",
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
  tuningBaseConfigData = {
    name: "default.yaml",
    path: "configs/default.yaml",
    text: "detector:\n  allowed_labels: [sports ball]\n  confidence_threshold: 0.25\n",
    raw: {
      detector: {
        allowed_labels: ["sports ball"],
        confidence_threshold: 0.25,
      },
    },
    resolved: {
      detector: {
        allowed_labels: ["sports ball"],
        confidence_threshold: 0.25,
      },
    },
    summary: {
      name: "default.yaml",
      path: "configs/default.yaml",
      input_video: SOURCE.path,
      postprocess_enabled: true,
      follow_cam_enabled: true,
      exists: { yaml: true },
    },
  };
  vi.spyOn(globalThis.crypto, "randomUUID")
    .mockReturnValueOnce("11111111-1111-4111-8111-111111111111")
    .mockReturnValueOnce("22222222-2222-4222-8222-222222222222")
    .mockReturnValue("33333333-3333-4333-8333-333333333333");
});

describe("ProductionTrialStep mutation safety", () => {
  it("locks every request and tuning control while a submission is pending", async () => {
    const settings = {
      base_config_name: "default.yaml",
      start_frame: 0,
      max_frames: 300,
      enable_postprocess: true,
      enable_follow_cam: true,
      tuning_patch: {},
    };
    const submission = await buildProductionTrialSubmission({
      workflow_id: "workflow-a",
      source: SOURCE,
      calibration: CALIBRATION,
      settings,
      parent_run_id: null,
      submission_id: "submission-pending-lock",
      output_id: "output-pending-lock",
      generation: 1,
      created_at: NOW,
    });
    const pending = setPendingProductionTrial(
      createProductionTrialState(settings),
      submission.pending,
    );
    renderStep({ trial: pending });

    expect(screen.getByLabelText("Base configuration")).toBeDisabled();
    expect(screen.getByLabelText("Start frame")).toBeDisabled();
    expect(screen.getByLabelText("Frame count")).toBeDisabled();
    expect(
      screen.getByLabelText("Clean the ball track after the trial"),
    ).toBeDisabled();
    expect(
      screen.getByLabelText("Generate follow-camera evidence"),
    ).toBeDisabled();
    expect(
      await screen.findByLabelText("detector.confidence_threshold"),
    ).toBeDisabled();
    expect(
      screen.getByRole("button", { name: "Save adjustments" }),
    ).toBeDisabled();
    expect(
      screen.getByRole("button", { name: "Save and rerun" }),
    ).toBeDisabled();
    expect(
      screen.getByRole("button", { name: "Return to field setup" }),
    ).toBeDisabled();
    expect(
      screen.getByRole("button", { name: "Retry as a new trial" }),
    ).toBeEnabled();
  });

  it("discards a deferred tuning mutation when a pending submission appears", async () => {
    const settings = {
      base_config_name: "default.yaml",
      start_frame: 0,
      max_frames: 300,
      enable_postprocess: true,
      enable_follow_cam: true,
      tuning_patch: {},
    };
    const initial = createProductionTrialState(settings);
    const submission = await buildProductionTrialSubmission({
      workflow_id: "workflow-a",
      source: SOURCE,
      calibration: CALIBRATION,
      settings,
      parent_run_id: null,
      submission_id: "submission-deferred-lock",
      output_id: "output-deferred-lock",
      generation: 1,
      created_at: NOW,
    });
    const pending = setPendingProductionTrial(initial, submission.pending);
    const originalDigest = globalThis.crypto.subtle.digest.bind(
      globalThis.crypto.subtle,
    );
    let release!: () => void;
    const pause = new Promise<void>((resolve) => {
      release = resolve;
    });
    const digest = vi
      .spyOn(globalThis.crypto.subtle, "digest")
      .mockImplementationOnce(async (...args) => {
        await pause;
        return originalDigest(...args);
      });
    const view = renderStep({ trial: initial });
    const threshold = await screen.findByLabelText(
      "detector.confidence_threshold",
    );
    await view.user.clear(threshold);
    await view.user.type(threshold, "0.15");
    await view.user.click(
      screen.getByRole("button", { name: "Save adjustments" }),
    );
    await waitFor(() => expect(digest).toHaveBeenCalledOnce());

    view.rerenderStep({ trial: pending });
    release();
    await act(async () => undefined);

    expect(view.onTrialChange).not.toHaveBeenCalled();
    digest.mockRestore();
  });

  it("locks restored base selection before lineage lookup and corrects it from the authoritative latest attempt", async () => {
    const { state } = await trialWithAttempt("completed");
    const restored = {
      ...state,
      settings: { ...state.settings, base_config_name: "alternate.yaml" },
    };
    runQueryPending = true;
    const view = renderStep({ trial: restored });
    const select = screen.getByLabelText("Base configuration");
    expect(select).toBeDisabled();
    expect(select).toHaveValue("alternate.yaml");

    runQueryPending = false;
    runData = run("completed", {
      run_id: state.attempts.at(-1)!.run_id,
      config_sha256: "b".repeat(64),
    });
    view.rerenderStep({});

    await waitFor(() => expect(select).toHaveValue("default.yaml"));
    expect(view.onTrialChange).toHaveBeenCalledWith(
      expect.objectContaining({
        settings: expect.objectContaining({
          base_config_name: "default.yaml",
        }),
      }),
      restored,
    );
  });

  it("keeps a restored base selection locked when lineage lookup fails", async () => {
    const { state } = await trialWithAttempt("failed");
    runQueryError = new Error("offline");
    const { onTrialChange } = renderStep({ trial: state });

    expect(screen.getByLabelText("Base configuration")).toBeDisabled();
    expect(onTrialChange).not.toHaveBeenCalled();
  });

  it("settles after an active run is restored with a newer server status", async () => {
    const { state } = await trialWithAttempt("queued");
    runData = run("running");
    const errors: unknown[][] = [];
    const errorSpy = vi
      .spyOn(console, "error")
      .mockImplementation((...args) => {
        errors.push(args);
      });
    const queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    });
    function Harness() {
      const [current, setCurrent] = useState(state);
      const currentRef = useRef(current);
      currentRef.current = current;
      return (
        <ProductionTrialStep
          workflowId="workflow-a"
          source={SOURCE}
          calibration={CALIBRATION}
          trial={current}
          pendingConfig={null}
          confirmedConfig={null}
          onTrialChange={(next, expected) => {
            if (
              JSON.stringify(currentRef.current) !== JSON.stringify(expected)
            ) {
              return false;
            }
            currentRef.current = next;
            setCurrent(next);
            return true;
          }}
          onPendingConfigChange={() => true}
          onConfirmedConfigChange={() => true}
          onReturnToFieldSetup={() => undefined}
          onUsabilityChange={() => undefined}
        />
      );
    }
    render(
      <QueryClientProvider client={queryClient}>
        <LanguageProvider>
          <Harness />
        </LanguageProvider>
      </QueryClientProvider>,
    );
    await waitFor(() =>
      expect(screen.getByTestId("trial-run-status")).toHaveTextContent(
        "Running",
      ),
    );
    await act(async () => {
      await Promise.resolve();
    });
    expect(
      errors.filter(([first]) =>
        String(first).includes("Maximum update depth"),
      ),
    ).toEqual([]);
    errorSpy.mockRestore();
  });

  it("does not publish a stale active query over a terminal trial observation", async () => {
    const { state: active } = await trialWithAttempt("running");
    const terminal = {
      ...active,
      active_run_id: null,
      attempts: active.attempts.map((attempt) => ({
        ...attempt,
        last_observed: {
          ...attempt.last_observed,
          status: "cancelled" as const,
        },
      })),
    };
    runData = run("running");
    const onTrialChange = vi.fn(() => true);
    renderStep({ trial: terminal, onTrialChange });
    await act(async () => {
      await Promise.resolve();
    });
    expect(onTrialChange).not.toHaveBeenCalled();
    expect(screen.getByTestId("trial-run-status")).toHaveTextContent("Running");
  });

  it("persists pending state before create and a double click sends one POST", async () => {
    createMutate.mockResolvedValue(run("queued"));
    const { user, onTrialChange } = renderStep();
    const start = await screen.findByRole("button", {
      name: "Start bounded trial",
    });
    await user.dblClick(start);
    await waitFor(() => expect(createMutate).toHaveBeenCalledOnce());
    expect(onTrialChange).toHaveBeenCalled();
    const firstPersisted = onTrialChange.mock
      .calls[0][0] as ProductionTrialState;
    expect(firstPersisted.pending_submission).not.toBeNull();
    expect(onTrialChange.mock.calls[0][1]).toBeNull();
    expect(createMutate.mock.invocationCallOrder[0]).toBeGreaterThan(
      onTrialChange.mock.invocationCallOrder[0],
    );
  });

  it("does not create when pending persistence fails", async () => {
    const onTrialChange = vi.fn((_trial: ProductionTrialState) => false);
    const { user } = renderStep({ onTrialChange });
    await user.click(
      await screen.findByRole("button", { name: "Start bounded trial" }),
    );
    await waitFor(() => expect(onTrialChange).toHaveBeenCalledOnce());
    expect(createMutate).not.toHaveBeenCalled();
  });

  it("fails closed when fresh health cannot be verified", async () => {
    healthRefetch.mockResolvedValueOnce({
      data: undefined,
      isError: true,
      error: new Error("offline"),
    });
    const { user, onTrialChange } = renderStep();
    await user.click(
      await screen.findByRole("button", { name: "Start bounded trial" }),
    );
    expect(
      await screen.findByText(/health could not be verified/i),
    ).toBeVisible();
    expect(onTrialChange).toHaveBeenCalledOnce();
    expect(createMutate).not.toHaveBeenCalled();
  });

  it("keeps the pending snapshot while health preflight is slow", async () => {
    let releaseHealth!: (value: {
      data: { status: string; active_run_id: null };
    }) => void;
    healthRefetch.mockReturnValueOnce(
      new Promise((resolve) => {
        releaseHealth = resolve;
      }),
    );
    const { user, onTrialChange } = renderStep();
    await user.click(
      await screen.findByRole("button", { name: "Start bounded trial" }),
    );
    await waitFor(() => expect(onTrialChange).toHaveBeenCalledOnce());
    expect(
      (onTrialChange.mock.calls[0][0] as ProductionTrialState)
        .pending_submission,
    ).not.toBeNull();
    expect(createMutate).not.toHaveBeenCalled();
    releaseHealth({ data: { status: "ok", active_run_id: null } });
  });

  it("keeps the original pending request while health is deferred and retry is attempted", async () => {
    let releaseHealth!: (value: {
      data: { status: string; active_run_id: null };
    }) => void;
    healthRefetch.mockReturnValueOnce(
      new Promise((resolve) => {
        releaseHealth = resolve;
      }),
    );
    createMutate.mockResolvedValue(run("queued"));
    const view = renderStep();
    await view.user.click(
      await screen.findByRole("button", { name: "Start bounded trial" }),
    );
    await waitFor(() => expect(view.onTrialChange).toHaveBeenCalledOnce());
    const originalPending = view.onTrialChange.mock.calls[0][0] as
      | ProductionTrialState
      | undefined;
    expect(originalPending?.pending_submission).not.toBeNull();

    view.rerenderStep({ trial: originalPending });
    const retry = screen.getByRole("button", {
      name: "Retry as a new trial",
    });
    expect(retry).toBeDisabled();
    await view.user.click(retry);
    expect(healthRefetch).toHaveBeenCalledOnce();
    expect(createMutate).not.toHaveBeenCalled();

    releaseHealth({ data: { status: "ok", active_run_id: null } });
    await waitFor(() => expect(createMutate).toHaveBeenCalledOnce());
    expect(createMutate).toHaveBeenCalledWith({
      data: originalPending?.pending_submission?.request,
    });
    const pendingWrites = view.onTrialChange.mock.calls
      .map(([state]) => state as ProductionTrialState)
      .filter((state) => state.pending_submission);
    expect(pendingWrites).toHaveLength(1);
    expect(pendingWrites[0].pending_submission?.submission_id).toBe(
      originalPending?.pending_submission?.submission_id,
    );
  });

  it("invalidates a deferred start after unmount and lets the remounted pending trial retry once", async () => {
    let releaseOldHealth!: (value: {
      data: { status: string; active_run_id: null };
    }) => void;
    healthRefetch.mockReturnValueOnce(
      new Promise((resolve) => {
        releaseOldHealth = resolve;
      }),
    );
    createMutate.mockResolvedValue(run("queued"));
    const oldOnTrialChange = vi.fn(
      (_trial: ProductionTrialState, _expected: ProductionTrialState | null) =>
        true,
    );
    const oldView = renderStep({ onTrialChange: oldOnTrialChange });
    await oldView.user.click(
      await screen.findByRole("button", { name: "Start bounded trial" }),
    );
    await waitFor(() => expect(oldOnTrialChange).toHaveBeenCalledOnce());
    const durablePending = oldOnTrialChange.mock.calls[0][0] as
      | ProductionTrialState
      | undefined;
    expect(durablePending?.pending_submission).not.toBeNull();
    oldView.unmount();

    const newOnTrialChange = vi.fn(
      (_trial: ProductionTrialState, _expected: ProductionTrialState | null) =>
        true,
    );
    const newView = renderStep({
      trial: durablePending,
      onTrialChange: newOnTrialChange,
    });
    const retry = await screen.findByRole("button", {
      name: "Retry as a new trial",
    });
    await newView.user.dblClick(retry);
    await waitFor(() => expect(createMutate).toHaveBeenCalledOnce());

    await act(async () => {
      releaseOldHealth({ data: { status: "ok", active_run_id: null } });
      await Promise.resolve();
    });
    expect(createMutate).toHaveBeenCalledOnce();
    expect(oldOnTrialChange).toHaveBeenCalledOnce();
    expect(
      newOnTrialChange.mock.calls.filter(
        ([state]) => (state as ProductionTrialState).pending_submission,
      ),
    ).toHaveLength(1);
  });

  it("keeps the pending snapshot while the create response is slow", async () => {
    let releaseCreate!: (value: RunRecord) => void;
    createMutate.mockReturnValueOnce(
      new Promise((resolve) => {
        releaseCreate = resolve;
      }),
    );
    const { user, onTrialChange } = renderStep();
    await user.click(
      await screen.findByRole("button", { name: "Start bounded trial" }),
    );
    await waitFor(() => expect(createMutate).toHaveBeenCalledOnce());
    const persisted = onTrialChange.mock.calls[0][0] as ProductionTrialState;
    expect(persisted.pending_submission).not.toBeNull();
    expect(onTrialChange).toHaveBeenCalledTimes(1);
    releaseCreate(run("queued"));
  });

  it("does not write back a POST response after unmount and reconciles the durable pending trial on remount", async () => {
    let releaseCreate!: (value: RunRecord) => void;
    createMutate.mockReturnValueOnce(
      new Promise((resolve) => {
        releaseCreate = resolve;
      }),
    );
    const oldOnTrialChange = vi.fn(
      (_trial: ProductionTrialState, _expected: ProductionTrialState | null) =>
        true,
    );
    const oldView = renderStep({ onTrialChange: oldOnTrialChange });
    await oldView.user.click(
      await screen.findByRole("button", { name: "Start bounded trial" }),
    );
    await waitFor(() => expect(createMutate).toHaveBeenCalledOnce());
    const durablePending = oldOnTrialChange.mock.calls[0][0] as
      | ProductionTrialState
      | undefined;
    expect(durablePending?.pending_submission).not.toBeNull();
    oldView.unmount();

    await act(async () => {
      releaseCreate(run("queued"));
      await Promise.resolve();
    });
    expect(oldOnTrialChange).toHaveBeenCalledOnce();

    runsData = [recoveredRun(durablePending!.pending_submission!, "queued")];
    const newOnTrialChange = vi.fn(
      (_trial: ProductionTrialState, _expected: ProductionTrialState | null) =>
        true,
    );
    renderStep({
      trial: durablePending,
      onTrialChange: newOnTrialChange,
    });
    await waitFor(() =>
      expect(newOnTrialChange).toHaveBeenCalledWith(
        expect.objectContaining({
          pending_submission: null,
          active_run_id: `production_trial_${durablePending!.pending_submission!.output_id}`,
        }),
        durablePending,
      ),
    );
    expect(createMutate).toHaveBeenCalledOnce();
    expect(oldOnTrialChange).toHaveBeenCalledOnce();
  });

  it("ignores a stale create response after the workflow context changes", async () => {
    let releaseCreate!: (value: RunRecord) => void;
    createMutate.mockImplementation(
      () =>
        new Promise<RunRecord>((resolve) => {
          releaseCreate = resolve;
        }),
    );
    const { user, onTrialChange, rerenderStep } = renderStep();
    await user.click(
      await screen.findByRole("button", { name: "Start bounded trial" }),
    );
    await waitFor(() => expect(createMutate).toHaveBeenCalledOnce());
    const nextSource = { ...SOURCE, path: "data/match-b.mp4" };
    rerenderStep({
      source: nextSource,
      calibration: {
        ...CALIBRATION,
        confirmed_frames: CALIBRATION.confirmed_frames.map((frame) => ({
          ...frame,
          input_video: nextSource.path,
        })),
      },
      trial: null,
    });
    await act(async () => {
      releaseCreate(run("queued"));
    });
    expect(onTrialChange).toHaveBeenCalledTimes(1);
  });

  it("does not cancel when the latest draft cannot be persisted", async () => {
    const { state } = await trialWithAttempt("running");
    runData = run("running");
    const onTrialChange = vi.fn((_trial: ProductionTrialState) => false);
    const { user } = renderStep({ trial: state, onTrialChange });
    await user.click(screen.getByRole("button", { name: "Cancel trial" }));
    await waitFor(() => expect(onTrialChange).toHaveBeenCalled());
    expect(cancelMutate).not.toHaveBeenCalled();
  });

  it("retains the active trial when cancellation fails on the network", async () => {
    const { state } = await trialWithAttempt("running");
    runData = run("running");
    cancelMutate.mockRejectedValueOnce(new Error("cancel offline"));
    const { user, onTrialChange } = renderStep({ trial: state });
    await user.click(screen.getByRole("button", { name: "Cancel trial" }));
    expect(await screen.findByText("cancel offline")).toBeVisible();
    expect(cancelMutate).toHaveBeenCalledOnce();
    expect(onTrialChange).toHaveBeenCalledOnce();
    expect(onTrialChange).toHaveBeenCalledWith(state, state);
    expect(state.active_run_id).toBe("trial-1");
  });

  it("does not derive when pending configuration persistence fails", async () => {
    const state = await acceptedTrial();
    installReadableEvidence();
    const onPendingConfigChange = vi.fn(
      (_pending: ProductionPendingConfigConfirmation | null) => false,
    );
    const { user } = renderStep({ trial: state, onPendingConfigChange });
    await makeLiveEvidenceReady();
    await user.click(
      screen.getByRole("button", { name: "Confirm configuration" }),
    );
    await waitFor(() => expect(onPendingConfigChange).toHaveBeenCalledOnce());
    expect(deriveMutate).not.toHaveBeenCalled();
  });

  it.each(["queued", "running", "completed"] as const)(
    "reconciles a pending %s submission on reload without creating another run",
    async (status) => {
      const { submission } = await trialWithAttempt("queued");
      const pending = setPendingProductionTrial(
        createProductionTrialState({
          base_config_name: "default.yaml",
          start_frame: 0,
          max_frames: 300,
          enable_postprocess: true,
          enable_follow_cam: true,
          tuning_patch: {},
        }),
        submission.pending,
      );
      runsData = [recoveredRun(submission.pending, status)];
      const { onTrialChange } = renderStep({ trial: pending });
      await waitFor(() =>
        expect(onTrialChange).toHaveBeenCalledWith(
          expect.objectContaining({
            pending_submission: null,
            active_run_id:
              status === "completed"
                ? null
                : `production_trial_${submission.pending.output_id}`,
          }),
          pending,
        ),
      );
      expect(createMutate).not.toHaveBeenCalled();
    },
  );

  it("shows a preflight active-run conflict and keeps the persisted pending draft", async () => {
    healthRefetch.mockResolvedValueOnce({
      data: {
        status: "ok",
        active_run_id: "occupying-run",
        config_count: 1,
        run_count: 1,
      },
    });
    const { user, onTrialChange } = renderStep();
    await user.click(
      await screen.findByRole("button", { name: "Start bounded trial" }),
    );
    expect(createMutate).not.toHaveBeenCalled();
    expect(await screen.findByText(/occupying-run/)).toBeVisible();
    expect(
      screen.getByRole("link", { name: "View occupying run" }),
    ).toHaveAttribute("href", "/history?run=occupying-run");
    expect(
      (onTrialChange.mock.calls[0][0] as ProductionTrialState)
        .pending_submission,
    ).not.toBeNull();
  });

  it("handles a POST 409 race without dropping tuning or auto-resubmitting", async () => {
    createMutate.mockRejectedValue({ status: 409, message: "conflict" });
    healthRefetch
      .mockResolvedValueOnce({ data: { status: "ok", active_run_id: null } })
      .mockResolvedValueOnce({
        data: { status: "ok", active_run_id: "race-run" },
      });
    runsRefetch.mockResolvedValueOnce({ data: [] });
    const { user, onTrialChange } = renderStep();
    await user.click(
      await screen.findByRole("button", { name: "Start bounded trial" }),
    );
    await waitFor(() => expect(createMutate).toHaveBeenCalledOnce());
    expect(await screen.findByText(/race-run/)).toBeVisible();
    expect(createMutate).toHaveBeenCalledTimes(1);
    const persisted = onTrialChange.mock.calls[0][0] as ProductionTrialState;
    expect(persisted.settings.max_frames).toBe(300);
    expect(persisted.pending_submission).not.toBeNull();
  });

  it("retries an uncertain pending result only after an explicit click and uses a fresh submission", async () => {
    const { submission } = await trialWithAttempt("queued");
    const pending = setPendingProductionTrial(
      createProductionTrialState({
        base_config_name: "default.yaml",
        start_frame: 0,
        max_frames: 300,
        enable_postprocess: true,
        enable_follow_cam: true,
        tuning_patch: {},
      }),
      submission.pending,
    );
    createMutate.mockResolvedValue(run("queued"));
    const { user, onTrialChange } = renderStep({ trial: pending });
    expect(createMutate).not.toHaveBeenCalled();
    const retry = screen.getByRole("button", {
      name: "Retry as a new trial",
    });
    await waitFor(() => expect(retry).toBeEnabled());
    await user.dblClick(retry);
    await waitFor(() => expect(createMutate).toHaveBeenCalledOnce());
    const pendingWrites = onTrialChange.mock.calls
      .map(([state]) => state as ProductionTrialState)
      .filter((state) => state.pending_submission);
    expect(pendingWrites.at(-1)?.pending_submission?.submission_id).not.toBe(
      submission.pending.submission_id,
    );
  });

  it("reconciles an exact pending run before retry and sends no duplicate POST", async () => {
    const { submission } = await trialWithAttempt("queued");
    const pending = setPendingProductionTrial(
      createProductionTrialState({
        base_config_name: "default.yaml",
        start_frame: 0,
        max_frames: 300,
        enable_postprocess: true,
        enable_follow_cam: true,
        tuning_patch: {},
      }),
      submission.pending,
    );
    const matching = recoveredRun(submission.pending);
    runsRefetch.mockResolvedValueOnce({ data: [matching], isError: false });
    const { user, onTrialChange } = renderStep({ trial: pending });
    await user.click(
      screen.getByRole("button", { name: "Retry as a new trial" }),
    );
    await waitFor(() =>
      expect(onTrialChange).toHaveBeenCalledWith(
        expect.objectContaining({
          active_run_id: `production_trial_${submission.pending.output_id}`,
        }),
        pending,
      ),
    );
    expect(createMutate).not.toHaveBeenCalled();
  });
});

function componentTrackCsv(detected: number, predicted: number, lost: number) {
  const statuses = [
    ...Array.from({ length: detected }, () => "Detected"),
    ...Array.from({ length: predicted }, () => "Predicted"),
    ...Array.from({ length: lost }, () => "Lost"),
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

function installReadableEvidence() {
  diagnosisData = {
    schema_version: "1.0",
    run_id: "trial-1",
    legacy_quality_gate_status: "warn",
    trial_signal_gate_v2: ACCEPTABLE_GATE,
    tuning_schema_version: "1.0",
  };
  artifactsData = [
    {
      name: "run_manifest.json",
      path: "run_manifest.json",
      kind: "json",
      exists: true,
      size_bytes: 10,
    },
    {
      name: "metrics_report.json",
      path: "metrics_report.json",
      kind: "json",
      exists: true,
      size_bytes: 10,
    },
    {
      name: "ball_track.csv",
      path: "ball_track.csv",
      kind: "csv",
      exists: true,
      size_bytes: 10,
    },
    {
      name: "ball_audit.json",
      path: "ball_audit.json",
      kind: "json",
      exists: true,
      size_bytes: 10,
    },
    {
      name: "ball_track.cleaned.csv",
      path: "ball_track.cleaned.csv",
      kind: "csv",
      exists: true,
      size_bytes: 10,
    },
    {
      name: "follow_cam.mp4",
      path: "follow_cam.mp4",
      kind: "video",
      exists: true,
      size_bytes: 100,
      content_type: "video/mp4",
    },
  ];
  artifactBodies = {
    "run_manifest.json": {
      schema_version: "1.0",
      run_id: "trial-1",
      input_video: SOURCE.path,
      config_name: "default.yaml",
      status: "completed",
      notes: null,
    },
    "metrics_report.json": null,
    "ball_track.csv": componentTrackCsv(200, 50, 50),
    "ball_track.cleaned.csv": componentTrackCsv(210, 50, 40),
  };
  auditData = {
    schema_version: "1.0",
    generated_at: NOW,
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
  };
  runData = run("completed", {
    artifacts: artifactsData as never,
    stats: {
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
      trial_signal_gate_v2: ACCEPTABLE_GATE,
    },
    trial_signal_gate_v2: ACCEPTABLE_GATE,
  });
  artifactBodies["metrics_report.json"] = {
    schema_version: "1.0",
    generated_at: NOW,
    tracks: {
      raw: (runData.stats as Record<string, unknown>).raw,
      cleaned: (runData.stats as Record<string, unknown>).cleaned,
    },
    quality_gate: (runData.stats as Record<string, unknown>).quality_gate,
    trial_signal_gate_v2: ACCEPTABLE_GATE,
  };
}

function installTrialGate(gate: ProductionTrialSignalGateV2 | null) {
  diagnosisData = gate
    ? {
        schema_version: "1.0",
        run_id: "trial-1",
        legacy_quality_gate_status: "warn",
        trial_signal_gate_v2: gate,
        tuning_schema_version: "1.0",
      }
    : undefined;
  if (!runData) return;
  const stats = runData.stats as Record<string, unknown>;
  const metrics = artifactBodies["metrics_report.json"] as Record<
    string,
    unknown
  >;
  if (gate) {
    stats.trial_signal_gate_v2 = gate;
    runData.trial_signal_gate_v2 = gate;
    metrics.trial_signal_gate_v2 = gate;
  } else {
    delete stats.trial_signal_gate_v2;
    delete runData.trial_signal_gate_v2;
    delete metrics.trial_signal_gate_v2;
  }
}

async function makeLiveEvidenceReady() {
  const video = document.querySelector<HTMLVideoElement>(
    '[aria-labelledby="trial-video-title"] video',
  );
  if (!video) throw new Error("expected trial video");
  fireEvent.loadedMetadata(video);
  fireEvent.canPlay(video);
  await screen.findByTestId("trial-evidence-ready");
}

describe("ProductionTrialStep evidence and configuration", () => {
  it("requires real video readiness and explicit acceptance after every evidence body is readable", async () => {
    const { state } = await trialWithAttempt("completed");
    installReadableEvidence();
    const { user, onTrialChange } = renderStep({ trial: state });
    expect(
      artifactQueryRequests
        .slice(0, 4)
        .map(({ name, request }) => ({ name, request })),
    ).toEqual([
      {
        name: "run_manifest.json",
        request: {
          cache: "no-store",
          headers: { "Cache-Control": "no-store" },
        },
      },
      {
        name: "metrics_report.json",
        request: {
          cache: "no-store",
          headers: { "Cache-Control": "no-store" },
        },
      },
      {
        name: "ball_track.csv",
        request: {
          cache: "no-store",
          headers: { "Cache-Control": "no-store" },
          responseType: "text",
        },
      },
      {
        name: "ball_track.cleaned.csv",
        request: {
          cache: "no-store",
          headers: { "Cache-Control": "no-store" },
          responseType: "text",
        },
      },
    ]);
    expect(
      screen.queryByRole("button", { name: "Accept this trial" }),
    ).not.toBeInTheDocument();
    const video = screen
      .getByLabelText("Playable trial evidence", {
        selector: "section",
      })
      .querySelector("video");
    if (!video) throw new Error("expected trial video");
    fireEvent.loadedMetadata(video);
    expect(
      screen.queryByRole("button", { name: "Accept this trial" }),
    ).not.toBeInTheDocument();
    fireEvent.canPlay(video);
    expect(
      screen.queryByRole("button", { name: "Accept this trial" }),
    ).not.toBeInTheDocument();
    await user.click(
      await screen.findByLabelText(
        /I visually reviewed this evidence and confirm/i,
      ),
    );
    const accept = await screen.findByRole("button", {
      name: "Accept this trial",
    });
    await user.click(accept);
    await waitFor(() =>
      expect(onTrialChange).toHaveBeenCalledWith(
        expect.objectContaining({
          accepted: expect.objectContaining({ run_id: "trial-1" }),
        }),
        expect.anything(),
      ),
    );
    const accepted = onTrialChange.mock.calls.at(
      -1,
    )?.[0] as ProductionTrialState;
    expect(accepted.attempts[0].last_observed.evidence_generation).toBe(
      accepted.accepted?.readiness.evidence_generation,
    );
    expect(runRefetch).toHaveBeenCalledOnce();
    expect(artifactsRefetch).toHaveBeenCalledOnce();
    expect(artifactRefetch).toHaveBeenCalledTimes(4);
    expect(artifactRefetch.mock.calls.map(([name]) => name)).toEqual([
      "run_manifest.json",
      "metrics_report.json",
      "ball_track.csv",
      "ball_track.cleaned.csv",
    ]);
    expect(
      artifactRefetch.mock.calls
        .filter(([name]) => String(name).endsWith(".csv"))
        .map(([name, request]) => ({ name, request })),
    ).toEqual([
      {
        name: "ball_track.csv",
        request: {
          cache: "no-store",
          headers: { "Cache-Control": "no-store" },
          responseType: "text",
        },
      },
      {
        name: "ball_track.cleaned.csv",
        request: {
          cache: "no-store",
          headers: { "Cache-Control": "no-store" },
          responseType: "text",
        },
      },
    ]);
    expect(auditRefetch).toHaveBeenCalledOnce();
  });

  it("keeps Accept single-flight while the final evidence refresh is pending", async () => {
    const { state } = await trialWithAttempt("completed");
    installReadableEvidence();
    let releaseRawTrack!: (value: {
      data: unknown;
      isError: false;
      dataUpdatedAt: number;
    }) => void;
    const rawTrackPending = new Promise<{
      data: unknown;
      isError: false;
      dataUpdatedAt: number;
    }>((resolve) => {
      releaseRawTrack = resolve;
    });
    artifactRefetch.mockImplementation(async (name: string) => {
      if (name === "ball_track.csv") return rawTrackPending;
      return { data: artifactBodies[name], isError: false, dataUpdatedAt: 2 };
    });
    const { user, onTrialChange } = renderStep({ trial: state });
    await makeLiveEvidenceReady();
    await user.click(
      screen.getByLabelText(/I visually reviewed this evidence and confirm/i),
    );
    const accept = screen.getByRole("button", { name: "Accept this trial" });
    await user.click(accept);
    await waitFor(() => expect(accept).toBeDisabled());
    await user.click(accept);
    expect(runRefetch).toHaveBeenCalledOnce();
    releaseRawTrack({
      data: artifactBodies["ball_track.csv"],
      isError: false,
      dataUpdatedAt: 2,
    });
    await waitFor(() => expect(onTrialChange).toHaveBeenCalledOnce());
  });

  it("does not accept when fresh evidence has a different generation", async () => {
    const { state } = await trialWithAttempt("completed");
    installReadableEvidence();
    const { user, onTrialChange } = renderStep({ trial: state });
    await makeLiveEvidenceReady();
    await user.click(
      screen.getByLabelText(/I visually reviewed this evidence and confirm/i),
    );
    artifactBodies["ball_track.csv"] = String(
      artifactBodies["ball_track.csv"],
    ).replace("0,1,2,0.9,Detected", "0,2,2,0.8,Detected");
    await user.click(screen.getByRole("button", { name: "Accept this trial" }));
    expect(
      await screen.findByText(/evidence changed during the final check/i),
    ).toBeVisible();
    expect(onTrialChange).not.toHaveBeenCalled();
    expect(
      screen.queryByTestId("trial-evidence-ready"),
    ).not.toBeInTheDocument();
  });

  it("blocks acceptance when an artifact body or main video is missing", async () => {
    const { state } = await trialWithAttempt("completed");
    installReadableEvidence();
    delete artifactBodies["ball_track.csv"];
    artifactsData = artifactsData.filter(
      (item) => item.name !== "follow_cam.mp4",
    );
    renderStep({ trial: state });
    expect(
      await screen.findByText(/cannot be read: ball_track.csv/i),
    ).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "Accept this trial" }),
    ).not.toBeInTheDocument();
  });

  it("shows an all-lost diagnosis and offers bounded adjustment instead of acceptance", async () => {
    const { state } = await trialWithAttempt("completed");
    installReadableEvidence();
    installTrialGate(ALL_LOST_GATE);
    renderStep({ trial: state });
    await makeLiveEvidenceReady();

    expect(screen.getByTestId("trial-diagnosis-code")).toHaveTextContent(
      "no_raw_candidates",
    );
    expect(
      screen.getByText("No raw ball candidates were found."),
    ).toBeVisible();
    expect(screen.getByText("Raw candidates")).toBeVisible();
    expect(screen.getByTestId("trial-debug-status-counts")).toHaveTextContent(
      /Detected frames \(debug\)[\s\S]*0 · Collected[\s\S]*Lost frames \(debug\)[\s\S]*300 · Collected/,
    );
    expect(screen.getAllByText("0 · Collected").length).toBeGreaterThan(0);
    expect(
      screen.queryByRole("button", { name: "Accept this trial" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Save and rerun" }),
    ).toBeVisible();
  });

  it("shows the model-to-class rejection boundary when every candidate class is rejected", async () => {
    const { state } = await trialWithAttempt("completed");
    installReadableEvidence();
    const filteringReasons = {
      "class_not_allowed:person": 12,
      confidence_below_min: 7,
      width_out_of_range: 6,
      height_out_of_range: 5,
      aspect_ratio_too_small: 4,
      aspect_ratio_too_large: 3,
      outside_filtering_roi: 2,
      "negative_zone:bench": 1,
      outside_ground_and_positive_zones: 1,
    };
    const classRejectedGate: ProductionTrialSignalGateV2 = {
      ...ALL_LOST_GATE,
      reason_codes: ["all_candidates_class_rejected", "zero_tracklet"],
      failure_classification: {
        code: "all_candidates_class_rejected",
        severity: "blocking",
        summary: "Backend summary",
        recommended_action: "Backend action",
      },
      stage_counts: {
        ...ALL_LOST_GATE.stage_counts!,
        raw_candidates: { value: 12, status: "collected" },
        class_mapped_candidates: { value: 0, status: "collected" },
        rejection_reasons: filteringReasons,
      },
      diagnostics: {
        ...ALL_LOST_GATE.diagnostics,
        rejection_reasons: {
          status: "collected",
          value: filteringReasons,
        },
      },
    };
    installTrialGate(classRejectedGate);
    renderStep({ trial: state });

    expect(await screen.findByTestId("trial-diagnosis-code")).toHaveTextContent(
      "all_candidates_class_rejected",
    );
    expect(screen.getByTestId("trial-detection-stage-chain")).toHaveTextContent(
      /Raw candidates[\s\S]*12[\s\S]*Class-mapped candidates[\s\S]*0/,
    );
    expect(
      screen.getByTestId("trial-stage-rejection-reasons"),
    ).toHaveTextContent("Class not allowed: person: 12");
    const reasons = screen.getByTestId("trial-stage-rejection-reasons");
    expect(reasons).toHaveTextContent(
      "Confidence below the configured minimum",
    );
    expect(reasons).toHaveTextContent(
      "Candidate width is outside the allowed range",
    );
    expect(reasons).toHaveTextContent(
      "Candidate height is outside the allowed range",
    );
    expect(reasons).toHaveTextContent("Candidate aspect ratio is too small");
    expect(reasons).toHaveTextContent("Candidate aspect ratio is too large");
    expect(reasons).toHaveTextContent(
      "Candidate is outside the filtering area",
    );
    expect(reasons).toHaveTextContent(
      "Candidate is inside an excluded zone: bench",
    );
    expect(reasons).toHaveTextContent(
      "Candidate is outside the field and every positive zone",
    );
    expect(reasons).toHaveTextContent("(negative_zone:bench)");
  });

  it("maps real filtering codes to friendly English and Chinese labels", () => {
    const codes = [
      "confidence_below_min",
      "width_out_of_range",
      "height_out_of_range",
      "aspect_ratio_too_small",
      "aspect_ratio_too_large",
      "outside_filtering_roi",
      "negative_zone:bench",
      "outside_ground_and_positive_zones",
    ];
    for (const code of codes) {
      expect(
        translations.en.production.trialDiagnosisRejectionReason(code),
      ).not.toBe(code);
      expect(
        translations.zh.production.trialDiagnosisRejectionReason(code),
      ).not.toBe(code);
    }
    expect(
      translations.en.production.trialDiagnosisReason(
        "cleaned_frame_count_mismatch",
      ),
    ).toContain("Cleaned-track");
    expect(
      translations.zh.production.trialDiagnosisReason(
        "cleaned_frame_count_mismatch",
      ),
    ).toContain("清洗后轨迹");
  });

  it("renders missing budgets and follow-camera motion as not collected instead of zero", async () => {
    const { state } = await trialWithAttempt("completed");
    installReadableEvidence();
    const missingDiagnosticsGate: ProductionTrialSignalGateV2 = {
      ...ALL_LOST_GATE,
      status: "insufficient_evidence",
      reason_codes: [
        "ai_review_trigger_budget_not_collected",
        "event_candidate_budget_not_collected",
        "follow_cam_motion_not_collected",
      ],
      failure_classification: {
        code: "insufficient_evidence",
        severity: "blocking",
        summary: "Backend summary",
        recommended_action: "Backend action",
      },
      diagnostics: {
        ...ALL_LOST_GATE.diagnostics,
        ai_review_trigger_count: notCollected(),
        ai_review_triggers_per_100_frames: notCollected(),
        event_candidate_count: notCollected(),
        event_candidates_per_100_frames: notCollected(),
        follow_cam: {
          status: "not_collected",
          max_pan_step_px: notCollected(),
          max_pan_accel_px: notCollected(),
          max_zoom_step_ratio: notCollected(),
        },
      },
    };
    installTrialGate(missingDiagnosticsGate);
    renderStep({ trial: state });

    const diagnostics = await screen.findByTestId("trial-typed-diagnostics");
    expect(diagnostics).toHaveTextContent("AI review triggers / 100 frames");
    expect(diagnostics).toHaveTextContent("Follow-camera maximum pan step");
    expect(diagnostics).toHaveTextContent("— · Not collected");
    expect(diagnostics).not.toHaveTextContent("0 · Not collected");
  });

  it("renders operational and stage reconciliation reasons as readable prose with raw codes", async () => {
    const { state } = await trialWithAttempt("completed");
    installReadableEvidence();
    const reconciliationGate: ProductionTrialSignalGateV2 = {
      ...ALL_LOST_GATE,
      status: "insufficient_evidence",
      coverage_complete: false,
      reason_codes: [
        "trial_option_conflict:postprocess",
        "frame_exception",
        "cleaned_frame_count_mismatch",
        "stage_counter_not_collected:lost_frames",
        "filtered_candidate_count_exceeds_class_mapped",
        "rejection_reasons_not_collected",
        "raw_audit_tracklet_count_not_collected",
      ],
      failure_classification: {
        code: "insufficient_evidence",
        severity: "blocking",
        summary: "Backend summary",
        recommended_action: "Backend action",
      },
      stage_counts: {
        ...ALL_LOST_GATE.stage_counts!,
        coverage_status: "invalid",
        lost_frames: { value: null, status: "not_collected" },
        reconciliation: {
          status: "mismatch",
          reason_codes: ["debug_frame_exception:1"],
        },
      },
    };
    installTrialGate(reconciliationGate);
    renderStep({ trial: state });

    const diagnosis = await screen.findByTestId("trial-diagnosis");
    expect(diagnosis).toHaveTextContent(
      "The saved trial option conflicts with the executed module state: post-processing.",
    );
    expect(diagnosis).toHaveTextContent(
      "At least one debug frame ended with an execution exception.",
    );
    expect(diagnosis).toHaveTextContent(
      "Cleaned-track and evaluated-frame counts disagree.",
    );
    expect(diagnosis).toHaveTextContent(
      "Required stage counter was not collected: debug Lost frames.",
    );
    expect(diagnosis).toHaveTextContent(
      "Filtered candidates exceed class-mapped candidates.",
    );
    expect(diagnosis).toHaveTextContent(
      "Stage rejection-reason counters were not collected.",
    );
    expect(diagnosis).toHaveTextContent(
      "The raw-track tracklet count was not collected from the full ball audit.",
    );
    expect(diagnosis).toHaveTextContent("(trial_option_conflict:postprocess)");
  });

  it("exposes the versioned field-setup action and invokes Step 2 navigation", async () => {
    const { user, onReturnToFieldSetup } = renderStep();

    const action = await screen.findByTestId("trial-field-setup-action");
    expect(action).toHaveTextContent(
      "invalidates this trial and every downstream result",
    );
    await user.click(
      screen.getByRole("button", { name: "Return to field setup" }),
    );
    expect(onReturnToFieldSetup).toHaveBeenCalledTimes(1);
  });

  it("saves only selected backend-approved detector labels", async () => {
    const { user, onTrialChange } = renderStep();
    const labels = await screen.findByLabelText("detector.allowed_labels");

    await user.selectOptions(labels, ["sports ball", "ball"]);
    await user.click(screen.getByRole("button", { name: "Save adjustments" }));

    await waitFor(() => expect(onTrialChange).toHaveBeenCalled());
    const next = onTrialChange.mock.calls.at(-1)?.[0] as ProductionTrialState;
    expect(next.settings.tuning_patch).toMatchObject({
      detector: { allowed_labels: ["sports ball", "ball"] },
      metadata: {
        production_tuning: {
          values: {
            "detector.allowed_labels": ["sports ball", "ball"],
          },
        },
      },
    });
    expect(next.settings.tuning_patch).not.toHaveProperty(
      "detector.model_path",
    );
  });

  it("keeps acceptance blocked when the v2 diagnosis gate is missing", async () => {
    const { state } = await trialWithAttempt("completed");
    installReadableEvidence();
    installTrialGate(null);
    renderStep({ trial: state });
    await makeLiveEvidenceReady();

    expect(
      screen.getByText(
        "The trial diagnosis is unavailable, so acceptance remains blocked.",
      ),
    ).toBeVisible();
    expect(
      screen.queryByLabelText(/I visually reviewed this evidence/i),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Accept this trial" }),
    ).not.toBeInTheDocument();
  });

  it("localizes blocking diagnosis prose and evidence states in Chinese", async () => {
    localStorage.setItem("app-language", "zh");
    const { state } = await trialWithAttempt("completed");
    installReadableEvidence();
    const insufficientGate: ProductionTrialSignalGateV2 = {
      ...ALL_LOST_GATE,
      status: "insufficient_evidence",
      trajectory_acceptable: true,
      signal_acceptable: true,
      acceptance_metrics_complete: true,
      reason_codes: ["evidence_not_collected:tight_crop"],
      failure_classification: {
        code: "insufficient_evidence",
        severity: "blocking",
        summary: "Backend English summary must not be shown.",
        recommended_action: "Backend English action must not be shown.",
      },
      evidence: {
        ...ALL_LOST_GATE.evidence,
        tight_crop: "not_collected",
      },
    };
    installTrialGate(insufficientGate);
    renderStep({ trial: state });

    expect(
      screen.getByText("必需的证据或指标尚不完整。", { exact: false }),
    ).toBeVisible();
    expect(screen.getByText(/补齐列出的证据或指标后重新检查/)).toBeVisible();
    expect(screen.getByText("局部裁剪证据")).toBeVisible();
    expect(screen.getByText("未采集")).toBeVisible();
    expect(
      screen.queryByText(/Backend English (summary|action)/),
    ).not.toBeInTheDocument();
  });

  it("saves only schema-approved tuning values as a versioned patch before rerun", async () => {
    createMutate.mockResolvedValue(run("queued"));
    const { user, onTrialChange } = renderStep();
    const threshold = await screen.findByLabelText(
      "detector.confidence_threshold",
    );
    expect(threshold).toHaveValue(0.25);
    await user.clear(threshold);
    await user.type(threshold, "0.15");
    await user.click(screen.getByRole("button", { name: "Save and rerun" }));

    await waitFor(() => expect(createMutate).toHaveBeenCalledOnce());
    const saved = onTrialChange.mock.calls
      .map(([next]) => next as ProductionTrialState)
      .find(
        (next) =>
          (next.settings.tuning_patch.detector as Record<string, unknown>)
            ?.confidence_threshold === 0.15,
      );
    expect(saved).toBeDefined();
    expect(saved?.settings.tuning_patch).toMatchObject({
      detector: { confidence_threshold: 0.15 },
      metadata: {
        production_tuning: {
          schema_version: "1.0",
          values: { "detector.confidence_threshold": 0.15 },
        },
      },
    });
    expect(createMutate.mock.calls[0][0].data.config_patch).toMatchObject({
      detector: { confidence_threshold: 0.15 },
      metadata: { production_tuning: { schema_version: "1.0" } },
    });
  });

  it("rejects a tuning value outside the backend-approved range", async () => {
    const { user, onTrialChange } = renderStep();
    const threshold = await screen.findByLabelText(
      "detector.confidence_threshold",
    );
    await user.clear(threshold);
    await user.type(threshold, "0.95");
    await user.click(screen.getByRole("button", { name: "Save adjustments" }));

    expect(
      await screen.findByText(/outside the approved range/i),
    ).toBeVisible();
    expect(onTrialChange).not.toHaveBeenCalled();
    expect(createMutate).not.toHaveBeenCalled();
  });

  it("localizes trial status, quality labels, evidence reasons, and actions in Chinese", async () => {
    localStorage.setItem("app-language", "zh");
    const { state } = await trialWithAttempt("completed");
    installReadableEvidence();
    const { user } = renderStep({ trial: state });
    expect(screen.getByTestId("trial-run-status")).toHaveTextContent("已完成");
    await makeLiveEvidenceReady();
    expect(screen.getByText(/系统识别:/)).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "接受本次试跑" }),
    ).not.toBeInTheDocument();
    await user.click(screen.getByLabelText(/我已目视检查本次证据/));
    expect(screen.getByRole("button", { name: "接受本次试跑" })).toBeVisible();
  });

  it("persists a fresh pending config before derive and hashes the returned server text", async () => {
    const state = await acceptedTrial();
    installReadableEvidence();
    let returnedDetail: Record<string, unknown> | null = null;
    deriveMutate.mockImplementation(async ({ data }) => {
      returnedDetail = {
        name: expectedProductionConfigName(data.output_name),
        path: `configs/generated/${data.output_name}`,
        text: "server: exact-yaml\n",
        raw: data.patch,
        resolved: data.patch,
        summary: {
          name: expectedProductionConfigName(data.output_name),
          path: `configs/generated/${data.output_name}`,
          input_video: SOURCE.path,
          postprocess_enabled: true,
          follow_cam_enabled: false,
          exists: { yaml: true },
        },
      };
      configData = returnedDetail;
      return returnedDetail;
    });
    const { user, onPendingConfigChange, onConfirmedConfigChange } = renderStep(
      { trial: state },
    );
    await makeLiveEvidenceReady();
    await user.click(
      screen.getByRole("button", { name: "Confirm configuration" }),
    );
    await waitFor(() => expect(deriveMutate).toHaveBeenCalledOnce());
    expect(onPendingConfigChange).toHaveBeenCalledOnce();
    expect(deriveMutate.mock.invocationCallOrder[0]).toBeGreaterThan(
      onPendingConfigChange.mock.invocationCallOrder[0],
    );
    await waitFor(() => expect(onConfirmedConfigChange).toHaveBeenCalledOnce());
    const confirmed = onConfirmedConfigChange.mock.calls[0][0];
    expect(confirmed.name).toMatch(
      /^generated\/production_workflow-a_[0-9a-f-]+\.yaml$/,
    );
    expect(confirmed.sha256).toMatch(/^[a-f\d]{64}$/);
    expect(returnedDetail).not.toBeNull();
  });

  it("ignores a stale derive response after the workflow context changes", async () => {
    const state = await acceptedTrial();
    installReadableEvidence();
    let releaseDerive!: (value: Record<string, unknown>) => void;
    deriveMutate.mockImplementation(
      () =>
        new Promise<Record<string, unknown>>((resolve) => {
          releaseDerive = resolve;
        }),
    );
    const {
      user,
      onPendingConfigChange,
      onConfirmedConfigChange,
      rerenderStep,
    } = renderStep({ trial: state });
    await makeLiveEvidenceReady();
    await user.click(
      screen.getByRole("button", { name: "Confirm configuration" }),
    );
    await waitFor(() => expect(deriveMutate).toHaveBeenCalledOnce());
    const pending = onPendingConfigChange.mock.calls[0][0];
    if (!pending) throw new Error("expected pending config");
    const nextSource = { ...SOURCE, path: "data/match-b.mp4" };
    rerenderStep({
      source: nextSource,
      calibration: {
        ...CALIBRATION,
        confirmed_frames: CALIBRATION.confirmed_frames.map((frame) => ({
          ...frame,
          input_video: nextSource.path,
        })),
      },
      pendingConfig: null,
    });
    await act(async () => {
      releaseDerive({
        name: expectedProductionConfigName(pending.output_name),
        path: `configs/generated/${pending.output_name}`,
        text: "server: stale-yaml\n",
        raw: pending.request.patch,
        resolved: pending.request.patch,
        summary: {
          name: expectedProductionConfigName(pending.output_name),
          path: `configs/generated/${pending.output_name}`,
          input_video: SOURCE.path,
          postprocess_enabled: true,
          follow_cam_enabled: false,
          exists: { yaml: true },
        },
      });
    });
    expect(onConfirmedConfigChange).not.toHaveBeenCalled();
  });

  it("keeps accepted inputs locked until the operator confirms invalidation", async () => {
    const state = await acceptedTrial();
    runData = run("completed");
    const { user, onTrialChange } = renderStep({ trial: state });
    const unlock = screen.getByRole("button", {
      name: "Unlock trial settings",
    });
    expect(screen.getByLabelText("Start frame")).toBeDisabled();
    await user.click(unlock);
    await user.click(screen.getByRole("button", { name: "Keep locked" }));
    expect(onTrialChange).not.toHaveBeenCalled();
    expect(unlock).toHaveFocus();

    await user.click(unlock);
    await user.click(
      screen.getByRole("button", { name: "Unlock and invalidate" }),
    );
    expect(onTrialChange).toHaveBeenCalledWith(
      expect.objectContaining({
        attempts: expect.arrayContaining([
          expect.objectContaining({ run_id: "trial-1" }),
        ]),
        accepted: null,
      }),
      state,
    );
  });

  it("rechecks the server config text and lineage before reporting Next as usable", async () => {
    const state = await acceptedTrial();
    runData = run("completed");
    const pending = await buildProductionConfigConfirmation({
      workflow_id: "workflow-a",
      source: SOURCE,
      calibration: CALIBRATION,
      trial: state,
      output_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
      generation: 1,
      confirmed_at: NOW,
    });
    const detail = {
      name: expectedProductionConfigName(pending.output_name),
      path: `configs/generated/${pending.output_name}`,
      text: "server: verified\n",
      raw: pending.request.patch ?? {},
      resolved: pending.request.patch ?? {},
      summary: {
        name: expectedProductionConfigName(pending.output_name),
        path: `configs/generated/${pending.output_name}`,
        input_video: SOURCE.path,
        postprocess_enabled: true,
        follow_cam_enabled: false,
        exists: { yaml: true },
      },
    };
    const confirmed = await finalizeProductionConfigConfirmation(
      pending,
      detail,
    );

    configPending = true;
    const loading = renderStep({ trial: state, confirmedConfig: confirmed });
    const loadingAlert = screen
      .getByText("Verifying saved configuration…")
      .closest('[role="alert"]');
    expect(loadingAlert).not.toHaveClass("text-destructive");
    expect(loading.onUsabilityChange).not.toHaveBeenCalledWith(true);
    loading.unmount();
    configPending = false;

    configData = detail;
    const verified = renderStep({ trial: state, confirmedConfig: confirmed });
    await waitFor(() =>
      expect(verified.onUsabilityChange).toHaveBeenCalledWith(true),
    );
    expect(screen.getByText("Configuration snapshot verified")).toBeVisible();
    verified.unmount();

    configData = { ...detail, text: "server: tampered\n" };
    const tampered = renderStep({ trial: state, confirmedConfig: confirmed });
    await waitFor(() =>
      expect(
        screen.getByText("The confirmed configuration text was modified."),
      ).toBeVisible(),
    );
    expect(tampered.onUsabilityChange).not.toHaveBeenCalledWith(true);
    tampered.unmount();

    configData = undefined;
    configError = { status: 404 };
    const deleted = renderStep({ trial: state, confirmedConfig: confirmed });
    expect(
      await screen.findByText("The confirmed configuration was deleted."),
    ).toBeVisible();
    expect(deleted.onUsabilityChange).not.toHaveBeenCalledWith(true);
  });
});
