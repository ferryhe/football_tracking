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
import type { BallAuditReport, RunRecord } from "@workspace/api-client-react";

import { LanguageProvider } from "@/contexts/LanguageContext";
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
  type ProductionTrialState,
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
const auditRefetch = vi.fn();
let runsData: RunRecord[] = [];
let runData: RunRecord | undefined;
let artifactsData: Array<Record<string, unknown>> = [];
let artifactBodies: Record<string, unknown> = {};
let auditData: BallAuditReport | undefined;
let configData: Record<string, unknown> | undefined;
let configError: unknown = null;
let configPending = false;

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
  useGetRun: () => ({ ...queryBase, data: runData, refetch: runRefetch }),
  useListArtifacts: () => ({
    ...queryBase,
    data: artifactsData,
    refetch: artifactsRefetch,
  }),
  useGetArtifact: (_runId: string, name: string) => ({
    ...queryBase,
    data: artifactBodies[name],
    refetch: () => artifactRefetch(name),
  }),
  useGetBallAuditReport: () => ({
    ...queryBase,
    data: auditData,
    refetch: auditRefetch,
  }),
  useGetConfig: () => ({
    ...queryBase,
    data: configData,
    isPending: configPending,
    isError: configError !== null,
    error: configError,
  }),
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
  runsData = [];
  runData = undefined;
  artifactsData = [];
  artifactBodies = {};
  auditData = undefined;
  configData = undefined;
  configError = null;
  configPending = false;
  vi.spyOn(globalThis.crypto, "randomUUID")
    .mockReturnValueOnce("11111111-1111-4111-8111-111111111111")
    .mockReturnValueOnce("22222222-2222-4222-8222-222222222222")
    .mockReturnValue("33333333-3333-4333-8333-333333333333");
});

describe("ProductionTrialStep mutation safety", () => {
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
    await user.click(
      screen.getByRole("button", { name: "Retry as a new trial" }),
    );
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
    },
  });
  artifactBodies["metrics_report.json"] = {
    schema_version: "1.0",
    generated_at: NOW,
    tracks: {
      raw: (runData.stats as Record<string, unknown>).raw,
      cleaned: (runData.stats as Record<string, unknown>).cleaned,
    },
    quality_gate: (runData.stats as Record<string, unknown>).quality_gate,
  };
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

  it("localizes trial status, quality labels, evidence reasons, and actions in Chinese", async () => {
    localStorage.setItem("app-language", "zh");
    const { state } = await trialWithAttempt("completed");
    installReadableEvidence();
    renderStep({ trial: state });
    expect(screen.getByTestId("trial-run-status")).toHaveTextContent("已完成");
    await makeLiveEvidenceReady();
    expect(screen.getByText(/系统识别:/)).toBeVisible();
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
