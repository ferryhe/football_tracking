import { StrictMode, type ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { LanguageProvider } from "@/contexts/LanguageContext";

const manifestSha256 = "a".repeat(64);
const queueSha256 = "b".repeat(64);

const host = vi.hoisted(() => ({
  cancel: vi.fn(),
  evidenceOptions: null as null | Record<string, unknown>,
  navigate: vi.fn(),
  prepare: vi.fn(),
  refresh: vi.fn(),
  resetCancel: vi.fn(),
  resetPrepare: vi.fn(),
  reviewOptions: null as null | Record<string, unknown>,
}));

const parentRun = {
  run_id: "parent-run",
  source: "broadcast_hybrid",
  status: "completed",
  created_at: "2026-07-15T10:00:00Z",
  output_dir: "outputs/parent-run",
  artifacts: [],
  broadcast: {
    status: "needs_review",
    status_generation: "status-generation-1",
    blocking_reasons: [] as string[],
    limitations: [],
  },
};

let cancelMutation: Record<string, unknown>;
let evidenceQuery: Record<string, unknown>;
let importMutation: Record<string, unknown>;
let operationQuery: Record<string, unknown>;
let reconfirmMutation: Record<string, unknown>;
let reviewQuery: Record<string, unknown>;

vi.mock("wouter", () => ({
  useLocation: () => ["/broadcast", host.navigate],
  useSearch: () => "?run=parent-run",
}));

vi.mock("@/components/broadcast/BroadcastReviewStep", () => ({
  BroadcastReviewStep: () => (
    <div data-testid="existing-broadcast-review">existing manual review</div>
  ),
}));

vi.mock("@/components/broadcast/BroadcastSetupStep", () => ({
  BroadcastSetupStep: () => <div>broadcast setup</div>,
}));

vi.mock("@/components/broadcast/BroadcastRenderStep", () => ({
  BROADCAST_DELIVERY_ARTIFACTS: [],
  BroadcastRenderStep: () => <div>broadcast render</div>,
}));

vi.mock("@/lib/broadcastWorkflow", () => ({
  broadcastArtifactQueryIdentity: () => ({
    scope: "needs_review",
    deliveryReady: false,
  }),
  broadcastCancellationTarget: () => null,
  broadcastRecomputeRecoveryMode: () => "none",
  buildBroadcastCreateRequest: () => ({ ok: false, messages: [] }),
  localizeBroadcastWorkflowMessage: (message: string) => message,
  mergeBroadcastArtifacts: () => [],
  recoverBroadcastWorkflowRun: () => ({
    state: "needs_review",
    parentRun,
    operationRun: null,
    pollRunIds: [],
    messages: [],
  }),
  resolveBroadcastMontageArtifact: () => ({ ok: false, messages: [] }),
  validateAndBuildBroadcastReviewActions: () => ({
    ok: false,
    messages: [],
  }),
}));

function idleMutation() {
  return {
    error: null,
    isPending: false,
    mutateAsync: vi.fn(),
  };
}

vi.mock("@workspace/api-client-react", () => ({
  getGetArtifactUrl: (runId: string, name: string) =>
    `/api/runs/${runId}/artifacts/${name}`,
  getGetBroadcastReviewEvidenceQueryKey: (runId: string) => [
    "review-evidence",
    runId,
  ],
  getGetBroadcastReviewWindowsQueryKey: (runId: string) => [
    "review-windows",
    runId,
  ],
  getGetRunQueryKey: (runId: string) => ["run", runId],
  getListArtifactsQueryKey: (runId: string) => ["artifacts", runId],
  getListRunsQueryKey: () => ["runs"],
  useCancelRun: () => cancelMutation,
  useCreateRun: idleMutation,
  useGetBroadcastReviewEvidence: (
    _runId: string,
    options: Record<string, unknown>,
  ) => {
    host.evidenceOptions = options;
    return evidenceQuery;
  },
  useGetBroadcastReviewWindows: (
    _runId: string,
    options: Record<string, unknown>,
  ) => {
    host.reviewOptions = options;
    return reviewQuery;
  },
  useGetRun: (runId: string) =>
    runId === "parent-run"
      ? { data: parentRun, error: null, isLoading: false }
      : operationQuery,
  useImportBroadcastReviewEvidence: () => importMutation,
  useListArtifacts: () => ({
    data: [],
    error: null,
    isSuccess: true,
  }),
  useListRuns: () => ({
    data: [parentRun],
    error: null,
    isLoading: false,
  }),
  useReconfirmBroadcastConfigLineage: () => reconfirmMutation,
  useRecomputeBroadcastTrajectory: idleMutation,
  useRenderBroadcastHybrid: idleMutation,
  useSubmitBroadcastReviewActions: idleMutation,
  useSubmitBroadcastTerminalTailReview: idleMutation,
}));

import BroadcastPage from "./broadcast";

function wrapper({ children }: { children: ReactNode }) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return (
    <QueryClientProvider client={queryClient}>
      <LanguageProvider>{children}</LanguageProvider>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  parentRun.broadcast.blocking_reasons = [];
  host.evidenceOptions = null;
  host.reviewOptions = null;
  host.prepare.mockResolvedValue({ run_id: "import-job-1" });
  host.cancel.mockResolvedValue(undefined);
  host.refresh.mockResolvedValue(undefined);
  reconfirmMutation = idleMutation();
  evidenceQuery = {
    data: {
      run_id: "parent-run",
      status: "not_available",
      bundles: [],
      blocker_code: "review_evidence_bundle_not_available",
      recovery_action: "provision_qualified_review_evidence",
    },
    error: null,
    isLoading: false,
    refetch: host.refresh,
  };
  operationQuery = { data: undefined, error: null, isLoading: false };
  importMutation = {
    error: null,
    isPending: false,
    mutateAsync: host.prepare,
    reset: host.resetPrepare,
  };
  cancelMutation = {
    error: null,
    isPending: false,
    mutateAsync: host.cancel,
    reset: host.resetCancel,
  };
  reviewQuery = {
    data: {
      run_id: "parent-run",
      status: "ready",
      queue_sha256: queueSha256,
      review_item_count: 0,
      items: [],
    },
    error: null,
    isError: false,
    isLoading: false,
    isSuccess: true,
  };
});

describe("legacy Broadcast review-evidence host", () => {
  it("wraps the run identity and long blocking reasons without losing accessible text", () => {
    const longBlockingReason = `blocking_reason_${"x".repeat(160)}`;
    parentRun.broadcast.blocking_reasons = [longBlockingReason];
    render(<BroadcastPage />, { wrapper });

    expect(screen.getByText("parent-run")).toHaveClass(
      "max-w-full",
      "whitespace-normal",
      "break-all",
    );
    const reason = screen.getByText(longBlockingReason);
    expect(reason).toHaveClass("min-w-0", "[overflow-wrap:anywhere]");
    const alert = screen
      .getByText("Blocking reasons")
      .closest('[role="alert"]');
    expect(alert).not.toBeNull();
    expect(alert).toHaveTextContent("Blocking reasons");
    expect(alert).toHaveTextContent(longBlockingReason);
  });

  it("moves from unavailable through explicit prepare, progress/cancel, and ready review handoff", async () => {
    const user = userEvent.setup();
    const renderPage = () => (
      <StrictMode>
        <BroadcastPage />
      </StrictMode>
    );
    const view = render(renderPage(), { wrapper });

    expect(screen.getByRole("status")).toHaveTextContent(
      "No compatible evidence bundle",
    );
    expect((host.reviewOptions?.query as { enabled?: boolean }).enabled).toBe(
      true,
    );
    expect(screen.queryByTestId("existing-broadcast-review")).toBeNull();

    evidenceQuery = {
      ...evidenceQuery,
      data: {
        run_id: "parent-run",
        status: "available",
        bundles: [
          {
            status: "available",
            bundle_id: "bundle-qualified-1",
            bundle_manifest_sha256: manifestSha256,
            total_size_bytes: 64 * 1024 * 1024,
            required_free_bytes: 96 * 1024 * 1024,
            available_free_bytes: 256 * 1024 * 1024,
            attempt_quota_bytes: 128 * 1024 * 1024,
            capacity_status: "sufficient",
            inbox_entry: "bundle-qualified-1",
          },
        ],
      },
    };
    view.rerender(renderPage());
    expect(host.prepare).not.toHaveBeenCalled();
    expect(screen.getByText("Required free space")).toBeVisible();
    expect(screen.getByText("96.0 MB")).toBeVisible();
    expect(screen.getByText("Available free space")).toBeVisible();
    expect(screen.getByText("256.0 MB")).toBeVisible();
    expect(screen.getByText("Attempt quota")).toBeVisible();
    expect(screen.getByText("128.0 MB")).toBeVisible();
    await user.click(
      screen.getByRole("button", { name: "Prepare review evidence" }),
    );
    expect(host.prepare).toHaveBeenCalledWith({
      runId: "parent-run",
      data: {
        bundle_id: "bundle-qualified-1",
        bundle_manifest_sha256: manifestSha256,
      },
    });

    evidenceQuery = {
      ...evidenceQuery,
      data: {
        run_id: "parent-run",
        status: "copying",
        active_job_id: "import-job-1",
        stage: "copying",
        progress_percent: 37.5,
        can_cancel: true,
        bundles: [],
      },
    };
    operationQuery = {
      ...operationQuery,
      data: {
        run_id: "import-job-1",
        broadcast: {
          request: {
            bundle_id: "bundle-qualified-1",
            bundle_manifest_sha256: manifestSha256,
          },
        },
      },
    };
    view.rerender(renderPage());
    expect(
      screen.getByRole("progressbar", { name: "Evidence import progress" }),
    ).toHaveAttribute("aria-valuenow", "37.5");
    await user.click(screen.getByRole("button", { name: "Cancel import" }));
    expect(host.cancel).toHaveBeenCalledWith({ runId: "import-job-1" });

    evidenceQuery = {
      ...evidenceQuery,
      data: {
        run_id: "parent-run",
        status: "ready",
        generation_id: "review-evidence-generation-1",
        queue_sha256: queueSha256,
        bundles: [],
      },
    };
    reviewQuery = {
      data: {
        run_id: "parent-run",
        status: "ready",
        queue_sha256: queueSha256,
        review_item_count: 0,
        items: [],
      },
      error: null,
      isError: false,
      isLoading: false,
    };
    view.rerender(renderPage());

    expect(screen.getByText("review-evidence-generation-1")).toBeVisible();
    expect(screen.getByText(queueSha256)).toBeVisible();
    expect(screen.getByTestId("existing-broadcast-review")).toBeVisible();
    expect((host.reviewOptions?.query as { enabled?: boolean }).enabled).toBe(
      true,
    );

    await user.click(screen.getByRole("button", { name: "Refresh state" }));
    expect(host.refresh).toHaveBeenCalledOnce();
  });

  it("shows an insufficient bundle's capacity and prevents preparation", () => {
    evidenceQuery = {
      ...evidenceQuery,
      data: {
        run_id: "parent-run",
        status: "available",
        bundles: [
          {
            status: "available",
            bundle_id: "bundle-insufficient",
            bundle_manifest_sha256: manifestSha256,
            required_free_bytes: 96 * 1024 * 1024,
            available_free_bytes: 32 * 1024 * 1024,
            attempt_quota_bytes: 128 * 1024 * 1024,
            capacity_status: "insufficient",
            error_code: "insufficient_capacity",
            inbox_entry: "bundle-insufficient",
          },
        ],
      },
    };
    render(<BroadcastPage />, { wrapper });

    expect(screen.getByText("32.0 MB")).toBeVisible();
    expect(screen.getByRole("alert")).toHaveTextContent(
      "insufficient_capacity",
    );
    expect(
      screen.getByRole("button", { name: "Prepare review evidence" }),
    ).toBeDisabled();
    expect(host.prepare).not.toHaveBeenCalled();
  });

  it.each(["load", "prepare", "cancel"] as const)(
    "announces a visible %s error from the evidence host",
    (kind) => {
      if (kind === "load") {
        evidenceQuery = {
          ...evidenceQuery,
          error: new Error(`${kind} evidence error`),
        };
      } else if (kind === "prepare") {
        importMutation = {
          ...importMutation,
          error: new Error(`${kind} evidence error`),
        };
      } else {
        cancelMutation = {
          ...cancelMutation,
          error: new Error(`${kind} evidence error`),
        };
      }
      render(<BroadcastPage />, { wrapper });

      const detail = screen.getByText(`${kind} evidence error`);
      expect(detail.closest('[role="alert"]')).not.toBeNull();
    },
  );
});
