import { useState } from "react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { LanguageProvider } from "@/contexts/LanguageContext";
import {
  createProductionDraft,
  updateProductionCalibration,
  updateProductionSource,
  type ProductionDraft,
  type SourceSignature,
} from "@/lib/productionWorkflow";
import { ProductionWorkspace } from "./ProductionWorkspace";

vi.mock("./ProductionCalibrationStep", () => ({
  ProductionCalibrationStep: ({
    onUsabilityChange,
  }: {
    onUsabilityChange?: (usable: boolean) => void;
  }) => (
    <div>
      interactive calibration
      <button type="button" onClick={() => onUsabilityChange?.(true)}>
        mark preview usable
      </button>
      <button type="button" onClick={() => onUsabilityChange?.(false)}>
        mark preview unusable
      </button>
    </div>
  ),
}));

const videos: SourceSignature[] = [
  {
    path: "data/match-a.mp4",
    size_bytes: 1_024,
    modified_at: "2026-07-14T10:00:00Z",
  },
  {
    path: "data/match-b.mp4",
    size_bytes: 2_048,
    modified_at: "2026-07-14T11:00:00Z",
  },
];

function draftAtTrial(): ProductionDraft {
  return {
    ...createProductionDraft("2026-07-14T12:00:00Z", "workflow-trial"),
    source: videos[0],
    calibration: {
      source_resolution: { width: 1_920, height: 1_080 },
      suggestion: null,
      approved_polygon: [
        [0, 0],
        [1_919, 0],
        [1_919, 1_079],
      ],
      exclusions: [],
      polygon_digest: "c".repeat(64),
      confirmed_frames: [10, 20, 30].map((frame_index, sample_index) => ({
        input_video: videos[0].path,
        frame_index,
        frame_time_seconds: frame_index / 25,
        sample_index,
        source_resolution: { width: 1_920, height: 1_080 },
        polygon_digest: "c".repeat(64),
      })),
    },
    trial: {
      latest_run_id: "trial-a",
      accepted_run_id: "trial-a",
    },
  };
}

function draftAtFullTracking(): ProductionDraft {
  return {
    ...draftAtTrial(),
    workflow_id: "workflow-full",
    confirmed_config: {
      name: "locked-a.yaml",
      sha256: "a".repeat(64),
    },
  };
}

function draftAtReady(): ProductionDraft {
  return {
    ...draftAtFullTracking(),
    workflow_id: "workflow-ready",
    status: "completed",
    full_run: {
      run_id: "full-a",
      status: "ready",
    },
    verified_product: {
      run_id: "full-a",
      artifact_name: "broadcast.mp4",
      status_generation: "b".repeat(64),
    },
  };
}

function renderWorkspace(
  initialDraft = createProductionDraft("2026-07-14T12:00:00Z", "workflow-a"),
  overrides: Partial<React.ComponentProps<typeof ProductionWorkspace>> = {},
) {
  const onSaveExit = vi.fn();
  const onStartNew = vi.fn();

  function Harness() {
    const [draft, setDraft] = useState<ProductionDraft>(initialDraft);
    return (
      <LanguageProvider>
        <ProductionWorkspace
          draft={draft}
          videos={videos}
          onSourceChange={(source) =>
            setDraft((current) =>
              updateProductionSource(current, source, "2026-07-14T12:05:00Z"),
            )
          }
          onCalibrationChange={(calibration) =>
            setDraft((current) =>
              updateProductionCalibration(current, calibration),
            )
          }
          onSaveExit={onSaveExit}
          onStartNew={onStartNew}
          {...overrides}
        />
      </LanguageProvider>
    );
  }

  return {
    user: userEvent.setup(),
    ...render(<Harness />),
    onSaveExit,
    onStartNew,
  };
}

describe("ProductionWorkspace", () => {
  it("shows only the current source step and guards Next", async () => {
    const { user } = renderWorkspace();

    expect(
      screen.getByRole("heading", { name: "Choose the original video" }),
    ).toBeVisible();
    expect(
      screen.queryByRole("heading", { name: "Field calibration" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByText("Trial and tuning")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();

    await user.selectOptions(
      screen.getByLabelText("Original video"),
      "data/match-a.mp4",
    );
    expect(screen.getByRole("button", { name: "Next" })).toBeEnabled();
  });

  it("collapses the completed source and opens calibration after Next", async () => {
    const { user } = renderWorkspace();
    await user.selectOptions(
      screen.getByLabelText("Original video"),
      "data/match-a.mp4",
    );
    await user.click(screen.getByRole("button", { name: "Next" }));

    expect(screen.getByText("Original video selected")).toBeVisible();
    expect(screen.getByText(/data\/match-a\.mp4/)).toBeVisible();
    expect(
      screen.getByRole("heading", { name: "Field calibration" }),
    ).toBeVisible();
    expect(
      screen.queryByRole("heading", { name: "Choose the original video" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByText("Trial and tuning")).not.toBeInTheDocument();
  });

  it("passes the complete source signature selected by the operator", async () => {
    const onSourceChange = vi.fn();
    const { user } = renderWorkspace(undefined, { onSourceChange });
    await user.selectOptions(
      screen.getByLabelText("Original video"),
      "data/match-b.mp4",
    );
    expect(onSourceChange).toHaveBeenCalledWith(videos[1]);
  });

  it("restores a calibration-stage draft and supports workspace actions", async () => {
    const draft = updateProductionSource(
      createProductionDraft("2026-07-14T12:00:00Z", "workflow-a"),
      videos[0],
    );
    const { user, onSaveExit, onStartNew } = renderWorkspace(draft);

    expect(
      screen.getByRole("heading", { name: "Field calibration" }),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Back" }));
    expect(
      screen.getByRole("heading", { name: "Choose the original video" }),
    ).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Save and exit" }));
    await user.click(
      screen.getByRole("button", { name: "Start new production" }),
    );
    expect(onSaveExit).toHaveBeenCalledOnce();
    expect(onStartNew).toHaveBeenCalledOnce();
  });

  it("restores a trial-stage draft with source and calibration summaries", () => {
    renderWorkspace(draftAtTrial());

    expect(
      screen.getByRole("heading", { name: "Trial and tuning" }),
    ).toBeVisible();
    expect(screen.getByTestId("completed-stage-source")).toBeVisible();
    expect(screen.getByTestId("completed-stage-calibration")).toBeVisible();
    expect(
      screen.queryByTestId("completed-stage-full_tracking"),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
  });

  it("blocks leaving a completed calibration while its current preview is unusable", async () => {
    const { user } = renderWorkspace(draftAtTrial());
    await user.click(screen.getByRole("button", { name: "Back" }));

    const next = screen.getByRole("button", { name: "Next" });
    expect(next).toBeDisabled();
    await user.click(
      screen.getByRole("button", { name: "mark preview usable" }),
    );
    expect(next).toBeEnabled();
    await user.click(
      screen.getByRole("button", { name: "mark preview unusable" }),
    );
    expect(next).toBeDisabled();
  });

  it("restores a full-tracking draft and navigates forward after Back", async () => {
    const { user } = renderWorkspace(draftAtFullTracking());

    expect(
      screen.getByRole("heading", { name: "Full tracking and review" }),
    ).toBeVisible();
    expect(screen.getByTestId("completed-stage-source")).toBeVisible();
    expect(screen.getByTestId("completed-stage-calibration")).toBeVisible();
    expect(screen.getByTestId("completed-stage-trial")).toBeVisible();
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "Back" }));
    expect(
      screen.getByRole("heading", { name: "Trial and tuning" }),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: "Next" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "Next" }));
    expect(
      screen.getByRole("heading", { name: "Full tracking and review" }),
    ).toBeVisible();
  });

  it("restores a ready draft with every completed stage collapsed", async () => {
    const { user } = renderWorkspace(draftAtReady());

    expect(
      screen.getByRole("heading", { name: "Final product" }),
    ).toBeVisible();
    for (const stage of ["source", "calibration", "trial", "full_tracking"]) {
      expect(screen.getByTestId(`completed-stage-${stage}`)).toBeVisible();
    }
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "Back" }));
    expect(
      screen.getByRole("heading", { name: "Full tracking and review" }),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: "Next" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "Next" }));
    expect(
      screen.getByRole("heading", { name: "Final product" }),
    ).toBeVisible();
  });

  it("renders Chinese labels when the saved language is Chinese", () => {
    localStorage.setItem("app-language", "zh");
    renderWorkspace();
    expect(screen.getByRole("heading", { name: "选择原片" })).toBeVisible();
    expect(screen.getByText("步骤 1/5 · 原片")).toBeVisible();
  });
});
