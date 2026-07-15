import { describe, expect, it } from "vitest";

import {
  addConfirmedCalibrationFrame,
  calibrationIsComplete,
  createEmptyCalibration,
  createLatestRequestGate,
  mergePolygonDigestIfCurrent,
  replaceApprovedPolygon,
  responseMatchesCalibrationRequest,
  resolutionsMatch,
  type CalibrationFramePreview,
} from "./productionCalibration";

const SOURCE_PATH = "data/match-a.mp4";
const RESOLUTION = { width: 1_920, height: 1_080 };
const DIGEST = "a".repeat(64);
const POLYGON = [
  [100, 100],
  [1_800, 100],
  [1_800, 1_000],
] as [number, number][];

function preview(
  frameIndex: number,
  sampleIndex = frameIndex,
): CalibrationFramePreview {
  return {
    input_video: SOURCE_PATH,
    frame_width: RESOLUTION.width,
    frame_height: RESOLUTION.height,
    frame_index: frameIndex,
    frame_time_seconds: frameIndex / 25,
    sample_index: sampleIndex,
    sample_count: 5,
  };
}

describe("calibration async request identity", () => {
  it("accepts only the latest generation", () => {
    const gate = createLatestRequestGate();
    const stale = gate.begin();
    const current = gate.begin();
    expect(gate.isCurrent(stale)).toBe(false);
    expect(gate.isCurrent(current)).toBe(true);
    gate.invalidate();
    expect(gate.isCurrent(current)).toBe(false);
  });

  it("binds preview and suggestion responses to source, frame, resolution, and generation", () => {
    const expected = {
      generation: 3,
      current_generation: 3,
      source_path: SOURCE_PATH,
      frame_index: 20,
      resolution: RESOLUTION,
    };
    expect(responseMatchesCalibrationRequest(preview(20), expected)).toBe(true);
    expect(
      responseMatchesCalibrationRequest(
        { ...preview(20), input_video: "data/other.mp4" },
        expected,
      ),
    ).toBe(false);
    expect(responseMatchesCalibrationRequest(preview(21), expected)).toBe(
      false,
    );
    expect(
      responseMatchesCalibrationRequest(
        { ...preview(20), frame_width: 1_280 },
        expected,
      ),
    ).toBe(false);
    expect(
      responseMatchesCalibrationRequest(preview(20), {
        ...expected,
        current_generation: 4,
      }),
    ).toBe(false);
  });

  it("treats missing and mismatched resolutions as unequal", () => {
    expect(resolutionsMatch(null, RESOLUTION)).toBe(false);
    expect(resolutionsMatch(RESOLUTION, null)).toBe(false);
    expect(resolutionsMatch(RESOLUTION, { width: 1_280, height: 1_080 })).toBe(
      false,
    );
    expect(resolutionsMatch(RESOLUTION, { width: 1_920, height: 720 })).toBe(
      false,
    );
    expect(resolutionsMatch(RESOLUTION, { ...RESOLUTION })).toBe(true);
  });
});

describe("three-frame calibration evidence", () => {
  function approved() {
    return replaceApprovedPolygon(
      createEmptyCalibration(),
      POLYGON,
      [],
      RESOLUTION,
      DIGEST,
    );
  }

  function confirm(
    state: ReturnType<typeof approved>,
    frame: CalibrationFramePreview,
  ) {
    return addConfirmedCalibrationFrame(state, {
      preview: frame,
      source_path: SOURCE_PATH,
      source_resolution: RESOLUTION,
      polygon_digest: DIGEST,
      overlay_ready: true,
      polygon_valid: true,
    });
  }

  it("requires exactly three distinct actual frames, sorts them, and binds their digest", () => {
    const one = confirm(approved(), preview(30, 2));
    expect(one.ok).toBe(true);
    if (!one.ok) throw new Error("expected frame 30");
    const two = confirm(one.calibration, preview(10, 0));
    expect(two.ok).toBe(true);
    if (!two.ok) throw new Error("expected frame 10");
    const three = confirm(two.calibration, preview(20, 1));
    expect(three.ok).toBe(true);
    if (!three.ok) throw new Error("expected frame 20");

    expect(
      three.calibration.confirmed_frames.map((frame) => frame.frame_index),
    ).toEqual([10, 20, 30]);
    expect(
      three.calibration.confirmed_frames.every(
        (frame) => frame.polygon_digest === DIGEST,
      ),
    ).toBe(true);
    expect(calibrationIsComplete(three.calibration, SOURCE_PATH)).toBe(true);
    expect(confirm(three.calibration, preview(40))).toEqual({
      ok: false,
      reason: "already_complete",
    });
  });

  it("rejects duplicate actual frames even when sample indices differ", () => {
    const first = confirm(approved(), preview(10, 0));
    if (!first.ok) throw new Error("expected first frame");
    expect(confirm(first.calibration, preview(10, 4))).toEqual({
      ok: false,
      reason: "duplicate_frame",
    });
  });

  it.each([
    ["preview_not_ready", { overlay_ready: false }],
    ["invalid_polygon", { polygon_valid: false }],
    ["preview_mismatch", { source_path: "data/other.mp4" }],
    [
      "resolution_mismatch",
      { source_resolution: { width: 1_280, height: 720 } },
    ],
    ["digest_mismatch", { polygon_digest: "b".repeat(64) }],
  ] as const)("rejects %s", (reason, change) => {
    const result = addConfirmedCalibrationFrame(approved(), {
      preview: preview(10),
      source_path: SOURCE_PATH,
      source_resolution: RESOLUTION,
      polygon_digest: DIGEST,
      overlay_ready: true,
      polygon_valid: true,
      ...change,
    });
    expect(result).toEqual({ ok: false, reason });
  });

  it("rejects a preview returned for another source or resolution", () => {
    expect(
      addConfirmedCalibrationFrame(approved(), {
        preview: { ...preview(10), input_video: "data/other.mp4" },
        source_path: SOURCE_PATH,
        source_resolution: RESOLUTION,
        polygon_digest: DIGEST,
        overlay_ready: true,
        polygon_valid: true,
      }),
    ).toEqual({ ok: false, reason: "preview_mismatch" });
    expect(
      addConfirmedCalibrationFrame(approved(), {
        preview: { ...preview(10), frame_width: 1_280 },
        source_path: SOURCE_PATH,
        source_resolution: RESOLUTION,
        polygon_digest: DIGEST,
        overlay_ready: true,
        polygon_valid: true,
      }),
    ).toEqual({ ok: false, reason: "preview_mismatch" });
  });

  it("rejects a calibration suggestion bound to another source", () => {
    const state = {
      ...approved(),
      suggestion: {
        source_path: "data/other.mp4",
        source: "detector",
        confidence: "detected" as const,
        field_coverage: 0.5,
        source_resolution: RESOLUTION,
        frame_index: 10,
        polygon: POLYGON,
      },
    };
    expect(confirm(state, preview(10))).toEqual({
      ok: false,
      reason: "source_mismatch",
    });
  });

  it("clears frame confirmations when the approved polygon changes", () => {
    const first = confirm(approved(), preview(10));
    if (!first.ok) throw new Error("expected first frame");
    const changed = replaceApprovedPolygon(
      first.calibration,
      [...POLYGON, [100, 1_000]],
      [],
      RESOLUTION,
      "b".repeat(64),
    );
    expect(changed.confirmed_frames).toEqual([]);
    expect(calibrationIsComplete(changed, SOURCE_PATH)).toBe(false);
  });

  it("does not complete with invalid approved polygon coordinates", () => {
    const one = confirm(approved(), preview(10));
    if (!one.ok) throw new Error("expected frame 10");
    const two = confirm(one.calibration, preview(20));
    if (!two.ok) throw new Error("expected frame 20");
    const three = confirm(two.calibration, preview(30));
    if (!three.ok) throw new Error("expected frame 30");
    expect(
      calibrationIsComplete(
        {
          ...three.calibration,
          approved_polygon: [
            [0, 0],
            [1, 1],
          ],
        },
        SOURCE_PATH,
      ),
    ).toBe(false);
  });

  it("preserves confirmations when all approved polygon inputs are unchanged", () => {
    const first = confirm(approved(), preview(10));
    if (!first.ok) throw new Error("expected first frame");
    const exclusions = [
      [
        [1, 1],
        [2, 1],
        [2, 2],
      ],
    ] as [number, number][][];
    const withExclusions = replaceApprovedPolygon(
      first.calibration,
      POLYGON,
      exclusions,
      RESOLUTION,
      DIGEST,
    );
    const unchanged = replaceApprovedPolygon(
      {
        ...withExclusions,
        confirmed_frames: first.calibration.confirmed_frames,
      },
      POLYGON,
      exclusions,
      RESOLUTION,
      DIGEST,
    );
    expect(unchanged.confirmed_frames).toEqual(
      first.calibration.confirmed_frames,
    );
    expect(unchanged.exclusions).toEqual(exclusions);
  });

  it("merges a deferred digest into the latest suggestion without rolling it back", () => {
    const provisional = replaceApprovedPolygon(
      createEmptyCalibration(),
      POLYGON,
      [],
      RESOLUTION,
      DIGEST,
    );
    const latest = {
      ...provisional,
      polygon_digest: null,
      suggestion: {
        source_path: SOURCE_PATH,
        source: "new-detector-result",
        confidence: "detected" as const,
        field_coverage: 0.8,
        source_resolution: RESOLUTION,
        frame_index: 20,
        polygon: [
          [20, 20],
          [100, 20],
          [100, 100],
        ] as [number, number][],
      },
    };

    expect(
      mergePolygonDigestIfCurrent(latest, POLYGON, RESOLUTION, DIGEST),
    ).toEqual({
      ...latest,
      polygon_digest: DIGEST,
    });
    expect(
      mergePolygonDigestIfCurrent(
        latest,
        [...POLYGON, [100, 1_000]],
        RESOLUTION,
        DIGEST,
      ),
    ).toBeNull();
  });
});
