import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ComponentPropsWithoutRef, ReactNode } from "react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { LanguageProvider } from "@/contexts/LanguageContext";
import type {
  AnnotationBox,
  AnnotationPoint,
} from "@/lib/ballAnnotationGeometry";

import { BallAnnotationCanvas } from "./BallAnnotationCanvas";

const ignoreDecodeState = () => undefined;

vi.mock("react-konva", () => {
  function konvaEvent(x = 195, y = 150, deltaY = -1) {
    const stage: any = {
      getStage: () => stage,
      getPointerPosition: () => ({ x, y }),
      x: () => x,
      y: () => y,
      position: vi.fn(),
    };
    return {
      target: stage,
      evt: { preventDefault: vi.fn(), deltaY },
    };
  }
  return {
    Stage: ({
      children,
      ...props
    }: {
      children?: ReactNode;
      [key: string]: any;
    }) => (
      <div data-testid="konva-stage">
        <button type="button" onClick={() => props.onMouseDown?.(konvaEvent())}>
          stage mouse down
        </button>
        <button
          type="button"
          onClick={() => props.onTouchStart?.(konvaEvent(200, 150))}
        >
          stage touch start
        </button>
        <button
          type="button"
          onClick={() => props.onMouseMove?.(konvaEvent(260, 170))}
        >
          stage mouse move
        </button>
        <button
          type="button"
          onClick={() => props.onTouchMove?.(konvaEvent(270, 180))}
        >
          stage touch move
        </button>
        <button type="button" onClick={() => props.onMouseUp?.()}>
          stage mouse up
        </button>
        <button type="button" onClick={() => props.onTouchEnd?.()}>
          stage touch end
        </button>
        <button type="button" onClick={() => props.onTouchCancel?.()}>
          stage touch cancel
        </button>
        <button
          type="button"
          onClick={() => props.onWheel?.(konvaEvent(210, 160, -1))}
        >
          stage wheel in
        </button>
        <button
          type="button"
          onClick={() => props.onWheel?.(konvaEvent(210, 160, 1))}
        >
          stage wheel out
        </button>
        <button
          type="button"
          onClick={() => props.onDragEnd?.(konvaEvent(5, 6))}
        >
          stage drag end
        </button>
        {children}
      </div>
    ),
    Layer: ({ children }: { children?: ReactNode }) => <div>{children}</div>,
    Image: () => <div data-testid="konva-image" />,
    Rect: (props: {
      dash?: number[];
      hitStrokeWidth?: number;
      name?: string;
      onDragEnd?: (event: any) => void;
    }) => (
      <div
        data-testid={props.name ?? "konva-rect"}
        data-dash={props.dash?.join(",") ?? ""}
        data-hit-stroke-width={props.hitStrokeWidth ?? ""}
      >
        {props.onDragEnd && (
          <button
            type="button"
            onClick={() => props.onDragEnd?.(konvaEvent(250, 160))}
          >
            drag {props.name}
          </button>
        )}
      </div>
    ),
    Circle: (props: {
      hitStrokeWidth?: number;
      name?: string;
      onDragEnd?: (event: any) => void;
    }) => (
      <div
        data-testid={props.name ?? "konva-point"}
        data-hit-stroke-width={props.hitStrokeWidth ?? ""}
      >
        {props.onDragEnd && (
          <button
            type="button"
            onClick={() => props.onDragEnd?.(konvaEvent(220, 155))}
          >
            drag {props.name}
          </button>
        )}
      </div>
    ),
    Text: ({ text }: { text: string }) => <span>{text}</span>,
  };
});

function Harness({
  initialPoint = [100, 200],
  initialBox = { left: 90, top: 190, right: 110, bottom: 210 },
  suggestion = null,
  disabled = false,
  zoom = 1,
  pan = { x: 0, y: 0 },
  onGeometryChange = () => undefined,
  onViewportChange = () => undefined,
  imageUrl = "blob:verified-frame",
  onImageDecodeStateChange = ignoreDecodeState,
}: {
  initialPoint?: AnnotationPoint | null;
  initialBox?: AnnotationBox | null;
  suggestion?: AnnotationBox | null;
  disabled?: boolean;
  zoom?: number;
  pan?: { x: number; y: number };
  onGeometryChange?: (geometry: {
    point: AnnotationPoint | null;
    box: AnnotationBox | null;
  }) => void;
  onViewportChange?: (viewport: {
    zoom: number;
    pan: { x: number; y: number };
  }) => void;
  imageUrl?: string;
  onImageDecodeStateChange?: (state: "loading" | "ready" | "failed") => void;
}) {
  const [point, setPoint] = useState(initialPoint);
  const [box, setBox] = useState(initialBox);
  const [history, setHistory] = useState<
    Array<{ point: AnnotationPoint | null; box: AnnotationBox | null }>
  >([]);

  function replace(
    nextPoint: AnnotationPoint | null,
    nextBox: AnnotationBox | null,
  ) {
    setHistory((current) => [...current, { point, box }]);
    setPoint(nextPoint);
    setBox(nextBox);
  }

  return (
    <LanguageProvider>
      <BallAnnotationCanvas
        sourceSize={{ width: 5_120, height: 1_440 }}
        displaySize={{ width: 390, height: 260 }}
        imageUrl={imageUrl}
        point={point}
        box={box}
        suggestion={suggestion}
        zoom={zoom}
        pan={pan}
        disabled={disabled}
        canUndo={history.length > 0}
        onGeometryChange={(next) => {
          onGeometryChange(next);
          replace(next.point, next.box);
        }}
        onClearGeometry={() => replace(null, null)}
        onUndo={() => {
          const previous = history.at(-1);
          if (!previous) return;
          setPoint(previous.point);
          setBox(previous.box);
          setHistory((current) => current.slice(0, -1));
        }}
        onViewportChange={onViewportChange}
        onImageDecodeStateChange={onImageDecodeStateChange}
      />
    </LanguageProvider>
  );
}

describe("BallAnnotationCanvas", () => {
  it("offers accessible source-pixel inputs and moves the point by keyboard", async () => {
    const user = userEvent.setup();
    render(<Harness />);

    const x = screen.getByLabelText("Ball center X (source pixels)");
    expect(x).toHaveValue(100);
    await user.clear(x);
    await user.type(x, "125.5");
    fireEvent.blur(x);
    expect(x).toHaveValue(125.5);
    expect(screen.getByLabelText("Box left (source pixels)")).toHaveValue(
      115.5,
    );
    expect(screen.getByLabelText("Box right (source pixels)")).toHaveValue(
      135.5,
    );

    fireEvent.keyDown(screen.getByTestId("ball-annotation-workspace"), {
      key: "ArrowRight",
    });
    expect(x).toHaveValue(126.5);
    expect(screen.getByLabelText("Box left (source pixels)")).toHaveValue(
      116.5,
    );
    fireEvent.keyDown(screen.getByTestId("ball-annotation-workspace"), {
      key: "ArrowDown",
      shiftKey: true,
    });
    expect(screen.getByLabelText("Ball center Y (source pixels)")).toHaveValue(
      210,
    );
  });

  it("clears and restores draft geometry without implying a saved deletion", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(
      screen.getByRole("button", { name: "Clear draft geometry" }),
    );
    expect(screen.getByLabelText("Ball center X (source pixels)")).toHaveValue(
      null,
    );
    await user.click(screen.getByRole("button", { name: "Undo" }));
    expect(screen.getByLabelText("Ball center X (source pixels)")).toHaveValue(
      100,
    );
  });

  it("recomputes the canonical point when a box edge changes", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const left = screen.getByLabelText("Box left (source pixels)");
    await user.clear(left);
    await user.type(left, "80");
    fireEvent.blur(left);
    expect(left).toHaveValue(80);
    expect(screen.getByLabelText("Ball center X (source pixels)")).toHaveValue(
      95,
    );
  });

  it("renders advisory boxes as dashed and explicitly unconfirmed", () => {
    render(
      <Harness
        suggestion={{ left: 3_820, top: 860, right: 3_833, bottom: 874 }}
      />,
    );
    expect(screen.getByText("Unconfirmed suggestion")).toBeVisible();
    expect(screen.getByTestId("suggested-ball-box")).toHaveAttribute(
      "data-dash",
      "8,6",
    );
    expect(
      screen.getByText("A suggestion is never saved as confirmed truth."),
    ).toBeVisible();
  });

  it("keeps controls usable at 390px and exposes zoom and mode buttons", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const workspace = screen.getByTestId("ball-annotation-workspace");
    expect(workspace).toHaveClass("min-w-0");
    expect(screen.getByTestId("ball-annotation-touch-surface")).toHaveAttribute(
      "data-max-zoom",
      "14",
    );
    await user.click(screen.getByRole("button", { name: "Zoom in" }));
    await user.click(screen.getByRole("button", { name: "Pan image" }));
    expect(screen.getByRole("button", { name: "Pan image" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("exposes all four confirmed-box resize handles", () => {
    render(<Harness />);
    expect(
      screen.getByTestId("confirmed-ball-box-top-left"),
    ).toBeInTheDocument();
    expect(
      screen.getByTestId("confirmed-ball-box-top-right"),
    ).toBeInTheDocument();
    expect(
      screen.getByTestId("confirmed-ball-box-bottom-left"),
    ).toBeInTheDocument();
    expect(
      screen.getByTestId("confirmed-ball-box-bottom-right"),
    ).toBeInTheDocument();
    expect(screen.getByTestId("confirmed-ball-box")).toHaveAttribute(
      "data-hit-stroke-width",
      "44",
    );
    for (const handle of [
      "confirmed-ball-box-top-left",
      "confirmed-ball-box-top-right",
      "confirmed-ball-box-bottom-left",
      "confirmed-ball-box-bottom-right",
      "confirmed-ball-center",
    ]) {
      expect(screen.getByTestId(handle)).toHaveAttribute(
        "data-hit-stroke-width",
        "32",
      );
    }
  });

  it("scopes touch gesture capture to the annotation surface", () => {
    render(<Harness />);
    expect(screen.getByTestId("ball-annotation-touch-surface")).toHaveStyle({
      touchAction: "none",
    });
    expect(
      screen.getByTestId("ball-annotation-workspace").style.touchAction,
    ).toBe("");
  });

  it("lets a keyboard-only operator create the first point and box", async () => {
    const user = userEvent.setup();
    render(<Harness initialPoint={null} initialBox={null} />);

    expect(
      screen.getByLabelText("Ball center X (source pixels)"),
    ).toBeDisabled();
    await user.click(
      screen.getByRole("button", { name: "Add point at frame center" }),
    );
    expect(screen.getByLabelText("Ball center X (source pixels)")).toHaveValue(
      2_560,
    );
    await user.click(
      screen.getByRole("button", { name: "Add editable box around point" }),
    );
    expect(screen.getByLabelText("Box left (source pixels)")).toHaveValue(
      2_554,
    );
    expect(screen.getByLabelText("Box right (source pixels)")).toHaveValue(
      2_566,
    );
  });

  it("supports point, box, touch, wheel, pan, and drag Konva events", async () => {
    const user = userEvent.setup();
    const onViewportChange = vi.fn();
    render(<Harness onViewportChange={onViewportChange} />);

    await user.click(screen.getByRole("button", { name: "stage mouse down" }));
    expect(
      screen.getByLabelText("Ball center X (source pixels)"),
    ).not.toHaveValue(100);

    await user.click(
      screen.getByRole("button", { name: "Draw confirmed box" }),
    );
    await user.click(screen.getByRole("button", { name: "stage touch start" }));
    await user.click(screen.getByRole("button", { name: "stage touch move" }));
    await user.click(screen.getByRole("button", { name: "stage touch end" }));
    expect(screen.getByLabelText("Box right (source pixels)")).not.toHaveValue(
      110,
    );

    await user.click(screen.getByRole("button", { name: "stage wheel in" }));
    await user.click(screen.getByRole("button", { name: "stage wheel out" }));
    await user.click(screen.getByRole("button", { name: "Pan image" }));
    await user.click(screen.getByRole("button", { name: "stage drag end" }));
    expect(onViewportChange).toHaveBeenCalledTimes(3);

    await user.click(
      screen.getByRole("button", { name: /drag confirmed-ball-box$/ }),
    );
    await user.click(
      screen.getByRole("button", { name: "drag confirmed-ball-box-top-left" }),
    );
  });

  it("discards an in-progress touch box when the browser cancels the gesture", async () => {
    const user = userEvent.setup();
    render(<Harness initialPoint={null} initialBox={null} />);
    await user.click(
      screen.getByRole("button", { name: "Draw confirmed box" }),
    );
    await user.click(screen.getByRole("button", { name: "stage touch start" }));
    await user.click(screen.getByRole("button", { name: "stage touch move" }));
    await user.click(
      screen.getByRole("button", { name: "stage touch cancel" }),
    );
    await user.click(screen.getByRole("button", { name: "stage touch end" }));
    expect(screen.getByLabelText("Box left (source pixels)")).toBeDisabled();
    expect(screen.getByLabelText("Box left (source pixels)")).toHaveValue(null);
  });

  it("handles workspace shortcuts, point-only movement, and invalid inputs", async () => {
    const user = userEvent.setup();
    const { rerender } = render(<Harness key="point-only" initialBox={null} />);
    const workspace = screen.getByTestId("ball-annotation-workspace");
    fireEvent.keyDown(workspace, { key: "ArrowLeft" });
    expect(screen.getByLabelText("Ball center X (source pixels)")).toHaveValue(
      99,
    );
    fireEvent.keyDown(workspace, { key: "z", ctrlKey: true });
    expect(screen.getByLabelText("Ball center X (source pixels)")).toHaveValue(
      100,
    );
    fireEvent.keyDown(workspace, { key: "Delete" });
    expect(
      screen.getByLabelText("Ball center X (source pixels)"),
    ).toBeDisabled();

    rerender(<Harness key="reset" />);
    const right = screen.getByLabelText("Box right (source pixels)");
    await user.clear(right);
    await user.type(right, "80");
    fireEvent.blur(right);
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Coordinates must stay inside the source frame.",
    );
    const x = screen.getByLabelText("Ball center X (source pixels)");
    await user.clear(x);
    await user.type(x, "99999");
    fireEvent.keyDown(x, { key: "Enter" });
    expect(screen.getByRole("alert")).toBeVisible();
  });

  it("bounds zoom/reset controls and ignores edits when disabled", async () => {
    const user = userEvent.setup();
    const pointGeometryChange = vi.fn();
    const pointRender = render(
      <Harness initialBox={null} onGeometryChange={pointGeometryChange} />,
    );
    pointRender.rerender(
      <Harness
        initialBox={null}
        disabled
        onGeometryChange={pointGeometryChange}
      />,
    );
    await user.click(
      screen.getByRole("button", { name: "drag confirmed-ball-center" }),
    );
    expect(pointGeometryChange).not.toHaveBeenCalled();
    pointRender.unmount();

    const boxGeometryChange = vi.fn();
    const boxRender = render(<Harness onGeometryChange={boxGeometryChange} />);
    await user.click(
      screen.getByRole("button", { name: "Draw confirmed box" }),
    );
    boxRender.rerender(
      <Harness disabled onGeometryChange={boxGeometryChange} />,
    );
    await user.click(
      screen.getByRole("button", { name: /drag confirmed-ball-box$/ }),
    );
    await user.click(
      screen.getByRole("button", { name: "drag confirmed-ball-box-top-left" }),
    );
    expect(boxGeometryChange).not.toHaveBeenCalled();
    boxRender.unmount();

    const onViewportChange = vi.fn();
    const viewportRender = render(
      <Harness
        zoom={14}
        pan={{ x: 10, y: 12 }}
        onViewportChange={onViewportChange}
      />,
    );
    await user.click(screen.getByRole("button", { name: "Pan image" }));
    viewportRender.rerender(
      <Harness
        zoom={14}
        pan={{ x: 10, y: 12 }}
        disabled
        onViewportChange={onViewportChange}
      />,
    );
    expect(screen.getByRole("button", { name: "Zoom in" })).toBeDisabled();
    fireEvent.keyDown(screen.getByTestId("ball-annotation-workspace"), {
      key: "Delete",
    });
    await user.click(screen.getByRole("button", { name: "stage mouse down" }));
    await user.click(screen.getByRole("button", { name: "stage wheel in" }));
    await user.click(screen.getByRole("button", { name: "stage drag end" }));
    expect(onViewportChange).not.toHaveBeenCalled();
  });

  it("reports delayed decode, resets for a replacement URL, and fails closed on decode error", async () => {
    const images: Array<{
      onload: null | (() => void);
      onerror: null | (() => void);
    }> = [];
    class MockImage {
      decoding = "";
      onload: null | (() => void) = null;
      onerror: null | (() => void) = null;
      set src(_value: string) {
        images.push(this);
      }
    }
    vi.stubGlobal("Image", MockImage);
    const onImageDecodeStateChange = vi.fn();
    const rendered = render(
      <Harness
        imageUrl="blob:verified-frame"
        onImageDecodeStateChange={onImageDecodeStateChange}
      />,
    );
    await waitFor(() => expect(images).toHaveLength(1));
    expect(onImageDecodeStateChange).toHaveBeenLastCalledWith("loading");
    expect(screen.queryByTestId("konva-image")).not.toBeInTheDocument();

    act(() => images[0].onload?.());
    await waitFor(() =>
      expect(screen.getByTestId("konva-image")).toBeVisible(),
    );
    expect(onImageDecodeStateChange).toHaveBeenLastCalledWith("ready");

    rendered.rerender(
      <Harness
        imageUrl="blob:replacement-frame"
        onImageDecodeStateChange={onImageDecodeStateChange}
      />,
    );
    await waitFor(() => expect(images).toHaveLength(2));
    expect(onImageDecodeStateChange).toHaveBeenLastCalledWith("loading");
    expect(screen.queryByTestId("konva-image")).not.toBeInTheDocument();
    expect(images[0].onload).toBeNull();
    expect(images[0].onerror).toBeNull();

    act(() => images[1].onerror?.());
    await waitFor(() =>
      expect(onImageDecodeStateChange).toHaveBeenLastCalledWith("failed"),
    );
    expect(screen.queryByTestId("konva-image")).not.toBeInTheDocument();

    rendered.unmount();
    expect(images[1].onload).toBeNull();
    expect(images[1].onerror).toBeNull();
    vi.unstubAllGlobals();
  });
});
