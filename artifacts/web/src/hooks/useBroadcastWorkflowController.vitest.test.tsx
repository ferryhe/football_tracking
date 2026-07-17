import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type {
  ArtifactSummary,
  BroadcastReviewWindowsResponse,
  RunRecord,
} from "@workspace/api-client-react";

const submitReviewMutate = vi.fn();
const submitTerminalTailReviewMutate = vi.fn();
const recomputeMutate = vi.fn();
const renderMutate = vi.fn();
const cancelMutate = vi.fn();
let requestedRunData: RunRecord | undefined;
let runsData: RunRecord[] = [];
let artifactsData: ArtifactSummary[] = [];
let reviewData: BroadcastReviewWindowsResponse | undefined;
let capturedRunQueryOptions: Record<string, unknown> | undefined;
let capturedRunsQueryOptions: Record<string, unknown> | undefined;
let capturedArtifactsQueryOptions: Record<string, unknown> | undefined;
let capturedReviewQueryOptions: Record<string, unknown> | undefined;
const capturedArtifactCalls: Array<{
  runId: string;
  params: { status_generation?: string | null } | undefined;
  options: Record<string, unknown>;
}> = [];
const capturedReviewCalls: Array<{
  runId: string;
  options: Record<string, unknown>;
}> = [];

const queryBase = {
  dataUpdatedAt: 1,
  isPending: false,
  isLoading: false,
  isFetching: false,
  isSuccess: true,
  isError: false,
  error: null,
  refetch: vi.fn(),
};

vi.mock("@workspace/api-client-react", () => ({
  getGetArtifactUrl: (
    runId: string,
    name: string,
    params?: { status_generation?: string | null },
  ) =>
    `/api/runs/${runId}/artifacts/${name}${
      params?.status_generation
        ? `?status_generation=${params.status_generation}`
        : ""
    }`,
  getGetBroadcastReviewWindowsQueryKey: (runId: string) => [
    "broadcast-review",
    runId,
  ],
  getGetRunQueryKey: (runId: string) => ["run", runId],
  getListArtifactsQueryKey: (
    runId: string,
    params?: { status_generation?: string | null },
  ) => ["artifacts", runId, ...(params ? [params] : [])],
  getListRunsQueryKey: () => ["runs"],
  useGetRun: (_runId: string, options: Record<string, unknown>) => {
    capturedRunQueryOptions = options;
    return { ...queryBase, data: requestedRunData };
  },
  useListRuns: (options: Record<string, unknown>) => {
    capturedRunsQueryOptions = options;
    return { ...queryBase, data: runsData };
  },
  useListArtifacts: (
    _runId: string,
    params: { status_generation?: string | null } | undefined,
    options: Record<string, unknown>,
  ) => {
    capturedArtifactsQueryOptions = options;
    capturedArtifactCalls.push({ runId: _runId, params, options });
    return { ...queryBase, data: artifactsData };
  },
  useGetBroadcastReviewWindows: (
    runId: string,
    options: Record<string, unknown>,
  ) => {
    capturedReviewQueryOptions = options;
    capturedReviewCalls.push({ runId, options });
    return {
      ...queryBase,
      data: reviewData,
    };
  },
  useSubmitBroadcastReviewActions: () => ({
    isPending: false,
    mutateAsync: submitReviewMutate,
  }),
  useSubmitBroadcastTerminalTailReview: () => ({
    isPending: false,
    mutateAsync: submitTerminalTailReviewMutate,
  }),
  useRecomputeBroadcastTrajectory: () => ({
    isPending: false,
    mutateAsync: recomputeMutate,
  }),
  useRenderBroadcastHybrid: () => ({
    isPending: false,
    mutateAsync: renderMutate,
  }),
  useCancelRun: () => ({
    isPending: false,
    mutateAsync: cancelMutate,
  }),
}));

import { useBroadcastWorkflowController } from "./useBroadcastWorkflowController";

function parentRun(
  broadcastStatus: string,
  changes: Partial<RunRecord> = {},
): RunRecord {
  return {
    run_id: "parent-1",
    source: "broadcast_hybrid",
    status: "completed",
    created_at: "2026-07-15T12:00:00Z",
    output_dir: "outputs/parent-1",
    broadcast: { status: broadcastStatus },
    ...changes,
  };
}

function operationRun(
  operation: "recompute" | "render",
  status: RunRecord["status"] = "running",
): RunRecord {
  return {
    run_id: `${operation}-1`,
    source: `broadcast_hybrid_${operation}`,
    status,
    created_at: "2026-07-15T12:01:00Z",
    output_dir: `outputs/${operation}-1`,
    parent_run_id: "parent-1",
    broadcast: {
      status: operation === "render" ? "rendering" : "recomputing",
      operation,
      operation_status: status === "running" ? "running" : "completed",
      parent_run_id: "parent-1",
    },
  };
}

function wrapper() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return function TestWrapper({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

beforeEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
  requestedRunData = undefined;
  runsData = [];
  artifactsData = [];
  reviewData = undefined;
  capturedRunQueryOptions = undefined;
  capturedRunsQueryOptions = undefined;
  capturedArtifactsQueryOptions = undefined;
  capturedReviewQueryOptions = undefined;
  capturedArtifactCalls.length = 0;
  capturedReviewCalls.length = 0;
  recomputeMutate.mockResolvedValue({ run_id: "recompute-1" });
  submitTerminalTailReviewMutate.mockResolvedValue({
    details: { terminal_tail_review_sha256: "d".repeat(64) },
  });
  renderMutate.mockResolvedValue({ run_id: "render-1" });
  cancelMutate.mockResolvedValue({ status: "cancelled" });
});

describe("useBroadcastWorkflowController", () => {
  it("keeps all server queries disabled when the controller is disabled", () => {
    const { result } = renderHook(
      () =>
        useBroadcastWorkflowController({
          parentRunId: "parent-1",
          enabled: false,
          language: "en",
        }),
      { wrapper: wrapper() },
    );

    expect(result.current.recovery.state).toBe("setup");
    expect(
      (capturedRunQueryOptions?.query as { enabled?: boolean }).enabled,
    ).toBe(false);
    expect(
      (capturedArtifactsQueryOptions?.query as { enabled?: boolean }).enabled,
    ).toBe(false);
  });

  it("bypasses the HTTP cache across evidence generations and parents", () => {
    const firstParent = parentRun("needs_review");
    requestedRunData = firstParent;
    runsData = [firstParent];

    const { rerender } = renderHook(
      ({ parentRunId }: { parentRunId: string }) =>
        useBroadcastWorkflowController({
          parentRunId,
          enabled: true,
          language: "en",
        }),
      { initialProps: { parentRunId: "parent-1" }, wrapper: wrapper() },
    );

    expect(capturedRunQueryOptions?.request).toMatchObject({
      cache: "no-store",
      headers: { "Cache-Control": "no-store" },
    });
    expect(capturedRunsQueryOptions?.request).toMatchObject({
      cache: "no-store",
      headers: { "Cache-Control": "no-store" },
    });
    expect(capturedArtifactsQueryOptions).toMatchObject({
      query: { queryKey: ["artifacts", "parent-1", "mutable"] },
      request: { cache: "no-store" },
    });
    expect(capturedReviewQueryOptions).toMatchObject({
      query: { queryKey: ["broadcast-review", "parent-1"] },
      request: { cache: "no-store" },
    });

    const generation = "a".repeat(64);
    const readyParent = parentRun("ready", {
      broadcast: { status: "ready", status_generation: generation },
    });
    requestedRunData = readyParent;
    runsData = [readyParent];
    rerender({ parentRunId: "parent-1" });

    expect(capturedArtifactsQueryOptions).toMatchObject({
      query: {
        queryKey: [
          "artifacts",
          "parent-1",
          { status_generation: generation },
          `ready:${generation}`,
        ],
      },
      request: { cache: "no-store" },
    });
    expect(capturedArtifactCalls.at(-1)?.params).toEqual({
      status_generation: generation,
    });

    const secondParent = parentRun("needs_review", {
      run_id: "parent-2",
      output_dir: "outputs/parent-2",
    });
    requestedRunData = secondParent;
    runsData = [secondParent];
    rerender({ parentRunId: "parent-2" });

    expect(capturedRunQueryOptions).toMatchObject({
      query: { queryKey: ["run", "parent-2"] },
      request: { cache: "no-store" },
    });
    expect(capturedArtifactsQueryOptions).toMatchObject({
      query: { queryKey: ["artifacts", "parent-2", "mutable"] },
      request: { cache: "no-store" },
    });
    expect(capturedReviewQueryOptions).toMatchObject({
      query: { queryKey: ["broadcast-review", "parent-2"] },
      request: { cache: "no-store" },
    });
    expect(
      capturedArtifactCalls.every(
        ({ options }) =>
          (options.request as RequestInit | undefined)?.cache === "no-store",
      ),
    ).toBe(true);
    expect(
      capturedReviewCalls.every(
        ({ options }) =>
          (options.request as RequestInit | undefined)?.cache === "no-store",
      ),
    ).toBe(true);
    expect(capturedArtifactCalls.at(-1)?.runId).toBe("parent-2");
    expect(capturedReviewCalls.at(-1)?.runId).toBe("parent-2");
  });

  it("recovers an active child before the delayed parent update", () => {
    const parent = parentRun("trajectory_ready");
    const child = operationRun("render");
    requestedRunData = parent;
    runsData = [parent, child];

    const { result } = renderHook(
      () =>
        useBroadcastWorkflowController({
          parentRunId: "parent-1",
          enabled: true,
          language: "en",
        }),
      { wrapper: wrapper() },
    );

    expect(result.current.parent?.run_id).toBe("parent-1");
    expect(result.current.operation?.run_id).toBe("render-1");
    expect(result.current.recovery.state).toBe("rendering");
    expect(result.current.recovery.pollRunIds).toEqual([
      "parent-1",
      "render-1",
    ]);
  });

  it("scopes ready delivery artifacts to the immutable status generation", () => {
    const generation = "a".repeat(64);
    const parent = parentRun("ready", {
      broadcast: { status: "ready", status_generation: generation },
    });
    requestedRunData = parent;
    runsData = [parent];
    artifactsData = [
      {
        name: "broadcast.mp4",
        path: "outputs/parent-1/broadcast.mp4",
        kind: "video",
        exists: true,
        size_bytes: 123,
      },
    ];

    const { result } = renderHook(
      () =>
        useBroadcastWorkflowController({
          parentRunId: "parent-1",
          enabled: true,
          language: "en",
        }),
      { wrapper: wrapper() },
    );

    expect(result.current.delivery.queryIdentity.scope).toBe(
      `ready:${generation}`,
    );
    expect(result.current.delivery.listedArtifacts).toEqual(artifactsData);
    expect(result.current.delivery.urls["broadcast.mp4"]).toBe(
      `/api/runs/parent-1/artifacts/broadcast.mp4?status_generation=${generation}`,
    );
    expect(
      (capturedArtifactsQueryOptions?.query as { queryKey?: unknown[] })
        .queryKey,
    ).toEqual([
      "artifacts",
      "parent-1",
      { status_generation: generation },
      `ready:${generation}`,
    ]);
  });

  it("uses a synchronous lock to prevent duplicate review submission", async () => {
    const parent = parentRun("needs_review");
    requestedRunData = parent;
    runsData = [parent];
    reviewData = {
      run_id: "parent-1",
      status: "ready",
      queue_sha256: "b".repeat(64),
      review_item_count: 0,
      items: [],
    };
    const submission = deferred<{
      details: { review_decisions_sha256: string };
    }>();
    submitReviewMutate.mockReturnValue(submission.promise);

    const { result } = renderHook(
      () =>
        useBroadcastWorkflowController({
          parentRunId: "parent-1",
          enabled: true,
          language: "en",
        }),
      { wrapper: wrapper() },
    );

    let first!: Promise<void>;
    let duplicate!: Promise<void>;
    act(() => {
      first = result.current.actions.submitReview([], "operator");
      duplicate = result.current.actions.submitReview([], "operator");
    });
    expect(submitReviewMutate).toHaveBeenCalledTimes(1);

    submission.resolve({
      details: { review_decisions_sha256: "c".repeat(64) },
    });
    await act(async () => {
      await Promise.all([first, duplicate]);
    });

    expect(recomputeMutate).toHaveBeenCalledTimes(1);
    expect(recomputeMutate).toHaveBeenCalledWith({
      runId: "parent-1",
      data: { review_decisions_sha256: "c".repeat(64) },
    });
  });

  it("accepts hash-bound terminal-tail evidence before review and recompute", async () => {
    const parent = parentRun("needs_review");
    requestedRunData = parent;
    runsData = [parent];
    reviewData = {
      run_id: "parent-1",
      status: "ready",
      queue_sha256: "b".repeat(64),
      review_item_count: 0,
      items: [],
      terminal_tail_review: {
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
      },
    };
    submitReviewMutate.mockResolvedValue({
      details: { review_decisions_sha256: "c".repeat(64) },
    });
    const { result } = renderHook(
      () =>
        useBroadcastWorkflowController({
          parentRunId: "parent-1",
          enabled: true,
          language: "en",
        }),
      { wrapper: wrapper() },
    );

    await act(async () => {
      await result.current.actions.submitReview([], "quality-lead");
    });

    expect(submitTerminalTailReviewMutate).toHaveBeenCalledWith({
      runId: "parent-1",
      data: {
        decision: "accept_terminal_shortfall",
        reviewer_id: "quality-lead",
        evidence_sha256: "5".repeat(64),
      },
    });
    expect(submitTerminalTailReviewMutate.mock.invocationCallOrder[0]).toBeLessThan(
      submitReviewMutate.mock.invocationCallOrder[0],
    );
    expect(submitReviewMutate.mock.invocationCallOrder[0]).toBeLessThan(
      recomputeMutate.mock.invocationCallOrder[0],
    );
  });

  it("acknowledges the terminal tail without fabricating review decisions when the queue is missing", async () => {
    const parent = parentRun("needs_review", {
      broadcast: {
        status: "needs_review",
        blocking_reasons: [
          "terminal_decoder_shortfall_requires_operator_review",
          "missing_qualified_selective_review_queue",
        ],
      },
    });
    requestedRunData = parent;
    runsData = [parent];
    reviewData = {
      run_id: "parent-1",
      status: "needs_review",
      reason: "missing_qualified_selective_review_queue",
      queue_sha256: null,
      review_item_count: 0,
      items: [],
      terminal_tail_review: {
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
      },
    };
    const submission = deferred<{
      details: { terminal_tail_review_sha256: string };
    }>();
    submitTerminalTailReviewMutate.mockReturnValue(submission.promise);
    const { result } = renderHook(
      () =>
        useBroadcastWorkflowController({
          parentRunId: "parent-1",
          enabled: true,
          language: "en",
        }),
      { wrapper: wrapper() },
    );

    let first!: Promise<void>;
    let duplicate!: Promise<void>;
    act(() => {
      first = result.current.actions.acceptTerminalTailReview(" quality-lead ");
      duplicate =
        result.current.actions.acceptTerminalTailReview("quality-lead");
    });
    expect(submitTerminalTailReviewMutate).toHaveBeenCalledTimes(1);
    expect(submitTerminalTailReviewMutate).toHaveBeenCalledWith({
      runId: "parent-1",
      data: {
        decision: "accept_terminal_shortfall",
        reviewer_id: "quality-lead",
        evidence_sha256: "5".repeat(64),
      },
    });

    submission.resolve({
      details: { terminal_tail_review_sha256: "6".repeat(64) },
    });
    await act(async () => {
      await Promise.all([first, duplicate]);
    });

    expect(submitReviewMutate).not.toHaveBeenCalled();
    expect(recomputeMutate).not.toHaveBeenCalled();
    expect(
      parent.broadcast?.blocking_reasons,
    ).toContain("missing_qualified_selective_review_queue");
  });

  it("does not auto-recompute persisted decisions while terminal-tail review is required", async () => {
    const parent = parentRun("needs_review");
    requestedRunData = parent;
    runsData = [parent];
    artifactsData = [
      {
        name: "review_decisions.json",
        path: "outputs/parent-1/review_decisions.json",
        kind: "json",
        exists: true,
        size_bytes: 3,
      },
    ];
    reviewData = {
      run_id: "parent-1",
      status: "ready",
      items: [],
      terminal_tail_review: {
        status: "required",
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
      },
    };

    const { result } = renderHook(
      () =>
        useBroadcastWorkflowController({
          parentRunId: "parent-1",
          enabled: true,
          language: "en",
        }),
      { wrapper: wrapper() },
    );
    await act(async () => Promise.resolve());

    expect(result.current.review.recomputeRecoveryMode).toBe("none");
    expect(recomputeMutate).not.toHaveBeenCalled();
  });

  it("does not recover stale decisions after tail acceptance while the qualified queue is missing", async () => {
    const parent = parentRun("needs_review", {
      broadcast: {
        status: "needs_review",
        blocking_reasons: ["missing_qualified_selective_review_queue"],
      },
    });
    requestedRunData = parent;
    runsData = [parent];
    artifactsData = [
      {
        name: "review_decisions.json",
        path: "outputs/parent-1/review_decisions.json",
        kind: "json",
        exists: true,
        size_bytes: 3,
      },
    ];
    reviewData = {
      run_id: "parent-1",
      status: "needs_review",
      reason: "missing_qualified_selective_review_queue",
      queue_sha256: null,
      review_item_count: 0,
      items: [],
      terminal_tail_review: {
        status: "accepted",
        decision: "accept_terminal_shortfall",
        reviewer_id: "quality-lead",
        reviewed_at: "2026-07-15T12:05:00Z",
        acknowledgement_sha256: "6".repeat(64),
      },
    };
    const fetchArtifact = vi.fn();
    vi.stubGlobal("fetch", fetchArtifact);

    const { result } = renderHook(
      () =>
        useBroadcastWorkflowController({
          parentRunId: "parent-1",
          enabled: true,
          language: "en",
        }),
      { wrapper: wrapper() },
    );
    await act(async () => Promise.resolve());

    expect(result.current.review.recomputeRecoveryMode).toBe("none");
    expect(fetchArtifact).not.toHaveBeenCalled();
    expect(recomputeMutate).not.toHaveBeenCalled();
  });

  it("waits for terminal-tail state before recovering persisted decisions exactly once", async () => {
    const parent = parentRun("needs_review");
    requestedRunData = parent;
    runsData = [parent];
    artifactsData = [
      {
        name: "review_decisions.json",
        path: "outputs/parent-1/review_decisions.json",
        kind: "json",
        exists: true,
        size_bytes: 3,
      },
    ];
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(new Uint8Array([1, 2, 3]), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );

    const { result, rerender } = renderHook(
      () =>
        useBroadcastWorkflowController({
          parentRunId: "parent-1",
          enabled: true,
          language: "en",
        }),
      { wrapper: wrapper() },
    );

    await act(async () => Promise.resolve());
    expect(result.current.review.recomputeRecoveryMode).toBe("none");
    expect(recomputeMutate).not.toHaveBeenCalled();
    expect(fetch).not.toHaveBeenCalled();

    reviewData = {
      run_id: "parent-1",
      status: "ready",
      queue_sha256: "b".repeat(64),
      items: [],
      terminal_tail_review: { status: "not_required" },
    };
    rerender();

    await waitFor(() => expect(recomputeMutate).toHaveBeenCalledTimes(1));
    expect(result.current.review.recomputeRecoveryMode).toBe("auto");
    expect(fetch).toHaveBeenCalledWith(
      "/api/runs/parent-1/artifacts/review_decisions.json",
      expect.objectContaining({
        cache: "no-store",
        headers: {
          Accept: "application/octet-stream, application/json",
          "Cache-Control": "no-store",
        },
      }),
    );
    await act(async () => Promise.resolve());
    expect(recomputeMutate).toHaveBeenCalledTimes(1);
  });

  it("maps render conflicts to stale evidence and refreshes recovery queries", async () => {
    const parent = parentRun("trajectory_ready");
    requestedRunData = parent;
    runsData = [parent];
    renderMutate.mockRejectedValue({ status: 409 });
    const client = new QueryClient();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const TestWrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
    const { result } = renderHook(
      () =>
        useBroadcastWorkflowController({
          parentRunId: "parent-1",
          enabled: true,
          language: "en",
        }),
      { wrapper: TestWrapper },
    );

    await act(async () => {
      await result.current.actions.render({
        trajectory_generation_id: "trajectory-1",
        target_width: 1_920,
        target_height: 1_080,
      });
    });

    expect(result.current.errors.action?.code).toBe("staleEvidence");
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["runs"] });
    expect(invalidate).toHaveBeenCalledWith({
      queryKey: ["run", "parent-1"],
    });
  });

  it("ignores a render response after switching to a different parent", async () => {
    const firstParent = parentRun("trajectory_ready");
    requestedRunData = firstParent;
    runsData = [firstParent];
    const queued = deferred<{ run_id: string }>();
    renderMutate.mockReturnValue(queued.promise);
    const client = new QueryClient();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const TestWrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
    const { result, rerender } = renderHook(
      ({ parentRunId }: { parentRunId: string }) =>
        useBroadcastWorkflowController({
          parentRunId,
          enabled: true,
          language: "en",
        }),
      { initialProps: { parentRunId: "parent-1" }, wrapper: TestWrapper },
    );

    let pending!: Promise<void>;
    act(() => {
      pending = result.current.actions.render({
        trajectory_generation_id: "trajectory-1",
        target_width: 1_920,
        target_height: 1_080,
      });
    });
    const secondParent = parentRun("trajectory_ready", {
      run_id: "parent-2",
      output_dir: "outputs/parent-2",
    });
    requestedRunData = secondParent;
    runsData = [secondParent];
    rerender({ parentRunId: "parent-2" });
    const invalidationsBeforeOldResponse = invalidate.mock.calls.length;

    queued.resolve({ run_id: "render-old" });
    await act(async () => {
      await pending;
    });

    expect(invalidate).toHaveBeenCalledTimes(invalidationsBeforeOldResponse);
    expect(result.current.parent?.run_id).toBe("parent-2");
    expect(result.current.errors.action).toBeNull();
    await waitFor(() => expect(result.current.pending.render).toBe(false));
  });

  it("does not refresh queries after an in-flight action outlives unmount", async () => {
    const parent = parentRun("trajectory_ready");
    requestedRunData = parent;
    runsData = [parent];
    const queued = deferred<{ run_id: string }>();
    renderMutate.mockReturnValue(queued.promise);
    const client = new QueryClient();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const TestWrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
    const { result, unmount } = renderHook(
      () =>
        useBroadcastWorkflowController({
          parentRunId: "parent-1",
          enabled: true,
          language: "en",
        }),
      { wrapper: TestWrapper },
    );

    let pending!: Promise<void>;
    act(() => {
      pending = result.current.actions.render({
        trajectory_generation_id: "trajectory-1",
        target_width: 1_920,
        target_height: 1_080,
      });
    });
    unmount();
    const invalidationsBeforeOldResponse = invalidate.mock.calls.length;

    queued.resolve({ run_id: "render-after-unmount" });
    await pending;

    expect(invalidate).toHaveBeenCalledTimes(invalidationsBeforeOldResponse);
  });
});
