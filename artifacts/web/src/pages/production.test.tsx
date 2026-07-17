import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { LanguageProvider, useLanguage } from "@/contexts/LanguageContext";
import {
  createSafeBrowserStorage,
  type SafeBrowserStorage,
} from "@/lib/browserStorage";
import {
  PRODUCTION_DRAFT_STORAGE_KEY,
  createProductionDraft,
  saveProductionDraft,
  updateProductionSource,
  updateProductionTrial,
  type SourceSignature,
} from "@/lib/productionWorkflow";
import {
  appendProductionTrialAttempt,
  buildProductionTrialSubmission,
  createProductionTrialState,
  setPendingProductionTrial,
} from "@/lib/productionTrial";

const source: SourceSignature = {
  path: "data/match-a.mp4",
  size_bytes: 1_024,
  modified_at: "2026-07-14T10:00:00Z",
};

const refetch = vi.fn();
let queryResult: Record<string, unknown>;
const trialStepCapture = vi.hoisted(() => ({
  props: null as null | {
    onTrialChange: (
      trial: ReturnType<typeof createProductionTrialState>,
      expected: ReturnType<typeof createProductionTrialState> | null,
    ) => boolean;
    stopButtonRef?: React.Ref<HTMLButtonElement>;
  },
}));

vi.mock("@workspace/api-client-react", () => ({
  useListInputVideos: () => queryResult,
}));

vi.mock("@/components/production/ProductionCalibrationStep", () => ({
  ProductionCalibrationStep: () => <div>interactive calibration</div>,
}));

vi.mock("@/components/production/ProductionTrialStep", () => ({
  ProductionTrialStep: (props: {
    onTrialChange: (
      trial: ReturnType<typeof createProductionTrialState>,
      expected: ReturnType<typeof createProductionTrialState> | null,
    ) => boolean;
    stopButtonRef?: React.Ref<HTMLButtonElement>;
  }) => {
    trialStepCapture.props = props;
    return (
      <div>
        interactive trial
        <button ref={props.stopButtonRef} type="button">
          Cancel trial
        </button>
      </div>
    );
  },
}));

import { ProductionPageContent } from "./production";

function TestLanguageToggle() {
  const { language, setLanguage } = useLanguage();
  return (
    <button
      type="button"
      onClick={() => setLanguage(language === "en" ? "zh" : "en")}
    >
      test language toggle
    </button>
  );
}

function renderPage(storage?: SafeBrowserStorage) {
  return {
    user: userEvent.setup(),
    ...render(
      <LanguageProvider>
        <TestLanguageToggle />
        <ProductionPageContent storage={storage} />
      </LanguageProvider>,
    ),
  };
}

beforeEach(() => {
  window.history.replaceState({}, "", "/production");
  createSafeBrowserStorage().removeItem(PRODUCTION_DRAFT_STORAGE_KEY);
  queryResult = {
    data: { root_dir: "data", videos: [source] },
    isLoading: false,
    isError: false,
    error: null,
    refetch,
  };
  refetch.mockReset();
  trialStepCapture.props = null;
});

function completedCalibrationDraft() {
  const draft = updateProductionSource(
    createProductionDraft("2026-07-14T12:00:00Z", "workflow-a"),
    source,
  );
  draft.calibration = {
    source_resolution: { width: 1_920, height: 1_080 },
    suggestion: null,
    approved_polygon: [
      [0, 0],
      [1_919, 0],
      [1_919, 1_079],
    ],
    exclusions: [],
    polygon_digest: "c".repeat(64),
    confirmed_frames: [1, 2, 3].map((frame_index, sample_index) => ({
      input_video: source.path,
      frame_index,
      frame_time_seconds: frame_index / 25,
      sample_index,
      source_resolution: { width: 1_920, height: 1_080 },
      polygon_digest: "c".repeat(64),
    })),
  };
  return draft;
}

async function draftWithUnsettledTrial(kind: "pending" | "running") {
  const draft = completedCalibrationDraft();
  const settings = createProductionTrialState().settings;
  const submission = await buildProductionTrialSubmission({
    workflow_id: draft.workflow_id,
    source,
    calibration: draft.calibration!,
    settings,
    parent_run_id: null,
    submission_id: "submission-unsettled",
    output_id: "output-unsettled",
    generation: 1,
    created_at: "2026-07-14T12:05:00Z",
  });
  const pending = setPendingProductionTrial(
    createProductionTrialState(settings),
    submission.pending,
  );
  const trial =
    kind === "pending"
      ? pending
      : appendProductionTrialAttempt(pending, {
          run: {
            run_id: `production_trial_${submission.pending.output_id}`,
            status: "running",
          },
          pending: submission.pending,
          observed_at: "2026-07-14T12:06:00Z",
        });
  return updateProductionTrial(draft, trial);
}

describe("ProductionPage", () => {
  it("fails closed for a run URL without a persisted parent and offers focused history", () => {
    window.history.replaceState({}, "", "/production?run=unknown-run");
    renderPage();
    expect(screen.getByTestId("production-full-run-error")).toBeVisible();
    expect(
      screen.getByRole("link", { name: /production history/i }),
    ).toHaveAttribute("href", "/history?run=unknown-run&from=production");
    expect(
      screen.queryByRole("heading", { name: /match production/i }),
    ).toBeNull();
  });

  it("explains legacy baseline migration while keeping source prerequisites", () => {
    window.history.replaceState({}, "", "/production?from=baseline");
    renderPage();
    expect(screen.getByRole("status")).toHaveTextContent(
      /baseline link moved here.*original video/i,
    );
    expect(screen.getByRole("heading", { name: /choose the original video/i })).toBeVisible();
  });

  it("saves and exits to Production History instead of looping to Production", async () => {
    const view = renderPage();
    await view.user.click(screen.getByRole("button", { name: "Save and exit" }));
    expect(`${window.location.pathname}${window.location.search}`).toBe("/history");
  });

  it("applies sequential async trial callbacks to the latest persisted draft", () => {
    saveProductionDraft(localStorage, completedCalibrationDraft());
    renderPage();
    expect(trialStepCapture.props).not.toBeNull();
    const callback = trialStepCapture.props!.onTrialChange;
    const first = createProductionTrialState({
      base_config_name: "default.yaml",
      start_frame: 0,
      max_frames: 100,
      enable_postprocess: true,
      enable_follow_cam: true,
      tuning_patch: {},
    });
    const second = {
      ...first,
      settings: { ...first.settings, start_frame: 50, max_frames: 200 },
    };
    act(() => {
      expect(callback(first, null)).toBe(true);
      expect(callback(second, first)).toBe(true);
    });
    const persisted = JSON.parse(
      localStorage.getItem(PRODUCTION_DRAFT_STORAGE_KEY) ?? "null",
    );
    expect(persisted.trial.settings).toMatchObject({
      start_frame: 50,
      max_frames: 200,
    });
    expect(persisted.calibration.polygon_digest).toBe("c".repeat(64));
  });

  it("rejects a stale trial callback whose expected snapshot was replaced", () => {
    saveProductionDraft(localStorage, completedCalibrationDraft());
    renderPage();
    const callback = trialStepCapture.props!.onTrialChange;
    const current = createProductionTrialState();
    const staleResult = {
      ...current,
      settings: { ...current.settings, start_frame: 99 },
    };
    act(() => {
      expect(callback(current, null)).toBe(true);
      expect(callback(staleResult, null)).toBe(false);
    });
    const persisted = JSON.parse(
      localStorage.getItem(PRODUCTION_DRAFT_STORAGE_KEY) ?? "null",
    );
    expect(persisted.trial.settings.start_frame).toBe(0);
  });
  it("renders loading, error, and empty catalog states", async () => {
    queryResult = { ...queryResult, data: undefined, isLoading: true };
    const loading = renderPage();
    expect(screen.getByText("Loading original videos…")).toBeVisible();
    loading.unmount();

    queryResult = {
      ...queryResult,
      data: undefined,
      isLoading: false,
      isError: true,
      error: new Error("offline"),
    };
    const error = renderPage();
    expect(
      screen.getByText("Original videos could not be loaded."),
    ).toBeVisible();
    await error.user.click(screen.getByRole("button", { name: "Retry" }));
    expect(refetch).toHaveBeenCalledOnce();
    error.unmount();

    queryResult = {
      ...queryResult,
      data: { root_dir: "data", videos: [] },
      isError: false,
      error: null,
    };
    renderPage();
    expect(screen.getByText("No input videos are available.")).toBeVisible();
  });

  it("recovers explicitly from a corrupt draft", async () => {
    localStorage.setItem(PRODUCTION_DRAFT_STORAGE_KEY, "{broken");
    const { user } = renderPage();
    expect(
      screen.getByRole("heading", { name: "Production draft needs recovery" }),
    ).toBeVisible();
    expect(
      screen.getByText(
        "The saved production draft is damaged and cannot be opened safely.",
      ),
    ).toBeVisible();

    await user.click(
      screen.getByRole("button", { name: "Discard draft and start over" }),
    );
    expect(
      screen.getByRole("heading", { name: "Choose the original video" }),
    ).toBeVisible();
    expect(localStorage.getItem(PRODUCTION_DRAFT_STORAGE_KEY)).toBeNull();
  });

  it("keeps an unsupported draft until the operator discards it", () => {
    const raw = JSON.stringify({ schema_version: 99 });
    localStorage.setItem(PRODUCTION_DRAFT_STORAGE_KEY, raw);
    renderPage();
    expect(
      screen.getByText("This draft uses unsupported schema version 99."),
    ).toBeVisible();
    expect(localStorage.getItem(PRODUCTION_DRAFT_STORAGE_KEY)).toBe(raw);
  });

  it("restores a valid source-bound draft and relocalizes its notice", async () => {
    const draft = updateProductionSource(
      createProductionDraft("2026-07-14T12:00:00Z", "workflow-a"),
      source,
    );
    saveProductionDraft(localStorage, draft);
    const { user } = renderPage();
    expect(
      screen.getByText("Your unfinished production was restored."),
    ).toBeVisible();
    expect(
      screen.getByRole("heading", { name: "Field calibration" }),
    ).toBeVisible();
    await user.click(
      screen.getByRole("button", { name: "test language toggle" }),
    );
    expect(screen.getByText("已恢复未完成的比赛制作。")).toBeVisible();
  });

  it("relocalizes a visible persistence error", async () => {
    const failingStorage: SafeBrowserStorage = {
      isPersistent: true,
      unavailableReason: null,
      getItem: () => null,
      setItem: () => {
        throw new DOMException("blocked", "SecurityError");
      },
      removeItem: () => undefined,
    };
    const { user } = renderPage(failingStorage);

    await user.selectOptions(
      screen.getByLabelText("Original video"),
      source.path,
    );
    expect(
      screen.getByText(/draft could not be saved on this device.*blocked/i),
    ).toBeVisible();
    await user.click(
      screen.getByRole("button", { name: "test language toggle" }),
    );
    expect(screen.getByText(/无法在当前设备保存草稿.*blocked/i)).toBeVisible();
  });

  it("detects same-path file replacement and waits for explicit reset", async () => {
    const draft = updateProductionSource(
      createProductionDraft("2026-07-14T12:00:00Z", "workflow-a"),
      source,
    );
    draft.calibration = {
      source_resolution: { width: 1_920, height: 1_080 },
      suggestion: null,
      approved_polygon: [
        [0, 0],
        [1_919, 0],
        [1_919, 1_079],
      ],
      exclusions: [],
      polygon_digest: "c".repeat(64),
      confirmed_frames: [1, 2, 3].map((frame_index, sample_index) => ({
        input_video: source.path,
        frame_index,
        frame_time_seconds: frame_index / 25,
        sample_index,
        source_resolution: { width: 1_920, height: 1_080 },
        polygon_digest: "c".repeat(64),
      })),
    };
    saveProductionDraft(localStorage, draft);
    queryResult = {
      ...queryResult,
      data: {
        root_dir: "data",
        videos: [{ ...source, size_bytes: source.size_bytes + 10 }],
      },
    };

    const { user } = renderPage();
    expect(
      screen.getByText(/file changed after the draft was saved/i),
    ).toBeVisible();
    await user.click(
      screen.getByRole("button", {
        name: "Use current file and reset downstream",
      }),
    );
    expect(
      screen.queryByText(/file changed after the draft was saved/i),
    ).not.toBeInTheDocument();
    expect(
      screen.getByText(/calibration and all downstream results were cleared/i),
    ).toBeVisible();
    const stored = JSON.parse(
      localStorage.getItem(PRODUCTION_DRAFT_STORAGE_KEY) ?? "{}",
    );
    expect(stored.source.size_bytes).toBe(source.size_bytes + 10);
    expect(stored.calibration).toBeNull();
  });

  it("requires confirmation before replacing an unfinished draft", async () => {
    const draft = updateProductionSource(
      createProductionDraft("2026-07-14T12:00:00Z", "workflow-a"),
      source,
    );
    saveProductionDraft(localStorage, draft);
    const { user } = renderPage();

    const startNewButton = screen.getByRole("button", {
      name: "Start new production",
    });
    await user.click(startNewButton);
    expect(screen.getByRole("alertdialog")).toBeVisible();
    expect(screen.getByText("Replace unfinished production?")).toBeVisible();
    await user.click(
      screen.getByRole("button", { name: "Keep current production" }),
    );
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(startNewButton).toHaveFocus();
    expect(
      JSON.parse(localStorage.getItem(PRODUCTION_DRAFT_STORAGE_KEY) ?? "{}")
        .source,
    ).toEqual(source);

    await user.click(startNewButton);
    await user.click(
      screen.getByRole("button", { name: "Replace and start new" }),
    );
    expect(
      screen.getByRole("heading", { name: "Choose the original video" }),
    ).toBeVisible();
    expect(localStorage.getItem(PRODUCTION_DRAFT_STORAGE_KEY)).toBeNull();
  });

  it("preserves an active trial when Back or Start New is attempted", async () => {
    const active = await draftWithUnsettledTrial("running");
    saveProductionDraft(localStorage, active);
    const { user } = renderPage();
    const cancel = screen.getByRole("button", { name: "Cancel trial" });

    await user.click(screen.getByRole("button", { name: "Back" }));
    expect(screen.getByText(/still queued or running/i)).toBeVisible();
    expect(cancel).toHaveFocus();
    await user.click(
      screen.getByRole("button", { name: "Start new production" }),
    );
    expect(
      screen.queryByText("Replace unfinished production?"),
    ).not.toBeInTheDocument();
    expect(cancel).toHaveFocus();

    const persisted = JSON.parse(
      localStorage.getItem(PRODUCTION_DRAFT_STORAGE_KEY) ?? "null",
    );
    expect(persisted.trial.active_run_id).toBe(
      "production_trial_output-unsettled",
    );
  });

  it("preserves a pending slow submission through upstream and Start New attempts", async () => {
    const pending = await draftWithUnsettledTrial("pending");
    saveProductionDraft(localStorage, pending);
    queryResult = {
      ...queryResult,
      data: {
        root_dir: "data",
        videos: [{ ...source, size_bytes: source.size_bytes + 1 }],
      },
    };
    const { user } = renderPage();
    const heading = screen.getByRole("heading", { name: "Trial and tuning" });
    expect(
      screen.queryByTestId("production-source-select"),
    ).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Back" }));
    expect(screen.getByText(/still being checked/i)).toBeVisible();
    expect(heading).toHaveFocus();
    await user.click(
      screen.getByRole("button", { name: "Start new production" }),
    );
    expect(
      screen.queryByText("Replace unfinished production?"),
    ).not.toBeInTheDocument();

    const persisted = JSON.parse(
      localStorage.getItem(PRODUCTION_DRAFT_STORAGE_KEY) ?? "null",
    );
    expect(persisted.trial.pending_submission.submission_id).toBe(
      "submission-unsettled",
    );
  });

  it("renders with an unsaved fallback when the localStorage getter throws", async () => {
    const descriptor = Object.getOwnPropertyDescriptor(window, "localStorage");
    Object.defineProperty(window, "localStorage", {
      configurable: true,
      get() {
        throw new DOMException("blocked", "SecurityError");
      },
    });

    try {
      const view = renderPage();
      expect(
        screen.getByRole("heading", { name: "Choose the original video" }),
      ).toBeVisible();
      expect(
        screen.getByText(/changes are kept only for this browser session/i),
      ).toBeVisible();
      await view.user.selectOptions(
        screen.getByLabelText("Original video"),
        source.path,
      );
      expect(screen.getByRole("button", { name: "Next" })).toBeEnabled();
      view.unmount();
    } finally {
      if (descriptor) {
        Object.defineProperty(window, "localStorage", descriptor);
      } else {
        Reflect.deleteProperty(window, "localStorage");
      }
    }
  });

  it("restores the in-memory draft after save and exit when storage is read-only", async () => {
    const setItem = vi
      .spyOn(Storage.prototype, "setItem")
      .mockImplementation(() => {
        throw new DOMException("read only", "SecurityError");
      });

    try {
      const first = renderPage();
      await first.user.selectOptions(
        screen.getByLabelText("Original video"),
        source.path,
      );
      expect(
        screen.getByText(/changes are kept only for this browser session/i),
      ).toBeVisible();
      await first.user.click(
        screen.getByRole("button", { name: "test language toggle" }),
      );
      expect(screen.getByText(/修改只会保留在当前浏览器会话中/)).toBeVisible();
      await first.user.click(
        screen.getByRole("button", { name: "保存并退出" }),
      );
      first.unmount();

      const remounted = renderPage();
      expect(screen.getByRole("heading", { name: "球场校准" })).toBeVisible();
      expect(screen.getByText(/data\/match-a\.mp4/)).toBeVisible();
      expect(screen.getByText(/修改只会保留在当前浏览器会话中/)).toBeVisible();
      remounted.unmount();
    } finally {
      setItem.mockRestore();
    }
  });
});
