export type AnnotationPoint = [number, number];

export interface AnnotationSize {
  width: number;
  height: number;
}

export interface AnnotationBox {
  left: number;
  top: number;
  right: number;
  bottom: number;
}

export interface DisplayBox {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface AnnotationTransform {
  source: AnnotationSize;
  display: AnnotationSize;
  scale: number;
  offsetX: number;
  offsetY: number;
  contentWidth: number;
  contentHeight: number;
  zoom: number;
  panX: number;
  panY: number;
  devicePixelRatio: number;
}

export interface AnnotationTransformInput {
  source: AnnotationSize;
  display: AnnotationSize;
  zoom?: number;
  pan?: { x: number; y: number };
  devicePixelRatio?: number;
}

type GeometryValidation =
  | { valid: true }
  | { valid: false; reason: "non_finite" | "out_of_bounds" | "empty" };

function requirePositiveSize(size: AnnotationSize, label: string) {
  if (
    !Number.isFinite(size.width) ||
    !Number.isFinite(size.height) ||
    size.width <= 0 ||
    size.height <= 0
  ) {
    throw new Error(`${label} dimensions must be finite and positive.`);
  }
}

function requireFinitePoint([x, y]: AnnotationPoint, label: string) {
  if (!Number.isFinite(x) || !Number.isFinite(y)) {
    throw new Error(`${label} must be finite.`);
  }
}

export function annotationMaxZoom(
  source: AnnotationSize,
  display: AnnotationSize,
): number {
  requirePositiveSize(source, "Source");
  requirePositiveSize(display, "Display");
  const oneCssPixelPerSourcePixel = Math.ceil(
    Math.max(source.width / display.width, source.height / display.height),
  );
  return Math.min(32, Math.max(8, oneCssPixelPerSourcePixel));
}

export function computeAnnotationTransform({
  source,
  display,
  zoom = 1,
  pan = { x: 0, y: 0 },
  devicePixelRatio = 1,
}: AnnotationTransformInput): AnnotationTransform {
  requirePositiveSize(source, "Source");
  requirePositiveSize(display, "Display");
  if (!Number.isFinite(zoom) || zoom <= 0) {
    throw new Error("Zoom must be finite and positive.");
  }
  requireFinitePoint([pan.x, pan.y], "Pan");
  if (!Number.isFinite(devicePixelRatio) || devicePixelRatio <= 0) {
    throw new Error("Device pixel ratio must be finite and positive.");
  }
  const containScale = Math.min(
    display.width / source.width,
    display.height / source.height,
  );
  const containedWidth = source.width * containScale;
  const containedHeight = source.height * containScale;
  return {
    source,
    display,
    scale: containScale * zoom,
    offsetX: (display.width - containedWidth) / 2 + pan.x,
    offsetY: (display.height - containedHeight) / 2 + pan.y,
    contentWidth: containedWidth * zoom,
    contentHeight: containedHeight * zoom,
    zoom,
    panX: pan.x,
    panY: pan.y,
    devicePixelRatio,
  };
}

export function sourcePointToDisplay(
  [x, y]: AnnotationPoint,
  transform: AnnotationTransform,
): AnnotationPoint {
  return [
    transform.offsetX + x * transform.scale,
    transform.offsetY + y * transform.scale,
  ];
}

export function displayPointToSource(
  [x, y]: AnnotationPoint,
  transform: AnnotationTransform,
): AnnotationPoint {
  return [
    (x - transform.offsetX) / transform.scale,
    (y - transform.offsetY) / transform.scale,
  ];
}

export function displayPointToBacking(
  [x, y]: AnnotationPoint,
  transform: AnnotationTransform,
): AnnotationPoint {
  return [x * transform.devicePixelRatio, y * transform.devicePixelRatio];
}

export function backingPointToDisplay(
  [x, y]: AnnotationPoint,
  transform: AnnotationTransform,
): AnnotationPoint {
  return [x / transform.devicePixelRatio, y / transform.devicePixelRatio];
}

export function sourceBoxToDisplay(
  box: AnnotationBox,
  transform: AnnotationTransform,
): DisplayBox {
  const [x, y] = sourcePointToDisplay([box.left, box.top], transform);
  return {
    x,
    y,
    width: (box.right - box.left) * transform.scale,
    height: (box.bottom - box.top) * transform.scale,
  };
}

export function displayBoxToSource(
  box: DisplayBox,
  transform: AnnotationTransform,
): AnnotationBox {
  const [left, top] = displayPointToSource([box.x, box.y], transform);
  const [right, bottom] = displayPointToSource(
    [box.x + box.width, box.y + box.height],
    transform,
  );
  return { left, top, right, bottom };
}

export function normalizeSourceBox(
  start: AnnotationPoint,
  end: AnnotationPoint,
): AnnotationBox {
  requireFinitePoint(start, "Box start");
  requireFinitePoint(end, "Box end");
  return {
    left: Math.min(start[0], end[0]),
    top: Math.min(start[1], end[1]),
    right: Math.max(start[0], end[0]),
    bottom: Math.max(start[1], end[1]),
  };
}

export function clampSourcePoint(
  [x, y]: AnnotationPoint,
  source: AnnotationSize,
): AnnotationPoint {
  requirePositiveSize(source, "Source");
  requireFinitePoint([x, y], "Source point");
  const maxX = source.width - source.width * Number.EPSILON;
  const maxY = source.height - source.height * Number.EPSILON;
  return [Math.max(0, Math.min(maxX, x)), Math.max(0, Math.min(maxY, y))];
}

export function clampSourceBox(
  box: AnnotationBox,
  source: AnnotationSize,
): AnnotationBox {
  requirePositiveSize(source, "Source");
  if (
    [box.left, box.top, box.right, box.bottom].some(
      (coordinate) => !Number.isFinite(coordinate),
    )
  ) {
    throw new Error("Source box coordinates must be finite.");
  }
  return {
    left: Math.max(0, Math.min(source.width, box.left)),
    top: Math.max(0, Math.min(source.height, box.top)),
    right: Math.max(0, Math.min(source.width, box.right)),
    bottom: Math.max(0, Math.min(source.height, box.bottom)),
  };
}

export function displayPointInsideSource(
  point: AnnotationPoint,
  transform: AnnotationTransform,
): boolean {
  const [x, y] = displayPointToSource(point, transform);
  return (
    x >= 0 &&
    x < transform.source.width &&
    y >= 0 &&
    y < transform.source.height
  );
}

export function validateSourcePoint(
  point: AnnotationPoint,
  source: AnnotationSize,
): GeometryValidation {
  if (point.some((coordinate) => !Number.isFinite(coordinate))) {
    return { valid: false, reason: "non_finite" };
  }
  if (
    point[0] < 0 ||
    point[0] >= source.width ||
    point[1] < 0 ||
    point[1] >= source.height
  ) {
    return { valid: false, reason: "out_of_bounds" };
  }
  return { valid: true };
}

export function validateSourceBox(
  box: AnnotationBox,
  source: AnnotationSize,
): GeometryValidation {
  if (
    [box.left, box.top, box.right, box.bottom].some(
      (coordinate) => !Number.isFinite(coordinate),
    )
  ) {
    return { valid: false, reason: "non_finite" };
  }
  if (box.left >= box.right || box.top >= box.bottom) {
    return { valid: false, reason: "empty" };
  }
  if (
    box.left < 0 ||
    box.top < 0 ||
    box.right > source.width ||
    box.bottom > source.height
  ) {
    return { valid: false, reason: "out_of_bounds" };
  }
  return { valid: true };
}

export function panForZoomAtDisplayPoint(
  transform: AnnotationTransform,
  nextZoom: number,
  displayPoint: AnnotationPoint,
): { x: number; y: number } {
  const sourcePoint = displayPointToSource(displayPoint, transform);
  const unzoomedScale = transform.scale / transform.zoom;
  const containedWidth = transform.source.width * unzoomedScale;
  const containedHeight = transform.source.height * unzoomedScale;
  return {
    x:
      displayPoint[0] -
      (transform.display.width - containedWidth) / 2 -
      sourcePoint[0] * unzoomedScale * nextZoom,
    y:
      displayPoint[1] -
      (transform.display.height - containedHeight) / 2 -
      sourcePoint[1] * unzoomedScale * nextZoom,
  };
}
