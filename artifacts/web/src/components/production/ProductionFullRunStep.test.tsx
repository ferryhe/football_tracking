import { StrictMode, useRef, useState } from "react";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  ArtifactSummary,
  BroadcastReviewCandidate,
  BroadcastReviewWindowsResponse,
  ConfigDetail,
  CreateRunRequest,
  RunRecord,
} from "@workspace/api-client-react";

import { LanguageProvider } from "@/contexts/LanguageContext";
import type { BroadcastWorkflowController } from "@/hooks/useBroadcastWorkflowController";
import { PRODUCTION_BROADCAST_DELIVERY_ARTIFACTS } from "@/lib/broadcastDelivery";
import type { ProductionCalibrationDraft } from "@/lib/productionCalibration";
import {
  appendProductionFullRunAttempt,
  buildProductionFullRunSubmission,
  createProductionFullRunState,
  setPendingProductionFullRun,
  type ProductionFullRunState,
} from "@/lib/productionBroadcast";
import {
  buildProductionConfigConfirmation,
  expectedProductionConfigName,
  finalizeProductionConfigConfirmation,
} from "@/lib/productionConfigFreeze";
import {
  appendProductionTrialAttempt,
  buildProductionTrialSubmission,
  canonicalJson,
  createProductionTrialState,
  materializedProductionTrialConfigName,
  setPendingProductionTrial,
  sha256Text,
  type ProductionTrialState,
} from "@/lib/productionTrial";
import type {
  ProductionProductEvidence,
  SourceSignature,
} from "@/lib/productionWorkflow";
import { ProductionFullRunStep } from "./ProductionFullRunStep";

const api = vi.hoisted(() => ({
  configOptions: null as unknown,
  inputOptions: null as unknown,
  acceptedTrialRunOptions: null as unknown,
  acceptedTrialArtifactOptions: null as unknown,
  runsOptions: [] as unknown[],
  currentRunOptions: [] as unknown[],
  controllerInputs: [] as unknown[],
  configRefetch: vi.fn(),
  inputRefetch: vi.fn(),
  acceptedTrialRunRefetch: vi.fn(),
  acceptedTrialArtifactRefetch: vi.fn(),
  healthRefetch: vi.fn(),
  runsRefetch: vi.fn(),
  createRun: vi.fn(),
  cancelRun: vi.fn(),
  cancelRunPending: false,
  runsData: [] as RunRecord[],
  runData: null as RunRecord | null,
  acceptedTrialRunData: null as RunRecord | null,
  acceptedTrialArtifacts: [] as ArtifactSummary[],
  controller: null as unknown,
  acceptTerminalTailReview: vi.fn(),
  submitReview: vi.fn(),
  retryRecompute: vi.fn(),
  renderBroadcast: vi.fn(),
  cancelWorkflow: vi.fn(),
  refreshWorkflow: vi.fn(),
}));

vi.mock("@/hooks/useBroadcastWorkflowController", () => ({
  useBroadcastWorkflowController: (input: unknown) => {
    api.controllerInputs.push(input);
    return api.controller;
  },
}));

vi.mock("@workspace/api-client-react", () => ({
  getGetArtifactUrl: (
    runId: string,
    artifactName: string,
    params?: { status_generation?: string | null },
  ) => {
    const suffix = params?.status_generation
      ? `?status_generation=${params.status_generation}`
      : "";
    return `/api/runs/${runId}/artifacts/${artifactName}${suffix}`;
  },
  getGetConfigQueryKey: (name: string) => ["config", name],
  getGetHealthQueryKey: () => ["health"],
  getGetRunQueryKey: (runId: string) => ["run", runId],
  getListArtifactsQueryKey: (runId: string) => ["artifacts", runId],
  getListInputVideosQueryKey: () => ["inputs"],
  getListRunsQueryKey: () => ["runs"],
  useGetConfig: (_name: string, options: unknown) => {
    api.configOptions = options;
    return { refetch: api.configRefetch };
  },
  useGetHealth: () => ({ refetch: api.healthRefetch }),
  useListInputVideos: (options: unknown) => {
    api.inputOptions = options;
    return { refetch: api.inputRefetch };
  },
  useListArtifacts: (_runId: string, _params: unknown, options: unknown) => {
    api.acceptedTrialArtifactOptions = options;
    return { refetch: api.acceptedTrialArtifactRefetch };
  },
  useListRuns: (options: unknown) => {
    api.runsOptions.push(options);
    return {
      data: api.runsData,
      isError: false,
      refetch: api.runsRefetch,
    };
  },
  useGetRun: (runId: string, options: unknown) => {
    if (runId === "trial-accepted") {
      api.acceptedTrialRunOptions = options;
      return {
        data: null,
        refetch: api.acceptedTrialRunRefetch,
      };
    }
    api.currentRunOptions.push(options);
    return { data: api.runData };
  },
  useCreateRun: () => ({ isPending: false, mutateAsync: api.createRun }),
  useCancelRun: () => ({
    isPending: api.cancelRunPending,
    mutateAsync: api.cancelRun,
  }),
}));

const NOW = "2026-07-15T16:00:00.000Z";
const SOURCE: SourceSignature = {
  path: "data/match-a.mp4",
  size_bytes: 12_345,
  modified_at: "2026-07-15T00:00:00Z",
};
const CALIBRATION: ProductionCalibrationDraft = {
  source_resolution: { width: 1_920, height: 1_080 },
  suggestion: null,
  approved_polygon: [
    [100, 100],
    [1_800, 100],
    [1_700, 950],
    [200, 900],
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

async function acceptedTrial(): Promise<ProductionTrialState> {
  const settings = {
    base_config_name: "default.yaml",
    start_frame: 125,
    max_frames: 300,
    enable_postprocess: true,
    enable_follow_cam: true,
    tuning_patch: { detector: { confidence: 0.2 } },
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
        },
      },
    },
  };
}

function acceptedTrialRun(trial: ProductionTrialState): RunRecord {
  const accepted = trial.accepted!;
  const attempt = trial.attempts.find(
    (candidate) => candidate.run_id === accepted.run_id,
  )!;
  return {
    run_id: accepted.run_id,
    source: "api",
    status: "completed",
    created_at: NOW,
    config_name: materializedProductionTrialConfigName(
      attempt.request.config_name!,
      accepted.run_id,
    ),
    input_video: attempt.request.input_video,
    parent_run_id: attempt.request.parent_run_id,
    output_dir: `outputs/${accepted.run_id}`,
    modules_enabled: {
      postprocess: attempt.request.enable_postprocess!,
      follow_cam: attempt.request.enable_follow_cam!,
    },
    notes: attempt.request.notes,
  };
}

function acceptedArtifacts(trial: ProductionTrialState): ArtifactSummary[] {
  return trial.accepted!.readiness.artifact_names.map((name) => ({
    name,
    path: `outputs/trial-accepted/${name}`,
    kind: name.endsWith(".mp4") ? "video" : "file",
    exists: true,
    size_bytes: 100,
  }));
}

async function fixture() {
  const trial = await acceptedTrial();
  api.acceptedTrialRunData = acceptedTrialRun(trial);
  api.acceptedTrialArtifacts = acceptedArtifacts(trial);
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
  const confirmedConfig = await finalizeProductionConfigConfirmation(
    pending,
    detail,
  );
  return { trial, detail, confirmedConfig };
}

function runFor(
  request: CreateRunRequest,
  changes: Partial<RunRecord> = {},
): RunRecord {
  return {
    run_id: request.output_dir_name!,
    source: "broadcast_hybrid",
    status: "queued",
    created_at: NOW,
    config_name: request.config_name,
    input_video: request.input_video,
    parent_run_id: request.parent_run_id,
    output_dir: `outputs/${request.output_dir_name}`,
    modules_enabled: {
      postprocess: Boolean(request.enable_postprocess),
      follow_cam: false,
    },
    notes: request.notes,
    broadcast: {
      status: "tracking",
      quality_profile: "stable_broadcast",
      max_manual_review_windows: request.max_manual_review_windows,
    },
    ...changes,
  };
}

function controllerFor(
  input: {
    parent?: RunRecord | null;
    operation?: RunRecord | null;
    state?: BroadcastWorkflowController["recovery"]["state"];
    review?: BroadcastReviewWindowsResponse | null;
    reviewMode?: BroadcastWorkflowController["review"]["recomputeRecoveryMode"];
    artifacts?: ArtifactSummary[] | null;
    deliveryUrls?: BroadcastWorkflowController["delivery"]["urls"];
  } = {},
): BroadcastWorkflowController {
  const parent = input.parent ?? null;
  const operation = input.operation ?? null;
  const state = input.state ?? (parent ? "tracking" : "setup");
  return {
    recovery: {
      state,
      messages: [],
      pollRunIds: [],
      parentRun: parent,
      operationRun: operation,
    },
    parent,
    operation,
    artifacts: input.artifacts ?? [],
    review: {
      data: input.review ?? null,
      localizedData: input.review ?? null,
      isLoading: false,
      isError: false,
      error: null,
      decisionsArtifact: null,
      recomputeRecoveryMode: input.reviewMode ?? "none",
      recoveryAttemptState: "idle",
    },
    montage: { urlsByCandidateId: {}, messages: [] },
    delivery: {
      queryIdentity: {
        scope: state === "ready" ? "ready:missing" : "mutable",
        deliveryReady: false,
      },
      listedArtifacts: input.artifacts ?? null,
      listSucceeded: input.artifacts != null,
      urls: input.deliveryUrls ?? {},
    },
    workflowMessages: [],
    pending: {
      initialLoad: false,
      review: false,
      recompute: false,
      render: false,
      cancel: false,
      recovery: false,
    },
    errors: { action: null, query: null },
    actions: {
      refresh: api.refreshWorkflow,
      acceptTerminalTailReview: api.acceptTerminalTailReview,
      submitReview: api.submitReview,
      retryRecompute: api.retryRecompute,
      render: api.renderBroadcast,
      cancel: api.cancelWorkflow,
      clearError: vi.fn(),
    },
  };
}

function reviewCandidate(): BroadcastReviewCandidate {
  return {
    candidate_id: "candidate-1",
    candidate_fingerprint: "a".repeat(64),
    variant_id: "full",
    frame_index: 100,
    bbox: [1, 2, 3, 4],
    detector_source: "detector",
    detector_confidence: 0.8,
    predicted_label: "match_ball",
    prediction_confidence: 0.7,
    selective_decision: "abstain",
    review_kind: "policy_abstention",
    evidence: {
      sample_id: "sample-1",
      sha256: "b".repeat(64),
      dataset_version: "c".repeat(64),
      artifacts: {
        tight_tensor: {
          path: "samples/sample-1/tight.npy",
          sha256: "d".repeat(64),
          size_bytes: 12,
        },
        context_tensor: {
          path: "samples/sample-1/context.npy",
          sha256: "e".repeat(64),
          size_bytes: 34,
        },
        review_montage: {
          path: "samples/sample-1/review_montage.png",
          sha256: "f".repeat(64),
          size_bytes: 56,
        },
      },
    },
  };
}

function reviewResponse(
  candidates: BroadcastReviewCandidate[],
  terminalTailReview?: BroadcastReviewWindowsResponse["terminal_tail_review"],
): BroadcastReviewWindowsResponse {
  return {
    run_id: "full-parent",
    status: "ready",
    queue_sha256: "1".repeat(64),
    review_item_count: candidates.length === 0 ? 0 : 1,
    items:
      candidates.length === 0
        ? []
        : [
            {
              review_item_id: "window-1",
              variant_id: "full",
              start_frame: 0,
              end_frame: 100,
              duration_seconds: 5,
              compliance: "compliant",
              priority: 1,
              candidates,
            },
          ],
    ...(terminalTailReview
      ? { terminal_tail_review: terminalTailReview }
      : {}),
  };
}

async function deliveryFixture(
  existing: Awaited<ReturnType<typeof trackingState>>,
) {
  const stable = {
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
        sha256: "a".repeat(64),
        size_bytes: 1_234,
      },
    },
    final_bindings: {},
    capabilities: {},
  };
  const generation = await sha256Text(canonicalJson(stable));
  const report = {
    ...stable,
    generated_at: NOW,
    status_generation: generation,
  };
  const qualityBytes = new TextEncoder().encode(canonicalJson(report));
  const artifacts: ArtifactSummary[] =
    PRODUCTION_BROADCAST_DELIVERY_ARTIFACTS.map((name, index) => ({
      name,
      path: `sealed/${name}`,
      kind: name.endsWith(".mp4") ? "video" : "file",
      exists: true,
      size_bytes:
        name === "broadcast_quality_report.json"
          ? qualityBytes.byteLength
          : name === "broadcast.mp4"
            ? 1_234
            : index + 10,
    }));
  const parent = parentAt(existing, "ready", {
    status_generation: generation,
    trajectory_generation_id: `trajectory-${"a".repeat(24)}`,
    limitations: stable.limitations,
  });
  return { stable, generation, report, qualityBytes, artifacts, parent };
}

function readyController(
  ready: Awaited<ReturnType<typeof deliveryFixture>>,
  artifacts = ready.artifacts,
): BroadcastWorkflowController {
  const controller = controllerFor({
    parent: ready.parent,
    state: "ready",
    artifacts,
  });
  controller.delivery = {
    queryIdentity: {
      scope: `ready:${ready.generation}`,
      deliveryReady: true,
    },
    listedArtifacts: artifacts,
    listSucceeded: true,
    urls: Object.fromEntries(
      PRODUCTION_BROADCAST_DELIVERY_ARTIFACTS.map((name) => [
        name,
        `/api/runs/${ready.parent.run_id}/artifacts/${name}?status_generation=${ready.generation}`,
      ]),
    ),
  };
  return controller;
}

function parentAt(
  existing: Awaited<ReturnType<typeof trackingState>>,
  state: "needs_review" | "trajectory_ready" | "ready" | "failed" | "cancelled",
  changes: Partial<RunRecord["broadcast"]> = {},
): RunRecord {
  return runFor(existing.state.attempts[0].request, {
    status: state === "failed" || state === "cancelled" ? state : "completed",
    broadcast: {
      status: state,
      quality_profile: "stable_broadcast",
      max_manual_review_windows:
        existing.state.attempts[0].request.max_manual_review_windows,
      ...changes,
    },
  });
}

function renderOperation(
  parent: RunRecord,
  status: "running" | "failed" | "cancelled",
  runId = "render-child",
): RunRecord {
  return {
    run_id: runId,
    source: "broadcast_hybrid_render",
    status,
    created_at: NOW,
    parent_run_id: parent.run_id,
    output_dir: `outputs/${runId}`,
    progress: { stage: "render", percent: status === "running" ? 45 : 100 },
    broadcast: {
      operation: "render",
      operation_status: status,
      parent_run_id: parent.run_id,
    },
  };
}

function tamperFullRunHash(
  state: ProductionFullRunState,
): ProductionFullRunState {
  return {
    ...state,
    attempts: state.attempts.map((attempt, index) =>
      index === 0 ? { ...attempt, request_sha256: "0".repeat(64) } : attempt,
    ),
  };
}

function queryEnabled(options: unknown): boolean {
  return Boolean(
    (options as { query?: { enabled?: boolean } } | null)?.query?.enabled,
  );
}

function controllerEnabled(input: unknown): boolean {
  return Boolean((input as { enabled?: boolean } | null)?.enabled);
}

async function trackingState(input: Awaited<ReturnType<typeof fixture>>) {
  const submission = await buildProductionFullRunSubmission({
    workflow_id: "workflow-a",
    source: SOURCE,
    calibration: CALIBRATION,
    trial: input.trial,
    confirmed_config: input.confirmedConfig,
    config_verification: {
      status: "verified",
      sha256: input.confirmedConfig.sha256,
    },
    submission_id: "full-submission-existing",
    output_id: "22222222-2222-4222-8222-222222222222",
    generation: 1,
    created_at: NOW,
  });
  const waiting = setPendingProductionFullRun(
    createProductionFullRunState(),
    submission.pending,
  );
  const run = runFor(submission.pending.request, { status: "running" });
  return {
    run,
    state: appendProductionFullRunAttempt(waiting, {
      run,
      pending: submission.pending,
      observed_at: NOW,
    }),
  };
}

async function pendingState(input: Awaited<ReturnType<typeof fixture>>) {
  const submission = await buildProductionFullRunSubmission({
    workflow_id: "workflow-a",
    source: SOURCE,
    calibration: CALIBRATION,
    trial: input.trial,
    confirmed_config: input.confirmedConfig,
    config_verification: {
      status: "verified",
      sha256: input.confirmedConfig.sha256,
    },
    submission_id: "full-submission-pending",
    output_id: "99999999-9999-4999-8999-999999999999",
    generation: 1,
    created_at: NOW,
  });
  return setPendingProductionFullRun(
    createProductionFullRunState(),
    submission.pending,
  );
}

function renderStep(
  input: Awaited<ReturnType<typeof fixture>>,
  initial: ProductionFullRunState | null = null,
  requestedRunId: string | null = null,
  strict = false,
  focusRequest = 0,
) {
  const transitions: ProductionFullRunState[] = [];
  const productChanges: ProductionProductEvidence[] = [];
  const persistCurrent = vi.fn(() => true);
  const parentChange = vi.fn();
  let latest = initial;
  let latestProduct: ProductionProductEvidence | null = null;
  let replaceFullRun: ((next: ProductionFullRunState | null) => void) | null =
    null;
  let refreshHarness: (() => void) | null = null;

  function Harness() {
    const [fullRun, setFullRun] = useState(initial);
    const [, forceRender] = useState(0);
    const [verifiedProduct, setVerifiedProduct] =
      useState<ProductionProductEvidence | null>(null);
    const currentRef = useRef(fullRun);
    replaceFullRun = setFullRun;
    refreshHarness = () => forceRender((value) => value + 1);
    currentRef.current = fullRun;
    latest = fullRun;
    latestProduct = verifiedProduct;
    return (
      <LanguageProvider>
        <ProductionFullRunStep
          workflowId="workflow-a"
          source={SOURCE}
          calibration={CALIBRATION}
          trial={input.trial}
          confirmedConfig={input.confirmedConfig}
          fullRun={fullRun}
          verifiedProduct={verifiedProduct}
          requestedRunId={requestedRunId}
          focusRequest={focusRequest}
          onFullRunChange={(next, expectedRevision) => {
            if ((currentRef.current?.revision ?? 0) !== expectedRevision) {
              return false;
            }
            transitions.push(next);
            currentRef.current = next;
            latest = next;
            setFullRun(next);
            return true;
          }}
          onPersistCurrent={persistCurrent}
          onVerifiedProduct={(product, expectedRevision) => {
            if ((currentRef.current?.revision ?? 0) !== expectedRevision) {
              return false;
            }
            productChanges.push(product);
            latestProduct = product;
            setVerifiedProduct(product);
            return true;
          }}
          onParentRunIdChange={parentChange}
        />
      </LanguageProvider>
    );
  }

  const element = <Harness />;
  return {
    user: userEvent.setup(),
    transitions,
    productChanges,
    persistCurrent,
    parentChange,
    latest: () => latest,
    latestProduct: () => latestProduct,
    replaceFullRun: (next: ProductionFullRunState | null) =>
      replaceFullRun?.(next),
    refreshHarness: () => refreshHarness?.(),
    ...render(strict ? <StrictMode>{element}</StrictMode> : element),
  };
}

let uuidIndex = 0;
const UUIDS = [
  "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
  "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
  "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
];

beforeEach(() => {
  localStorage.removeItem("app-language");
  uuidIndex = 0;
  vi.spyOn(globalThis.crypto, "randomUUID").mockImplementation(
    () =>
      (UUIDS[uuidIndex++] ??
        UUIDS.at(-1)!) as `${string}-${string}-${string}-${string}-${string}`,
  );
  api.configOptions = null;
  api.inputOptions = null;
  api.acceptedTrialRunOptions = null;
  api.acceptedTrialArtifactOptions = null;
  api.runsOptions = [];
  api.currentRunOptions = [];
  api.controllerInputs = [];
  api.configRefetch.mockReset();
  api.inputRefetch.mockReset().mockImplementation(async () => ({
    isError: false,
    data: {
      root_dir: "data",
      videos: [{ name: "match-a.mp4", ...SOURCE }],
    },
  }));
  api.acceptedTrialRunRefetch.mockReset().mockImplementation(async () => ({
    isError: false,
    data: api.acceptedTrialRunData,
  }));
  api.acceptedTrialArtifactRefetch.mockReset().mockImplementation(async () => ({
    isError: false,
    data: api.acceptedTrialArtifacts,
  }));
  api.healthRefetch.mockReset().mockResolvedValue({
    isError: false,
    data: { status: "ok", active_run_id: null },
  });
  api.runsRefetch.mockReset().mockResolvedValue({
    isError: false,
    data: [],
  });
  api.createRun.mockReset();
  api.cancelRun.mockReset();
  api.cancelRunPending = false;
  api.acceptTerminalTailReview.mockReset().mockResolvedValue(undefined);
  api.submitReview.mockReset().mockResolvedValue(undefined);
  api.retryRecompute.mockReset().mockResolvedValue(undefined);
  api.renderBroadcast.mockReset().mockResolvedValue(undefined);
  api.cancelWorkflow.mockReset().mockResolvedValue(undefined);
  api.refreshWorkflow.mockReset().mockResolvedValue(undefined);
  api.runsData = [];
  api.runData = null;
  api.acceptedTrialRunData = null;
  api.acceptedTrialArtifacts = [];
  api.controller = controllerFor();
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("ProductionFullRunStep", () => {
  it("keeps remote full-run reads disabled until the request hash is verified", async () => {
    const input = await fixture();
    const existing = await trackingState(input);
    api.runData = existing.run;
    api.controller = controllerFor({
      parent: existing.run,
      state: "tracking",
    });

    renderStep(input, existing.state, existing.run.run_id);

    expect(screen.getByRole("status")).toHaveAttribute("aria-live", "polite");
    expect(screen.getByRole("status")).toHaveAttribute("aria-atomic", "true");
    expect(
      screen.getByTestId("production-full-run-integrity-validating"),
    ).toHaveAttribute("role", "presentation");
    expect(screen.queryByRole("alert")).toBeNull();
    expect(controllerEnabled(api.controllerInputs[0])).toBe(false);
    expect(queryEnabled(api.runsOptions[0])).toBe(false);
    expect(queryEnabled(api.currentRunOptions[0])).toBe(false);
    await waitFor(() =>
      expect(api.controllerInputs.some(controllerEnabled)).toBe(true),
    );
    expect(api.runsOptions.some(queryEnabled)).toBe(true);
    expect(api.currentRunOptions.some(queryEnabled)).toBe(true);
  });

  it("fails closed for a tampered ready request hash without remote reads or product links", async () => {
    const input = await fixture();
    const existing = await trackingState(input);
    const ready = await deliveryFixture(existing);
    api.runData = ready.parent;
    api.controller = readyController(ready);
    const fetchQuality = vi.fn();
    vi.stubGlobal("fetch", fetchQuality);

    const view = renderStep(
      input,
      tamperFullRunHash(existing.state),
      ready.parent.run_id,
    );

    const invalid = await screen.findByTestId(
      "production-full-run-integrity-invalid",
    );
    expect(invalid).toHaveAttribute("role", "alert");
    expect(screen.getByRole("status")).toHaveAttribute("aria-live", "off");
    expect(
      api.controllerInputs.every((entry) => !controllerEnabled(entry)),
    ).toBe(true);
    expect(api.runsOptions.every((entry) => !queryEnabled(entry))).toBe(true);
    expect(api.currentRunOptions.every((entry) => !queryEnabled(entry))).toBe(
      true,
    );
    expect(fetchQuality).not.toHaveBeenCalled();
    expect(api.refreshWorkflow).not.toHaveBeenCalled();
    expect(view.productChanges).toHaveLength(0);
    expect(screen.queryByTestId("production-product-ready")).toBeNull();
    expect(screen.queryAllByRole("link")).toHaveLength(0);
  });

  it("freshly verifies canonical config, persists pending before one exact POST, and locks double click", async () => {
    const input = await fixture();
    api.configRefetch.mockResolvedValue({ isError: false, data: input.detail });
    const view = renderStep(input);
    api.createRun.mockImplementation(
      async ({ data }: { data: CreateRunRequest }) => {
        expect(view.latest()?.pending_submission?.request).toEqual(data);
        return runFor(data);
      },
    );

    await view.user.dblClick(screen.getByTestId("production-start-full-run"));
    await waitFor(() => expect(api.createRun).toHaveBeenCalledTimes(1));
    const request = api.createRun.mock.calls[0][0].data as CreateRunRequest;
    expect(
      (api.configOptions as { request?: RequestInit }).request?.cache,
    ).toBe("no-store");
    expect((api.inputOptions as { request?: RequestInit }).request?.cache).toBe(
      "no-store",
    );
    expect(
      (api.acceptedTrialRunOptions as { request?: RequestInit }).request?.cache,
    ).toBe("no-store");
    expect(
      (api.acceptedTrialArtifactOptions as { request?: RequestInit }).request
        ?.cache,
    ).toBe("no-store");
    expect(api.inputRefetch).toHaveBeenCalledTimes(1);
    expect(api.acceptedTrialRunRefetch).toHaveBeenCalledTimes(1);
    expect(api.acceptedTrialArtifactRefetch).toHaveBeenCalledTimes(1);
    expect(request).toMatchObject({
      config_name: input.confirmedConfig.name,
      input_video: SOURCE.path,
      parent_run_id: "trial-accepted",
      start_frame: 0,
      max_frames: null,
      enable_follow_cam: false,
      pipeline_mode: "broadcast_hybrid",
    });
    expect(request.config_patch).toBeUndefined();
    expect(view.transitions[0].pending_submission).not.toBeNull();
    expect(view.latest()?.current_run_id).toBe(request.output_dir_name);
  });

  it.each([
    [
      "a replaced source",
      () =>
        api.inputRefetch.mockResolvedValueOnce({
          isError: false,
          data: {
            root_dir: "data",
            videos: [
              {
                name: "match-a.mp4",
                ...SOURCE,
                size_bytes: SOURCE.size_bytes + 1,
              },
            ],
          },
        }),
    ],
    [
      "a deleted accepted trial",
      () =>
        api.acceptedTrialRunRefetch.mockResolvedValueOnce({
          isError: true,
          data: undefined,
        }),
    ],
    [
      "an invalid accepted trial",
      () =>
        api.acceptedTrialRunRefetch.mockResolvedValueOnce({
          isError: false,
          data: { ...api.acceptedTrialRunData!, status: "failed" },
        }),
    ],
    [
      "deleted accepted evidence",
      () =>
        api.acceptedTrialArtifactRefetch.mockResolvedValueOnce({
          isError: false,
          data: api.acceptedTrialArtifacts.slice(1),
        }),
    ],
  ])("keeps the draft and blocks POST for %s", async (_label, invalidate) => {
    const input = await fixture();
    api.configRefetch.mockResolvedValue({ isError: false, data: input.detail });
    invalidate();
    const view = renderStep(input);

    await view.user.click(screen.getByTestId("production-start-full-run"));
    await screen.findByText(/no longer matches its verified evidence/i);
    expect(api.createRun).not.toHaveBeenCalled();
    expect(view.latest()).toBeNull();
    expect(view.transitions).toHaveLength(0);
  });

  it("fails closed for changed config, unhealthy service, and a foreign active run", async () => {
    const input = await fixture();
    const view = renderStep(input);
    api.configRefetch.mockResolvedValue({
      isError: false,
      data: { ...input.detail, text: "tampered\n" },
    });
    await view.user.click(screen.getByTestId("production-start-full-run"));
    await screen.findByText(/no longer matches/i);
    expect(api.createRun).not.toHaveBeenCalled();

    view.unmount();
    api.configRefetch.mockResolvedValue({ isError: false, data: input.detail });
    api.healthRefetch.mockResolvedValueOnce({ isError: true, data: undefined });
    const unhealthy = renderStep(input);
    await unhealthy.user.click(screen.getByTestId("production-start-full-run"));
    await screen.findByText(/health could not be verified/i);
    expect(unhealthy.latest()?.pending_submission).not.toBeNull();
    expect(api.createRun).not.toHaveBeenCalled();

    unhealthy.unmount();
    api.healthRefetch.mockResolvedValueOnce({
      isError: false,
      data: { status: "ok", active_run_id: "foreign-run" },
    });
    const foreign = renderStep(input);
    await foreign.user.click(screen.getByTestId("production-start-full-run"));
    await screen.findByText(/foreign-run/);
    expect(api.createRun).not.toHaveBeenCalled();
  });

  it("reconciles a lost or 409 response only with the exact created run", async () => {
    const input = await fixture();
    api.configRefetch.mockResolvedValue({ isError: false, data: input.detail });
    const view = renderStep(input);
    api.createRun.mockImplementation(
      async ({ data }: { data: CreateRunRequest }) => {
        api.runsRefetch.mockResolvedValueOnce({
          isError: false,
          data: [runFor(data)],
        });
        throw Object.assign(new Error("connection lost"), { status: 409 });
      },
    );
    await view.user.click(screen.getByTestId("production-start-full-run"));
    await waitFor(() => expect(view.latest()?.current_run_id).toBeTruthy());
    expect(view.latest()?.pending_submission).toBeNull();

    view.unmount();
    api.createRun.mockReset().mockImplementation(async ({ data }) => {
      api.runsRefetch.mockResolvedValueOnce({
        isError: false,
        data: [runFor(data, { notes: "{}" })],
      });
      throw Object.assign(new Error("conflict"), { status: 409 });
    });
    api.healthRefetch
      .mockResolvedValueOnce({
        isError: false,
        data: { status: "ok", active_run_id: null },
      })
      .mockResolvedValueOnce({
        isError: false,
        data: { status: "ok", active_run_id: "foreign-run" },
      });
    const conflict = renderStep(input);
    await conflict.user.click(screen.getByTestId("production-start-full-run"));
    await screen.findByText(/foreign-run/);
    expect(conflict.latest()?.pending_submission).not.toBeNull();
    expect(conflict.latest()?.current_run_id).toBeNull();
    expect(api.createRun).toHaveBeenCalledTimes(1);
  });

  it("clears an unreconciled pending identity before retrying with a new UUID", async () => {
    const input = await fixture();
    const pending = await pendingState(input);
    api.configRefetch.mockResolvedValue({ isError: false, data: input.detail });
    api.createRun.mockImplementation(async ({ data }) => runFor(data));
    const view = renderStep(input, pending);

    await view.user.click(
      await screen.findByTestId("production-retry-full-run"),
    );
    await waitFor(() => expect(api.createRun).toHaveBeenCalledTimes(1));
    const request = api.createRun.mock.calls[0][0].data as CreateRunRequest;
    expect(request.output_dir_name).not.toBe(
      pending.pending_submission?.expected_run_id,
    );
    expect(request.parent_run_id).toBe("trial-accepted");
    expect(view.transitions[0]).toMatchObject({
      revision: 2,
      pending_submission: null,
    });
  });

  it("persists before cancel and retries failed work with a new UUID and accepted-trial parent", async () => {
    const input = await fixture();
    const existing = await trackingState(input);
    api.runData = existing.run;
    api.configRefetch.mockResolvedValue({ isError: false, data: input.detail });
    api.cancelRun.mockImplementation(async () => {
      expect(view.persistCurrent).toHaveBeenCalledWith(existing.state.revision);
      return runFor(existing.state.attempts[0].request, {
        status: "cancelled",
        broadcast: { status: "cancelled" },
      });
    });
    const view = renderStep(input, existing.state, existing.run.run_id);
    await view.user.click(
      await screen.findByRole("button", { name: /cancel full/i }),
    );
    expect(view.persistCurrent).toHaveBeenCalledWith(existing.state.revision);
    expect(api.cancelRun).toHaveBeenCalledTimes(1);

    const failed: ProductionFullRunState = {
      ...existing.state,
      attempts: existing.state.attempts.map((attempt) => ({
        ...attempt,
        last_observed: {
          ...attempt.last_observed,
          run_status: "failed",
          workflow_state: "failed",
        },
      })),
    };
    view.unmount();
    api.runData = null;
    api.createRun
      .mockReset()
      .mockImplementation(async ({ data }) => runFor(data));
    const retry = renderStep(input, failed, failed.current_run_id);
    await retry.user.click(
      await screen.findByTestId("production-retry-full-run"),
    );
    await waitFor(() => expect(api.createRun).toHaveBeenCalledTimes(1));
    const request = api.createRun.mock.calls[0][0].data as CreateRunRequest;
    expect(request.output_dir_name).not.toBe(existing.run.run_id);
    expect(request.parent_run_id).toBe("trial-accepted");
  });

  it("drives full tracking cancellation from the local mutation and lock", async () => {
    const input = await fixture();
    const existing = await trackingState(input);
    api.runData = existing.run;
    api.cancelRunPending = true;
    api.controller = controllerFor({
      parent: existing.run,
      state: "tracking",
    });
    const pending = renderStep(input, existing.state, existing.run.run_id);
    expect(
      await screen.findByRole("button", { name: /cancel full tracking/i }),
    ).toBeDisabled();
    pending.unmount();

    api.cancelRunPending = false;
    const controller = controllerFor({
      parent: existing.run,
      state: "tracking",
    });
    controller.pending.cancel = true;
    api.controller = controller;
    let finishCancel!: (run: RunRecord) => void;
    api.cancelRun.mockImplementation(
      () =>
        new Promise<RunRecord>((resolve) => {
          finishCancel = resolve;
        }),
    );
    const view = renderStep(input, existing.state, existing.run.run_id);
    const cancel = await screen.findByRole("button", {
      name: /cancel full tracking/i,
    });
    expect(cancel).toBeEnabled();

    await view.user.dblClick(cancel);

    await waitFor(() => expect(api.cancelRun).toHaveBeenCalledTimes(1));
    expect(cancel).toBeDisabled();
    expect(api.cancelWorkflow).not.toHaveBeenCalled();
    act(() => finishCancel(parentAt(existing, "cancelled")));
    await waitFor(() =>
      expect(
        view.transitions.some(
          (transition) =>
            transition.attempts[0].last_observed.workflow_state === "cancelled",
        ),
      ).toBe(true),
    );
  });

  it("canonicalizes a child URL and blocks an unknown URL without a draft parent", async () => {
    const input = await fixture();
    const existing = await trackingState(input);
    api.runData = existing.run;
    api.runsData = [
      existing.run,
      runFor(existing.state.attempts[0].request, {
        run_id: "child-run",
        source: "broadcast_hybrid_recompute",
        parent_run_id: existing.run.run_id,
      }),
    ];
    const child = renderStep(input, existing.state, "child-run");
    await waitFor(() =>
      expect(child.parentChange).toHaveBeenCalledWith(existing.run.run_id),
    );
    child.unmount();

    api.runData = null;
    api.runsData = [];
    renderStep(input, null, "unknown-run");
    expect(screen.getByTestId("production-full-run-error")).toBeVisible();
    expect(
      screen.getByRole("link", { name: /legacy broadcast/i }),
    ).toHaveAttribute("href", "/broadcast?run=unknown-run");
    expect(screen.queryByTestId("production-start-full-run")).toBeNull();
  });

  it("submits an explicit zero-candidate review exactly once under StrictMode", async () => {
    const input = await fixture();
    const existing = await trackingState(input);
    const parent = parentAt(existing, "needs_review");
    api.runData = parent;
    api.controller = controllerFor({
      parent,
      state: "needs_review",
      review: reviewResponse([]),
    });
    const view = renderStep(input, existing.state, parent.run_id, true);

    await waitFor(() =>
      expect(
        screen.getByTestId("production-full-run-status"),
      ).toHaveTextContent(/needs review/i),
    );
    await view.user.dblClick(
      await screen.findByRole("button", {
        name: /continue without candidate decisions/i,
      }),
    );
    await waitFor(() => expect(api.submitReview).toHaveBeenCalledTimes(1));
    expect(api.submitReview).toHaveBeenCalledWith([], "");
  });

  it("shows and explicitly confirms a two-frame terminal-tail limitation even with no candidates", async () => {
    const input = await fixture();
    const existing = await trackingState(input);
    const parent = parentAt(existing, "needs_review");
    const response = reviewResponse([], {
      status: "required",
      reason: "terminal_decoder_shortfall_requires_operator_review",
      evidence: {
        source_video_sha256: "1".repeat(64),
        tracking_contract_sha256: "2".repeat(64),
        action_signal_report_sha256: "3".repeat(64),
        temporal_chunks_report_sha256: "4".repeat(64),
        reported_frame_count: 5194,
        verified_frame_count: 5192,
        gap_frames: 2,
        gap_seconds: 0.1,
        evidence_sha256: "5".repeat(64),
      },
    });
    api.runData = parent;
    api.controller = controllerFor({
      parent,
      state: "needs_review",
      review: response,
    });
    const view = renderStep(input, existing.state, parent.run_id);

    expect(
      await screen.findByTestId("production-terminal-tail-review"),
    ).toHaveTextContent(/reports 5194 frames/i);
    expect(screen.getByTestId("production-terminal-tail-review")).toHaveTextContent(
      /final 2 source frames \(0\.1s\)/i,
    );
    expect(screen.getByLabelText(/reviewer id/i)).toBeVisible();
    const submit = screen.getByRole("button", {
      name: /continue without candidate decisions/i,
    });
    expect(submit).toBeDisabled();

    await view.user.click(
      screen.getByLabelText(/verified product excludes this damaged terminal tail/i),
    );
    expect(submit).toBeEnabled();
    await view.user.click(submit);

    await waitFor(() => expect(api.submitReview).toHaveBeenCalledTimes(1));
    expect(api.submitReview).toHaveBeenCalledWith([], "operator");
  });

  it("submits only the terminal-tail acknowledgement when the qualified review queue is missing", async () => {
    const input = await fixture();
    const existing = await trackingState(input);
    const parent = parentAt(existing, "needs_review");
    const response: BroadcastReviewWindowsResponse = {
      ...reviewResponse([], {
        status: "required",
        reason: "terminal_decoder_shortfall_requires_operator_review",
        evidence: {
          source_video_sha256: "1".repeat(64),
          tracking_contract_sha256: "2".repeat(64),
          action_signal_report_sha256: "3".repeat(64),
          temporal_chunks_report_sha256: "4".repeat(64),
          reported_frame_count: 5194,
          verified_frame_count: 5192,
          gap_frames: 2,
          gap_seconds: 0.1,
          evidence_sha256: "5".repeat(64),
        },
      }),
      status: "needs_review",
      reason: "missing_qualified_selective_review_queue",
      queue_sha256: null,
    };
    api.runData = parent;
    api.controller = controllerFor({
      parent,
      state: "needs_review",
      review: response,
    });
    const view = renderStep(input, existing.state, parent.run_id);

    const submit = await screen.findByRole("button", {
      name: /confirm terminal source limitation/i,
    });
    expect(submit).toBeDisabled();
    expect(
      screen.queryByRole("button", {
        name: /continue without candidate decisions/i,
      }),
    ).toBeNull();
    expect(screen.getByText("missing_qualified_selective_review_queue")).toBeVisible();

    const reviewer = screen.getByLabelText(/reviewer id/i);
    await view.user.clear(reviewer);
    await view.user.click(
      screen.getByLabelText(
        /verified product excludes this damaged terminal tail/i,
      ),
    );
    expect(submit).toBeDisabled();
    await view.user.type(reviewer, "quality-lead");
    expect(submit).toBeEnabled();
    await view.user.dblClick(submit);

    await waitFor(() =>
      expect(api.acceptTerminalTailReview).toHaveBeenCalledTimes(1),
    );
    expect(api.acceptTerminalTailReview).toHaveBeenCalledWith("quality-lead");
    expect(api.submitReview).not.toHaveBeenCalled();
    expect(api.retryRecompute).not.toHaveBeenCalled();
  });

  it("reuses verified montage review, reviewer identity, and explicit decisions", async () => {
    const input = await fixture();
    const existing = await trackingState(input);
    const parent = parentAt(existing, "needs_review");
    const response = reviewResponse([reviewCandidate()]);
    api.runData = parent;
    api.controller = controllerFor({
      parent,
      state: "needs_review",
      review: response,
    });
    (api.controller as BroadcastWorkflowController).montage = {
      urlsByCandidateId: { "candidate-1": "/api/evidence/montage.png" },
      messages: [],
    };
    const view = renderStep(input, existing.state, parent.run_id);

    const montage = await screen.findByAltText(
      /verified candidate review montage/i,
    );
    fireEvent.load(montage);
    await view.user.clear(screen.getByLabelText(/reviewer id/i));
    await view.user.type(screen.getByLabelText(/reviewer id/i), "quality-lead");
    await view.user.click(screen.getByRole("radio", { name: /confirm ball/i }));
    await view.user.click(
      screen.getByRole("button", { name: /submit review decisions/i }),
    );

    await waitFor(() => expect(api.submitReview).toHaveBeenCalledTimes(1));
    expect(api.submitReview).toHaveBeenCalledWith(
      [{ candidate_id: "candidate-1", action: "confirm_ball" }],
      "quality-lead",
    );
  });

  it("offers an explicit recompute retry after a failed child operation", async () => {
    const input = await fixture();
    const existing = await trackingState(input);
    const parent = parentAt(existing, "needs_review");
    api.runData = parent;
    api.controller = controllerFor({
      parent,
      state: "needs_review",
      reviewMode: "retry",
    });
    const view = renderStep(input, existing.state, parent.run_id);

    await view.user.dblClick(
      await screen.findByRole("button", {
        name: /retry trajectory recomputation/i,
      }),
    );
    await waitFor(() => expect(api.retryRecompute).toHaveBeenCalledTimes(1));
  });

  it("reuses the trajectory render step and synchronously locks duplicate render", async () => {
    const input = await fixture();
    const existing = await trackingState(input);
    const trajectoryId = `trajectory-${"a".repeat(24)}`;
    const parent = parentAt(existing, "trajectory_ready", {
      trajectory_generation_id: trajectoryId,
    });
    api.runData = parent;
    api.controller = controllerFor({ parent, state: "trajectory_ready" });
    const view = renderStep(input, existing.state, parent.run_id);

    await view.user.dblClick(
      await screen.findByRole("button", { name: /render broadcast/i }),
    );
    await waitFor(() => expect(api.renderBroadcast).toHaveBeenCalledTimes(1));
    expect(api.renderBroadcast).toHaveBeenCalledWith({
      trajectory_generation_id: trajectoryId,
      target_width: 1920,
      target_height: 1080,
    });
  });

  it.each(["failed", "cancelled"] as const)(
    "unlocks render retry after the authoritative child is %s",
    async (terminalStatus) => {
      const input = await fixture();
      const existing = await trackingState(input);
      const parent = parentAt(existing, "trajectory_ready", {
        trajectory_generation_id: `trajectory-${"a".repeat(24)}`,
      });
      api.runData = parent;
      api.controller = controllerFor({ parent, state: "trajectory_ready" });
      const view = renderStep(input, existing.state, parent.run_id);

      await view.user.dblClick(
        await screen.findByRole("button", { name: /render broadcast/i }),
      );
      await waitFor(() => expect(api.renderBroadcast).toHaveBeenCalledTimes(1));

      const activeOperation = renderOperation(parent, "running");
      api.controller = controllerFor({
        parent,
        operation: activeOperation,
        state: "rendering",
      });
      act(() => view.refreshHarness());
      await waitFor(() =>
        expect(
          screen.getByTestId("production-full-run-status"),
        ).toHaveTextContent(/rendering/i),
      );

      api.controller = controllerFor({
        parent,
        operation: renderOperation(
          parent,
          terminalStatus,
          activeOperation.run_id,
        ),
        state: "trajectory_ready",
      });
      act(() => view.refreshHarness());
      await view.user.click(
        await screen.findByRole("button", { name: /render broadcast/i }),
      );

      await waitFor(() => expect(api.renderBroadcast).toHaveBeenCalledTimes(2));
    },
  );

  it("keeps the render lock when an unobserved old terminal child arrives late", async () => {
    const input = await fixture();
    const existing = await trackingState(input);
    const parent = parentAt(existing, "trajectory_ready", {
      trajectory_generation_id: `trajectory-${"a".repeat(24)}`,
    });
    api.runData = parent;
    api.controller = controllerFor({ parent, state: "trajectory_ready" });
    const view = renderStep(input, existing.state, parent.run_id);

    await view.user.dblClick(
      await screen.findByRole("button", { name: /render broadcast/i }),
    );
    await waitFor(() => expect(api.renderBroadcast).toHaveBeenCalledTimes(1));

    api.controller = controllerFor({
      parent,
      operation: renderOperation(parent, "failed", "old-render-child"),
      state: "trajectory_ready",
    });
    act(() => view.refreshHarness());
    await view.user.dblClick(
      await screen.findByRole("button", { name: /render broadcast/i }),
    );

    expect(api.renderBroadcast).toHaveBeenCalledTimes(1);
  });

  it("unlocks render retry when an observed render returns to trajectory ready without an active child", async () => {
    const input = await fixture();
    const existing = await trackingState(input);
    const parent = parentAt(existing, "trajectory_ready", {
      trajectory_generation_id: `trajectory-${"a".repeat(24)}`,
    });
    api.runData = parent;
    api.controller = controllerFor({ parent, state: "trajectory_ready" });
    const view = renderStep(input, existing.state, parent.run_id);

    await view.user.dblClick(
      await screen.findByRole("button", { name: /render broadcast/i }),
    );
    await waitFor(() => expect(api.renderBroadcast).toHaveBeenCalledTimes(1));

    api.controller = controllerFor({
      parent,
      operation: renderOperation(parent, "running"),
      state: "rendering",
    });
    act(() => view.refreshHarness());
    await waitFor(() =>
      expect(
        screen.getByTestId("production-full-run-status"),
      ).toHaveTextContent(/rendering/i),
    );

    api.controller = controllerFor({ parent, state: "trajectory_ready" });
    act(() => view.refreshHarness());
    await view.user.click(
      await screen.findByRole("button", { name: /render broadcast/i }),
    );

    await waitFor(() => expect(api.renderBroadcast).toHaveBeenCalledTimes(2));
  });

  it("persists an active child observation before controller cancellation", async () => {
    const input = await fixture();
    const existing = await trackingState(input);
    const trajectoryId = `trajectory-${"a".repeat(24)}`;
    const parent = parentAt(existing, "trajectory_ready", {
      trajectory_generation_id: trajectoryId,
    });
    const operation: RunRecord = {
      run_id: "render-child",
      source: "broadcast_hybrid_render",
      status: "running",
      created_at: NOW,
      parent_run_id: parent.run_id,
      output_dir: "outputs/render-child",
      progress: { stage: "render", percent: 45 },
      broadcast: {
        operation: "render",
        operation_status: "running",
        parent_run_id: parent.run_id,
      },
    };
    api.runData = parent;
    api.controller = controllerFor({
      parent,
      operation,
      state: "rendering",
    });
    const view = renderStep(input, existing.state, parent.run_id, false, 1);

    await waitFor(() =>
      expect(view.latest()?.attempts[0].last_observed).toMatchObject({
        workflow_state: "rendering",
        operation: { run_id: "render-child", kind: "render" },
      }),
    );
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: /cancel operation/i }),
      ).toHaveFocus(),
    );
    await view.user.click(
      screen.getByRole("button", { name: /cancel operation/i }),
    );
    expect(view.persistCurrent).toHaveBeenCalledWith(view.latest()!.revision);
    expect(api.cancelWorkflow).toHaveBeenCalledTimes(1);
  });

  it("publishes product evidence only after report, metadata, and canplay verification", async () => {
    const input = await fixture();
    const existing = await trackingState(input);
    const ready = await deliveryFixture(existing);
    api.runData = ready.parent;
    api.controller = readyController(ready);
    const fetchQuality = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      arrayBuffer: async () => ready.qualityBytes.slice().buffer,
    });
    vi.stubGlobal("fetch", fetchQuality);
    const view = renderStep(input, existing.state, ready.parent.run_id);

    expect(screen.queryByTestId("production-product-ready")).toBeNull();
    expect(screen.queryAllByRole("link")).toHaveLength(0);
    const video = await waitFor(() => {
      const element = view.container.querySelector("video");
      expect(element).not.toBeNull();
      return element!;
    });
    expect(fetchQuality).toHaveBeenCalledWith(
      `/api/runs/${ready.parent.run_id}/artifacts/broadcast_quality_report.json?status_generation=${ready.generation}`,
      expect.objectContaining({ cache: "no-store" }),
    );
    fireEvent.loadedMetadata(video);
    expect(view.productChanges).toHaveLength(0);
    fireEvent.canPlay(video);

    await waitFor(() => expect(view.productChanges).toHaveLength(1));
    expect(view.latestProduct()).toMatchObject({
      run_id: ready.parent.run_id,
      status_generation: ready.generation,
      artifact_name: "broadcast.mp4",
    });
    expect(screen.getByTestId("production-product-ready")).toBeVisible();
    expect(screen.getByText("source_audio_not_preserved")).toBeVisible();
    const downloads = screen.getAllByRole("link");
    expect(downloads).toHaveLength(8);
    for (const link of downloads) {
      expect(link.getAttribute("href")).toContain(
        `/api/runs/${ready.parent.run_id}/artifacts/`,
      );
    }
  });

  it("does not finalize a playable product when its request hash becomes invalid", async () => {
    const input = await fixture();
    const existing = await trackingState(input);
    const ready = await deliveryFixture(existing);
    api.runData = ready.parent;
    api.controller = readyController(ready);
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        arrayBuffer: async () => ready.qualityBytes.slice().buffer,
      }),
    );
    const view = renderStep(input, existing.state, ready.parent.run_id);
    const video = await waitFor(() => {
      const element = view.container.querySelector("video");
      expect(element).not.toBeNull();
      return element!;
    });

    act(() => {
      video.dispatchEvent(new Event("loadedmetadata", { bubbles: true }));
      video.dispatchEvent(new Event("canplay", { bubbles: true }));
      view.replaceFullRun(tamperFullRunHash(existing.state));
    });

    await screen.findByTestId("production-full-run-integrity-invalid");
    expect(view.productChanges).toHaveLength(0);
    expect(screen.queryByTestId("production-product-ready")).toBeNull();
    expect(screen.queryAllByRole("link")).toHaveLength(0);
  });

  it("blocks missing, duplicate, wrong-generation, and malformed delivery without exposing URLs", async () => {
    const input = await fixture();
    const existing = await trackingState(input);
    const ready = await deliveryFixture(existing);

    const wrongGeneration = new TextEncoder().encode(
      canonicalJson({ ...ready.report, status_generation: "b".repeat(64) }),
    );
    const malformed = new TextEncoder().encode("{");
    const cases = [
      {
        artifacts: ready.artifacts.slice(1),
        bytes: ready.qualityBytes,
      },
      {
        artifacts: [...ready.artifacts.slice(0, -1), ready.artifacts[0]],
        bytes: ready.qualityBytes,
      },
      {
        artifacts: ready.artifacts.map((artifact) =>
          artifact.name === "broadcast_quality_report.json"
            ? { ...artifact, size_bytes: wrongGeneration.byteLength }
            : artifact,
        ),
        bytes: wrongGeneration,
      },
      {
        artifacts: ready.artifacts.map((artifact) =>
          artifact.name === "broadcast_quality_report.json"
            ? { ...artifact, size_bytes: malformed.byteLength }
            : artifact,
        ),
        bytes: malformed,
      },
    ];

    for (const invalid of cases) {
      api.runData = ready.parent;
      api.controller = readyController(ready, invalid.artifacts);
      vi.stubGlobal(
        "fetch",
        vi.fn().mockResolvedValue({
          ok: true,
          status: 200,
          arrayBuffer: async () => invalid.bytes.slice().buffer,
        }),
      );
      const view = renderStep(input, existing.state, ready.parent.run_id);
      await screen.findByTestId("production-delivery-blocked");
      expect(screen.queryByTestId("production-product-ready")).toBeNull();
      expect(screen.queryAllByRole("link")).toHaveLength(0);
      expect(view.productChanges).toHaveLength(0);
      view.unmount();
    }
  });

  it("blocks a final video load error without persisting or exposing downloads", async () => {
    const input = await fixture();
    const existing = await trackingState(input);
    const ready = await deliveryFixture(existing);
    api.runData = ready.parent;
    api.controller = readyController(ready);
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        arrayBuffer: async () => ready.qualityBytes.slice().buffer,
      }),
    );
    const view = renderStep(input, existing.state, ready.parent.run_id);
    const video = await waitFor(() => {
      const element = view.container.querySelector("video");
      expect(element).not.toBeNull();
      return element!;
    });
    fireEvent.error(video);

    await screen.findByTestId("production-delivery-blocked");
    expect(view.productChanges).toHaveLength(0);
    expect(screen.queryAllByRole("link")).toHaveLength(0);
  });

  it("localizes the production status and reused review action in Chinese", async () => {
    localStorage.setItem("app-language", "zh");
    const input = await fixture();
    const existing = await trackingState(input);
    const parent = parentAt(existing, "needs_review");
    api.runData = parent;
    api.controller = controllerFor({
      parent,
      state: "needs_review",
      review: reviewResponse([]),
    });
    renderStep(input, existing.state, parent.run_id);

    await waitFor(() =>
      expect(
        screen.getByTestId("production-full-run-status"),
      ).toHaveTextContent("需要人工复核"),
    );
    expect(
      await screen.findByRole("button", { name: "无候选决定并继续" }),
    ).toBeVisible();
  });
});
