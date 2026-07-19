import { describe, expect, it } from "vitest";

import {
  annotationMaxZoom,
  backingPointToDisplay,
  clampSourcePoint,
  clampSourceBox,
  computeAnnotationTransform,
  displayBoxToSource,
  displayPointInsideSource,
  displayPointToBacking,
  displayPointToSource,
  normalizeSourceBox,
  panForZoomAtDisplayPoint,
  sourceBoxToDisplay,
  sourcePointToDisplay,
  validateSourceBox,
  validateSourcePoint,
  type AnnotationPoint,
} from "./ballAnnotationGeometry";

const SOURCE = { width: 5_120, height: 1_440 };

describe("ball annotation source/display geometry", () => {
  it("raises the zoom ceiling until an ultrawide source pixel reaches one CSS pixel", () => {
    expect(annotationMaxZoom(SOURCE, { width: 390, height: 260 })).toBe(14);
    expect(annotationMaxZoom(SOURCE, { width: 320, height: 220 })).toBe(16);
    expect(annotationMaxZoom(SOURCE, { width: 200, height: 220 })).toBe(26);
    expect(
      annotationMaxZoom(
        { width: 1_920, height: 1_080 },
        { width: 960, height: 540 },
      ),
    ).toBe(8);
  });

  it("rejects invalid dimensions when deriving the zoom ceiling", () => {
    expect(() => annotationMaxZoom(SOURCE, { width: 0, height: 260 })).toThrow(
      "finite and positive",
    );
  });

  it("centers an ultrawide source in a letterboxed display", () => {
    expect(
      computeAnnotationTransform({
        source: SOURCE,
        display: { width: 390, height: 300 },
      }),
    ).toEqual({
      source: SOURCE,
      display: { width: 390, height: 300 },
      scale: 390 / 5_120,
      offsetX: 0,
      offsetY: 95.15625,
      contentWidth: 390,
      contentHeight: 109.6875,
      zoom: 1,
      panX: 0,
      panY: 0,
      devicePixelRatio: 1,
    });
  });

  it.each([
    [0, 0],
    [5_119.999, 0],
    [5_119.999, 1_439.999],
    [0, 1_439.999],
    [3_827.42, 865.75],
  ] satisfies AnnotationPoint[])(
    "round-trips source point %j through resize, zoom, and pan",
    (x, y) => {
      const transform = computeAnnotationTransform({
        source: SOURCE,
        display: { width: 377, height: 640 },
        zoom: 3.25,
        pan: { x: -211.5, y: 79.25 },
        devicePixelRatio: 2,
      });
      const display = sourcePointToDisplay([x, y], transform);
      const restored = displayPointToSource(display, transform);
      expect(restored[0]).toBeCloseTo(x, 9);
      expect(restored[1]).toBeCloseTo(y, 9);
      expect(
        backingPointToDisplay(
          displayPointToBacking(display, transform),
          transform,
        ),
      ).toEqual(display);
    },
  );

  it("maps boxes through the same source-pixel edge transform", () => {
    const transform = computeAnnotationTransform({
      source: SOURCE,
      display: { width: 1_024, height: 512 },
      zoom: 2,
      pan: { x: -20, y: 10 },
      devicePixelRatio: 2,
    });
    const box = { left: 3_820, top: 860, right: 3_833, bottom: 874 };
    const display = sourceBoxToDisplay(box, transform);
    expect(display.width).toBeCloseTo(5.2);
    expect(display.height).toBeCloseTo(5.6);
    expect(display.x).toBeCloseTo(1_508);
    expect(display.y).toBeCloseTo(466);
    expect(displayBoxToSource(display, transform)).toEqual(box);
  });

  it("uses half-open point bounds and edge-inclusive box bounds", () => {
    expect(validateSourcePoint([0, 0], SOURCE)).toEqual({ valid: true });
    expect(validateSourcePoint([5_119.999, 1_439.999], SOURCE)).toEqual({
      valid: true,
    });
    expect(validateSourcePoint([5_120, 1], SOURCE)).toEqual({
      valid: false,
      reason: "out_of_bounds",
    });
    expect(
      validateSourceBox(
        { left: 0, top: 0, right: 5_120, bottom: 1_440 },
        SOURCE,
      ),
    ).toEqual({ valid: true });
    expect(
      validateSourceBox({ left: 1, top: 1, right: 1, bottom: 2 }, SOURCE),
    ).toEqual({ valid: false, reason: "empty" });
  });

  it("normalizes draw direction and clamps pointer boxes to source edges", () => {
    expect(normalizeSourceBox([20, 30], [10, 5])).toEqual({
      left: 10,
      top: 5,
      right: 20,
      bottom: 30,
    });
    expect(
      clampSourceBox(
        { left: -10, top: -5, right: 5_200, bottom: 1_500 },
        SOURCE,
      ),
    ).toEqual({ left: 0, top: 0, right: 5_120, bottom: 1_440 });
  });

  it("clamps points strictly below the source's half-open maximum", () => {
    const clamped = clampSourcePoint([5_120, 1_440], SOURCE);
    expect(clamped[0]).toBeLessThan(SOURCE.width);
    expect(clamped[1]).toBeLessThan(SOURCE.height);
    expect(validateSourcePoint(clamped, SOURCE)).toEqual({ valid: true });
  });

  it.each([
    { left: Number.NaN, top: 0, right: 1, bottom: 1 },
    { left: 0, top: 0, right: Number.POSITIVE_INFINITY, bottom: 1 },
  ])("fails closed instead of clamping a non-finite box", (box) => {
    expect(() => clampSourceBox(box, SOURCE)).toThrow("finite");
  });

  it("keeps the source point under the cursor fixed while zooming", () => {
    const cursor: AnnotationPoint = [217.5, 318.25];
    const before = computeAnnotationTransform({
      source: SOURCE,
      display: { width: 390, height: 640 },
      zoom: 1.75,
      pan: { x: -53, y: 17 },
    });
    const sourceUnderCursor = displayPointToSource(cursor, before);
    const nextZoom = 4.5;
    const next = computeAnnotationTransform({
      source: SOURCE,
      display: before.display,
      zoom: nextZoom,
      pan: panForZoomAtDisplayPoint(before, nextZoom, cursor),
    });
    expect(displayPointToSource(cursor, next)[0]).toBeCloseTo(
      sourceUnderCursor[0],
      9,
    );
    expect(displayPointToSource(cursor, next)[1]).toBeCloseTo(
      sourceUnderCursor[1],
      9,
    );
  });

  it.each([
    [
      { width: 0, height: 1 },
      { width: 10, height: 10 },
    ],
    [SOURCE, { width: Number.NaN, height: 10 }],
  ])("rejects invalid dimensions", (source, display) => {
    expect(() => computeAnnotationTransform({ source, display })).toThrow(
      "finite and positive",
    );
  });

  it.each([
    { zoom: 0 },
    { zoom: Number.NaN },
    { pan: { x: Number.POSITIVE_INFINITY, y: 0 } },
    { devicePixelRatio: 0 },
    { devicePixelRatio: Number.NaN },
  ])("rejects invalid viewport input %j", (patch) => {
    expect(() =>
      computeAnnotationTransform({
        source: SOURCE,
        display: { width: 390, height: 260 },
        ...patch,
      }),
    ).toThrow();
  });

  it("distinguishes letterbox space from half-open source pixels", () => {
    const transform = computeAnnotationTransform({
      source: SOURCE,
      display: { width: 390, height: 300 },
    });
    expect(displayPointInsideSource([10, 10], transform)).toBe(false);
    expect(
      displayPointInsideSource(
        sourcePointToDisplay([0, 0], transform),
        transform,
      ),
    ).toBe(true);
    expect(
      displayPointInsideSource(
        sourcePointToDisplay([SOURCE.width, SOURCE.height], transform),
        transform,
      ),
    ).toBe(false);
  });

  it.each([
    [Number.NaN, 0],
    [0, Number.POSITIVE_INFINITY],
  ] satisfies AnnotationPoint[])(
    "rejects a non-finite draw endpoint %j",
    (x, y) => {
      expect(() => normalizeSourceBox([0, 0], [x, y])).toThrow("finite");
    },
  );

  it("fails closed for invalid point and box validation inputs", () => {
    expect(validateSourcePoint([Number.NaN, 0], SOURCE)).toEqual({
      valid: false,
      reason: "non_finite",
    });
    expect(validateSourcePoint([0, -1], SOURCE)).toEqual({
      valid: false,
      reason: "out_of_bounds",
    });
    expect(
      validateSourceBox(
        { left: 0, top: 0, right: Number.POSITIVE_INFINITY, bottom: 1 },
        SOURCE,
      ),
    ).toEqual({ valid: false, reason: "non_finite" });
    expect(
      validateSourceBox({ left: -1, top: 0, right: 2, bottom: 2 }, SOURCE),
    ).toEqual({ valid: false, reason: "out_of_bounds" });
    expect(() => clampSourcePoint([Number.NaN, 0], SOURCE)).toThrow("finite");
    expect(() => clampSourcePoint([0, 0], { width: -1, height: 1 })).toThrow(
      "positive",
    );
  });
});
