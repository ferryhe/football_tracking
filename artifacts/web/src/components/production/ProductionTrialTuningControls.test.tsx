import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { LanguageProvider } from "@/contexts/LanguageContext";
import { translations } from "@/lib/i18n";
import type {
  ProductionTrialTuningControl,
  ProductionTuningValue,
} from "@/lib/productionTrial";

import { ProductionTrialTuningControls } from "./ProductionTrialTuningControls";

const controls: ProductionTrialTuningControl[] = [
  {
    path: "detector.confidence_threshold",
    section: "detector",
    kind: "number",
    minimum: 0.01,
    maximum: 0.9,
    step: 0.01,
    runtime_impact: "low",
    description:
      "Lower values find more tiny balls but can also keep more false positives.",
    description_zh: "数值越低越容易找到远处小球，但也可能保留更多误检。",
  },
  {
    path: "sahi.slice_height",
    section: "sahi",
    kind: "integer",
    minimum: 320,
    maximum: 1920,
    step: 16,
    runtime_impact: "high",
    description: "Height of each small-object inference slice.",
    description_zh: "小目标切片推理使用的切片高度。",
  },
  {
    path: "filtering.min_width",
    section: "filtering",
    kind: "number",
    minimum: 1,
    maximum: 100,
    step: 1,
    runtime_impact: "low",
    description: "Smallest candidate width kept for selection.",
    description_zh: "进入候选选择的最小候选宽度。",
  },
  {
    path: "selection.min_accept_score",
    section: "selection",
    kind: "number",
    minimum: 0.01,
    maximum: 0.95,
    step: 0.01,
    runtime_impact: "low",
    description: "Minimum score required to select a candidate.",
    description_zh: "候选进入轨迹前需要达到的最低分数。",
  },
  {
    path: "tracking.max_lost_frames",
    section: "tracking",
    kind: "integer",
    minimum: 0,
    maximum: 120,
    step: 1,
    runtime_impact: "low",
    description: "How long tracking may continue without a detection.",
    description_zh: "没有检测结果时仍允许轨迹延续的帧数。",
  },
  {
    path: "postprocess.low_confidence_threshold",
    section: "postprocess",
    kind: "number",
    minimum: 0,
    maximum: 1,
    step: 0.01,
    runtime_impact: "low",
    description: "Threshold used to clean weak trajectory points.",
    description_zh: "清理低可信轨迹点所使用的阈值。",
  },
];

const currentValues: Record<string, ProductionTuningValue> = {
  "detector.confidence_threshold": 0.25,
  "sahi.slice_height": 720,
  "filtering.min_width": 2,
  "selection.min_accept_score": 0.2,
  "tracking.max_lost_frames": 15,
  "postprocess.low_confidence_threshold": 0.15,
};

function renderControls(
  changes: Partial<
    React.ComponentProps<typeof ProductionTrialTuningControls>
  > = {},
) {
  const onValueChange = vi.fn();
  const props: React.ComponentProps<typeof ProductionTrialTuningControls> = {
    controls,
    currentValues,
    draft: currentValues,
    disabled: false,
    onValueChange,
    ...changes,
  };
  const view = render(
    <LanguageProvider>
      <ProductionTrialTuningControls {...props} />
    </LanguageProvider>,
  );
  return { ...view, props, onValueChange, user: userEvent.setup() };
}

describe("ProductionTrialTuningControls", () => {
  it("groups the approved controls into six compact keyboard-accessible tabs", async () => {
    const { user } = renderControls();

    expect(
      screen.getByRole("tablist", {
        name: "Bounded trial parameter categories",
      }),
    ).toBeVisible();
    expect(screen.getAllByRole("tab")).toHaveLength(6);
    expect(
      screen.getByRole("spinbutton", {
        name: "Detection confidence threshold",
      }),
    ).toBeVisible();

    const detectorTab = screen.getByRole("tab", { name: "Detector" });
    const slicingTab = screen.getByRole("tab", {
      name: "Small-object slicing",
    });
    detectorTab.focus();
    await user.keyboard("{ArrowRight}");

    expect(slicingTab).toHaveFocus();
    expect(
      screen.getByRole("spinbutton", { name: "Slice height" }),
    ).toBeVisible();
    expect(
      screen.queryByRole("spinbutton", {
        name: "Detection confidence threshold",
      }),
    ).toBeNull();

    await user.keyboard("{End}");
    expect(screen.getByRole("tab", { name: "Post-processing" })).toHaveFocus();
    expect(
      screen.getByRole("spinbutton", {
        name: "Low-confidence threshold",
      }),
    ).toBeVisible();

    await user.keyboard("{Home}");
    expect(detectorTab).toHaveFocus();
    expect(
      screen.getByRole("spinbutton", {
        name: "Detection confidence threshold",
      }),
    ).toBeVisible();
  });

  it("keeps the 44px help control usable while mutations are locked", async () => {
    const { user } = renderControls({ disabled: true });
    const input = screen.getByRole("spinbutton", {
      name: "Detection confidence threshold",
    });
    const help = screen.getByRole("button", {
      name: "Explain Detection confidence threshold",
    });

    expect(input).toBeDisabled();
    expect(help).toBeEnabled();
    expect(help).toHaveClass("h-11", "w-11");

    help.focus();
    await user.keyboard("{Enter}");
    let explanation = screen.getByRole("dialog", {
      name: "Detection confidence threshold",
    });
    expect(explanation).toHaveTextContent(
      "Lower values find more tiny balls but can also keep more false positives.",
    );
    expect(explanation).toHaveTextContent("Range 0.01–0.9 · step 0.01");
    expect(explanation).toHaveTextContent("Runtime impact: Low");
    expect(explanation).toHaveTextContent("detector.confidence_threshold");

    await user.keyboard("{Escape}");
    expect(explanation).not.toBeInTheDocument();
    expect(help).toHaveFocus();

    await user.keyboard(" ");
    explanation = screen.getByRole("dialog", {
      name: "Detection confidence threshold",
    });
    expect(explanation).toBeVisible();
    await user.keyboard("{Escape}");
    expect(help).toHaveFocus();
    expect(screen.getByRole("tab", { name: "Tracking" })).toBeEnabled();
  });

  it("preserves proposed values while moving between categories", async () => {
    const { user } = renderControls({
      draft: {
        ...currentValues,
        "detector.confidence_threshold": 0.18,
      },
    });

    expect(
      screen.getByRole("spinbutton", {
        name: "Detection confidence threshold",
      }),
    ).toHaveValue(0.18);
    expect(screen.getByText("Changed")).toBeVisible();

    await user.click(screen.getByRole("tab", { name: "Tracking" }));
    await user.click(screen.getByRole("tab", { name: "Detector" }));

    expect(
      screen.getByRole("spinbutton", {
        name: "Detection confidence threshold",
      }),
    ).toHaveValue(0.18);
    expect(screen.getByText("Changed")).toBeVisible();
  });

  it("shows friendly labels while emitting the unchanged backend value", async () => {
    const onValueChange = vi.fn();
    const inferenceControl: ProductionTrialTuningControl = {
      path: "detector.inference_mode",
      section: "detector",
      kind: "select",
      options: ["direct_full_frame", "sahi"],
      runtime_impact: "high",
      description: "Choose full-frame or sliced small-object inference.",
      description_zh: "选择整帧或面向小目标的切片推理。",
    };
    const { user } = renderControls({
      controls: [inferenceControl],
      currentValues: { "detector.inference_mode": "direct_full_frame" },
      draft: { "detector.inference_mode": "direct_full_frame" },
      onValueChange,
    });

    await user.selectOptions(
      screen.getByRole("combobox", { name: "Inference mode" }),
      "sahi",
    );

    expect(
      screen.getByRole("option", { name: "Small-object slicing" }),
    ).toHaveValue("sahi");
    expect(onValueChange).toHaveBeenCalledWith(
      "detector.inference_mode",
      "sahi",
    );
  });

  it("provides friendly English and Chinese names for every approved path", () => {
    const paths = [
      "detector.allowed_labels",
      "detector.inference_mode",
      "detector.device",
      "detector.use_half",
      "detector.confidence_threshold",
      "detector.image_size",
      "sahi.slice_height",
      "sahi.slice_width",
      "sahi.overlap_height_ratio",
      "sahi.overlap_width_ratio",
      "sahi.postprocess_match_threshold",
      "filtering.min_confidence",
      "filtering.min_width",
      "filtering.max_width",
      "filtering.min_height",
      "filtering.max_height",
      "filtering.min_aspect_ratio",
      "filtering.max_aspect_ratio",
      "selection.min_accept_score",
      "selection.stable_history_length",
      "selection.weights.distance_score",
      "selection.weights.direction_score",
      "selection.weights.velocity_score",
      "selection.weights.acceleration_penalty",
      "selection.weights.trajectory_length_bonus",
      "selection.weights.confidence",
      "selection.priors.enabled",
      "selection.priors.player_foot_radius_px",
      "selection.priors.player_foot_bonus",
      "selection.priors.recent_player_frame_window",
      "tracking.max_lost_frames",
      "tracking.match_distance",
      "tracking.max_speed",
      "tracking.max_acceleration",
      "tracking.predicted_confidence_decay",
      "postprocess.max_detected_island_length",
      "postprocess.stable_segment_min_length",
      "postprocess.min_jump_distance",
      "postprocess.low_confidence_threshold",
    ];

    for (const path of paths) {
      expect(translations.en.production.trialTuningControlLabel(path)).not.toBe(
        path,
      );
      expect(translations.zh.production.trialTuningControlLabel(path)).not.toBe(
        path,
      );
    }
    expect(
      translations.en.production.trialTuningControlLabel("future.control"),
    ).toBe("future.control");
    expect(
      translations.zh.production.trialTuningControlLabel("future.control"),
    ).toBe("future.control");
  });

  it("renders localized Chinese labels and help without changing the control path", async () => {
    window.localStorage.setItem("app-language", "zh");
    const { user, unmount } = renderControls();

    expect(
      screen.getByRole("tablist", { name: "有限试跑参数分类" }),
    ).toBeVisible();
    expect(screen.getByRole("tab", { name: "检测器" })).toBeVisible();
    const help = screen.getByRole("button", {
      name: "查看“检测置信度”说明",
    });
    await user.click(help);
    expect(
      screen.getByRole("dialog", { name: "检测置信度" }),
    ).toHaveTextContent("detector.confidence_threshold");

    unmount();
    window.localStorage.setItem("app-language", "en");
  });
});
