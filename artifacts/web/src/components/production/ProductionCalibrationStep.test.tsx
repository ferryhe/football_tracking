import { useState } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { LanguageProvider } from "@/contexts/LanguageContext";
import {
  createEmptyCalibration,
  type ProductionCalibrationDraft,
} from "@/lib/productionCalibration";
import type { SourceSignature } from "@/lib/productionWorkflow";

const capture = vi.fn();
const suggest = vi.fn();

vi.mock("@workspace/api-client-react", () => ({
  useCaptureFieldPreview: () => ({ mutateAsync: capture, isPending: false }),
  useSuggestFieldSetup: () => ({ mutateAsync: suggest, isPending: false }),
}));

vi.mock("./FieldPolygonEditor", async () => {
  const React = await import("react");
  return {
    default: (props: {
      approved: [number, number][];
      onChange: (points: [number, number][]) => void;
      onReadyChange: (ready: boolean) => void;
      onSelectVertex: (index: number | null) => void;
    }) => {
      React.useEffect(() => {
        props.onReadyChange(true);
        return () => props.onReadyChange(false);
      }, [props.onReadyChange]);
      return (
        <div data-testid="mock-konva-editor">
          <button
            type="button"
            onClick={() => props.onChange([...props.approved, [300, 300]])}
          >
            mock pointer add
          </button>
          <button type="button" onClick={() => props.onSelectVertex(0)}>
            mock select first
          </button>
        </div>
      );
    },
  };
});

import { ProductionCalibrationStep } from "./ProductionCalibrationStep";

const SOURCE: SourceSignature = {
  path: "data/match-a.mp4",
  size_bytes: 1_024,
  modified_at: "2026-07-14T10:00:00Z",
};

function preview(
  sampleIndex: number,
  frameIndex = sampleIndex * 10,
  source = SOURCE.path,
) {
  return {
    input_video: source,
    preview_data_url: `data:image/png;base64,frame-${frameIndex}`,
    frame_width: 1_920,
    frame_height: 1_080,
    frame_index: frameIndex,
    frame_time_seconds: frameIndex / 25,
    sample_index: sampleIndex,
    sample_count: 3,
  };
}

function suggestion(
  frameIndex = 10,
  polygon: [number, number][] = [
    [100, 100],
    [1_800, 100],
    [1_800, 1_000],
  ],
) {
  return {
    ...preview(1, frameIndex),
    preview_bounds: [0, 0, 1_919, 1_079] as [number, number, number, number],
    field_polygon: polygon,
    expanded_polygon: polygon,
    field_roi: [0, 0, 1_919, 1_079] as [number, number, number, number],
    expanded_roi: [0, 0, 1_919, 1_079] as [number, number, number, number],
    confidence: "detected" as const,
    source: "detector-v2",
    field_coverage: 0.72,
    config_patch: {},
  };
}

function approvedCalibration(): ProductionCalibrationDraft {
  return {
    ...createEmptyCalibration(),
    source_resolution: { width: 1_920, height: 1_080 },
    approved_polygon: [
      [100, 100],
      [1_800, 100],
      [1_800, 1_000],
    ],
    polygon_digest: "a".repeat(64),
  };
}

function completedCalibration(): ProductionCalibrationDraft {
  const current = approvedCalibration();
  current.confirmed_frames = [10, 20, 30].map((frameIndex, sampleIndex) => ({
    input_video: SOURCE.path,
    frame_index: frameIndex,
    frame_time_seconds: frameIndex / 25,
    sample_index: sampleIndex + 1,
    source_resolution: { width: 1_920, height: 1_080 },
    polygon_digest: current.polygon_digest!,
  }));
  return current;
}

function renderStep(
  initial: ProductionCalibrationDraft | null = null,
  onUsabilityChange?: (usable: boolean) => void,
) {
  let latest: ProductionCalibrationDraft | null = initial;

  function Harness() {
    const [calibration, setCalibration] = useState(initial);
    latest = calibration;
    return (
      <LanguageProvider>
        <ProductionCalibrationStep
          source={SOURCE}
          calibration={calibration}
          onChange={setCalibration}
          onUsabilityChange={onUsabilityChange}
        />
      </LanguageProvider>
    );
  }

  return {
    user: userEvent.setup(),
    latest: () => latest,
    ...render(<Harness />),
  };
}

async function loadCurrentImage(frameIndex = 10) {
  const image = await screen.findByAltText(
    `Original source frame ${frameIndex}`,
  );
  fireEvent.load(image);
  await screen.findByTestId("mock-konva-editor");
}

beforeEach(() => {
  capture.mockReset();
  suggest.mockReset();
  capture.mockImplementation(({ data }: { data: { sample_index?: number } }) =>
    Promise.resolve(preview(data.sample_index ?? 1)),
  );
  suggest.mockResolvedValue(suggestion());
});

describe("ProductionCalibrationStep", () => {
  it("keeps system suggestion separate, shows both coordinate sets, and never clamps typed values", async () => {
    const view = renderStep();
    await loadCurrentImage();

    await view.user.click(
      screen.getByRole("button", { name: "Request system suggestion" }),
    );
    expect(
      await screen.findByTestId("suggested-coordinates"),
    ).toHaveTextContent("(100, 100)");
    expect(screen.getByTestId("approved-coordinates")).toHaveTextContent("—");
    expect(screen.getByText(/Source: detector-v2/)).toBeVisible();
    expect(screen.getByText(/Field coverage: 72%/)).toBeVisible();

    await view.user.click(
      screen.getByRole("button", { name: "Use this suggestion" }),
    );
    await waitFor(() =>
      expect(view.latest()?.polygon_digest).toMatch(/^[a-f\d]{64}$/),
    );
    expect(screen.getByTestId("approved-coordinates")).toHaveTextContent(
      "(100, 100)",
    );

    const x = screen.getByLabelText("Point 1 X coordinate");
    await view.user.clear(x);
    await view.user.type(x, "1920");
    fireEvent.blur(x);
    expect(x).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByText(/it was not clamped/i)).toBeVisible();
    expect(view.latest()?.approved_polygon[0][0]).toBe(100);
  });

  it("marks a restored complete calibration unusable until coordinate input is valid and committed", async () => {
    const current = completedCalibration();
    const originalEvidence = structuredClone(current);
    const onUsabilityChange = vi.fn();
    const view = renderStep(current, onUsabilityChange);
    await loadCurrentImage();
    await waitFor(() =>
      expect(onUsabilityChange).toHaveBeenLastCalledWith(true),
    );

    const x = screen.getByLabelText("Point 1 X coordinate");
    await view.user.clear(x);
    await view.user.type(x, "1920");
    fireEvent.blur(x);
    expect(x).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByText(/it was not clamped/i)).toBeVisible();
    expect(view.latest()).toEqual(originalEvidence);
    await waitFor(() =>
      expect(onUsabilityChange).toHaveBeenLastCalledWith(false),
    );

    await view.user.clear(x);
    await view.user.type(x, "120");
    expect(x).toHaveAttribute("aria-invalid", "false");
    expect(onUsabilityChange).toHaveBeenLastCalledWith(false);
    await x.focus();
    await view.user.keyboard("{Enter}");
    await waitFor(() =>
      expect(view.latest()?.approved_polygon[0][0]).toBe(120),
    );
    expect(view.latest()?.confirmed_frames).toEqual([]);
    await waitFor(() =>
      expect(onUsabilityChange).toHaveBeenLastCalledWith(true),
    );
  });

  it("canonicalizes equivalent coordinate formats without changing restored evidence", async () => {
    const current = completedCalibration();
    const originalEvidence = structuredClone(current);
    const onUsabilityChange = vi.fn();
    const view = renderStep(current, onUsabilityChange);
    await loadCurrentImage();
    await waitFor(() =>
      expect(onUsabilityChange).toHaveBeenLastCalledWith(true),
    );

    const x = screen.getByLabelText("Point 1 X coordinate");
    await view.user.clear(x);
    await view.user.type(x, "100.0");
    expect(onUsabilityChange).toHaveBeenLastCalledWith(false);
    await view.user.keyboard("{Enter}");
    await waitFor(() => expect(x).toHaveValue("100"));
    expect(x).toHaveAttribute("aria-invalid", "false");
    expect(view.latest()).toEqual(originalEvidence);
    await waitFor(() =>
      expect(onUsabilityChange).toHaveBeenLastCalledWith(true),
    );

    await view.user.clear(x);
    await view.user.type(x, "0100");
    expect(onUsabilityChange).toHaveBeenLastCalledWith(false);
    fireEvent.blur(x);
    await waitFor(() => expect(x).toHaveValue("100"));
    expect(x).toHaveAttribute("aria-invalid", "false");
    expect(view.latest()).toEqual(originalEvidence);
    await waitFor(() =>
      expect(onUsabilityChange).toHaveBeenLastCalledWith(true),
    );
  });

  it("does not let a stale preview response replace a newer source", async () => {
    let resolveFirst!: (value: ReturnType<typeof preview>) => void;
    const first = new Promise<ReturnType<typeof preview>>((resolve) => {
      resolveFirst = resolve;
    });
    capture
      .mockReturnValueOnce(first)
      .mockResolvedValueOnce(preview(1, 90, "data/match-b.mp4"));
    const onChange = vi.fn();
    const view = render(
      <LanguageProvider>
        <ProductionCalibrationStep
          source={SOURCE}
          calibration={null}
          onChange={onChange}
        />
      </LanguageProvider>,
    );
    view.rerender(
      <LanguageProvider>
        <ProductionCalibrationStep
          source={{ ...SOURCE, path: "data/match-b.mp4" }}
          calibration={null}
          onChange={onChange}
        />
      </LanguageProvider>,
    );

    expect(
      await screen.findByAltText("Original source frame 90"),
    ).toBeVisible();
    resolveFirst(preview(1, 10));
    await Promise.resolve();
    expect(
      screen.queryByAltText("Original source frame 10"),
    ).not.toBeInTheDocument();
  });

  it("blocks old-frame suggestions while navigation is pending and discards an earlier response", async () => {
    let resolveSuggestion!: (value: ReturnType<typeof suggestion>) => void;
    let resolveNextPreview!: (value: ReturnType<typeof preview>) => void;
    const nextPreview = new Promise<ReturnType<typeof preview>>((resolve) => {
      resolveNextPreview = resolve;
    });
    suggest.mockReturnValue(
      new Promise<ReturnType<typeof suggestion>>((resolve) => {
        resolveSuggestion = resolve;
      }),
    );
    capture.mockResolvedValueOnce(preview(1)).mockReturnValueOnce(nextPreview);
    const view = renderStep();
    await loadCurrentImage(10);
    await view.user.click(
      screen.getByRole("button", { name: "Request system suggestion" }),
    );
    await view.user.click(screen.getByRole("button", { name: "Next frame" }));
    const requestButton = screen.getByRole("button", {
      name: "Request system suggestion",
    });
    expect(requestButton).toBeDisabled();
    fireEvent.click(requestButton);
    expect(suggest).toHaveBeenCalledTimes(1);

    resolveNextPreview(preview(2));
    await loadCurrentImage(20);
    resolveSuggestion(suggestion(10));
    await waitFor(() => expect(view.latest()?.suggestion).toBeNull());

    expect(
      screen.queryByTestId("suggested-coordinates"),
    ).not.toBeInTheDocument();
    expect(view.latest()?.suggestion).toBeNull();
  });

  it.each([0, 3])(
    "fails closed on a preview resolution mismatch with %i confirmed frames and recovers on a matching preview",
    async (confirmedFrameCount) => {
      const current = approvedCalibration();
      current.suggestion = {
        source_path: SOURCE.path,
        source: "detector-v2",
        confidence: "detected",
        field_coverage: 0.72,
        source_resolution: { width: 1_920, height: 1_080 },
        frame_index: 10,
        polygon: [
          [100, 100],
          [1_800, 100],
          [1_800, 1_000],
        ],
      };
      current.confirmed_frames = [10, 20, 30]
        .slice(0, confirmedFrameCount)
        .map((frameIndex, index) => ({
          input_video: SOURCE.path,
          frame_index: frameIndex,
          frame_time_seconds: frameIndex / 25,
          sample_index: index + 1,
          source_resolution: { width: 1_920, height: 1_080 },
          polygon_digest: current.polygon_digest!,
        }));
      const originalEvidence = structuredClone(current);
      const onUsabilityChange = vi.fn();
      capture.mockResolvedValueOnce({
        ...preview(1),
        frame_width: 1_280,
        frame_height: 720,
      });
      const view = renderStep(current, onUsabilityChange);

      const image = await screen.findByAltText("Original source frame 10");
      fireEvent.load(image);
      expect(
        screen.getByText(/resolution differs from the saved calibration/i),
      ).toBeVisible();
      expect(screen.queryByTestId("mock-konva-editor")).not.toBeInTheDocument();
      expect(
        screen.getByRole("button", { name: "Request another suggestion" }),
      ).toBeDisabled();
      expect(
        screen.getByRole("button", { name: "Use this suggestion" }),
      ).toBeDisabled();
      expect(screen.getByRole("button", { name: "Add point" })).toBeDisabled();
      expect(
        screen.getByRole("button", {
          name: /confirm this frame|frame already/i,
        }),
      ).toBeDisabled();
      expect(view.latest()).toEqual(originalEvidence);
      expect(onUsabilityChange).toHaveBeenLastCalledWith(false);

      await view.user.click(screen.getByRole("button", { name: "Next frame" }));
      await loadCurrentImage(20);
      expect(
        screen.queryByText(/resolution differs from the saved calibration/i),
      ).not.toBeInTheDocument();
      expect(
        screen.getByRole("button", { name: "Request another suggestion" }),
      ).toBeEnabled();
      expect(
        screen.getByRole("button", { name: "Use this suggestion" }),
      ).toBeEnabled();
      expect(view.latest()).toEqual(originalEvidence);
      await waitFor(() =>
        expect(onUsabilityChange).toHaveBeenLastCalledWith(true),
      );
    },
  );

  it("preserves approved values and frame evidence when requesting another suggestion", async () => {
    const current = approvedCalibration();
    current.confirmed_frames = [
      {
        input_video: SOURCE.path,
        frame_index: 10,
        frame_time_seconds: 0.4,
        sample_index: 1,
        source_resolution: { width: 1_920, height: 1_080 },
        polygon_digest: current.polygon_digest!,
      },
    ];
    const view = renderStep(current);
    await loadCurrentImage();
    await view.user.click(
      screen.getByRole("button", { name: "Request system suggestion" }),
    );

    expect(view.latest()?.approved_polygon).toEqual(current.approved_polygon);
    expect(view.latest()?.polygon_digest).toBe(current.polygon_digest);
    expect(view.latest()?.confirmed_frames).toEqual(current.confirmed_frames);
    await view.user.click(
      screen.getByRole("button", { name: "Use this suggestion" }),
    );
    expect(view.latest()?.confirmed_frames).toEqual(current.confirmed_frames);
  });

  it("supports pointer add, selected deletion, undo, clear, and keyboard point creation", async () => {
    const view = renderStep(approvedCalibration());
    await loadCurrentImage();

    await view.user.click(
      screen.getByRole("button", { name: "mock pointer add" }),
    );
    await waitFor(() =>
      expect(view.latest()?.approved_polygon).toHaveLength(4),
    );
    expect(view.latest()?.confirmed_frames).toEqual([]);

    await view.user.click(
      screen.getByRole("button", { name: "mock select first" }),
    );
    await view.user.click(
      screen.getByRole("button", { name: "Delete selected point" }),
    );
    expect(view.latest()?.approved_polygon).toHaveLength(3);

    await view.user.click(screen.getByRole("button", { name: "Undo" }));
    expect(view.latest()?.approved_polygon).toHaveLength(4);
    await view.user.click(screen.getByRole("button", { name: "Clear" }));
    expect(view.latest()?.approved_polygon).toEqual([]);
    await view.user.click(screen.getByRole("button", { name: "Add point" }));
    expect(view.latest()?.approved_polygon).toHaveLength(1);
  });

  it("confirms exactly three distinct actual frames with the current digest", async () => {
    const view = renderStep(approvedCalibration());
    await loadCurrentImage(10);

    await view.user.click(
      screen.getByRole("button", { name: "Confirm this frame" }),
    );
    expect(
      view.latest()?.confirmed_frames.map((frame) => frame.frame_index),
    ).toEqual([10]);

    await view.user.click(screen.getByRole("button", { name: "Next frame" }));
    await loadCurrentImage(20);
    await view.user.click(
      screen.getByRole("button", { name: "Confirm this frame" }),
    );

    await view.user.click(screen.getByRole("button", { name: "Next frame" }));
    await loadCurrentImage(30);
    await view.user.click(
      screen.getByRole("button", { name: "Confirm this frame" }),
    );

    expect(
      view.latest()?.confirmed_frames.map((frame) => frame.frame_index),
    ).toEqual([10, 20, 30]);
    expect(
      view
        .latest()
        ?.confirmed_frames.every(
          (frame) => frame.polygon_digest === view.latest()?.polygon_digest,
        ),
    ).toBe(true);
    expect(screen.getByText("3 frames confirmed")).toBeVisible();
    expect(
      screen.getByRole("button", { name: "Frame already confirmed" }),
    ).toBeDisabled();
  });

  it("explains why a video with fewer than three preview samples is blocked", async () => {
    capture.mockResolvedValue({ ...preview(1), sample_count: 2 });
    renderStep(approvedCalibration());
    await loadCurrentImage();
    expect(
      screen.getByText(/fewer than three distinct preview frames/i),
    ).toBeVisible();
  });
});
