import { describe, expect, it } from "vitest";

import {
  FinalizeBallAnnotationSessionResponse,
  GetBallAnnotationSessionResponse,
} from "../../../../lib/api-zod/src/generated/api";
import ballAnnotationApiGolden from "../../../../test_fixtures/contracts/ball_annotation_api_golden.v1.json";

describe("generated ball-annotation Zod contract", () => {
  it("accepts the deterministic session and result golden bodies", () => {
    const golden = ballAnnotationApiGolden as Record<string, unknown>;

    expect(
      GetBallAnnotationSessionResponse.safeParse(golden.development_session)
        .success,
    ).toBe(true);
    expect(
      GetBallAnnotationSessionResponse.safeParse(
        golden.development_proxy_session,
      ).success,
    ).toBe(true);
    expect(
      GetBallAnnotationSessionResponse.safeParse(golden.check_session_ready)
        .success,
    ).toBe(true);
    expect(
      FinalizeBallAnnotationSessionResponse.safeParse(golden.check_final_result)
        .success,
    ).toBe(true);
  });

  it("requires the explicit true-PTS and candidate-decision fields", () => {
    const raw = structuredClone(
      (ballAnnotationApiGolden as Record<string, any>).check_session_ready,
    );
    const candidateFrame = raw.frames.find(
      (frame: any) => frame.suggested_candidates.length > 0,
    );
    expect(candidateFrame).toBeDefined();

    delete raw.frames[0].true_presentation_timestamp;
    delete candidateFrame.suggested_candidates[0].decision;

    expect(GetBallAnnotationSessionResponse.safeParse(raw).success).toBe(false);
  });

  it.each([
    "session_request_authority",
    "detector_probe_authorities",
    "frame_review_proxy_authority",
  ])("requires generated final-package field %s", (field) => {
    const raw = structuredClone(
      (ballAnnotationApiGolden as Record<string, any>).check_final_result,
    );
    delete raw.package[field];

    expect(FinalizeBallAnnotationSessionResponse.safeParse(raw).success).toBe(
      false,
    );
  });
});
