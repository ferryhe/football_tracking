import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  getGetRunQueryKey,
  getListArtifactsQueryKey,
  getListAssetGroupsQueryKey,
  getListRunsQueryKey,
  type AssetGroup,
  type RunRecord,
} from "@workspace/api-client-react";

import { LanguageProvider } from "@/contexts/LanguageContext";

const mocks = vi.hoisted(() => ({
  listAssetGroups: vi.fn(),
  listRunArtifacts: vi.fn(),
  getRunArtifactJson: vi.fn(),
  cancelRun: vi.fn(),
  deleteRunOutput: vi.fn(),
}));

vi.mock("@/lib/api", () => ({ api: mocks }));

import {
  GroupedProductionHistory,
  invalidateProductionHistoryCaches,
} from "./GroupedProductionHistory";

const GENERATION = "a".repeat(64);

function run(runId: string, overrides: Partial<RunRecord> = {}): RunRecord {
  return {
    run_id: runId,
    source: "api",
    status: "completed",
    created_at: "2026-07-14T10:00:00Z",
    started_at: "2026-07-14T10:01:00Z",
    completed_at: "2026-07-14T10:02:00Z",
    config_name: "default.yaml",
    config_path: "config/default.yaml",
    input_video: "C:/videos/match.mp4",
    parent_run_id: null,
    output_dir: `C:/outputs/${runId}`,
    modules_enabled: {},
    artifacts: [],
    stats: {},
    broadcast: {},
    ai_candidate_lifecycle: {},
    progress: null,
    notes: null,
    error: null,
    ...overrides,
  };
}

const ready = run("product-ready", {
  source: "broadcast_hybrid",
  broadcast: {
    status: "ready",
    status_generation: GENERATION,
    limitations: ["Review sparse-ball windows before external release."],
  },
});
const activeParent = run("full-active", {
  source: "broadcast_hybrid",
  broadcast: {
    status: "trajectory_ready",
    last_operation: {
      operation_run_id: "render-active",
      operation: "render",
      status: "running",
    },
  },
});
const activeChild = run("render-active", {
  source: "broadcast_operation",
  status: "running",
  completed_at: null,
  parent_run_id: "full-active",
  broadcast: {
    parent_run_id: "full-active",
    operation: "render",
    operation_status: "running",
  },
  progress: {
    stage: "render",
    percent: 42,
    elapsed_seconds: 12,
  },
});
const leaf = run("leaf-output");

const assetGroup: AssetGroup = {
  group_id: "match",
  title: "match.mp4",
  input_video: {
    name: "match.mp4",
    path: "C:/videos/match.mp4",
    size_bytes: 1_000,
    modified_at: "2026-07-14T09:00:00Z",
  },
  last_activity_at: "2026-07-14T10:02:00Z",
  runs: [ready, activeParent, activeChild, leaf],
  configs: [
    {
      name: "default.yaml",
      path: "config/default.yaml",
      created_at: "2026-07-14T09:30:00Z",
      input_video: "C:/videos/match.mp4",
      output_dir: "C:/outputs/default",
      detector_model_path: "models/ball.pt",
      postprocess_enabled: true,
      follow_cam_enabled: true,
      exists: { config: true, input_video: true },
    },
  ],
  outputs: [],
  is_unbound: false,
};

function renderHistory() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const rendered = render(
    <QueryClientProvider client={queryClient}>
      <LanguageProvider>
        <GroupedProductionHistory />
      </LanguageProvider>
    </QueryClientProvider>,
  );
  return { ...rendered, queryClient };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((next) => {
    resolve = next;
  });
  return { promise, resolve };
}

describe("GroupedProductionHistory", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
    mocks.listAssetGroups.mockResolvedValue([assetGroup]);
    mocks.listRunArtifacts.mockResolvedValue([
      {
        name: "broadcast.mp4",
        path: "C:/outputs/product-ready/broadcast.mp4",
        kind: "video",
        exists: true,
        size_bytes: 1_000,
        content_type: "video/mp4",
      },
      {
        name: "broadcast_quality_report.json",
        path: "C:/outputs/product-ready/broadcast_quality_report.json",
        kind: "report",
        exists: true,
        size_bytes: 100,
        content_type: "application/json",
      },
    ]);
    mocks.getRunArtifactJson.mockResolvedValue({ overall_status: "pass" });
    mocks.cancelRun.mockImplementation(async (runId: string) =>
      run(runId, { status: "cancelled" }),
    );
    mocks.deleteRunOutput.mockResolvedValue({ deleted: true });
  });

  it("does not verify products on the list and verifies lazily in detail", async () => {
    const user = userEvent.setup();
    renderHistory();

    expect(await screen.findByTestId("asset-group-match")).toBeInTheDocument();
    expect(mocks.listRunArtifacts).not.toHaveBeenCalled();
    expect(mocks.getRunArtifactJson).not.toHaveBeenCalled();

    await user.click(screen.getByTestId("asset-group-toggle-match"));

    expect(mocks.listRunArtifacts).not.toHaveBeenCalled();
    expect(
      screen.getByTestId("group-products-unverified-match"),
    ).toHaveTextContent("1");
    await user.click(screen.getByTestId("timeline-toggle-product-ready"));

    expect(
      await screen.findByTestId("verified-product-product-ready"),
    ).toBeInTheDocument();
    expect(mocks.listRunArtifacts).toHaveBeenCalledTimes(1);
    expect(mocks.listRunArtifacts).toHaveBeenCalledWith(
      "product-ready",
      GENERATION,
    );
    expect(mocks.getRunArtifactJson).toHaveBeenCalledWith(
      "product-ready",
      "broadcast_quality_report.json",
      GENERATION,
    );
    expect(screen.getByTestId("product-preview-product-ready")).toHaveAttribute(
      "src",
      `/api/runs/product-ready/artifacts/broadcast.mp4?status_generation=${GENERATION}`,
    );
    expect(
      await screen.findByTestId("product-quality-product-ready"),
    ).toHaveTextContent("pass");
    expect(screen.getByText(/sparse-ball windows/i)).toBeInTheDocument();
    expect(
      screen.getByTestId("group-products-verified-match"),
    ).toHaveTextContent("1");
    expect(
      screen.getByTestId("group-products-unverified-match"),
    ).toHaveTextContent("0");

    await user.click(screen.getByTestId("asset-group-toggle-match"));
    await user.click(screen.getByTestId("asset-group-toggle-match"));
    await user.click(screen.getByTestId("timeline-toggle-product-ready"));
    await screen.findByTestId("verified-product-product-ready");
    expect(mocks.listRunArtifacts).toHaveBeenCalledTimes(1);
  });

  it("shows ready-without-media as blocked rather than a product", async () => {
    mocks.listRunArtifacts.mockResolvedValue([]);
    const user = userEvent.setup();
    renderHistory();
    await user.click(await screen.findByTestId("asset-group-toggle-match"));
    await user.click(screen.getByTestId("timeline-toggle-product-ready"));
    expect(
      await screen.findByTestId("product-missing-product-ready"),
    ).toHaveTextContent(/no verified broadcast\.mp4/i);
    expect(
      screen.queryByTestId("product-preview-product-ready"),
    ).not.toBeInTheDocument();
    expect(
      screen.getByTestId("group-products-unavailable-match"),
    ).toHaveTextContent("1");
    expect(
      screen.getByTestId("group-products-unverified-match"),
    ).toHaveTextContent("0");
  });

  it("fails closed without a valid status generation", async () => {
    mocks.listAssetGroups.mockResolvedValue([
      {
        ...assetGroup,
        runs: [
          {
            ...ready,
            broadcast: { status: "ready", status_generation: "invalid" },
          },
        ],
      },
    ]);
    const user = userEvent.setup();
    renderHistory();
    await user.click(await screen.findByTestId("asset-group-toggle-match"));
    await user.click(screen.getByTestId("timeline-toggle-product-ready"));
    expect(
      await screen.findByTestId("product-generation-invalid-product-ready"),
    ).toHaveTextContent(/no valid status generation/i);
    expect(mocks.listRunArtifacts).not.toHaveBeenCalled();
  });

  it("resets cached product counts when the status generation changes", async () => {
    const user = userEvent.setup();
    const { queryClient } = renderHistory();
    await user.click(await screen.findByTestId("asset-group-toggle-match"));
    await user.click(screen.getByTestId("timeline-toggle-product-ready"));
    await screen.findByTestId("verified-product-product-ready");
    expect(
      screen.getByTestId("group-products-verified-match"),
    ).toHaveTextContent("1");
    await user.click(screen.getByTestId("timeline-toggle-product-ready"));

    const nextGeneration = "b".repeat(64);
    act(() => {
      queryClient.setQueryData(
        ["asset-groups"],
        [
          {
            ...assetGroup,
            runs: assetGroup.runs?.map((candidate) =>
              candidate.run_id === ready.run_id
                ? {
                    ...candidate,
                    broadcast: {
                      ...candidate.broadcast,
                      status_generation: nextGeneration,
                    },
                  }
                : candidate,
            ),
          },
        ],
      );
    });

    await waitFor(() =>
      expect(
        screen.getByTestId("group-products-unverified-match"),
      ).toHaveTextContent("1"),
    );
    expect(
      screen.getByTestId("group-products-verified-match"),
    ).toHaveTextContent("0");
    expect(mocks.listRunArtifacts).toHaveBeenCalledTimes(1);
    await user.click(screen.getByTestId("timeline-toggle-product-ready"));
    await waitFor(() =>
      expect(mocks.listRunArtifacts).toHaveBeenCalledTimes(2),
    );
    expect(mocks.listRunArtifacts).toHaveBeenLastCalledWith(
      "product-ready",
      nextGeneration,
    );
  });

  it("shows original metadata, configuration snapshots, and named live progress", async () => {
    const user = userEvent.setup();
    renderHistory();
    await user.click(await screen.findByTestId("asset-group-toggle-match"));

    expect(screen.getByTestId("group-source-metadata-match")).toHaveTextContent(
      "match.mp4",
    );
    expect(screen.getByTestId("group-source-metadata-match")).toHaveTextContent(
      "1000 B",
    );
    expect(
      screen.getByTestId("group-config-snapshots-match"),
    ).toHaveTextContent("config/default.yaml");
    expect(
      screen.getByRole("progressbar", {
        name: /render-active rendering progress/i,
      }),
    ).toHaveAttribute("aria-valuetext", "42%");
    expect(
      screen.getByTestId("group-run-progress-render-active"),
    ).toHaveAttribute("aria-live", "polite");
  });

  it("keeps a config-only source group useful without artifact requests", async () => {
    mocks.listAssetGroups.mockResolvedValue([
      { ...assetGroup, runs: [], outputs: [] },
    ]);
    const user = userEvent.setup();
    renderHistory();
    await user.click(await screen.findByTestId("asset-group-toggle-match"));

    expect(screen.getByTestId("group-source-metadata-match")).toHaveTextContent(
      "C:/videos/match.mp4",
    );
    expect(
      screen.getByTestId("group-config-snapshots-match"),
    ).toHaveTextContent("default.yaml");
    expect(screen.queryByTestId(/timeline-run-/)).not.toBeInTheDocument();
    expect(mocks.listRunArtifacts).not.toHaveBeenCalled();
  });

  it("cancels the selected active child and enforces child-first deletion", async () => {
    const user = userEvent.setup();
    renderHistory();
    await user.click(await screen.findByTestId("asset-group-toggle-match"));

    await user.click(screen.getByTestId("timeline-toggle-full-active"));
    expect(screen.getByTestId("group-delete-full-active")).toBeDisabled();
    expect(
      screen.getByTestId("group-delete-blocker-full-active"),
    ).toHaveTextContent(/child output/i);
    await user.click(screen.getByTestId("group-cancel-full-active"));
    expect(
      screen.getAllByText("render-active", { selector: "span" }),
    ).toHaveLength(2);
    await user.click(screen.getByTestId("group-confirm-cancel-full-active"));
    await waitFor(() =>
      expect(mocks.cancelRun).toHaveBeenCalledWith("render-active"),
    );

    await user.click(screen.getByTestId("timeline-toggle-leaf-output"));
    await user.click(screen.getByTestId("group-delete-leaf-output"));
    await user.click(screen.getByTestId("group-confirm-delete-leaf-output"));
    await waitFor(() =>
      expect(mocks.deleteRunOutput).toHaveBeenCalledWith("leaf-output"),
    );
  });

  it("deduplicates repeated and parent-child cancellation attempts by target", async () => {
    const pending = deferred<RunRecord>();
    mocks.cancelRun.mockReturnValueOnce(pending.promise);
    const user = userEvent.setup();
    renderHistory();
    await user.click(await screen.findByTestId("asset-group-toggle-match"));
    await user.click(screen.getByTestId("timeline-toggle-full-active"));
    await user.click(screen.getByTestId("timeline-toggle-render-active"));
    await user.click(screen.getByTestId("group-cancel-full-active"));
    const confirm = screen.getByTestId("group-confirm-cancel-full-active");

    act(() => {
      confirm.click();
      confirm.click();
    });

    await waitFor(() => expect(mocks.cancelRun).toHaveBeenCalledTimes(1));
    expect(mocks.cancelRun).toHaveBeenCalledWith("render-active");
    expect(confirm).toBeDisabled();
    expect(screen.getByTestId("group-cancel-render-active")).toBeDisabled();
    fireEvent.click(screen.getByTestId("group-cancel-render-active"));
    fireEvent.click(screen.getByTestId("group-cancel-full-active"));
    expect(mocks.cancelRun).toHaveBeenCalledTimes(1);

    pending.resolve(run("render-active", { status: "cancelled" }));
    await waitFor(() =>
      expect(
        screen.getByTestId("group-cancel-render-active"),
      ).not.toBeDisabled(),
    );
  });

  it("deduplicates repeated deletion confirms while the target is pending", async () => {
    const pending = deferred<{ deleted: boolean }>();
    mocks.deleteRunOutput.mockReturnValueOnce(pending.promise);
    const user = userEvent.setup();
    renderHistory();
    await user.click(await screen.findByTestId("asset-group-toggle-match"));
    await user.click(screen.getByTestId("timeline-toggle-leaf-output"));
    await user.click(screen.getByTestId("group-delete-leaf-output"));
    const confirm = screen.getByTestId("group-confirm-delete-leaf-output");

    act(() => {
      confirm.click();
      confirm.click();
    });

    await waitFor(() => expect(mocks.deleteRunOutput).toHaveBeenCalledTimes(1));
    expect(confirm).toBeDisabled();
    fireEvent.click(screen.getByTestId("group-delete-leaf-output"));
    expect(mocks.deleteRunOutput).toHaveBeenCalledTimes(1);

    pending.resolve({ deleted: true });
    await waitFor(() =>
      expect(screen.getByTestId("group-delete-leaf-output")).not.toBeDisabled(),
    );
  });

  it("invalidates group, run, artifact, and product verification caches", async () => {
    const queryClient = new QueryClient();
    const keys = [
      ["asset-groups"],
      ["runs"],
      ["run", "run-a"],
      ["artifacts", "run-a"],
      getListAssetGroupsQueryKey(),
      getListRunsQueryKey(),
      getGetRunQueryKey("run-a"),
      getListArtifactsQueryKey("run-a"),
      getListArtifactsQueryKey("run-a", {
        status_generation: GENERATION,
      }),
      ["/api/runs/run-a/future-variant", { generation: GENERATION }],
      ["production-history", "artifact", "run-a", GENERATION, "quality"],
      ["production-history", "product", "run-a", GENERATION],
    ] as const;
    for (const key of keys) queryClient.setQueryData(key, {});

    await act(() => invalidateProductionHistoryCaches(queryClient, ["run-a"]));

    for (const key of keys) {
      expect(queryClient.getQueryState(key)?.isInvalidated).toBe(true);
    }
  });

  it("preserves search and status filtering in the grouped list", async () => {
    const user = userEvent.setup();
    renderHistory();
    await screen.findByTestId("asset-group-match");

    await user.click(screen.getByTestId("group-filter-active"));
    expect(screen.getByTestId("asset-group-match")).toBeInTheDocument();
    await user.clear(screen.getByTestId("group-search"));
    await user.type(screen.getByTestId("group-search"), "not-present");
    expect(screen.queryByTestId("asset-group-match")).not.toBeInTheDocument();
    expect(screen.getByText("No runs found")).toBeInTheDocument();
  });

  it("opens 1,000 ready candidates without N+1 requests and fetches one explicit row", async () => {
    mocks.listAssetGroups.mockResolvedValue([
      {
        ...assetGroup,
        runs: Array.from({ length: 1_000 }, (_, index) =>
          run(`fixture-run-${index}`, {
            source: "broadcast_hybrid",
            broadcast: { status: "ready", status_generation: GENERATION },
          }),
        ),
      },
    ]);
    const user = userEvent.setup();
    renderHistory();

    await screen.findByTestId("asset-group-match");
    await user.type(screen.getByTestId("group-search"), "fixture-run-999");
    expect(screen.getByTestId("asset-group-match")).toBeInTheDocument();
    await user.click(screen.getByTestId("asset-group-toggle-match"));
    expect(mocks.listRunArtifacts).not.toHaveBeenCalled();
    await user.click(screen.getByTestId("timeline-toggle-fixture-run-999"));
    await screen.findByTestId("verified-product-fixture-run-999");
    expect(mocks.listRunArtifacts).toHaveBeenCalledTimes(1);
    expect(mocks.listRunArtifacts).toHaveBeenCalledWith(
      "fixture-run-999",
      GENERATION,
    );
  });
});
