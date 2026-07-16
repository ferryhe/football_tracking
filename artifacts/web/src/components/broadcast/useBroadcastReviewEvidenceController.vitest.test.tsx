import { StrictMode, type ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  act,
  render,
  renderHook,
  screen,
  waitFor,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { BroadcastReviewEvidenceStep } from "./BroadcastReviewEvidenceStep";
import {
  broadcastReviewEvidencePollingInterval,
  useBroadcastReviewEvidenceController,
  type UseBroadcastReviewEvidenceControllerOptions,
} from "./useBroadcastReviewEvidenceController";
import { translations } from "@/lib/i18n";

const manifestSha256 = "a".repeat(64);

const api = vi.hoisted(() => ({
  cancel: vi.fn(),
  importEvidence: vi.fn(),
  refetchEvidence: vi.fn(),
  resetCancel: vi.fn(),
  resetImport: vi.fn(),
  useCancelRun: vi.fn(),
  useGetBroadcastReviewEvidence: vi.fn(),
  useGetRun: vi.fn(),
  useImportBroadcastReviewEvidence: vi.fn(),
}));

vi.mock("@workspace/api-client-react", () => ({
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
  useCancelRun: api.useCancelRun,
  useGetBroadcastReviewEvidence: api.useGetBroadcastReviewEvidence,
  useGetRun: api.useGetRun,
  useImportBroadcastReviewEvidence: api.useImportBroadcastReviewEvidence,
}));

let evidenceQuery: Record<string, unknown>;
let operationQuery: Record<string, unknown>;
let importMutation: Record<string, unknown>;
let cancelMutation: Record<string, unknown>;
let queryClient: QueryClient;

function availableBundle(bundleId = "bundle-qualified-1") {
  return {
    status: "available" as const,
    bundle_id: bundleId,
    bundle_manifest_sha256: manifestSha256,
    inbox_entry: bundleId,
  };
}

function wrapper({ children }: { children: ReactNode }) {
  return (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
}

function renderController(
  enabled = true,
  overrides: Partial<
    Omit<UseBroadcastReviewEvidenceControllerOptions, "runId" | "enabled">
  > = {},
) {
  const { messages, ...rest } = overrides;
  return renderHook(
    () =>
      useBroadcastReviewEvidenceController({
        runId: "parent-run",
        enabled,
        ...rest,
        messages: {
          ambiguousBundleRecovery: "Keep exactly one compatible bundle.",
          retryBundleUnavailableRecovery:
            "Restore the same compatible bundle before retrying.",
          ...messages,
        },
      }),
    { wrapper },
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  vi.spyOn(queryClient, "invalidateQueries").mockResolvedValue(undefined);

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
    refetch: api.refetchEvidence,
  };
  operationQuery = { data: undefined, error: null, isLoading: false };
  importMutation = {
    error: null,
    isPending: false,
    mutateAsync: api.importEvidence,
    reset: api.resetImport,
  };
  cancelMutation = {
    error: null,
    isPending: false,
    mutateAsync: api.cancel,
    reset: api.resetCancel,
  };
  api.refetchEvidence.mockResolvedValue(undefined);
  api.importEvidence.mockResolvedValue({ run_id: "import-job-1" });
  api.cancel.mockResolvedValue(undefined);
  api.useGetBroadcastReviewEvidence.mockImplementation(() => evidenceQuery);
  api.useGetRun.mockImplementation(() => operationQuery);
  api.useImportBroadcastReviewEvidence.mockImplementation(() => importMutation);
  api.useCancelRun.mockImplementation(() => cancelMutation);
});

describe("useBroadcastReviewEvidenceController", () => {
  it("queries only for an enabled parent and polls only active import states", () => {
    renderController(false);

    const [, disabledOptions] =
      api.useGetBroadcastReviewEvidence.mock.calls.at(-1)!;
    expect(disabledOptions.query.enabled).toBe(false);
    expect(api.importEvidence).not.toHaveBeenCalled();
    expect(
      broadcastReviewEvidencePollingInterval({ status: "available" }),
    ).toBe(false);
    expect(broadcastReviewEvidencePollingInterval({ status: "copying" })).toBe(
      2_000,
    );
    expect(
      broadcastReviewEvidencePollingInterval({ status: "committing" }),
    ).toBe(2_000);
  });

  it("maps not-available and fails closed when compatible bundles are ambiguous", () => {
    const { result, rerender } = renderController();
    expect(result.current.stepProps.state.status).toBe("not_available");
    expect(result.current.stepProps.onPrepare).toBeUndefined();

    evidenceQuery = {
      ...evidenceQuery,
      data: {
        run_id: "parent-run",
        status: "available",
        bundles: [availableBundle("bundle-a"), availableBundle("bundle-b")],
      },
    };
    rerender();

    expect(result.current.stepProps.state).toMatchObject({
      status: "blocked",
      blockerCode: "ambiguous_compatible_review_evidence_bundles",
      recoveryAction: "Keep exactly one compatible bundle.",
    });
    expect(result.current.stepProps.onPrepare).toBeUndefined();
  });

  it("maps selected bundle capacity and blocks an insufficient preparation", () => {
    evidenceQuery = {
      ...evidenceQuery,
      data: {
        run_id: "parent-run",
        status: "available",
        bundles: [
          {
            ...availableBundle(),
            total_size_bytes: 64,
            required_free_bytes: 96,
            available_free_bytes: 32,
            attempt_quota_bytes: 128,
            capacity_status: "insufficient",
            error_code: "insufficient_capacity",
          },
        ],
      },
    };
    const { result, rerender } = renderController();

    expect(result.current.stepProps.state).toMatchObject({
      status: "available",
      blockerCode: "insufficient_capacity",
      capacity: {
        totalSizeBytes: 64,
        requiredFreeBytes: 96,
        availableFreeBytes: 32,
        attemptQuotaBytes: 128,
        status: "insufficient",
      },
    });
    expect(result.current.stepProps.state.recoveryAction).toBe(
      "Free disk space or increase the per-attempt quota before preparing this bundle.",
    );
    expect(result.current.stepProps.onPrepare).toBeUndefined();

    evidenceQuery = {
      ...evidenceQuery,
      data: {
        ...(evidenceQuery.data as object),
        status: "failed",
        active_job_id: "terminal-job-1",
        retryable: true,
      },
    };
    operationQuery = {
      ...operationQuery,
      data: {
        run_id: "terminal-job-1",
        broadcast: {
          request: {
            bundle_id: "bundle-qualified-1",
            bundle_manifest_sha256: manifestSha256,
          },
        },
      },
    };
    rerender();
    expect(result.current.stepProps.onRetry).toBeUndefined();
    expect(result.current.stepProps.state.blockerCode).toBe(
      "insufficient_capacity",
    );
  });

  it("rejects malformed bundle data and wires active-state query polling", () => {
    evidenceQuery = {
      ...evidenceQuery,
      data: {
        run_id: "parent-run",
        status: "available",
        bundles: [
          {
            status: "invalid",
            bundle_id: "bundle-invalid",
            bundle_manifest_sha256: manifestSha256,
            inbox_entry: "bundle-invalid",
          },
        ],
      },
    };
    const { result, rerender } = renderController();
    expect(result.current.stepProps.state.status).toBe("not_available");

    const [, options] = api.useGetBroadcastReviewEvidence.mock.calls.at(-1)!;
    expect(
      options.query.refetchInterval({
        state: { data: { status: "validating" } },
      }),
    ).toBe(2_000);
    expect(options.query.refetchInterval({ state: { data: undefined } })).toBe(
      false,
    );

    evidenceQuery = {
      ...evidenceQuery,
      data: { run_id: "parent-run", status: "available" },
    };
    rerender();
    expect(result.current.stepProps.state.status).toBe("not_available");
  });

  it("formats recovery guidance and refreshes only while enabled", async () => {
    const formatRecoveryAction = vi.fn(
      (action: string) => `Formatted: ${action}`,
    );
    const enabled = renderController(true, { formatRecoveryAction });
    expect(enabled.result.current.stepProps.state.recoveryAction).toBe(
      "Formatted: provision_qualified_review_evidence",
    );
    await act(async () => {
      await enabled.result.current.refresh();
    });
    expect(api.refetchEvidence).toHaveBeenCalledTimes(1);
    enabled.unmount();

    const disabled = renderController(false, { formatRecoveryAction });
    await act(async () => {
      await disabled.result.current.refresh();
    });
    expect(api.refetchEvidence).toHaveBeenCalledTimes(1);
  });

  it("prepares the unique compatible bundle only after an explicit action and blocks double clicks", async () => {
    let resolveImport!: (value: { run_id: string }) => void;
    api.importEvidence.mockReturnValue(
      new Promise<{ run_id: string }>((resolve) => {
        resolveImport = resolve;
      }),
    );
    evidenceQuery = {
      ...evidenceQuery,
      data: {
        run_id: "parent-run",
        status: "available",
        bundles: [availableBundle()],
      },
    };
    const { result } = renderController();

    expect(api.importEvidence).not.toHaveBeenCalled();
    await act(async () => {
      const prepare = result.current.stepProps.onPrepare!;
      const bundle = result.current.stepProps.state.bundle!;
      void prepare(bundle);
      void prepare(bundle);
    });

    expect(api.importEvidence).toHaveBeenCalledTimes(1);
    expect(api.importEvidence).toHaveBeenCalledWith({
      runId: "parent-run",
      data: {
        bundle_id: "bundle-qualified-1",
        bundle_manifest_sha256: manifestSha256,
      },
    });

    resolveImport({ run_id: "import-job-1" });
    await waitFor(() =>
      expect(queryClient.invalidateQueries).toHaveBeenCalled(),
    );
  });

  it("keeps a StrictMode host double-click to one evidence import", async () => {
    let resolveImport!: (value: { run_id: string }) => void;
    api.importEvidence.mockReturnValue(
      new Promise<{ run_id: string }>((resolve) => {
        resolveImport = resolve;
      }),
    );
    evidenceQuery = {
      ...evidenceQuery,
      data: {
        run_id: "parent-run",
        status: "available",
        bundles: [availableBundle()],
      },
    };

    function Host() {
      const controller = useBroadcastReviewEvidenceController({
        runId: "parent-run",
        enabled: true,
      });
      return <BroadcastReviewEvidenceStep {...controller.stepProps} />;
    }

    const user = userEvent.setup();
    render(
      <StrictMode>
        <Host />
      </StrictMode>,
      { wrapper },
    );
    await user.dblClick(
      screen.getByRole("button", { name: "Prepare review evidence" }),
    );

    expect(api.importEvidence).toHaveBeenCalledTimes(1);
    await act(async () => {
      resolveImport({ run_id: "import-job-1" });
    });
    await waitFor(() =>
      expect(queryClient.invalidateQueries).toHaveBeenCalled(),
    );
  });

  it("shows progress, cancels the active child, and prohibits cancellation while committing", async () => {
    evidenceQuery = {
      ...evidenceQuery,
      data: {
        run_id: "parent-run",
        status: "copying",
        active_job_id: "import-job-1",
        stage: "copying",
        progress_percent: 42.5,
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
    const { result, rerender } = renderController();

    expect(result.current.stepProps.state).toMatchObject({
      status: "copying",
      progressPercent: 42.5,
    });
    await act(async () => {
      await result.current.stepProps.onCancel!();
    });
    expect(api.cancel).toHaveBeenCalledWith({ runId: "import-job-1" });

    evidenceQuery = {
      ...evidenceQuery,
      data: {
        ...(evidenceQuery.data as object),
        status: "committing",
        can_cancel: true,
      },
    };
    rerender();
    expect(result.current.stepProps.onCancel).toBeUndefined();
  });

  it("coalesces repeated cancellation actions while one request is in flight", async () => {
    let resolveCancel!: () => void;
    api.cancel.mockReturnValue(
      new Promise<void>((resolve) => {
        resolveCancel = resolve;
      }),
    );
    evidenceQuery = {
      ...evidenceQuery,
      data: {
        run_id: "parent-run",
        status: "copying",
        active_job_id: "import-job-1",
        can_cancel: true,
        bundles: [],
      },
    };
    const { result } = renderController();

    act(() => {
      result.current.stepProps.onCancel!();
      result.current.stepProps.onCancel!();
    });
    expect(api.cancel).toHaveBeenCalledTimes(1);

    resolveCancel();
    await waitFor(() =>
      expect(queryClient.invalidateQueries).toHaveBeenCalled(),
    );
  });

  it.each(["cancelled", "failed", "blocked"] as const)(
    "retries %s from the exact terminal child and retains its bundle identity",
    async (status) => {
      evidenceQuery = {
        ...evidenceQuery,
        data: {
          run_id: "parent-run",
          status,
          active_job_id: "terminal-job-1",
          retryable: true,
          bundles: [availableBundle()],
          error_code: "insufficient_capacity",
          recovery_action: "retry_review_evidence_import",
        },
      };
      operationQuery = {
        ...operationQuery,
        data: {
          run_id: "terminal-job-1",
          broadcast: {
            request: {
              bundle_id: "bundle-qualified-1",
              bundle_manifest_sha256: manifestSha256,
            },
          },
        },
      };
      const { result } = renderController();

      await act(async () => {
        await result.current.stepProps.onRetry!();
      });

      expect(api.importEvidence).toHaveBeenCalledWith({
        runId: "parent-run",
        data: {
          bundle_id: "bundle-qualified-1",
          bundle_manifest_sha256: manifestSha256,
          retry_from_job_id: "terminal-job-1",
        },
      });
    },
  );

  it.each(["cancelled", "failed", "blocked"] as const)(
    "lets %s prepare one different compatible bundle without retry lineage",
    async (status) => {
      const alternative = {
        ...availableBundle("bundle-qualified-2"),
        bundle_manifest_sha256: "c".repeat(64),
      };
      evidenceQuery = {
        ...evidenceQuery,
        data: {
          run_id: "parent-run",
          status,
          active_job_id: "terminal-job-1",
          retryable: true,
          bundles: [availableBundle(), alternative],
        },
      };
      operationQuery = {
        ...operationQuery,
        data: {
          run_id: "terminal-job-1",
          broadcast: {
            request: {
              bundle_id: "bundle-qualified-1",
              bundle_manifest_sha256: manifestSha256,
            },
          },
        },
      };
      const { result } = renderController();

      expect(result.current.stepProps.state.alternativeBundle).toEqual({
        bundleId: "bundle-qualified-2",
        manifestSha256: "c".repeat(64),
      });
      await act(async () => {
        await result.current.stepProps.onPrepareAlternative!(
          result.current.stepProps.state.alternativeBundle!,
        );
      });

      expect(api.importEvidence).toHaveBeenCalledWith({
        runId: "parent-run",
        data: {
          bundle_id: "bundle-qualified-2",
          bundle_manifest_sha256: "c".repeat(64),
        },
      });
    },
  );

  it("does not guess a retry bundle when the terminal child identity cannot be recovered", () => {
    evidenceQuery = {
      ...evidenceQuery,
      data: {
        run_id: "parent-run",
        status: "failed",
        active_job_id: "terminal-job-1",
        retryable: true,
        bundles: [availableBundle()],
      },
    };
    const { result } = renderController();

    expect(result.current.stepProps.onRetry).toBeUndefined();
    expect(result.current.stepProps.state.recoveryAction).toBe(
      "Restore the same compatible bundle before retrying.",
    );
  });

  it("hands a ready generation to the host and invalidates evidence plus review queries without importing", async () => {
    evidenceQuery = {
      ...evidenceQuery,
      data: {
        run_id: "parent-run",
        status: "ready",
        generation_id: "review-evidence-generation-1",
        queue_sha256: "b".repeat(64),
        bundles: [],
      },
    };
    const { result } = renderController();

    expect(result.current.isReady).toBe(true);
    expect(result.current.readyIdentity).toEqual({
      generationId: "review-evidence-generation-1",
      queueSha256: "b".repeat(64),
    });
    expect(result.current.stepProps.state).toMatchObject({
      generationId: "review-evidence-generation-1",
      queueSha256: "b".repeat(64),
    });
    expect(api.importEvidence).not.toHaveBeenCalled();
    await waitFor(() => {
      const keys = vi
        .mocked(queryClient.invalidateQueries)
        .mock.calls.map(([filters]) => filters?.queryKey);
      expect(keys).toContainEqual(["review-evidence", "parent-run"]);
      expect(keys).toContainEqual(["review-windows", "parent-run"]);
      expect(keys).toContainEqual(["artifacts", "parent-run"]);
      expect(keys).toContainEqual(["run", "parent-run"]);
    });
  });

  it("fails closed when a ready response omits its immutable identity", async () => {
    evidenceQuery = {
      ...evidenceQuery,
      data: {
        run_id: "parent-run",
        status: "ready",
        bundles: [],
      },
    };
    const { result, rerender } = renderController();
    expect(result.current.isReady).toBe(false);
    expect(result.current.readyIdentity).toBeNull();
    expect(queryClient.invalidateQueries).not.toHaveBeenCalled();

    evidenceQuery = {
      ...evidenceQuery,
      data: { run_id: "parent-run", status: "not_available", bundles: [] },
    };
    rerender();
    evidenceQuery = {
      ...evidenceQuery,
      data: { run_id: "parent-run", status: "ready", bundles: [] },
    };
    rerender();
    await act(async () => Promise.resolve());
    expect(result.current.isReady).toBe(false);
    expect(result.current.readyIdentity).toBeNull();
    expect(queryClient.invalidateQueries).not.toHaveBeenCalled();
  });

  it("ignores bundle identities that do not match the presented action", () => {
    evidenceQuery = {
      ...evidenceQuery,
      data: {
        run_id: "parent-run",
        status: "available",
        bundles: [availableBundle()],
      },
    };
    const available = renderController();
    available.result.current.stepProps.onPrepare!({
      bundleId: "different-bundle",
      manifestSha256,
    });
    expect(api.importEvidence).not.toHaveBeenCalled();
    available.unmount();

    const alternative = {
      ...availableBundle("bundle-qualified-2"),
      bundle_manifest_sha256: "c".repeat(64),
    };
    evidenceQuery = {
      ...evidenceQuery,
      data: {
        run_id: "parent-run",
        status: "failed",
        active_job_id: "terminal-job-1",
        retryable: true,
        bundles: [availableBundle(), alternative],
      },
    };
    operationQuery = {
      ...operationQuery,
      data: {
        run_id: "terminal-job-1",
        broadcast: {
          request: {
            bundle_id: "bundle-qualified-1",
            bundle_manifest_sha256: manifestSha256,
          },
        },
      },
    };
    const terminal = renderController();
    terminal.result.current.stepProps.onPrepareAlternative!({
      bundleId: "different-bundle",
      manifestSha256: "c".repeat(64),
    });
    expect(api.importEvidence).not.toHaveBeenCalled();
  });

  it("distinguishes pending prepare and retry actions", () => {
    evidenceQuery = {
      ...evidenceQuery,
      data: {
        run_id: "parent-run",
        status: "available",
        bundles: [availableBundle()],
      },
    };
    importMutation = {
      ...importMutation,
      isPending: true,
      variables: { data: {} },
    };
    const { result, rerender } = renderController();
    expect(result.current.stepProps.isPreparing).toBe(true);
    expect(result.current.stepProps.isRetrying).toBe(false);

    importMutation = {
      ...importMutation,
      variables: { data: { retry_from_job_id: "terminal-job-1" } },
    };
    rerender();
    expect(result.current.stepProps.isPreparing).toBe(false);
    expect(result.current.stepProps.isRetrying).toBe(true);
  });

  it("exposes GET, POST, and cancel failures with their operation kind", async () => {
    evidenceQuery = {
      ...evidenceQuery,
      error: new Error("GET failed"),
    };
    const getFailure = renderController();
    expect(getFailure.result.current.error).toMatchObject({ kind: "load" });
    getFailure.unmount();

    evidenceQuery = { ...evidenceQuery, error: null };
    operationQuery = {
      ...operationQuery,
      error: new Error("Child GET failed"),
    };
    const childGetFailure = renderController();
    expect(childGetFailure.result.current.error).toMatchObject({
      kind: "load",
    });
    childGetFailure.unmount();

    operationQuery = { ...operationQuery, error: null };
    evidenceQuery = {
      ...evidenceQuery,
      error: null,
      data: {
        run_id: "parent-run",
        status: "available",
        bundles: [availableBundle()],
      },
    };
    importMutation = {
      ...importMutation,
      error: new Error("POST failed"),
    };
    const postFailure = renderController();
    expect(postFailure.result.current.error).toMatchObject({ kind: "prepare" });
    postFailure.unmount();

    importMutation = { ...importMutation, error: null };
    cancelMutation = {
      ...cancelMutation,
      error: new Error("Cancel failed"),
    };
    const cancelFailure = renderController();
    expect(cancelFailure.result.current.error).toMatchObject({
      kind: "cancel",
    });
  });

  it("keeps the reusable host keyboard-accessible and narrow-screen safe", async () => {
    evidenceQuery = {
      ...evidenceQuery,
      data: {
        run_id: "parent-run",
        status: "available",
        bundles: [availableBundle()],
      },
    };

    function TestHost() {
      const controller = useBroadcastReviewEvidenceController({
        runId: "parent-run",
        enabled: true,
      });
      return (
        <BroadcastReviewEvidenceStep
          {...controller.stepProps}
          labels={translations.zh.broadcast.reviewEvidence}
        />
      );
    }

    const user = userEvent.setup();
    render(<TestHost />, { wrapper });
    expect(screen.getByTestId("broadcast-review-evidence-step")).toHaveClass(
      "min-w-0",
      "w-full",
    );
    expect(screen.getByRole("status")).toHaveTextContent("发现兼容证据包");
    const prepare = screen.getByRole("button", { name: "准备复核证据" });
    prepare.focus();
    await user.keyboard("{Enter}");
    expect(api.importEvidence).toHaveBeenCalledTimes(1);
    expect(translations.en.broadcast.reviewEvidence.title).toBe(
      "Prepare review evidence",
    );
  });

  it("lets a reusable host switch to the existing manual review when ready", () => {
    evidenceQuery = {
      ...evidenceQuery,
      data: {
        run_id: "parent-run",
        status: "ready",
        generation_id: "review-evidence-generation-1",
        queue_sha256: "b".repeat(64),
        bundles: [],
      },
    };

    function ReadyHost() {
      const controller = useBroadcastReviewEvidenceController({
        runId: "parent-run",
        enabled: true,
      });
      return controller.isReady ? (
        <div>existing manual review</div>
      ) : (
        <BroadcastReviewEvidenceStep {...controller.stepProps} />
      );
    }

    render(<ReadyHost />, { wrapper });
    expect(screen.getByText("existing manual review")).toBeVisible();
    expect(
      screen.queryByTestId("broadcast-review-evidence-step"),
    ).not.toBeInTheDocument();
    expect(api.importEvidence).not.toHaveBeenCalled();
  });
});
