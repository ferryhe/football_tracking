import assert from "node:assert/strict";

import {
  buildAIImprovementApprovalRequest,
  buildApprovedChildRunRequest,
  buildRejectIntentForStatusItem,
  buildStatusItemPresentation,
  groupAIImprovementStatusItems,
  isExecutableApprovedAction,
  statusItemCanBeApproved,
  statusItemNeedsHumanConfirmation,
} from "./aiImprovementStatus.ts";

const grouped = groupAIImprovementStatusItems({
  items_by_problem_type: {
    noise: [{ improvement_id: "imp-noise", recommended_action: "tighten_noise_filter" }],
    highlights: [{ candidate_id: "candidate-highlight", comparison_status: "pass" }],
    missing_ball: [{ improvement_id: "imp-missing", recommended_action: "localize_ball_roi" }],
    camera_motion: [{ candidate_id: "candidate-camera", promotion_status: "blocked" }],
  },
});
assert.deepEqual(grouped.map((group) => group.key), ["missing_ball", "noise", "camera_motion", "highlights"]);
assert.equal(grouped[0].items[0].improvement_id, "imp-missing");
assert.equal(grouped[3].items[0].candidate_id, "candidate-highlight");

const missingArtifacts = groupAIImprovementStatusItems({
  items_by_problem_type: {},
});
assert.equal(missingArtifacts.length, 4);
assert.deepEqual(missingArtifacts.map((group) => group.items.length), [0, 0, 0, 0]);

assert.equal(statusItemNeedsHumanConfirmation({ comparison_status: "warn" }), true);
assert.equal(statusItemNeedsHumanConfirmation({ promotion_status: "pending_confirmation" }), true);
assert.equal(statusItemNeedsHumanConfirmation({ comparison_status: "pass", promotion_status: "promoted" }), false);

const localSearchRoi = {
  coordinate_space: "image" as const,
  frame: 2079,
  x: 1660,
  y: 980,
  width: 180,
  height: 120,
  confidence: 0.84,
};
const approvalRequest = buildAIImprovementApprovalRequest(
  { improvement_id: "imp-localize", recommended_action: "localize_ball_roi" },
  "operator-ui",
  {
    id: "imp-localize",
    area: "missing ball",
    recommended_action: "localize_ball_roi",
    local_search_roi: localSearchRoi,
    rerun_scope: { start_frame: 2049, end_frame: 2544 },
    provenance: { source_packet_id: "packet_2079", visual_review_id: "visual_2079" },
  },
  ["imp-existing", "imp-localize"],
);
assert.deepEqual(approvalRequest.improvement_ids, ["imp-existing", "imp-localize"]);
assert.deepEqual(Object.keys(approvalRequest.local_search_roi_overrides ?? {}), ["imp-localize"]);
assert.deepEqual(approvalRequest.local_search_roi_overrides?.["imp-localize"], localSearchRoi);
assert.deepEqual(approvalRequest.rerun_scope_overrides?.["imp-localize"], { start_frame: 2049, end_frame: 2544 });

assert.equal(statusItemCanBeApproved({ improvement_id: "imp-localize" }), true);
assert.equal(statusItemCanBeApproved({ improvement_id: null }), false);
assert.throws(
  () =>
    buildAIImprovementApprovalRequest(
      { id: "candidate-only", candidate_id: "candidate-only", recommended_action: "localize_ball_roi" },
      "operator-ui",
      null,
      ["imp-existing"],
    ),
  /improvement_id/,
);

const rejectIntent = buildRejectIntentForStatusItem(
  { improvement_id: "imp-noise", candidate_id: "candidate-noise", approval_ids: ["approval-noise"] },
  "operator-ui",
);
assert.equal(rejectIntent.improvement_id, "imp-noise");
assert.equal(rejectIntent.candidate_id, "candidate-noise");
assert.deepEqual(rejectIntent.approval_ids, ["approval-noise"]);

const candidateOnlyRejectIntent = buildRejectIntentForStatusItem(
  { id: "candidate-only", candidate_id: "candidate-only", approval_ids: ["approval-candidate"] },
  "operator-ui",
);
assert.equal(candidateOnlyRejectIntent.improvement_id, null);
assert.equal(candidateOnlyRejectIntent.candidate_id, "candidate-only");

const childRunRequest = buildApprovedChildRunRequest(
  "run-123",
  {
    approval_id: "approval-localize",
    improvement_id: "imp-localize",
    approved_action: "localize_ball_roi",
  },
  "ai_improvement_approved_actions.json",
);
assert.deepEqual(childRunRequest.approved_action_ids, ["approval-localize"]);
assert.equal(childRunRequest.parent_run_id, "run-123");
assert.equal(childRunRequest.approved_actions_artifact_name, "ai_improvement_approved_actions.json");
assert.throws(
  () =>
    buildApprovedChildRunRequest(
      "run-123",
      {
        approval_id: "",
        improvement_id: "imp-localize",
        approved_action: "localize_ball_roi",
      },
      "ai_improvement_approved_actions.json",
    ),
  /approval_id/,
);
assert.equal(isExecutableApprovedAction({ approval_id: "approval-localize", approved_action: "localize_ball_roi" }), true);

const presentation = buildStatusItemPresentation(
  {
    improvement_id: "imp-localize",
    candidate_id: "candidate-localize",
    frame_window: { start_frame: 2049, end_frame: 2544 },
    evidence_ids: ["packet_2079"],
    recommended_action: "localize_ball_roi",
    approval_status: "approved",
    comparison_status: "unavailable",
    promotion_status: "not_promoted",
    artifact_references: [],
  },
  {
    id: "imp-localize",
    area: "missing ball",
    recommended_action: "localize_ball_roi",
    local_search_roi: localSearchRoi,
    evidence: [{ id: "visual_2079" }],
    provenance: { source_packet_id: "packet_2079", visual_review_id: "visual_2079", model_tier: "strong" },
  },
);
assert.equal(presentation.frameWindowLabel, "2049-2544");
assert.equal(presentation.roiState, "localize_ball_roi");
assert.equal(presentation.modelTier, "strong");
assert.deepEqual(presentation.evidenceIds, ["packet_2079", "visual_2079"]);
assert.deepEqual(presentation.localSearchRoi, localSearchRoi);
assert.deepEqual(presentation.provenance, {
  source_packet_id: "packet_2079",
  visual_review_id: "visual_2079",
  model_tier: "strong",
});
assert.equal(presentation.comparisonStatus, "unavailable");

const approvedActionPresentation = buildStatusItemPresentation(
  {
    candidate_id: "candidate-approved-only",
    approval_ids: ["approval-approved-only"],
    approved_action: "localize_ball_roi",
    comparison_status: "warn",
    promotion_status: "pending_confirmation",
    artifact_references: [],
  },
  null,
  { model_selection: { model: "gpt-improve-strong", source: "improvement_model" } },
  {
    approval_id: "approval-approved-only",
    improvement_id: "imp-approved-only",
    approved_action: "localize_ball_roi",
    local_search_roi: localSearchRoi,
    provenance: { source_packet_id: "approved_packet_2079", visual_review_id: "approved_visual_2079", model: "gpt-vision-strong" },
  },
);
assert.equal(approvedActionPresentation.improvementId, null);
assert.equal(approvedActionPresentation.roiState, "localize_ball_roi");
assert.deepEqual(approvedActionPresentation.localSearchRoi, localSearchRoi);
assert.deepEqual(approvedActionPresentation.evidenceIds, ["approved_packet_2079", "approved_visual_2079"]);
assert.equal(approvedActionPresentation.modelTier, "gpt-vision-strong");
assert.equal(approvedActionPresentation.needsHumanConfirmation, true);

const reportModelPresentation = buildStatusItemPresentation(
  { improvement_id: "imp-model", artifact_references: [] },
  null,
  { model_selection: { model: "gpt-improve-strong", source: "improvement_model" } },
);
assert.equal(reportModelPresentation.modelTier, "gpt-improve-strong");
