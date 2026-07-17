import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { AssetGroup, RunRecord } from "@workspace/api-client-react";

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
  configs: [],
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

    await user.click(screen.getByTestId("asset-group-toggle-match"));
    await user.click(screen.getByTestId("asset-group-toggle-match"));
    await screen.findByTestId("verified-product-product-ready");
    expect(mocks.listRunArtifacts).toHaveBeenCalledTimes(1);
  });

  it("shows ready-without-media as blocked rather than a product", async () => {
    mocks.listRunArtifacts.mockResolvedValue([]);
    const user = userEvent.setup();
    renderHistory();
    await user.click(await screen.findByTestId("asset-group-toggle-match"));
    expect(
      await screen.findByTestId("product-missing-product-ready"),
    ).toHaveTextContent(/no verified broadcast\.mp4/i);
    expect(
      screen.queryByTestId("product-preview-product-ready"),
    ).not.toBeInTheDocument();
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
    expect(
      await screen.findByTestId("product-generation-invalid-product-ready"),
    ).toHaveTextContent(/no valid status generation/i);
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

  it("invalidates group, run, artifact, and product verification caches", async () => {
    const queryClient = new QueryClient();
    queryClient.setQueryData(["asset-groups"], [assetGroup]);
    queryClient.setQueryData(["runs"], assetGroup.runs);
    queryClient.setQueryData(
      ["production-history", "artifact", "run-a", GENERATION, "quality"],
      {},
    );
    queryClient.setQueryData(
      ["production-history", "product", "run-a", GENERATION],
      [],
    );

    await act(() => invalidateProductionHistoryCaches(queryClient));

    for (const key of [
      ["asset-groups"],
      ["runs"],
      ["production-history", "artifact", "run-a", GENERATION, "quality"],
      ["production-history", "product", "run-a", GENERATION],
    ]) {
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

  it("keeps a 1,000-run group searchable without list-page artifact requests", async () => {
    mocks.listAssetGroups.mockResolvedValue([
      {
        ...assetGroup,
        runs: Array.from({ length: 1_000 }, (_, index) =>
          run(`fixture-run-${index}`),
        ),
      },
    ]);
    const user = userEvent.setup();
    renderHistory();

    await screen.findByTestId("asset-group-match");
    await user.type(screen.getByTestId("group-search"), "fixture-run-999");
    expect(screen.getByTestId("asset-group-match")).toBeInTheDocument();
    expect(mocks.listRunArtifacts).not.toHaveBeenCalled();
  });
});
