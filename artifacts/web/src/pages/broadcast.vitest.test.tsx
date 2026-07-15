import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type {
  ArtifactSummary,
  BroadcastReviewWindowsResponse,
  RunRecord,
} from "@workspace/api-client-react";

const navigate = vi.fn();
const createRun = vi.fn();
const submitReview = vi.fn();
const submitTerminalTailReview = vi.fn();
const recompute = vi.fn();
const renderBroadcast = vi.fn();
const cancelRun = vi.fn();
const buildRequest = vi.fn();
const requestedRunIds: string[] = [];
const artifactRunIds: string[] = [];
const reviewRunIds: string[] = [];
let search = "";
let requestedRunData: RunRecord | undefined;
let runsData: RunRecord[] = [];
let artifactsData: ArtifactSummary[] = [];
let reviewData: BroadcastReviewWindowsResponse | undefined;

const queryBase = {
  isPending: false,
  isLoading: false,
  isFetching: false,
  isSuccess: true,
  isError: false,
  error: null,
  refetch: vi.fn(),
};

vi.mock("wouter", () => ({
  useLocation: () => ["/broadcast", navigate],
  useSearch: () => search,
}));

vi.mock("@workspace/api-client-react", () => ({
  getGetArtifactUrl: (
    runId: string,
    name: string,
    params?: { status_generation?: string | null },
  ) =>
    `/api/runs/${encodeURIComponent(runId)}/artifacts/${encodeURIComponent(name)}${
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
  useCreateRun: () => ({ isPending: false, mutateAsync: createRun }),
  useGetRun: (runId: string) => {
    requestedRunIds.push(runId);
    return { ...queryBase, data: requestedRunData };
  },
  useListRuns: () => ({ ...queryBase, data: runsData }),
  useListArtifacts: (runId: string) => {
    artifactRunIds.push(runId);
    return { ...queryBase, data: artifactsData };
  },
  useGetBroadcastReviewWindows: (runId: string) => {
    reviewRunIds.push(runId);
    return { ...queryBase, data: reviewData };
  },
  useSubmitBroadcastReviewActions: () => ({
    isPending: false,
    mutateAsync: submitReview,
  }),
  useSubmitBroadcastTerminalTailReview: () => ({
    isPending: false,
    mutateAsync: submitTerminalTailReview,
  }),
  useRecomputeBroadcastTrajectory: () => ({
    isPending: false,
    mutateAsync: recompute,
  }),
  useRenderBroadcastHybrid: () => ({
    isPending: false,
    mutateAsync: renderBroadcast,
  }),
  useCancelRun: () => ({ isPending: false, mutateAsync: cancelRun }),
}));

vi.mock("@/contexts/LanguageContext", () => ({
  useLanguage: () => ({
    language: "en",
    t: {
      broadcast: {
        title: "Broadcast workflow",
        subtitle: "Build a broadcast",
        setupStep: "Setup",
        reviewStep: "Review",
        renderStep: "Render",
        submitFailed: "Submit failed",
        recomputeFailed: "Recompute failed",
        renderFailed: "Render failed",
        cancelFailed: "Cancel failed",
        staleEvidence: "Stale evidence",
        loadFailed: "Load failed",
        startFailed: "Start failed",
        blockingReasons: "Blocking reasons",
        refresh: "Refresh",
        loading: "Loading",
        reviewerId: "Reviewer",
      },
    },
  }),
}));

vi.mock("@/lib/broadcastWorkflow", async (importOriginal) => {
  const original =
    await importOriginal<typeof import("@/lib/broadcastWorkflow")>();
  return {
    ...original,
    buildBroadcastCreateRequest: (...args: unknown[]) => buildRequest(...args),
  };
});

vi.mock("@/components/broadcast/BroadcastSetupStep", () => ({
  BroadcastSetupStep: ({
    onSubmit,
  }: {
    onSubmit: (value: unknown) => void;
  }) => (
    <button type="button" onClick={() => onSubmit({ input: "fixture" })}>
      Start setup
    </button>
  ),
}));

vi.mock("@/components/broadcast/BroadcastReviewStep", () => ({
  BroadcastReviewStep: ({
    onSubmit,
    isSubmitting,
  }: {
    onSubmit?: (decisions: []) => void;
    isSubmitting?: boolean;
  }) => (
    <button
      type="button"
      disabled={isSubmitting}
      onClick={() => onSubmit?.([])}
    >
      Submit zero review
    </button>
  ),
}));

vi.mock("@/components/broadcast/BroadcastRenderStep", () => ({
  BROADCAST_DELIVERY_ARTIFACTS: [
    "broadcast.mp4",
    "broadcast_quality_report.json",
    "camera_target.csv",
    "ball_track.v2.csv",
    "review_decisions.json",
    "action_track.csv",
    "candidate_classifications.jsonl",
    "ball_candidates.jsonl",
  ],
  BroadcastRenderStep: ({
    run,
    artifactUrls,
    onRender,
    disabled,
  }: {
    run: RunRecord;
    artifactUrls: Record<string, string>;
    onRender: (request: Record<string, unknown>) => void;
    disabled?: boolean;
  }) => (
    <div>
      <p data-testid="render-state">{run.broadcast?.status}</p>
      <p data-testid="ready-video-url">{artifactUrls["broadcast.mp4"]}</p>
      <button
        type="button"
        disabled={disabled}
        onClick={() =>
          onRender({
            trajectory_generation_id: run.broadcast?.trajectory_generation_id,
            target_width: 1_920,
            target_height: 1_080,
          })
        }
      >
        Render once
      </button>
    </div>
  ),
}));

import BroadcastPage from "./broadcast";

const TRAJECTORY_GENERATION_ID = `trajectory-${"a".repeat(24)}`;
const STATUS_GENERATION = "b".repeat(64);
const REVIEW_DIGEST = "c".repeat(64);
const DELIVERY_ARTIFACTS = [
  "broadcast.mp4",
  "broadcast_quality_report.json",
  "camera_target.csv",
  "ball_track.v2.csv",
  "review_decisions.json",
  "action_track.csv",
  "candidate_classifications.jsonl",
  "ball_candidates.jsonl",
] as const;

function parentRun(
  status: "needs_review" | "trajectory_ready" | "ready",
): RunRecord {
  return {
    run_id: "parent one",
    source: "broadcast_hybrid",
    status: "completed",
    created_at: "2026-07-15T12:00:00Z",
    output_dir: "outputs/parent-one",
    artifacts: [],
    broadcast: {
      status,
      ...(status === "trajectory_ready" || status === "ready"
        ? { trajectory_generation_id: TRAJECTORY_GENERATION_ID }
        : {}),
      ...(status === "ready" ? { status_generation: STATUS_GENERATION } : {}),
    },
  };
}

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const Wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return render(<BroadcastPage />, { wrapper: Wrapper });
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

beforeEach(() => {
  vi.clearAllMocks();
  search = "";
  requestedRunData = undefined;
  runsData = [];
  artifactsData = [];
  reviewData = undefined;
  requestedRunIds.length = 0;
  artifactRunIds.length = 0;
  reviewRunIds.length = 0;
  buildRequest.mockReturnValue({ ok: true, value: { source: "fixture" } });
  createRun.mockResolvedValue({ run_id: "created parent" });
  recompute.mockResolvedValue({ run_id: "recompute-child" });
  renderBroadcast.mockResolvedValue({ run_id: "render-child" });
  cancelRun.mockResolvedValue({ status: "cancelled" });
});

describe("BroadcastPage controller integration", () => {
  it("keeps Setup on the legacy route and navigates to the created run", async () => {
    renderPage();

    fireEvent.click(screen.getByRole("button", { name: "Start setup" }));

    await waitFor(() =>
      expect(createRun).toHaveBeenCalledWith({ data: { source: "fixture" } }),
    );
    expect(navigate).toHaveBeenCalledWith("/broadcast?run=created%20parent");
  });

  it("passes the decoded legacy run query to generated API hooks", () => {
    search = "?run=parent%20one";
    requestedRunData = parentRun("needs_review");
    runsData = [requestedRunData];
    reviewData = {
      run_id: "parent one",
      status: "ready",
      queue_sha256: "d".repeat(64),
      review_item_count: 0,
      items: [],
    };
    renderPage();

    expect(requestedRunIds).toContain("parent one");
    expect(artifactRunIds).toContain("parent one");
    expect(reviewRunIds).toContain("parent one");
    expect(screen.getByText("parent one")).toBeInTheDocument();
  });

  it("wires zero-review through recompute and render, then recovers the generation-bound ready parent once", async () => {
    search = "?run=parent%20one";
    requestedRunData = parentRun("needs_review");
    runsData = [requestedRunData];
    reviewData = {
      run_id: "parent one",
      status: "ready",
      queue_sha256: "d".repeat(64),
      review_item_count: 0,
      items: [],
    };
    const reviewSubmission = deferred<{
      details: { review_decisions_sha256: string };
    }>();
    submitReview.mockReturnValue(reviewSubmission.promise);
    const view = renderPage();

    const reviewButton = screen.getByRole("button", {
      name: "Submit zero review",
    });
    fireEvent.click(reviewButton);
    fireEvent.click(reviewButton);
    expect(submitReview).toHaveBeenCalledTimes(1);
    expect(submitReview).toHaveBeenCalledWith({
      runId: "parent one",
      data: { actions: [] },
    });

    reviewSubmission.resolve({
      details: { review_decisions_sha256: REVIEW_DIGEST },
    });
    await waitFor(() => expect(recompute).toHaveBeenCalledTimes(1));
    expect(recompute).toHaveBeenCalledWith({
      runId: "parent one",
      data: { review_decisions_sha256: REVIEW_DIGEST },
    });

    requestedRunData = parentRun("trajectory_ready");
    runsData = [requestedRunData];
    view.rerender(<BroadcastPage />);
    expect(screen.getByTestId("render-state")).toHaveTextContent(
      "trajectory_ready",
    );

    const renderSubmission = deferred<{ run_id: string }>();
    renderBroadcast.mockReturnValue(renderSubmission.promise);
    const renderButton = screen.getByRole("button", { name: "Render once" });
    fireEvent.click(renderButton);
    fireEvent.click(renderButton);
    expect(renderBroadcast).toHaveBeenCalledTimes(1);
    expect(renderBroadcast).toHaveBeenCalledWith({
      runId: "parent one",
      data: {
        trajectory_generation_id: TRAJECTORY_GENERATION_ID,
        target_width: 1_920,
        target_height: 1_080,
      },
    });
    renderSubmission.resolve({ run_id: "render-child" });
    await waitFor(() => expect(renderBroadcast).toHaveBeenCalledTimes(1));

    requestedRunData = parentRun("ready");
    runsData = [requestedRunData];
    artifactsData = DELIVERY_ARTIFACTS.map((name) => ({
      name,
      path: `sealed/${name}`,
      kind: name === "broadcast.mp4" ? "video" : "file",
      exists: true,
      size_bytes: 100,
    }));
    view.rerender(<BroadcastPage />);

    expect(screen.getByTestId("render-state")).toHaveTextContent("ready");
    expect(screen.getByRole("button", { name: "Render once" })).toBeDisabled();
    expect(screen.getByTestId("ready-video-url")).toHaveTextContent(
      `/api/runs/parent%20one/artifacts/broadcast.mp4?status_generation=${STATUS_GENERATION}`,
    );
    expect(submitReview).toHaveBeenCalledTimes(1);
    expect(recompute).toHaveBeenCalledTimes(1);
    expect(renderBroadcast).toHaveBeenCalledTimes(1);
    expect(navigate).not.toHaveBeenCalled();
    expect(search).toBe("?run=parent%20one");
  });
});
