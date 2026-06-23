import assert from "node:assert/strict";

import {
  buildLifecycleCandidateIndex,
  getRunLifecycle,
  hasConcreteApprovalId,
  inferAIImprovementProblemType,
  lifecycleBlockingReasonLabel,
  lifecycleOperatorStateLabel,
  lifecycleProblemTypeLabel,
  lifecycleStatusLabel,
  presentLifecycleCandidate,
  presentLifecycleSummary,
  resolveImprovementLifecycleCandidate,
} from "./aiLifecycle.ts";
import { translations } from "./i18n.ts";
import type {
  AICandidateLifecycleCandidate,
  AICandidateLifecycleReport,
  AIImprovementItem,
  RunRecord,
} from "./types.ts";

function candidate(overrides: Partial<AICandidateLifecycleCandidate>): AICandidateLifecycleCandidate {
  return {
    candidate_id: overrides.candidate_id ?? "candidate-1",
    problem_type: overrides.problem_type ?? "missing_ball",
    improvement_ids: overrides.improvement_ids ?? [],
    approval_ids: overrides.approval_ids ?? [],
    artifact_paths: overrides.artifact_paths ?? [],
    stage: overrides.stage ?? "review_only",
    comparison_status: overrides.comparison_status ?? "none",
    promotion_status: overrides.promotion_status ?? "not_promoted",
    resolution_status: overrides.resolution_status ?? "none",
    blocking_reasons: overrides.blocking_reasons ?? [],
  };
}

function report(candidates: AICandidateLifecycleCandidate[]): AICandidateLifecycleReport {
  return {
    schema_version: "1.0",
    summary: {
      stage: candidates[0]?.stage ?? "review_only",
      comparison_status: candidates[0]?.comparison_status ?? "none",
      promotion_status: candidates[0]?.promotion_status ?? "not_promoted",
      resolution_status: candidates[0]?.resolution_status ?? "none",
      blocking_reasons: candidates.flatMap((item) => item.blocking_reasons ?? []),
      candidate_count: candidates.length,
      approved_action_count: candidates.filter((item) => (item.approval_ids ?? []).length > 0).length,
      comparison_report_count: candidates.filter((item) => item.comparison_status && item.comparison_status !== "none").length,
    },
    candidates,
  };
}

const stateCases: Array<[string, Partial<AICandidateLifecycleCandidate>, string, string]> = [
  ["empty", {}, "review_only", "AI review only"],
  ["review-only", { stage: "review_only" }, "review_only", "AI review only"],
  ["proposed", { stage: "proposed" }, "suggested", "suggested"],
  ["approved", { stage: "approved", approval_ids: ["approval-1"] }, "approved", "approved"],
  [
    "pending execution",
    { stage: "pending_execution", approval_ids: ["approval-1"], blocking_reasons: ["pending_api_execution"] },
    "waiting_to_execute",
    "waiting to execute",
  ],
  ["executed", { stage: "executed", artifact_paths: ["ai_candidates/noise/candidate-1/out.json"] }, "executed_candidate", "executed candidate"],
  ["pass", { stage: "compared", comparison_status: "pass" }, "passed_comparison", "passed comparison"],
  [
    "warn",
    { stage: "compared", comparison_status: "warn", promotion_status: "pending_confirmation", blocking_reasons: ["pending_human_confirmation"] },
    "warned_comparison",
    "warned comparison",
  ],
  ["fail", { stage: "compared", comparison_status: "fail" }, "failed_comparison", "failed comparison"],
  [
    "unavailable",
    { stage: "executed", comparison_status: "unavailable", blocking_reasons: ["missing_comparison"] },
    "rejected_or_blocked",
    "rejected or blocked",
  ],
  ["promoted", { stage: "finalized", comparison_status: "pass", promotion_status: "promoted" }, "promoted_final_output", "promoted final output"],
  ["rejected", { stage: "finalized", promotion_status: "rejected" }, "rejected_or_blocked", "rejected or blocked"],
  ["blocked", { stage: "finalized", promotion_status: "blocked", blocking_reasons: ["unsupported_type"] }, "rejected_or_blocked", "rejected or blocked"],
  ["resolved not visible", { stage: "finalized", resolution_status: "resolved_not_visible" }, "resolved_not_visible", "resolved not visible"],
];

for (const [name, input, expectedState, expectedLabel] of stateCases) {
  const presentation = presentLifecycleCandidate(candidate(input));
  assert.equal(presentation.state, expectedState, name);
  assert.equal(lifecycleOperatorStateLabel(presentation.state), expectedLabel, name);
}

const emptyCandidate = presentLifecycleCandidate(null);
assert.equal(emptyCandidate.state, "review_only");
assert.equal(emptyCandidate.isPublishable, false);

const reviewOnly = presentLifecycleCandidate(candidate({ stage: "review_only" }));
assert.equal(reviewOnly.isFinal, false);
assert.equal(reviewOnly.isPublishable, false);
assert.match(reviewOnly.label, /review/i);
assert.doesNotMatch(reviewOnly.label, /applied|final|improved/i);

const noiseWithComparison = presentLifecycleCandidate(
  candidate({ problem_type: "noise", comparison_status: "warn", promotion_status: "pending_confirmation" }),
);
assert.equal(noiseWithComparison.state, "warned_comparison");
assert.deepEqual(noiseWithComparison.evidence, [
  "Problem: Noise",
  "Candidate: candidate-1",
  "Comparison: warn",
  "Promotion: pending confirmation",
]);

assert.equal(lifecycleProblemTypeLabel("missing_ball"), "Missing ball");
assert.equal(lifecycleProblemTypeLabel("follow_cam"), "Follow-cam");
assert.equal(lifecycleProblemTypeLabel("camera_motion"), "Camera motion");
assert.equal(lifecycleProblemTypeLabel("noise_cleanup"), "Noise cleanup");

assert.equal(lifecycleBlockingReasonLabel("missing_comparison"), "Missing comparison report");
assert.equal(lifecycleBlockingReasonLabel("pending_api_execution"), "Waiting for execution");
assert.equal(lifecycleBlockingReasonLabel("unsafe_custom_reason"), "Unsafe custom reason");
assert.equal(lifecycleStatusLabel("pending_confirmation"), "pending confirmation");
assert.equal(translations.en.aiAnalysis.lifecycleStatusValues.pending_confirmation, "pending confirmation");
assert.equal(translations.zh.aiAnalysis.lifecycleStatusValues.pass, "通过");
assert.equal(translations.zh.aiAnalysis.lifecycleStatusValues.pending_confirmation, "等待确认");
assert.notEqual(translations.zh.aiAnalysis.lifecycleStatusValues.pass, "pass");

const linkedReport = report([
  candidate({
    candidate_id: "candidate-direct",
    improvement_ids: ["imp-direct"],
    approval_ids: ["approval-direct"],
    stage: "approved",
  }),
  candidate({
    candidate_id: "candidate-by-approval",
    improvement_ids: ["imp-other"],
    approval_ids: ["approval-linked"],
    stage: "approved",
  }),
]);
const index = buildLifecycleCandidateIndex(linkedReport);
assert.equal(index.byCandidateId.get("candidate-direct")?.candidate_id, "candidate-direct");
assert.equal(index.byImprovementId.get("imp-direct")?.candidate_id, "candidate-direct");
assert.equal(index.byApprovalId.get("approval-linked")?.candidate_id, "candidate-by-approval");

const itemByCandidate = { id: "imp-missing", candidate_id: "candidate-direct" } as AIImprovementItem;
assert.equal(resolveImprovementLifecycleCandidate(itemByCandidate, index, null)?.candidate_id, "candidate-direct");
const itemByImprovement = { id: "imp-direct" } as AIImprovementItem;
assert.equal(resolveImprovementLifecycleCandidate(itemByImprovement, index, null)?.candidate_id, "candidate-direct");
const itemByApproval = { id: "imp-unknown" } as AIImprovementItem;
assert.equal(
  resolveImprovementLifecycleCandidate(itemByApproval, index, { approval_id: "approval-linked" })?.candidate_id,
  "candidate-by-approval",
);

assert.equal(hasConcreteApprovalId({ approval_id: "approval-1" }), true);
assert.equal(hasConcreteApprovalId({ approval_id: " " }), false);
assert.equal(hasConcreteApprovalId(null), false);

const directLifecycle = report([candidate({ stage: "finalized", comparison_status: "pass", promotion_status: "promoted" })]);
const statsLifecycle = report([candidate({ stage: "compared", comparison_status: "pass" })]);
const runWithDirectLifecycle = {
  ai_candidate_lifecycle: directLifecycle,
  stats: { ai_candidate_lifecycle: statsLifecycle },
} as RunRecord;
assert.equal(getRunLifecycle(runWithDirectLifecycle), directLifecycle);
assert.equal(getRunLifecycle({ stats: { ai_candidate_lifecycle: statsLifecycle } } as RunRecord), statsLifecycle);

const promotedSummary = presentLifecycleSummary(directLifecycle);
assert.equal(promotedSummary.state, "promoted_final_output");
assert.equal(promotedSummary.isPublishable, true);
assert.equal(promotedSummary.isFinal, true);
const reviewSummary = presentLifecycleSummary(report([]));
assert.equal(reviewSummary.state, "review_only");
assert.equal(reviewSummary.isPublishable, false);
assert.doesNotMatch(reviewSummary.label, /final|improved/i);

const summaryOnlyLifecycle = {
  stage: "compared",
  comparison_status: "warn",
  promotion_status: "pending_confirmation",
  resolution_status: "none",
  blocking_reasons: ["pending_human_confirmation"],
  candidate_count: 2,
  approved_action_count: 1,
  comparison_report_count: 1,
} satisfies AICandidateLifecycleReport["summary"];
const normalizedStatsLifecycle = getRunLifecycle({
  stats: { ai_candidate_lifecycle: summaryOnlyLifecycle },
} as RunRecord);
assert.equal(normalizedStatsLifecycle?.summary, summaryOnlyLifecycle);
assert.deepEqual(normalizedStatsLifecycle?.candidates, []);
assert.equal(presentLifecycleSummary(normalizedStatsLifecycle).state, "warned_comparison");

const promotedWithoutComparison = presentLifecycleCandidate(
  candidate({
    stage: "finalized",
    comparison_status: "unavailable",
    promotion_status: "promoted",
    blocking_reasons: ["missing_comparison"],
  }),
);
assert.equal(promotedWithoutComparison.state, "rejected_or_blocked");
assert.equal(promotedWithoutComparison.isFinal, false);
assert.equal(promotedWithoutComparison.isPublishable, false);
assert.notEqual(promotedWithoutComparison.label, "promoted final output");

for (const [name, input, expectedState] of [
  [
    "resolved not visible with failed quality gate",
    { resolution_status: "resolved_not_visible", blocking_reasons: ["failed_quality_gate"] },
    "rejected_or_blocked",
  ],
  [
    "resolved not visible with failed comparison",
    { resolution_status: "resolved_not_visible", comparison_status: "fail" },
    "failed_comparison",
  ],
  [
    "resolved not visible with unavailable comparison",
    { resolution_status: "resolved_not_visible", comparison_status: "unavailable" },
    "rejected_or_blocked",
  ],
] as const) {
  const presentation = presentLifecycleCandidate(candidate({ stage: "finalized", ...input }));
  assert.equal(presentation.state, expectedState, name);
  assert.equal(presentation.isFinal, false, name);
  assert.equal(presentation.isPublishable, false, name);
  assert.notEqual(presentation.tone, "success", name);
}

assert.equal(
  inferAIImprovementProblemType({
    id: "imp-missing",
    candidate_id: "candidate-with-id",
    problem_type: "missing_ball",
    area: "unknown",
    recommended_action: "manual_review",
  } as AIImprovementItem),
  "missing_ball",
);
assert.equal(
  inferAIImprovementProblemType({
    id: "imp-noise",
    candidate_id: "candidate-noise",
    area: "False positive cleanup",
    recommended_action: "manual_review",
  } as AIImprovementItem),
  "noise",
);
assert.equal(
  inferAIImprovementProblemType({
    id: "imp-follow",
    candidate_id: "candidate-follow",
    area: "camera",
    recommended_action: "adjust_follow_cam",
  } as AIImprovementItem),
  "follow_cam",
);
assert.equal(
  inferAIImprovementProblemType({
    id: "imp-highlight",
    candidate_id: "candidate-highlight",
    area: "clip boundary",
    recommended_action: "adjust_highlight_window",
  } as AIImprovementItem),
  "highlight",
);
assert.equal(
  inferAIImprovementProblemType({
    id: "imp-not-highlight",
    candidate_id: "candidate-only",
    area: "manual review",
    recommended_action: "manual_review",
  } as AIImprovementItem),
  null,
);
