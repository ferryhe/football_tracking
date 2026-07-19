import { describe, expect, it } from "vitest";
import {
  getGetBallAnnotationFrameQueryOptions,
  getGetBallAnnotationFrameUrl,
  type BallTruePresentationTimestampView,
} from "@workspace/api-client-react";

describe("generated ball-annotation frame client", () => {
  it("exposes an explicit not-collected true PTS contract", () => {
    const timestamp: BallTruePresentationTimestampView = {
      status: "not_collected",
      value_seconds: null,
      method: null,
    };
    expect(timestamp).toEqual({
      status: "not_collected",
      value_seconds: null,
      method: null,
    });
  });

  it("keeps frame zero in the URL and enables its query", () => {
    expect(getGetBallAnnotationFrameUrl("annotation-session-1", 0)).toBe(
      "/api/ball-annotation-sessions/annotation-session-1/frames/0",
    );
    expect(
      getGetBallAnnotationFrameQueryOptions("annotation-session-1", 0).enabled,
    ).toBe(true);
  });

  it.each(["", "   "])("disables a frame query for blank session %j", (id) => {
    expect(getGetBallAnnotationFrameQueryOptions(id, 0).enabled).toBe(false);
  });

  it.each([undefined, null])(
    "disables a frame query for missing runtime session %s",
    (id) => {
      expect(
        getGetBallAnnotationFrameQueryOptions(id as unknown as string, 0)
          .enabled,
      ).toBe(false);
    },
  );

  it.each([-1, Number.NaN, Number.POSITIVE_INFINITY, 1.5])(
    "disables a frame query for invalid frame index %s",
    (frameIndex) => {
      expect(
        getGetBallAnnotationFrameQueryOptions(
          "annotation-session-1",
          frameIndex,
        ).enabled,
      ).toBe(false);
    },
  );
});
