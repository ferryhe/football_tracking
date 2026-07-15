export type FieldPoint = [number, number];

export interface FieldResolution {
  width: number;
  height: number;
}

export interface ContainTransform {
  scale: number;
  offsetX: number;
  offsetY: number;
  contentWidth: number;
  contentHeight: number;
}

export type PolygonValidationReason =
  | "too_few_points"
  | "non_finite"
  | "out_of_bounds"
  | "zero_area";

export type PolygonValidation =
  | { valid: true; reason: null }
  | { valid: false; reason: PolygonValidationReason };

export type CoordinateParseResult =
  | { ok: true; value: number }
  | { ok: false; reason: "not_a_number" | "out_of_bounds" };

function requirePositiveResolution(resolution: FieldResolution) {
  if (
    !Number.isFinite(resolution.width) ||
    !Number.isFinite(resolution.height) ||
    resolution.width <= 0 ||
    resolution.height <= 0
  ) {
    throw new Error("Field dimensions must be finite and positive.");
  }
}

export function computeContainTransform(
  source: FieldResolution,
  display: FieldResolution,
): ContainTransform {
  requirePositiveResolution(source);
  requirePositiveResolution(display);
  const scale = Math.min(
    display.width / source.width,
    display.height / source.height,
  );
  const contentWidth = Math.min(display.width, source.width * scale);
  const contentHeight = Math.min(display.height, source.height * scale);
  return {
    scale,
    offsetX: (display.width - contentWidth) / 2,
    offsetY: (display.height - contentHeight) / 2,
    contentWidth,
    contentHeight,
  };
}

export function sourcePointToDisplay(
  [x, y]: FieldPoint,
  transform: ContainTransform,
): FieldPoint {
  return [
    transform.offsetX + x * transform.scale,
    transform.offsetY + y * transform.scale,
  ];
}

export function displayPointToSource(
  [x, y]: FieldPoint,
  transform: ContainTransform,
): FieldPoint {
  return [
    (x - transform.offsetX) / transform.scale,
    (y - transform.offsetY) / transform.scale,
  ];
}

export function clampDisplayPointToSource(
  [x, y]: FieldPoint,
  transform: ContainTransform,
  source: FieldResolution,
): FieldPoint {
  const displayX = Math.max(
    transform.offsetX,
    Math.min(transform.offsetX + transform.contentWidth, x),
  );
  const displayY = Math.max(
    transform.offsetY,
    Math.min(transform.offsetY + transform.contentHeight, y),
  );
  const [sourceX, sourceY] = displayPointToSource(
    [displayX, displayY],
    transform,
  );
  return [
    Math.max(0, Math.min(source.width - 1, Math.round(sourceX))),
    Math.max(0, Math.min(source.height - 1, Math.round(sourceY))),
  ];
}

export function isDisplayPointInsideContent(
  [x, y]: FieldPoint,
  transform: ContainTransform,
): boolean {
  return (
    x >= transform.offsetX &&
    x <= transform.offsetX + transform.contentWidth &&
    y >= transform.offsetY &&
    y <= transform.offsetY + transform.contentHeight
  );
}

export function nearestVertexIndex(
  points: FieldPoint[],
  displayPoint: FieldPoint,
  transform: ContainTransform,
  hitRadius = 12,
): number | null {
  let nearest: number | null = null;
  let nearestDistance = hitRadius;
  points.forEach((point, index) => {
    const [x, y] = sourcePointToDisplay(point, transform);
    const distance = Math.hypot(displayPoint[0] - x, displayPoint[1] - y);
    if (distance <= nearestDistance) {
      nearest = index;
      nearestDistance = distance;
    }
  });
  return nearest;
}

export function parseCoordinate(
  value: string,
  exclusiveMaximum: number,
): CoordinateParseResult {
  if (value.trim() === "") return { ok: false, reason: "not_a_number" };
  const number = Number(value);
  if (!Number.isFinite(number)) return { ok: false, reason: "not_a_number" };
  if (number < 0 || number >= exclusiveMaximum) {
    return { ok: false, reason: "out_of_bounds" };
  }
  return { ok: true, value: number };
}

export function validatePolygon(
  points: FieldPoint[],
  resolution: FieldResolution,
): PolygonValidation {
  if (points.length < 3) return { valid: false, reason: "too_few_points" };
  if (points.some(([x, y]) => !Number.isFinite(x) || !Number.isFinite(y))) {
    return { valid: false, reason: "non_finite" };
  }
  if (
    points.some(
      ([x, y]) =>
        x < 0 || x >= resolution.width || y < 0 || y >= resolution.height,
    )
  ) {
    return { valid: false, reason: "out_of_bounds" };
  }
  const twiceArea = points.reduce((area, [x, y], index) => {
    const [nextX, nextY] = points[(index + 1) % points.length];
    return area + x * nextY - nextX * y;
  }, 0);
  if (Math.abs(twiceArea) <= Number.EPSILON) {
    return { valid: false, reason: "zero_area" };
  }
  return { valid: true, reason: null };
}

function canonicalNumber(value: number): number {
  if (!Number.isFinite(value))
    throw new Error("Polygon values must be finite.");
  return Object.is(value, -0) ? 0 : value;
}

export function canonicalPolygonJson(
  points: FieldPoint[],
  resolution: FieldResolution,
): string {
  const canonicalPoints = points.map(
    ([x, y]) => [canonicalNumber(x), canonicalNumber(y)] as FieldPoint,
  );
  return `{"width":${canonicalNumber(resolution.width)},"height":${canonicalNumber(
    resolution.height,
  )},"points":${JSON.stringify(canonicalPoints)}}`;
}

export async function polygonSha256(
  points: FieldPoint[],
  resolution: FieldResolution,
): Promise<string> {
  const bytes = new TextEncoder().encode(
    canonicalPolygonJson(points, resolution),
  );
  const digest = await globalThis.crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
}
