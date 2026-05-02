import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { I18nProvider } from "../lib/i18n";
import type { ConfigListItem, RunRecord } from "../lib/types";
import { AIPanel } from "./AIPanel";

describe("AIPanel helper copy", () => {
  it("keeps static guidance in tooltips instead of inline text", () => {
    const configs: ConfigListItem[] = [
      {
        name: "real_first_run.yaml",
        path: "C:/Projects/foot_ball_tracking/config/real_first_run.yaml",
        created_at: "2026-03-20T09:00:00Z",
        input_video: "C:/Projects/foot_ball_tracking/data/game_01.mp4",
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

    const run: RunRecord = {
      run_id: "baseline_run_20260323_120000",
      source: "api",
      status: "completed",
      created_at: "2026-03-23T12:00:00Z",
      started_at: "2026-03-23T12:00:05Z",
      completed_at: "2026-03-23T12:05:00Z",
      config_name: "real_first_run.yaml",
      config_path: "C:/Projects/foot_ball_tracking/config/real_first_run.yaml",
      input_video: "C:/Projects/foot_ball_tracking/data/game_01.mp4",
      output_dir: "C:/Projects/foot_ball_tracking/outputs/api_runs/baseline_run_20260323_120000",
      modules_enabled: { postprocess: true, follow_cam: true },
      artifacts: [],
      stats: {},
      notes: null,
      error: null,
    };

    render(
      <I18nProvider>
        <AIPanel
          run={run}
          configs={configs}
          targetInputVideo={run.input_video ?? undefined}
          onConfigDerived={vi.fn()}
          onRunCreated={vi.fn()}
        />
      </I18nProvider>,
    );

    expect(screen.queryByText("Explain one selected run, draft the next config patch, then launch a new task.")).not.toBeInTheDocument();
    expect(screen.queryByText("Pick the focused run above first, then trigger AI only when you need an explanation.")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Explain one selected run, draft the next config patch, then launch a new task.")).toBeInTheDocument();
    expect(screen.getByTitle("Pick the focused run above first, then trigger AI only when you need an explanation.")).toBeInTheDocument();
  });
});
