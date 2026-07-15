import { describe, expect, it } from "vitest";

import {
  canonicalPolygonJson,
  clampDisplayPointToSource,
  computeContainTransform,
  displayPointToSource,
  isDisplayPointInsideContent,
  nearestVertexIndex,
  parseCoordinate,
  polygonSha256,
  sourcePointToDisplay,
  validatePolygon,
  type FieldPoint,
} from "./fieldGeometry";

const SOURCE = { width: 1_920, height: 1_080 };

describe("field coordinate geometry", () => {
  it("computes a centered contain transform with letterboxing", () => {
    expect(
      computeContainTransform(SOURCE, { width: 1_000, height: 1_000 }),
    ).toEqual({
      scale: 1_000 / 1_920,
      offsetX: 0,
      offsetY: 218.75,
      contentWidth: 1_000,
      contentHeight: 562.5,
    });
  });

  it.each([
    [0, 0],
    [1_919, 0],
    [1_919, 1_079],
    [0, 1_079],
    [960.25, 540.75],
  ] satisfies FieldPoint[])(
    "round-trips source point %j within one display pixel",
    (x, y) => {
      const transform = computeContainTransform(SOURCE, {
        width: 377,
        height: 640,
      });
      const display = sourcePointToDisplay([x, y], transform);
      const restored = displayPointToSource(display, transform);
      const displayAgain = sourcePointToDisplay(restored, transform);
      expect(Math.abs(displayAgain[0] - display[0])).toBeLessThanOrEqual(1);
      expect(Math.abs(displayAgain[1] - display[1])).toBeLessThanOrEqual(1);
    },
  );

  it("clamps pointer input at the image content boundary without changing typed input", () => {
    const transform = computeContainTransform(SOURCE, {
      width: 1_000,
      height: 1_000,
    });
    expect(clampDisplayPointToSource([-100, -100], transform, SOURCE)).toEqual([
      0, 0,
    ]);
    expect(
      clampDisplayPointToSource([2_000, 2_000], transform, SOURCE),
    ).toEqual([1_919, 1_079]);
    expect(parseCoordinate("1920", 1_920)).toEqual({
      ok: false,
      reason: "out_of_bounds",
    });
  });

  it("rejects invalid dimensions", () => {
    expect(() =>
      computeContainTransform({ width: 0, height: 1 }, SOURCE),
    ).toThrow("positive");
  });

  it.each([
    [{ width: Number.NaN, height: 1 }, SOURCE],
    [{ width: 1, height: 0 }, SOURCE],
    [SOURCE, { width: -1, height: 100 }],
    [SOURCE, { width: 100, height: Number.POSITIVE_INFINITY }],
  ])("rejects every invalid source or display dimension", (source, display) => {
    expect(() => computeContainTransform(source, display)).toThrow("positive");
  });

  it("detects all letterboxed image content boundaries", () => {
    const transform = computeContainTransform(SOURCE, {
      width: 1_000,
      height: 1_000,
    });
    expect(isDisplayPointInsideContent([0, transform.offsetY], transform)).toBe(
      true,
    );
    expect(
      isDisplayPointInsideContent(
        [transform.contentWidth, transform.offsetY + transform.contentHeight],
        transform,
      ),
    ).toBe(true);
    expect(
      isDisplayPointInsideContent([-1, transform.offsetY], transform),
    ).toBe(false);
    expect(
      isDisplayPointInsideContent([1_001, transform.offsetY], transform),
    ).toBe(false);
    expect(
      isDisplayPointInsideContent([0, transform.offsetY - 1], transform),
    ).toBe(false);
    expect(
      isDisplayPointInsideContent(
        [0, transform.offsetY + transform.contentHeight + 1],
        transform,
      ),
    ).toBe(false);
  });

  it("selects only the nearest vertex within the display hit radius", () => {
    const transform = computeContainTransform(SOURCE, SOURCE);
    expect(
      nearestVertexIndex(
        [
          [100, 100],
          [110, 100],
        ],
        [108, 100],
        transform,
        12,
      ),
    ).toBe(1);
    expect(
      nearestVertexIndex([[100, 100]], [200, 200], transform, 12),
    ).toBeNull();
    expect(nearestVertexIndex([], [0, 0], transform)).toBeNull();
  });
});

describe("field polygon validation", () => {
  it("accepts the maximum source pixels and a non-zero-area polygon", () => {
    expect(
      validatePolygon(
        [
          [0, 0],
          [1_919, 0],
          [1_919, 1_079],
        ],
        SOURCE,
      ),
    ).toEqual({ valid: true, reason: null });
  });

  it.each([
    [
      "too_few_points",
      [
        [0, 0],
        [1, 1],
      ],
    ],
    [
      "non_finite",
      [
        [0, 0],
        [1, Number.NaN],
        [2, 0],
      ],
    ],
    [
      "out_of_bounds",
      [
        [0, 0],
        [1_920, 1],
        [2, 0],
      ],
    ],
    [
      "zero_area",
      [
        [0, 0],
        [1, 1],
        [2, 2],
      ],
    ],
  ] as const)("rejects %s", (reason, polygon) => {
    expect(validatePolygon(polygon as FieldPoint[], SOURCE)).toEqual({
      valid: false,
      reason,
    });
  });

  it.each([
    [
      [-1, 0],
      [1, 0],
      [1, 1],
    ],
    [
      [0, 0],
      [1_920, 0],
      [1, 1],
    ],
    [
      [0, -1],
      [1, 0],
      [1, 1],
    ],
    [
      [0, 0],
      [1, 0],
      [1, 1_080],
    ],
  ])("rejects half-open boundary violation %j", (...polygon) => {
    expect(validatePolygon(polygon as FieldPoint[], SOURCE)).toEqual({
      valid: false,
      reason: "out_of_bounds",
    });
  });

  it.each([
    ["", "not_a_number"],
    ["NaN", "not_a_number"],
    ["Infinity", "not_a_number"],
    ["-1", "out_of_bounds"],
    ["1080", "out_of_bounds"],
  ])("rejects typed coordinate %s as %s", (value, reason) => {
    expect(parseCoordinate(value, 1_080)).toEqual({ ok: false, reason });
  });

  it("preserves a valid fractional coordinate", () => {
    expect(parseCoordinate("100.25", 1_080)).toEqual({
      ok: true,
      value: 100.25,
    });
  });
});

describe("canonical polygon digest", () => {
  const polygon: FieldPoint[] = [
    [-0, 0],
    [1_919, 0],
    [1_919, 1_079],
  ];

  it("canonicalizes property order and negative zero", () => {
    expect(canonicalPolygonJson(polygon, SOURCE)).toBe(
      '{"width":1920,"height":1080,"points":[[0,0],[1919,0],[1919,1079]]}',
    );
  });

  it("produces a stable SHA-256 bound to points and source resolution", async () => {
    const first = await polygonSha256(polygon, SOURCE);
    const again = await polygonSha256(
      polygon.map(([x, y]) => [x, y]),
      SOURCE,
    );
    const anotherResolution = await polygonSha256(polygon, {
      width: 1_921,
      height: 1_080,
    });
    expect(first).toMatch(/^[a-f\d]{64}$/);
    expect(again).toBe(first);
    expect(anotherResolution).not.toBe(first);
  });

  it("refuses to canonicalize non-finite values", () => {
    expect(() => canonicalPolygonJson([[Number.NaN, 0]], SOURCE)).toThrow(
      "finite",
    );
    expect(() =>
      canonicalPolygonJson([[0, 0]], { width: Number.NaN, height: 1 }),
    ).toThrow("finite");
  });
});
