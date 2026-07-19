import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { LanguageProvider } from "@/contexts/LanguageContext";

import {
  ProductionFeasibilityDashboard,
  type FeasibilityDashboardView,
} from "./ProductionFeasibilityDashboard";

const view: FeasibilityDashboardView = {
  status: "insufficient_evidence",
  totalFrames: 30,
  annotatedFrames: 19,
  confirmedLocalizablePositiveFrames: 12,
  confirmedAbsentFrames: 4,
  unknownFrames: 1,
  excludedFrames: 3,
  unconfirmedSuggestions: 2,
  applicableScaleStrata: ["near", "mid", "far"],
  applicableLightingStrata: ["bright_sun", "shadow"],
  scalePositiveCounts: { near: 4, mid: 5, far: 2 },
  lightingPositiveCounts: { bright_sun: 8, shadow: 2 },
  motionPositiveCounts: { none: 7, airborne: 2, occluded: 1 },
  missingStrata: ["scale:far", "lighting:shadow"],
  reasonCodes: ["localizable_positive_support"],
  contradictions: [],
  requiresNewAttempt: false,
  datasetExpansionEligibility: {
    eligible: false,
    reasons: ["check_role_is_evaluation_only"],
    localizablePositiveSeedCount: 12,
    pendingSuggestionDecisionCount: 2,
  },
  authorityGates: {
    developmentPackageBound: true,
    checkProbeBound: true,
    sealedEvidenceBound: true,
  },
  rawCounts: {
    top1Matches: 8,
    top5Matches: 10,
    evaluablePositives: 12,
    falseCandidates: 7,
    evaluableFrames: 16,
    rawCandidates: 33,
    candidateBudget: 5,
  },
  intervals: {
    method: "clopper_pearson_one_sided_95",
    top1RecallLower: 0.42,
    top5RecallLower: 0.58,
    falseCandidatesPerFrameUpper: 0.71,
  },
  lockedAttempt: {
    sessionId: "annotation-session-1",
    profileId: "official-coco-yolo11s-sahi",
    profileSha256: "a".repeat(64),
    metricProfileId: "tiny_ball_feasibility_metric_v1",
    dataRole: "check",
    revealed: true,
  },
};

describe("ProductionFeasibilityDashboard", () => {
  it("shows progress, missing strata, raw counts, intervals, and attempt identity", () => {
    render(
      <LanguageProvider>
        <ProductionFeasibilityDashboard view={view} />
      </LanguageProvider>,
    );
    expect(screen.getByText("19 / 30 frames annotated")).toBeVisible();
    expect(screen.getByText("scale:far")).toBeVisible();
    expect(screen.getByText("lighting:shadow")).toBeVisible();
    expect(screen.getByText("10 / 12")).toBeVisible();
    expect(screen.getByText("58.0%")).toBeVisible();
    expect(screen.getByText("official-coco-yolo11s-sahi")).toBeVisible();
    expect(
      screen.getByText(
        "Data-isolated check · evaluation only, never training data",
      ),
    ).toBeVisible();
    expect(
      screen.getByTestId("ball-annotation-dashboard-governance"),
    ).toHaveTextContent(
      "One person may annotate development data and make local trial decisions, but their own work is not an independent production audit. / 一个人可以标注开发数据并作出本地试跑决定，但其本人完成的工作不构成独立生产审计。",
    );
    expect(screen.getByText("2 unconfirmed suggestions")).toBeVisible();
  });

  it("states the narrow authorization when feasibility passes", () => {
    render(
      <LanguageProvider>
        <ProductionFeasibilityDashboard
          view={{ ...view, status: "feasibility_passed", missingStrata: [] }}
        />
      </LanguageProvider>,
    );
    expect(
      screen.getByText(
        "Passed only authorizes expanding to 100–300 confirmed boxes. It does not approve a trial, camera, full run, or production use.",
      ),
    ).toBeVisible();
  });

  it("labels a small report as exploratory even when point targets pass", () => {
    render(
      <LanguageProvider>
        <ProductionFeasibilityDashboard
          view={{
            ...view,
            status: "feasibility_passed",
            confirmedLocalizablePositiveFrames: 15,
            confirmedAbsentFrames: 5,
            missingStrata: [],
          }}
        />
      </LanguageProvider>,
    );
    expect(screen.getByText("Exploratory small-sample result")).toBeVisible();
  });

  it("calls out a pre-reveal applicability contradiction as a blocking conflict", () => {
    render(
      <LanguageProvider>
        <ProductionFeasibilityDashboard
          view={{
            ...view,
            missingStrata: ["applicability_contradiction:lighting:backlight"],
          }}
        />
      </LanguageProvider>,
    );

    expect(
      screen.getByRole("alert", {
        name: "Declared strata conflict with confirmed annotations",
      }),
    ).toHaveTextContent(
      "Lighting ‘backlight’ was declared not applicable before reveal, but confirmed annotations use it.",
    );
  });

  it("renders diagnostic contradictions, retry reasons, and eligible development evidence", () => {
    render(
      <LanguageProvider>
        <ProductionFeasibilityDashboard
          view={{
            ...view,
            reasonCodes: [],
            contradictions: [
              {
                frameIndex: 12,
                diagnosticCodes: ["scale_stratum_mismatch"],
              },
            ],
            requiresNewAttempt: true,
            datasetExpansionEligibility: {
              ...view.datasetExpansionEligibility,
              eligible: true,
              reasons: [],
              pendingSuggestionDecisionCount: 0,
            },
          }}
        />
      </LanguageProvider>,
    );

    expect(screen.getAllByText("none")).toHaveLength(2);
    expect(screen.getByText("requires_new_attempt")).toBeVisible();
    expect(
      screen.getByRole("alert", {
        name: "Declared strata conflict with confirmed annotations",
      }),
    ).toHaveTextContent("Scale ‘frame 12: scale_stratum_mismatch’");
    expect(
      screen.getByText("Eligible to expand toward 100–300 confirmed boxes"),
    ).toBeVisible();
    expect(
      screen.getByText(
        "Recommended next step: Try another model/profile or collect harder development frames. Any next check must use a newly frozen unseen temporal group.",
      ),
    ).toBeVisible();
  });

  it("localizes zero-support, non-finite intervals, and development contradictions", () => {
    localStorage.setItem("app-language", "zh");
    render(
      <LanguageProvider>
        <ProductionFeasibilityDashboard
          view={{
            ...view,
            status: "feasibility_failed",
            totalFrames: 0,
            annotatedFrames: 0,
            unconfirmedSuggestions: 3,
            requiresNewAttempt: true,
            missingStrata: ["applicability_contradiction:scale:near"],
            intervals: { ...view.intervals, top1RecallLower: Number.NaN },
            lockedAttempt: {
              ...view.lockedAttempt,
              dataRole: "development",
              revealed: false,
            },
          }}
        />
      </LanguageProvider>,
    );
    expect(screen.getByText("已标注 0 / 0 帧")).toBeVisible();
    expect(screen.getByText("3 个未确认建议")).toBeVisible();
    expect(screen.getByText("—")).toBeVisible();
    expect(screen.getByText("开发集 · 仅为探索性证据")).toBeVisible();
    expect(
      screen.getByText(
        "建议的下一步: 更换模型或配置，或补充更困难的开发帧。任何下一次检查都必须使用新冻结的未见时间组。",
      ),
    ).toBeVisible();
    expect(
      screen.getByRole("alert", { name: "预声明层次与确认标注冲突" }),
    ).toHaveTextContent("尺度“near”在揭示前声明为不适用");
    localStorage.removeItem("app-language");
  });
});
