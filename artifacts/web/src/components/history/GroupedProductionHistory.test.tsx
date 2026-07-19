import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  getGetRunQueryKey,
  getGetHealthQueryKey,
  getListArtifactsQueryKey,
  getListAssetGroupsQueryKey,
  getListRunsQueryKey,
  type AssetGroup,
  type ConfigDetail,
  type RunRecord,
} from "@workspace/api-client-react";

import { LanguageProvider } from "@/contexts/LanguageContext";

const mocks = vi.hoisted(() => ({
  listAssetGroups: vi.fn(),
  listRunArtifacts: vi.fn(),
  getRunArtifactJson: vi.fn(),
  getConfig: vi.fn(),
  cancelRun: vi.fn(),
  deleteRunOutput: vi.fn(),
}));

vi.mock("@/lib/api", () => ({ api: mocks }));

import {
  GroupedProductionHistory,
  invalidateProductionHistoryCaches,
} from "./GroupedProductionHistory";

const GENERATION = "a".repeat(64);
const GENERATION_B = "b".repeat(64);
const CONFIG_DIGEST =
  "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824";

function trialMachineNote(): string {
  return JSON.stringify({
    schema_version: "1.0",
    purpose: "production_trial",
    workflow_id: "workflow-a",
    submission_id: "submission-trial",
    output_id: "accepted",
    generation: 1,
    calibration_digest: GENERATION,
    intent_sha256: GENERATION_B,
    start_frame: 10,
    max_frames: 300,
    enable_postprocess: true,
    enable_follow_cam: false,
  });
}

function fullMachineNote(
  outputId: string,
  overrides: Record<string, unknown> = {},
): string {
  return JSON.stringify({
    schema_version: "1.0",
    purpose: "production_full",
    workflow_id: "workflow-a",
    submission_id: `submission-${outputId}`,
    output_id: outputId,
    generation: 1,
    accepted_trial_run_id: "production_trial_accepted",
    accepted_trial_request_sha256: GENERATION,
    confirmed_config_name: "confirmed.yaml",
    expected_config_sha256: CONFIG_DIGEST,
    config_patch_sha256: GENERATION_B,
    calibration_digest: GENERATION,
    source_signature: {
      path: "C:/videos/match.mp4",
      size_bytes: 1_000,
      modified_at: "2026-07-14T09:00:00Z",
    },
    ...overrides,
  });
}

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
  artifacts: [
    {
      name: "event_candidates.json",
      path: "C:/outputs/product-ready/event_candidates.json",
      kind: "report",
      exists: true,
      size_bytes: 100,
      content_type: "application/json",
    },
  ],
  broadcast: {
    status: "ready",
    status_generation: GENERATION,
    limitations: ["Review sparse-ball windows before external release."],
  },
});
const acceptedTrial = run("production_trial_accepted", {
  notes: trialMachineNote(),
});
const historicalFull = run("production_full_historical", {
  source: "broadcast_hybrid",
  parent_run_id: acceptedTrial.run_id,
  config_name: "confirmed.yaml",
  config_path: "config/confirmed.yaml",
  notes: fullMachineNote("historical"),
  broadcast: { status: "ready", status_generation: GENERATION_B },
});
const historicalFullSibling = run("production_full_historical-sibling", {
  source: "broadcast_hybrid",
  parent_run_id: acceptedTrial.run_id,
  config_name: "confirmed.yaml",
  config_path: "config/confirmed.yaml",
  notes: fullMachineNote("historical-sibling", {
    config_patch_sha256: GENERATION,
  }),
  broadcast: { status: "trajectory_ready" },
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
  runs: [
    ready,
    acceptedTrial,
    historicalFull,
    historicalFullSibling,
    activeParent,
    activeChild,
    leaf,
  ],
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
    {
      name: "confirmed.yaml",
      path: "config/confirmed.yaml",
      created_at: "2026-07-14T09:40:00Z",
      input_video: "C:/videos/match.mp4",
      output_dir: "C:/outputs/confirmed",
      detector_model_path: "models/ball.pt",
      postprocess_enabled: true,
      follow_cam_enabled: false,
      exists: { config: true, input_video: true },
    },
  ],
  outputs: [],
  is_unbound: false,
};

function renderHistory(
  queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  }),
) {
  const rendered = render(
    <QueryClientProvider client={queryClient}>
      <LanguageProvider>
        <GroupedProductionHistory />
      </LanguageProvider>
    </QueryClientProvider>,
  );
  return { ...rendered, queryClient };
}

function currentConfigDetail(
  overrides: Partial<ConfigDetail> = {},
): ConfigDetail {
  return {
    name: "confirmed.yaml",
    path: "config/confirmed.yaml",
    text: "hello",
    raw: {
      metadata: {
        production_workflow: {
          schema_version: "1.0",
          workflow_id: "workflow-a",
          accepted_trial_run_id: acceptedTrial.run_id,
          calibration_digest: GENERATION,
          source_signature: {
            path: "C:/videos/match.mp4",
            size_bytes: 1_000,
            modified_at: "2026-07-14T09:00:00Z",
          },
          trial_request_sha256: GENERATION,
          trial_intent_sha256: GENERATION_B,
          patch_sha256: GENERATION_B,
        },
      },
    },
    resolved: {},
    summary: assetGroup.configs![1],
    ...overrides,
  };
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
    window.history.replaceState({}, "", "/history");
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
    mocks.getConfig.mockResolvedValue(currentConfigDetail());
    mocks.cancelRun.mockImplementation(async (runId: string) =>
      run(runId, { status: "cancelled" }),
    );
    mocks.deleteRunOutput.mockResolvedValue({ deleted: true });
  });

  it("opens an exact run deep link and exposes contextual advanced actions", async () => {
    window.history.replaceState(
      {},
      "",
      "/history?run=product-ready&from=broadcast",
    );
    renderHistory();

    expect(await screen.findByTestId("group-detail-match")).toBeVisible();
    expect(screen.getByTestId("timeline-toggle-product-ready")).toHaveAttribute(
      "aria-expanded",
      "true",
    );
    expect(
      screen.getByText(/broadcast link.*production history/i),
    ).toBeVisible();
    expect(screen.getByRole("link", { name: /open ai review/i })).toHaveAttribute(
      "href",
      "/ai?run=product-ready",
    );
    expect(
      screen.getByRole("link", { name: /open highlight tools/i }),
    ).toHaveAttribute("href", "/deliverable?run=product-ready");
    await screen.findByTestId("verified-product-product-ready");
    expect(mocks.listAssetGroups).toHaveBeenCalledTimes(1);
    expect(mocks.listRunArtifacts).toHaveBeenCalledTimes(1);
  });

  it("fails closed when an exact run deep link is absent", async () => {
    window.history.replaceState({}, "", "/history?run=missing-run");
    renderHistory();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /missing-run.*not found/i,
    );
    expect(mocks.listRunArtifacts).not.toHaveBeenCalled();
  });

  it("clears route-owned search, open, and focus state as the run query changes", async () => {
    window.history.replaceState({}, "", "/history?run=product-ready");
    renderHistory();
    expect(await screen.findByTestId("group-detail-match")).toBeVisible();
    expect(screen.getByTestId("group-search")).toHaveValue("product-ready");
    expect(screen.getByRole("link", { name: /open ai review/i })).toBeVisible();

    act(() => {
      window.history.pushState({}, "", "/history?run=missing-next");
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /missing-next.*not found/i,
    );
    await waitFor(() =>
      expect(screen.queryByTestId("group-detail-match")).toBeNull(),
    );
    expect(screen.getByTestId("group-search")).toHaveValue("");
    expect(screen.queryByRole("link", { name: /open ai review/i })).toBeNull();

    act(() => {
      window.history.pushState({}, "", "/history");
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
    expect(screen.getByTestId("group-search")).toHaveValue("");
    expect(screen.getByTestId("asset-group-toggle-match")).toHaveAttribute(
      "aria-expanded",
      "false",
    );
  });

  it("transfers exact-route row ownership without closing manually opened rows", async () => {
    const user = userEvent.setup();
    const readyB = run("product-ready-b", {
      source: "broadcast_hybrid",
      broadcast: {
        status: "ready",
        status_generation: GENERATION_B,
        limitations: [],
      },
    });
    mocks.listAssetGroups.mockResolvedValue([
      {
        ...assetGroup,
        runs: [
          ready,
          readyB,
          ...(assetGroup.runs ?? []).filter(
            (candidate) => candidate.run_id !== ready.run_id,
          ),
        ],
      },
    ]);
    mocks.listRunArtifacts.mockImplementation(async (runId: string) => [
      {
        name: "broadcast.mp4",
        path: `C:/outputs/${runId}/broadcast.mp4`,
        kind: "video",
        exists: true,
        size_bytes: 1_000,
        content_type: "video/mp4",
      },
      {
        name: "broadcast_quality_report.json",
        path: `C:/outputs/${runId}/broadcast_quality_report.json`,
        kind: "report",
        exists: true,
        size_bytes: 100,
        content_type: "application/json",
      },
    ]);
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    window.history.replaceState({}, "", "/history?run=product-ready");
    renderHistory(queryClient);

    await screen.findByTestId("verified-product-product-ready");
    await user.click(screen.getByTestId("timeline-toggle-leaf-output"));
    expect(screen.getByTestId("timeline-toggle-leaf-output")).toHaveAttribute(
      "aria-expanded",
      "true",
    );

    act(() => {
      queryClient.removeQueries({
        queryKey: ["production-history", "product"],
      });
      queryClient.removeQueries({
        queryKey: ["production-history", "artifact"],
      });
      mocks.listRunArtifacts.mockClear();
      mocks.getRunArtifactJson.mockClear();
      window.history.pushState({}, "", "/history?run=product-ready-b");
      window.dispatchEvent(new PopStateEvent("popstate"));
    });

    expect(
      await screen.findByTestId("verified-product-product-ready-b"),
    ).toBeVisible();
    expect(screen.getByTestId("timeline-toggle-product-ready")).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    expect(
      screen.getByTestId("timeline-toggle-product-ready-b"),
    ).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByTestId("timeline-toggle-leaf-output")).toHaveAttribute(
      "aria-expanded",
      "true",
    );
    expect(mocks.listRunArtifacts.mock.calls.map(([runId]) => runId)).toEqual([
      "product-ready-b",
    ]);
    expect(
      mocks.getRunArtifactJson.mock.calls.map(([runId]) => runId),
    ).toEqual(["product-ready-b"]);
  });

  it("hides unsupported advanced actions for an active row", async () => {
    const user = userEvent.setup();
    renderHistory();
    await screen.findByTestId("asset-group-match");
    await user.click(screen.getByTestId("asset-group-toggle-match"));
    await user.click(screen.getByTestId("timeline-toggle-render-active"));
    const row = screen.getByTestId("timeline-run-render-active");
    expect(within(row).queryByRole("link", { name: /ai review/i })).toBeNull();
    expect(
      within(row).queryByRole("link", { name: /highlight tools/i }),
    ).toBeNull();
  });

  it("does not verify products on the list and verifies lazily in detail", async () => {
    const user = userEvent.setup();
    renderHistory();

    expect(await screen.findByTestId("asset-group-match")).toBeInTheDocument();
    expect(mocks.listRunArtifacts).not.toHaveBeenCalled();
    expect(mocks.getRunArtifactJson).not.toHaveBeenCalled();
    expect(mocks.getConfig).not.toHaveBeenCalled();

    await user.click(screen.getByTestId("asset-group-toggle-match"));

    expect(mocks.listRunArtifacts).not.toHaveBeenCalled();
    expect(mocks.getConfig).not.toHaveBeenCalled();
    expect(screen.getByTestId("group-products-ready-match")).toHaveTextContent(
      "Ready candidates: 2",
    );
    expect(
      screen.getByTestId("group-products-unverified-match"),
    ).toHaveTextContent("2");
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
    ).toHaveTextContent("1");
    expect(
      screen.getByTestId("group-products-unavailable-match"),
    ).toHaveTextContent("0");
    expect(mocks.getConfig).not.toHaveBeenCalled();

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
    ).toHaveTextContent("1");
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
      ).toHaveTextContent("2"),
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

  it("separates sibling config verification when patch lineage differs", async () => {
    const user = userEvent.setup();
    renderHistory();
    await user.click(await screen.findByTestId("asset-group-toggle-match"));
    expect(mocks.getConfig).not.toHaveBeenCalled();

    await user.click(
      screen.getByTestId("timeline-toggle-production_full_historical"),
    );
    expect(
      await screen.findByTestId(
        "current-config-status-production_full_historical",
      ),
    ).toHaveTextContent("Current saved configuration verified");
    expect(mocks.getConfig).toHaveBeenCalledTimes(1);
    expect(mocks.getConfig).toHaveBeenCalledWith("confirmed.yaml");
    await user.click(
      screen.getByTestId("timeline-toggle-production_full_historical"),
    );

    await user.click(
      screen.getByTestId("timeline-toggle-production_full_historical-sibling"),
    );
    await waitFor(() =>
      expect(
        screen.getByTestId(
          "current-config-status-production_full_historical-sibling",
        ),
      ).toHaveTextContent(/lineage does not match/i),
    );
    expect(mocks.getConfig).toHaveBeenCalledTimes(2);
  });

  it("shows summary-only status while current config re-verification is pending", async () => {
    const pending = deferred<ConfigDetail>();
    mocks.getConfig.mockReturnValueOnce(pending.promise);
    const user = userEvent.setup();
    renderHistory();
    await user.click(await screen.findByTestId("asset-group-toggle-match"));
    await user.click(
      screen.getByTestId("timeline-toggle-production_full_historical"),
    );

    expect(
      screen.getByTestId("current-config-status-production_full_historical"),
    ).toHaveTextContent(/Summary only/i);
    expect(
      screen.getByTestId("current-config-status-production_full_historical"),
    ).toHaveAttribute("role", "status");
    expect(
      screen.getByTestId("current-config-status-production_full_historical"),
    ).toHaveAttribute("aria-live", "polite");
    pending.resolve(currentConfigDetail());
    await waitFor(() =>
      expect(
        screen.getByTestId("current-config-status-production_full_historical"),
      ).toHaveTextContent("Current saved configuration verified"),
    );
  });

  it("hides cached verification while current config refetch is pending", async () => {
    const firstUser = userEvent.setup();
    const first = renderHistory();
    await firstUser.click(
      await screen.findByTestId("asset-group-toggle-match"),
    );
    await firstUser.click(
      screen.getByTestId("timeline-toggle-production_full_historical"),
    );
    await screen.findByText("Current saved configuration verified");
    expect(mocks.getConfig).toHaveBeenCalledTimes(1);

    const queryClient = first.queryClient;
    first.unmount();
    const refetch = deferred<ConfigDetail>();
    mocks.getConfig.mockReturnValueOnce(refetch.promise);
    const secondUser = userEvent.setup();
    renderHistory(queryClient);
    await secondUser.click(
      await screen.findByTestId("asset-group-toggle-match"),
    );
    await secondUser.click(
      screen.getByTestId("timeline-toggle-production_full_historical"),
    );

    expect(
      screen.getByTestId("current-config-status-production_full_historical"),
    ).toHaveTextContent(/current file is being reverified/i);
    expect(
      screen.queryByText("Current saved configuration verified"),
    ).not.toBeInTheDocument();
    expect(mocks.getConfig).toHaveBeenCalledTimes(2);

    refetch.resolve(currentConfigDetail({ text: "changed" }));
    await waitFor(() =>
      expect(
        screen.getByTestId("current-config-status-production_full_historical"),
      ).toHaveTextContent("Current saved configuration was modified"),
    );
  });

  it("keeps historical full identity when current config text was modified", async () => {
    mocks.getConfig.mockResolvedValue(currentConfigDetail({ text: "changed" }));
    const user = userEvent.setup();
    renderHistory();
    await user.click(await screen.findByTestId("asset-group-toggle-match"));
    await user.click(
      screen.getByTestId("timeline-toggle-production_full_historical"),
    );

    expect(
      await screen.findByTestId(
        "current-config-status-production_full_historical",
      ),
    ).toHaveTextContent("Current saved configuration was modified");
    expect(
      within(
        screen.getByTestId("timeline-run-production_full_historical"),
      ).getByText("full"),
    ).toBeInTheDocument();
  });

  it("reports current config lineage mismatch without changing historical identity", async () => {
    const detail = currentConfigDetail();
    const metadata = (
      detail.raw.metadata as Record<string, Record<string, unknown>>
    ).production_workflow;
    mocks.getConfig.mockResolvedValue(
      currentConfigDetail({
        raw: {
          metadata: {
            production_workflow: {
              ...metadata,
              workflow_id: "wrong-workflow",
            },
          },
        },
      }),
    );
    const user = userEvent.setup();
    renderHistory();
    await user.click(await screen.findByTestId("asset-group-toggle-match"));
    await user.click(
      screen.getByTestId("timeline-toggle-production_full_historical"),
    );

    expect(
      await screen.findByTestId(
        "current-config-status-production_full_historical",
      ),
    ).toHaveTextContent(/lineage does not match/i);
    expect(
      within(
        screen.getByTestId("timeline-run-production_full_historical"),
      ).getByText("full"),
    ).toBeInTheDocument();
  });

  it("fetches a moved current config and reports lineage mismatch", async () => {
    const movedConfig = {
      ...assetGroup.configs![1],
      input_video: "C:/videos/other.mp4",
    };
    mocks.listAssetGroups.mockResolvedValue([
      {
        ...assetGroup,
        configs: assetGroup.configs?.filter(
          (config) => config.name !== "confirmed.yaml",
        ),
      },
      {
        group_id: "other",
        title: "other.mp4",
        input_video: {
          name: "other.mp4",
          path: "C:/videos/other.mp4",
          size_bytes: 2_000,
          modified_at: "2026-07-14T09:00:00Z",
        },
        runs: [],
        configs: [movedConfig],
        outputs: [],
        is_unbound: false,
      },
    ]);
    mocks.getConfig.mockResolvedValue(
      currentConfigDetail({ summary: movedConfig }),
    );
    const user = userEvent.setup();
    renderHistory();
    await user.click(await screen.findByTestId("asset-group-toggle-match"));
    await user.click(
      screen.getByTestId("timeline-toggle-production_full_historical"),
    );

    expect(
      await screen.findByTestId(
        "current-config-status-production_full_historical",
      ),
    ).toHaveTextContent(/lineage does not match/i);
    expect(mocks.getConfig).toHaveBeenCalledTimes(1);
    expect(
      within(
        screen.getByTestId("timeline-run-production_full_historical"),
      ).getByText("full"),
    ).toBeInTheDocument();
  });

  it.each([
    ["404 configuration not found", "Current saved configuration is missing"],
    ["503 configuration service unavailable", "could not be checked"],
  ])("maps current config fetch failure %s", async (message, expected) => {
    mocks.getConfig.mockRejectedValue(new Error(message));
    const user = userEvent.setup();
    renderHistory();
    await user.click(await screen.findByTestId("asset-group-toggle-match"));
    await user.click(
      screen.getByTestId("timeline-toggle-production_full_historical"),
    );

    expect(
      await screen.findByTestId(
        "current-config-status-production_full_historical",
      ),
    ).toHaveTextContent(expected);
    expect(mocks.getConfig).toHaveBeenCalledTimes(1);
    const container = screen.getByTestId(
      "current-config-status-production_full_historical",
    );
    expect(container).toHaveAttribute(
      "role",
      message.startsWith("404") ? "status" : "alert",
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

  it("keeps a pending cancellation locked across unmount and remount", async () => {
    const pending = deferred<RunRecord>();
    mocks.cancelRun.mockReturnValueOnce(pending.promise);
    const firstUser = userEvent.setup();
    const first = renderHistory();
    await firstUser.click(
      await screen.findByTestId("asset-group-toggle-match"),
    );
    await firstUser.click(screen.getByTestId("timeline-toggle-full-active"));
    await firstUser.click(screen.getByTestId("group-cancel-full-active"));
    await firstUser.click(
      screen.getByTestId("group-confirm-cancel-full-active"),
    );
    await waitFor(() => expect(mocks.cancelRun).toHaveBeenCalledTimes(1));

    const queryClient = first.queryClient;
    first.unmount();
    const secondUser = userEvent.setup();
    renderHistory(queryClient);
    await secondUser.click(
      await screen.findByTestId("asset-group-toggle-match"),
    );
    await secondUser.click(screen.getByTestId("timeline-toggle-full-active"));
    expect(screen.getByTestId("group-cancel-full-active")).toBeDisabled();
    fireEvent.click(screen.getByTestId("group-cancel-full-active"));
    expect(mocks.cancelRun).toHaveBeenCalledTimes(1);

    pending.resolve(run("render-active", { status: "cancelled" }));
    await waitFor(() =>
      expect(screen.getByTestId("group-cancel-full-active")).not.toBeDisabled(),
    );
  });

  it("keeps a pending deletion locked across unmount and remount", async () => {
    const pending = deferred<{ deleted: boolean }>();
    mocks.deleteRunOutput.mockReturnValueOnce(pending.promise);
    const firstUser = userEvent.setup();
    const first = renderHistory();
    await firstUser.click(
      await screen.findByTestId("asset-group-toggle-match"),
    );
    await firstUser.click(screen.getByTestId("timeline-toggle-leaf-output"));
    await firstUser.click(screen.getByTestId("group-delete-leaf-output"));
    await firstUser.click(
      screen.getByTestId("group-confirm-delete-leaf-output"),
    );
    await waitFor(() => expect(mocks.deleteRunOutput).toHaveBeenCalledTimes(1));

    const queryClient = first.queryClient;
    first.unmount();
    const secondUser = userEvent.setup();
    renderHistory(queryClient);
    await secondUser.click(
      await screen.findByTestId("asset-group-toggle-match"),
    );
    await secondUser.click(screen.getByTestId("timeline-toggle-leaf-output"));
    expect(screen.getByTestId("group-delete-leaf-output")).toBeDisabled();
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
      ["health"],
      ["runs"],
      ["run", "run-a"],
      ["artifacts", "run-a"],
      ["artifact-json", "run-a", "report.json"],
      ["ai-improvement-status", "run-a"],
      ["event-candidates", "run-a"],
      getGetHealthQueryKey(),
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

  it("uses ready-candidate semantics consistently in Chinese", async () => {
    localStorage.setItem("app-language", "zh");
    const user = userEvent.setup();
    renderHistory();
    await screen.findByTestId("asset-group-match");
    expect(screen.getByTestId("group-filter-ready")).toHaveTextContent(
      "就绪候选",
    );
    await user.click(screen.getByTestId("asset-group-toggle-match"));
    expect(screen.getByTestId("group-products-ready-match")).toHaveTextContent(
      "就绪候选: 2",
    );
    expect(
      screen.getByTestId("group-products-unverified-match"),
    ).toHaveTextContent("未验证: 2");
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
    expect(mocks.getConfig).not.toHaveBeenCalled();
    await user.click(screen.getByTestId("timeline-toggle-fixture-run-999"));
    await screen.findByTestId("verified-product-fixture-run-999");
    expect(mocks.listRunArtifacts).toHaveBeenCalledTimes(1);
    expect(mocks.listRunArtifacts).toHaveBeenCalledWith(
      "fixture-run-999",
      GENERATION,
    );
    expect(mocks.getConfig).not.toHaveBeenCalled();
  });
});
