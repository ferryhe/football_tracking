import { useState } from "react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { LanguageProvider } from "@/contexts/LanguageContext";
import {
  createProductionDraft,
  invalidateProductionDraft,
  updateConfirmedProductionConfig,
  updatePendingConfigConfirmation,
  updateProductionCalibration,
  updateProductionSource,
  updateProductionTrial,
  type ProductionDraft,
  type SourceSignature,
} from "@/lib/productionWorkflow";
import type { ProductionTrialState } from "@/lib/productionTrial";
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

vi.mock("./ProductionTrialStep", () => ({
  ProductionTrialStep: ({
    onUsabilityChange,
    stopButtonRef,
  }: {
    onUsabilityChange: (usable: boolean) => void;
    stopButtonRef?: React.Ref<HTMLButtonElement>;
  }) => (
    <div>
      interactive trial
      <button ref={stopButtonRef} type="button">
        Cancel trial
      </button>
      <button type="button" onClick={() => onUsabilityChange(true)}>
        mark trial usable
      </button>
      <button type="button" onClick={() => onUsabilityChange(false)}>
        mark trial unusable
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
    trial: null,
  };
}

function acceptedTrial(): ProductionTrialState {
  const intent = "d".repeat(64);
  const request = "e".repeat(64);
  const evidence = "f".repeat(64);
  return {
    settings: {
      base_config_name: "default.yaml",
      start_frame: 0,
      max_frames: 300,
      enable_postprocess: true,
      enable_follow_cam: true,
      tuning_patch: {},
    },
    attempts: [
      {
        run_id: "trial-a",
        generation: 1,
        submission_id: "submission-a",
        parent_run_id: null,
        intent_sha256: intent,
        request_sha256: request,
        request: {
          config_name: "default.yaml",
          input_video: videos[0].path,
          parent_run_id: null,
          output_dir_name: "production_trial_output-a",
          config_patch: {},
          enable_postprocess: true,
          enable_follow_cam: true,
          start_frame: 0,
          max_frames: 300,
          pipeline_mode: "standard" as const,
          notes: JSON.stringify({
            purpose: "production_trial",
            submission_id: "submission-a",
            output_id: "output-a",
          }),
        },
        created_at: "2026-07-14T12:00:00Z",
        last_observed: {
          status: "completed" as const,
          observed_at: "2026-07-14T12:00:00Z",
          evidence_generation: evidence,
        },
      },
    ],
    active_run_id: null,
    pending_submission: null,
    accepted: {
      run_id: "trial-a",
      intent_sha256: intent,
      request_sha256: request,
      accepted_at: "2026-07-14T12:00:00Z",
      readiness: {
        run_id: "trial-a",
        request_sha256: request,
        evidence_generation: evidence,
        verified_at: "2026-07-14T12:00:00Z",
        video_artifact_name: "follow_cam.mp4",
        artifact_names: [
          "run_manifest.json",
          "metrics_report.json",
          "ball_track.csv",
          "ball_audit.json",
          "ball_track.cleaned.csv",
          "follow_cam.mp4",
        ],
        quality: {
          frame_count: 300,
          detected: 200,
          predicted: 50,
          lost: 50,
          detected_ratio: 2 / 3,
          predicted_ratio: 1 / 6,
          lost_ratio: 1 / 6,
          longest_lost_streak: 5,
          false_positive_island_count: 1,
          max_step_px: 20,
          audit_tracklet_count: 3,
          audit_suspicious_tracklet_count: 1,
          audit_review_event_count: 1,
          audit_lost_gap_count: 2,
          quality_gate_status: null,
        },
      },
    },
  };
}

function draftAtActiveTrial(
  status: "queued" | "running" = "running",
): ProductionDraft {
  const trial = acceptedTrial();
  trial.accepted = null;
  trial.active_run_id = "trial-a";
  trial.attempts[0].last_observed = {
    ...trial.attempts[0].last_observed,
    status,
    evidence_generation: null,
  };
  return { ...draftAtTrial(), trial };
}

function draftAtPendingTrial(): ProductionDraft {
  const trial = acceptedTrial();
  trial.accepted = null;
  trial.pending_submission = {
    generation: 2,
    submission_id: "submission-pending",
    output_id: "output-pending",
    intent_sha256: "1".repeat(64),
    request_sha256: "2".repeat(64),
    request: {
      ...trial.attempts[0].request,
      parent_run_id: "trial-a",
      output_dir_name: "production_trial_output-pending",
    },
    created_at: "2026-07-14T12:05:00Z",
  };
  return { ...draftAtTrial(), trial };
}

function draftAtFullTracking(): ProductionDraft {
  return {
    ...draftAtTrial(),
    workflow_id: "workflow-full",
    trial: acceptedTrial(),
    confirmed_config: {
      name: "locked-a.yaml",
      sha256: "a".repeat(64),
      base_config_name: "default.yaml",
      patch: {},
      patch_sha256: "1".repeat(64),
      trial_patch_sha256: "2".repeat(64),
      workflow_id: "workflow-full",
      accepted_trial_run_id: "trial-a",
      trial_intent_sha256: "d".repeat(64),
      trial_request_sha256: "e".repeat(64),
      calibration_digest: "c".repeat(64),
      source_signature: videos[0],
      confirmed_at: "2026-07-14T12:00:00Z",
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
          onTrialChange={(trial) => {
            setDraft((current) => updateProductionTrial(current, trial));
            return true;
          }}
          onPendingConfigChange={(pending) => {
            setDraft((current) =>
              updatePendingConfigConfirmation(current, pending),
            );
            return true;
          }}
          onConfirmedConfigChange={(confirmed) => {
            setDraft((current) =>
              updateConfirmedProductionConfig(current, confirmed),
            );
            return true;
          }}
          onInvalidate={(from) => {
            setDraft((current) => invalidateProductionDraft(current, from));
            return true;
          }}
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

  it.each(["completed", "failed", "cancelled"] as const)(
    "requires explicit invalidation before reopening calibration with %s trial history",
    async (status) => {
      const draft = {
        ...draftAtTrial(),
        trial: { ...acceptedTrial(), accepted: null },
      };
      draft.trial.attempts[0].last_observed.status = status;
      const { user } = renderWorkspace(draft);
      const back = screen.getByRole("button", { name: "Back" });
      await user.click(back);
      expect(
        screen.getByRole("alertdialog", {
          name: "Edit locked upstream inputs?",
        }),
      ).toBeVisible();
      await user.click(
        screen.getByRole("button", { name: "Keep current evidence" }),
      );
      expect(
        screen.getByRole("heading", { name: "Trial and tuning" }),
      ).toBeVisible();
      expect(back).toHaveFocus();

      await user.click(back);
      await user.click(
        screen.getByRole("button", { name: "Invalidate and edit" }),
      );
      expect(
        screen.getByRole("heading", { name: "Field calibration" }),
      ).toBeVisible();
      expect(screen.getByText("interactive calibration")).toBeVisible();
    },
  );

  it.each(["queued", "running"] as const)(
    "keeps an %s trial monitored and blocks Back and Start New until it is cancelled",
    async (status) => {
      const onInvalidate = vi.fn(() => true);
      const onStartNew = vi.fn();
      const { user } = renderWorkspace(draftAtActiveTrial(status), {
        onInvalidate,
        onStartNew,
      });
      const stop = screen.getByRole("button", { name: "Cancel trial" });
      await user.click(screen.getByRole("button", { name: "Back" }));
      expect(screen.getByText(/still queued or running/i)).toBeVisible();
      expect(stop).toHaveFocus();
      expect(onInvalidate).not.toHaveBeenCalled();
      expect(screen.getByText("interactive trial")).toBeVisible();

      await user.click(
        screen.getByRole("button", { name: "Start new production" }),
      );
      expect(onStartNew).not.toHaveBeenCalled();
      expect(stop).toHaveFocus();
      expect(screen.getByText("interactive trial")).toBeVisible();
    },
  );

  it("keeps Trial and Stop reachable instead of exposing source replacement while active", () => {
    renderWorkspace(draftAtActiveTrial(), { sourceIssue: "changed" });
    expect(screen.getByText("interactive trial")).toBeVisible();
    expect(screen.getByRole("button", { name: "Cancel trial" })).toBeVisible();
    expect(
      screen.queryByTestId("production-source-select"),
    ).not.toBeInTheDocument();
  });

  it("keeps a pending submission while blocking Back, Start New, and source replacement", async () => {
    const onInvalidate = vi.fn(() => true);
    const onStartNew = vi.fn();
    const onSourceChange = vi.fn();
    const { user } = renderWorkspace(draftAtPendingTrial(), {
      sourceIssue: "changed",
      onInvalidate,
      onStartNew,
      onSourceChange,
    });
    const heading = screen.getByRole("heading", { name: "Trial and tuning" });
    expect(
      screen.queryByTestId("production-source-select"),
    ).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Back" }));
    expect(screen.getByText(/still being checked/i)).toBeVisible();
    expect(heading).toHaveFocus();
    expect(
      screen.getByRole("button", { name: "Cancel trial" }),
    ).not.toHaveFocus();

    await user.click(
      screen.getByRole("button", { name: "Start new production" }),
    );
    expect(onInvalidate).not.toHaveBeenCalled();
    expect(onStartNew).not.toHaveBeenCalled();
    expect(onSourceChange).not.toHaveBeenCalled();
    expect(screen.getByText("interactive trial")).toBeVisible();
  });

  it("returns focus to the source selector after cancelling or confirming source invalidation", async () => {
    const { user } = renderWorkspace(draftAtFullTracking(), {
      sourceIssue: "changed",
    });
    const select = screen.getByTestId("production-source-select");
    await user.selectOptions(select, "data/match-b.mp4");
    expect(screen.getByRole("alertdialog")).toBeVisible();
    await user.click(
      screen.getByRole("button", { name: "Keep current evidence" }),
    );
    expect(select).toHaveFocus();
    expect(select.closest('[aria-hidden="true"]')).toBeNull();

    await user.selectOptions(select, "data/match-b.mp4");
    await user.click(
      screen.getByRole("button", { name: "Invalidate and edit" }),
    );
    expect(select).toHaveFocus();
    expect(select).toHaveValue("data/match-b.mp4");
    expect(select.closest('[aria-hidden="true"]')).toBeNull();
  });

  it("restores a full-tracking draft and navigates forward after Back", async () => {
    const { user } = renderWorkspace(draftAtFullTracking());

    expect(
      screen.getByRole("heading", { name: "Trial and tuning" }),
    ).toBeVisible();
    expect(screen.getByTestId("completed-stage-source")).toBeVisible();
    expect(screen.getByTestId("completed-stage-calibration")).toBeVisible();
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "mark trial usable" }));
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
