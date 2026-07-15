import {
  validatePolygon,
  type FieldPoint,
  type FieldResolution,
} from "./fieldGeometry";

export type CalibrationConfidence = "config" | "detected" | "fallback";

export interface ProductionCalibrationSuggestion {
  source_path: string;
  source: string;
  confidence: CalibrationConfidence;
  field_coverage: number;
  source_resolution: FieldResolution;
  frame_index: number;
  polygon: FieldPoint[];
}

export interface ProductionCalibrationFrame {
  input_video: string;
  frame_index: number;
  frame_time_seconds: number;
  sample_index: number;
  source_resolution: FieldResolution;
  polygon_digest: string;
}

export interface ProductionCalibrationDraft {
  source_resolution: FieldResolution | null;
  suggestion: ProductionCalibrationSuggestion | null;
  approved_polygon: FieldPoint[];
  exclusions: FieldPoint[][];
  polygon_digest: string | null;
  confirmed_frames: ProductionCalibrationFrame[];
}

export interface CalibrationFramePreview {
  input_video: string;
  frame_width: number;
  frame_height: number;
  frame_index: number;
  frame_time_seconds: number;
  sample_index: number;
  sample_count: number;
}

export type AddConfirmedFrameResult =
  | { ok: true; calibration: ProductionCalibrationDraft }
  | {
      ok: false;
      reason:
        | "preview_not_ready"
        | "invalid_polygon"
        | "source_mismatch"
        | "resolution_mismatch"
        | "digest_mismatch"
        | "preview_mismatch"
        | "duplicate_frame"
        | "already_complete";
    };

export function createEmptyCalibration(): ProductionCalibrationDraft {
  return {
    source_resolution: null,
    suggestion: null,
    approved_polygon: [],
    exclusions: [],
    polygon_digest: null,
    confirmed_frames: [],
  };
}

export function createLatestRequestGate() {
  let generation = 0;
  return {
    begin() {
      generation += 1;
      return generation;
    },
    invalidate() {
      generation += 1;
    },
    isCurrent(candidate: number) {
      return candidate === generation;
    },
  };
}

export function resolutionsMatch(
  left: FieldResolution | null,
  right: FieldResolution | null,
): boolean {
  return Boolean(
    left && right && left.width === right.width && left.height === right.height,
  );
}

export function responseMatchesCalibrationRequest(
  response: CalibrationFramePreview,
  expected: {
    generation: number;
    current_generation: number;
    source_path: string;
    frame_index?: number;
    resolution?: FieldResolution;
  },
): boolean {
  return (
    expected.generation === expected.current_generation &&
    response.input_video === expected.source_path &&
    (expected.frame_index === undefined ||
      response.frame_index === expected.frame_index) &&
    (expected.resolution === undefined ||
      (response.frame_width === expected.resolution.width &&
        response.frame_height === expected.resolution.height))
  );
}

export function replaceApprovedPolygon(
  calibration: ProductionCalibrationDraft,
  approvedPolygon: FieldPoint[],
  exclusions: FieldPoint[][],
  sourceResolution: FieldResolution,
  polygonDigest: string,
): ProductionCalibrationDraft {
  const changed =
    calibration.polygon_digest !== polygonDigest ||
    !resolutionsMatch(calibration.source_resolution, sourceResolution) ||
    JSON.stringify(calibration.approved_polygon) !==
      JSON.stringify(approvedPolygon) ||
    JSON.stringify(calibration.exclusions) !== JSON.stringify(exclusions);
  return {
    ...calibration,
    source_resolution: { ...sourceResolution },
    approved_polygon: approvedPolygon.map(([x, y]) => [x, y]),
    exclusions: exclusions.map((polygon) => polygon.map(([x, y]) => [x, y])),
    polygon_digest: polygonDigest,
    confirmed_frames: changed ? [] : calibration.confirmed_frames,
  };
}

export function mergePolygonDigestIfCurrent(
  current: ProductionCalibrationDraft | null,
  approvedPolygon: FieldPoint[],
  sourceResolution: FieldResolution,
  polygonDigest: string,
): ProductionCalibrationDraft | null {
  if (
    !current ||
    !resolutionsMatch(current.source_resolution, sourceResolution) ||
    JSON.stringify(current.approved_polygon) !== JSON.stringify(approvedPolygon)
  ) {
    return null;
  }
  return { ...current, polygon_digest: polygonDigest };
}

export function addConfirmedCalibrationFrame(
  calibration: ProductionCalibrationDraft,
  input: {
    preview: CalibrationFramePreview;
    source_path: string;
    source_resolution: FieldResolution;
    polygon_digest: string;
    overlay_ready: boolean;
    polygon_valid: boolean;
  },
): AddConfirmedFrameResult {
  if (!input.overlay_ready) return { ok: false, reason: "preview_not_ready" };
  if (!input.polygon_valid) return { ok: false, reason: "invalid_polygon" };
  if (
    !resolutionsMatch(calibration.source_resolution, input.source_resolution)
  ) {
    return { ok: false, reason: "resolution_mismatch" };
  }
  if (input.preview.input_video !== input.source_path) {
    return { ok: false, reason: "preview_mismatch" };
  }
  const previewResolution = {
    width: input.preview.frame_width,
    height: input.preview.frame_height,
  };
  if (!resolutionsMatch(previewResolution, input.source_resolution)) {
    return { ok: false, reason: "preview_mismatch" };
  }
  if (
    calibration.suggestion?.source_path &&
    calibration.suggestion.source_path !== input.source_path
  ) {
    return { ok: false, reason: "source_mismatch" };
  }
  if (calibration.polygon_digest !== input.polygon_digest) {
    return { ok: false, reason: "digest_mismatch" };
  }
  if (calibration.confirmed_frames.length >= 3) {
    return { ok: false, reason: "already_complete" };
  }
  if (
    calibration.confirmed_frames.some(
      (frame) => frame.frame_index === input.preview.frame_index,
    )
  ) {
    return { ok: false, reason: "duplicate_frame" };
  }
  const confirmedFrame: ProductionCalibrationFrame = {
    input_video: input.preview.input_video,
    frame_index: input.preview.frame_index,
    frame_time_seconds: input.preview.frame_time_seconds,
    sample_index: input.preview.sample_index,
    source_resolution: { ...input.source_resolution },
    polygon_digest: input.polygon_digest,
  };
  return {
    ok: true,
    calibration: {
      ...calibration,
      confirmed_frames: [...calibration.confirmed_frames, confirmedFrame].sort(
        (left, right) => left.frame_index - right.frame_index,
      ),
    },
  };
}

export function calibrationIsComplete(
  calibration: ProductionCalibrationDraft | null,
  sourcePath: string,
): boolean {
  if (
    !calibration?.source_resolution ||
    !calibration.polygon_digest ||
    calibration.confirmed_frames.length !== 3 ||
    !validatePolygon(
      calibration.approved_polygon,
      calibration.source_resolution,
    ).valid
  ) {
    return false;
  }
  const actualFrames = new Set(
    calibration.confirmed_frames.map((frame) => frame.frame_index),
  );
  return (
    actualFrames.size === 3 &&
    calibration.confirmed_frames.every(
      (frame) =>
        frame.input_video === sourcePath &&
        frame.polygon_digest === calibration.polygon_digest &&
        resolutionsMatch(
          frame.source_resolution,
          calibration.source_resolution,
        ),
    )
  );
}
