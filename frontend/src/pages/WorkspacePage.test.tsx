import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { I18nProvider } from "../lib/i18n";
import type { ConfigListItem, InputCatalog, RunRecord } from "../lib/types";
import { WorkspacePage } from "./WorkspacePage";

function setLanguage(value: "en" | "zh") {
  window.localStorage.setItem("football-tracking-language", value);
}

function renderDeliveryStage() {
  const inputCatalog: InputCatalog = {
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

  const configs: ConfigListItem[] = [
    {
      name: "real_first_run.yaml",
      path: "C:/Projects/foot_ball_tracking/config/real_first_run.yaml",
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

  const runs: RunRecord[] = [
    {
      run_id: "run_20260323_120000",
      source: "api",
      status: "completed",
      created_at: "2026-03-23T12:00:00Z",
      started_at: "2026-03-23T12:00:05Z",
      completed_at: "2026-03-23T12:05:00Z",
      config_name: "real_first_run.yaml",
      config_path: "C:/Projects/foot_ball_tracking/config/real_first_run.yaml",
      input_video: inputCatalog.videos[0].path,
      output_dir: "C:/Projects/foot_ball_tracking/outputs/api_runs/run_20260323_120000",
      modules_enabled: { postprocess: true, follow_cam: true },
      artifacts: [
        {
          name: "ball_track.csv",
          path: "C:/Projects/foot_ball_tracking/outputs/api_runs/run_20260323_120000/ball_track.csv",
          kind: "csv",
          exists: true,
        },
      ],
      stats: {},
      notes: null,
      error: null,
    },
  ];

  return render(
    <I18nProvider>
      <WorkspacePage
        stage="delivery"
        inputCatalog={inputCatalog}
        configs={configs}
        runs={runs}
        selectedRun={runs[0]}
        selectedInputPath={inputCatalog.videos[0].path}
        selectedConfigName={configs[0].name}
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
        onCreateFollowCamRender={vi.fn(async () => runs[0])}
        onDeleteInputVideo={vi.fn(async () => undefined)}
        onDeleteConfig={vi.fn(async () => undefined)}
      />
    </I18nProvider>,
  );
}

describe("WorkspacePage delivery stage", () => {
  beforeEach(() => {
    window.localStorage.clear();
    setLanguage("en");
  });

  it("keeps standalone deliverable controls visible and resource management collapsed by default", () => {
    renderDeliveryStage();

    expect(screen.getByRole("button", { name: "Start 16:9 deliverable render" })).toBeInTheDocument();
    expect(screen.getByLabelText("Prefer cleaned track CSV")).toBeInTheDocument();
    expect(screen.getByLabelText("Show ball marker")).toBeInTheDocument();
    expect(screen.getByLabelText("Show frame text / annotation")).toBeInTheDocument();

    const resourceHeading = screen.getByText("Video and config cleanup");
    const resourcePanel = resourceHeading.closest("details");
    expect(resourcePanel).not.toBeNull();
    expect(resourcePanel?.open).toBe(false);
  });

  it("renders history rows in compact form with run id, time, and output folder", () => {
    renderDeliveryStage();

    expect(screen.getAllByText("run_20260323_120000").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Completed").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Baseline").length).toBeGreaterThan(0);
    expect(screen.getAllByText("C:/Projects/foot_ball_tracking/outputs/api_runs/run_20260323_120000").length).toBeGreaterThan(0);
    expect(screen.queryByText("Ran at")).not.toBeInTheDocument();
  });
});
