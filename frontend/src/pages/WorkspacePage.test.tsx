import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { I18nProvider } from "../lib/i18n";
import type { ConfigListItem, InputCatalog, RunRecord } from "../lib/types";
import { WorkspacePage, type WorkspaceStage } from "./WorkspacePage";

function setLanguage(value: "en" | "zh") {
  window.localStorage.setItem("football-tracking-language", value);
}

function buildInputCatalog(): InputCatalog {
  return {
    root_dir: "C:/Projects/foot_ball_tracking/data",
    videos: [
      {
        name: "game_01.mp4",
        path: "C:/Projects/foot_ball_tracking/data/game_01.mp4",
        size_bytes: 1_048_576,
        modified_at: "2026-03-23T12:30:00Z",
      },
    ],
  };
}

function buildConfigs(inputCatalog: InputCatalog): ConfigListItem[] {
  return [
    {
      name: "real_first_run.yaml",
      path: "C:/Projects/foot_ball_tracking/config/real_first_run.yaml",
      created_at: "2026-03-20T09:00:00Z",
      input_video: inputCatalog.videos[0].path,
      output_dir: "C:/Projects/foot_ball_tracking/outputs/game_01",
      detector_model_path: "C:/Projects/foot_ball_tracking/weights/football_ball_yolo.pt",
      postprocess_enabled: true,
      follow_cam_enabled: true,
      exists: {
        input_video: true,
        output_dir: true,
        detector_model_path: true,
      },
    },
  ];
}

function buildRuns(inputCatalog: InputCatalog): RunRecord[] {
  return [
    {
      run_id: "baseline_run_20260323_120000",
      source: "api",
      status: "completed",
      created_at: "2026-03-23T12:00:00Z",
      started_at: "2026-03-23T12:00:05Z",
      completed_at: "2026-03-23T12:05:00Z",
      config_name: "real_first_run.yaml",
      config_path: "C:/Projects/foot_ball_tracking/config/real_first_run.yaml",
      input_video: inputCatalog.videos[0].path,
      output_dir: "C:/Projects/foot_ball_tracking/outputs/api_runs/baseline_run_20260323_120000",
      modules_enabled: { postprocess: true, follow_cam: true },
      artifacts: [
        {
          name: "ball_track.csv",
          path: "C:/Projects/foot_ball_tracking/outputs/api_runs/baseline_run_20260323_120000/ball_track.csv",
          kind: "csv",
          exists: true,
        },
      ],
      stats: {},
      notes: null,
      error: null,
    },
    {
      run_id: "deliverable_run_20260323_121000",
      source: "follow_cam_render",
      status: "completed",
      created_at: "2026-03-23T12:10:00Z",
      started_at: "2026-03-23T12:10:03Z",
      completed_at: "2026-03-23T12:12:00Z",
      config_name: "real_first_run.yaml",
      config_path: "C:/Projects/foot_ball_tracking/config/real_first_run.yaml",
      input_video: inputCatalog.videos[0].path,
      parent_run_id: "baseline_run_20260323_120000",
      output_dir: "C:/Projects/foot_ball_tracking/outputs/api_runs/deliverable_run_20260323_121000",
      modules_enabled: { postprocess: false, follow_cam: true },
      artifacts: [
        {
          name: "ball_track.csv",
          path: "C:/Projects/foot_ball_tracking/outputs/api_runs/deliverable_run_20260323_121000/ball_track.csv",
          kind: "csv",
          exists: true,
        },
      ],
      stats: {},
      notes: null,
      error: null,
    },
    {
      run_id: "failed_run_20260323_122000",
      source: "api",
      status: "failed",
      created_at: "2026-03-23T12:20:00Z",
      started_at: "2026-03-23T12:20:02Z",
      completed_at: "2026-03-23T12:21:00Z",
      config_name: "real_first_run.yaml",
      config_path: "C:/Projects/foot_ball_tracking/config/real_first_run.yaml",
      input_video: inputCatalog.videos[0].path,
      output_dir: "C:/Projects/foot_ball_tracking/outputs/api_runs/failed_run_20260323_122000",
      modules_enabled: { postprocess: true, follow_cam: false },
      artifacts: [],
      stats: {},
      notes: null,
      error: "boom",
    },
  ];
}

function renderWorkspaceStage(
  stage: WorkspaceStage,
  overrides: {
    inputCatalog?: InputCatalog;
    configs?: ConfigListItem[];
    runs?: RunRecord[];
    selectedRun?: RunRecord | null;
    selectedConfigName?: string;
    selectedInputPath?: string;
  } = {},
) {
  const inputCatalog = overrides.inputCatalog ?? buildInputCatalog();
  const configs = overrides.configs ?? buildConfigs(inputCatalog);
  const runs = overrides.runs ?? buildRuns(inputCatalog);
  const selectedRun = overrides.selectedRun ?? runs[0] ?? null;
  const selectedConfigName = overrides.selectedConfigName ?? configs[0]?.name ?? "";
  const selectedInputPath = overrides.selectedInputPath ?? inputCatalog.videos[0]?.path ?? "";

  const onDeleteInputVideo = vi.fn(async () => undefined);
  const onDeleteConfig = vi.fn(async () => undefined);
  const onDeleteRunOutput = vi.fn(async () => undefined);

  return {
    onDeleteInputVideo,
    onDeleteConfig,
    onDeleteRunOutput,
    ...render(
      <I18nProvider>
        <WorkspacePage
          stage={stage}
          inputCatalog={inputCatalog}
          configs={configs}
          runs={runs}
          selectedRun={selectedRun}
          selectedInputPath={selectedInputPath}
          selectedConfigName={selectedConfigName}
          loading={false}
          launching={false}
          launchMessage={null}
          fieldPreview={null}
          fieldSuggestion={null}
          fieldLoading={false}
          fieldMessage={null}
          canLoadFieldFromConfig
          canStartBaseline
          onSelectRun={vi.fn()}
          onSelectInput={vi.fn()}
          onSelectConfig={vi.fn()}
          onCaptureFieldPreview={vi.fn(async () => undefined)}
          onLoadFieldFromConfig={vi.fn(async () => undefined)}
          onGenerateFieldSuggestion={vi.fn(async () => undefined)}
          onClearFieldSuggestion={vi.fn()}
          onUpdateFieldSuggestion={vi.fn()}
          onAcceptFieldSuggestion={vi.fn()}
          onStartBaselineRun={vi.fn(async () => undefined)}
          onCreateFollowCamRender={vi.fn(async () => runs[1])}
          onDeleteInputVideo={onDeleteInputVideo}
          onDeleteConfig={onDeleteConfig}
          onDeleteRunOutput={onDeleteRunOutput}
        />
      </I18nProvider>,
    ),
  };
}

describe("WorkspacePage deliverable and history stages", () => {
  beforeEach(() => {
    cleanup();
    window.localStorage.clear();
    setLanguage("en");
  });

  it("keeps deliverable controls visible without showing file management in the deliverable tab", () => {
    renderWorkspaceStage("deliverable");

    expect(screen.getByRole("button", { name: "Start 16:9 deliverable render" })).toBeInTheDocument();
    expect(screen.getByLabelText("Prefer cleaned track CSV")).toBeInTheDocument();
    expect(screen.getByLabelText("Show ball marker")).toBeInTheDocument();
    expect(screen.getByLabelText("Show frame text / annotation")).toBeInTheDocument();
    expect(screen.getByLabelText(/This does not rerun detector or baseline/)).toBeInTheDocument();
    expect(screen.queryByText("This does not rerun detector or baseline. It reuses the selected completed run and renders a clean deliverable.")).not.toBeInTheDocument();
    expect(screen.getByText(/A new deliverable folder will be created under:/)).toBeInTheDocument();
    expect(screen.queryByText("Video and config cleanup")).not.toBeInTheDocument();
  });

  it("filters history rows and keeps file management in the history tab", () => {
    renderWorkspaceStage("history");
    const historyFilter = screen.getByRole("tablist", { name: "History filter" });
    const historySection = historyFilter.closest("section");
    expect(historySection).not.toBeNull();
    const historyWithin = within(historySection!);

    const resourceHeading = screen.getByText("Video and config cleanup");
    const resourcePanel = resourceHeading.closest("details");
    expect(resourcePanel).not.toBeNull();
    expect(resourcePanel?.open).toBe(false);
    fireEvent.click(resourceHeading.closest("summary")!);
    expect(screen.getByText("Output folders")).toBeInTheDocument();
    expect(screen.getAllByText(/Mar/).length).toBeGreaterThan(0);

    expect(historyWithin.getAllByText("baseline_run_20260323_120000").length).toBeGreaterThan(0);
    expect(historyWithin.getAllByText("Completed").length).toBeGreaterThan(0);
    expect(historyWithin.getAllByText("Baseline").length).toBeGreaterThan(0);
    expect(historyWithin.getAllByText("C:/Projects/foot_ball_tracking/outputs/api_runs/baseline_run_20260323_120000").length).toBeGreaterThan(0);
    expect(historyWithin.queryByText("Ran at")).not.toBeInTheDocument();

    fireEvent.click(screen.getAllByRole("button", { name: "Deliverable" })[0]);
    expect(historyWithin.getAllByText("deliverable_run_20260323_121000").length).toBeGreaterThan(0);
    expect(historyWithin.queryByText("failed_run_20260323_122000")).not.toBeInTheDocument();

    fireEvent.click(screen.getAllByRole("button", { name: "Failed" })[0]);
    expect(historyWithin.getByText("failed_run_20260323_122000")).toBeInTheDocument();
    expect(historyWithin.queryByText("deliverable_run_20260323_121000")).not.toBeInTheDocument();
  });

  it("requires typing DELETE before a file delete action can proceed", async () => {
    const view = renderWorkspaceStage("history");

    const resourceHeading = screen.getByText("Video and config cleanup");
    fireEvent.click(resourceHeading.closest("summary")!);

    fireEvent.click(screen.getAllByRole("button", { name: "Delete" })[0]);
    expect(screen.getByRole("dialog", { name: "Confirm deletion" })).toBeInTheDocument();

    const confirmButton = screen.getByRole("button", { name: "Confirm delete" });
    expect(confirmButton).toBeDisabled();

    fireEvent.change(screen.getByLabelText("Type DELETE to continue"), { target: { value: "DELETE" } });
    expect(confirmButton).not.toBeDisabled();

    fireEvent.click(confirmButton);
    await waitFor(() => expect(view.onDeleteInputVideo).toHaveBeenCalledWith("game_01.mp4"));
  });

  it("sorts baseline configs by newest time and keeps helper copy inside tooltips", () => {
    const inputCatalog = buildInputCatalog();
    const baseConfig = buildConfigs(inputCatalog)[0];
    const configs: ConfigListItem[] = [
      {
        ...baseConfig,
        name: "older_probe.yaml",
        path: "C:/Projects/foot_ball_tracking/config/older_probe.yaml",
        created_at: "2025-01-01T10:00:00Z",
      },
      {
        ...baseConfig,
        name: "latest_probe.yaml",
        path: "C:/Projects/foot_ball_tracking/config/latest_probe.yaml",
        created_at: "2030-01-02T03:04:00Z",
      },
    ];

    renderWorkspaceStage("baseline", {
      inputCatalog,
      configs,
      selectedConfigName: "latest_probe.yaml",
    });

    const configSelect = screen.getAllByRole("combobox")[1];
    const options = within(configSelect).getAllByRole("option");
    expect(options[0]).toHaveTextContent("latest_probe.yaml");
    expect(options[1]).toHaveTextContent("older_probe.yaml");
    expect(screen.getAllByText("latest_probe.yaml").length).toBeGreaterThan(0);
    expect(screen.getAllByText(/Jan/).length).toBeGreaterThan(0);
    expect(screen.queryByText("Pick the source video for the next baseline run.")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Pick the source video for the next baseline run.")).toBeInTheDocument();
    expect(
      screen.getByLabelText("Keep the main surface simple. Open this only when you need full paths and config context."),
    ).toBeInTheDocument();
  });

  it("moves stage-2 focus helper copy into tooltips instead of inline notes", () => {
    renderWorkspaceStage("ai");

    expect(screen.queryByText("Only runs created from the current source clip appear here.")).not.toBeInTheDocument();
    expect(screen.getAllByLabelText("Only runs created from the current source clip appear here.").length).toBeGreaterThan(0);
  });
});
