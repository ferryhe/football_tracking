import type {
  AIApprovedAction,
  AIFrameWindow,
  AIImproveApprovalRequest,
  AIImprovementArtifactReference,
  AIImprovementStatusItem,
  AIImprovementStatusResponse,
  AIImprovementItem,
  AILocalSearchRoi,
  CreateRunRequest,
  HighlightRenderRequest,
} from "./types";

export type AIImprovementProblemGroupKey = "missing_ball" | "noise" | "camera_motion" | "highlights";

export interface AIImprovementStatusGroup {
  key: AIImprovementProblemGroupKey;
  items: AIImprovementStatusItem[];
}

export interface AIImprovementRejectIntent {
  item_key: string;
  improvement_id: string | null;
  candidate_id: string | null;
  approval_ids: string[];
  rejected_by: string;
}

export interface AIImprovementStatusItemPresentation {
  key: string;
  improvementId: string | null;
  candidateId: string | null;
  approvalIds: string[];
  consumedApprovalIds: string[];
  evidenceIds: string[];
  frameWindow: AIFrameWindow | null;
  frameWindowLabel: string;
  roiState: "localize_ball_roi" | "not_visible" | "none";
  localSearchRoi: AILocalSearchRoi | null;
  provenance: Record<string, unknown> | null;
  falsePositiveClass: string | null;
  confidence: number | null;
  confidenceLabel: string;
  recommendedAction: string | null;
  approvedAction: string | null;
  modelTier: string;
  approvalStatus: string;
  comparisonStatus: string;
  promotionStatus: string;
  needsHumanConfirmation: boolean;
  artifactReferences: AIImprovementArtifactReference[];
}

const GROUP_ORDER: AIImprovementProblemGroupKey[] = ["missing_ball", "noise", "camera_motion", "highlights"];
const EXECUTABLE_APPROVED_ACTIONS = new Set(["targeted_rerun", "rerun_ball_window", "localize_ball_roi"]);

export function groupAIImprovementStatusItems(
  status: Pick<AIImprovementStatusResponse, "items_by_problem_type"> | null | undefined,
): AIImprovementStatusGroup[] {
  const grouped: Record<AIImprovementProblemGroupKey, AIImprovementStatusItem[]> = {
    missing_ball: [],
    noise: [],
    camera_motion: [],
    highlights: [],
  };

  for (const [rawKey, items] of Object.entries(status?.items_by_problem_type ?? {})) {
    const key = normalizeProblemGroup(rawKey);
    if (!Array.isArray(items)) continue;
    grouped[key].push(...items.filter(isRecord));
  }

  return GROUP_ORDER.map((key) => ({ key, items: grouped[key] }));
}

export function buildAIImprovementApprovalRequest(
  item: Pick<AIImprovementStatusItem, "improvement_id" | "recommended_action">,
  approvedBy: string,
  reportItem?: Partial<AIImprovementItem> | null,
  existingImprovementIds: readonly string[] = [],
): AIImproveApprovalRequest {
  const improvementId = statusItemApprovalTargetId(item);
  if (!improvementId) {
    throw new Error("AI improvement approval requires an exact improvement_id.");
  }

  const request: AIImproveApprovalRequest = {
    improvement_ids: uniqueStrings([...existingImprovementIds, improvementId]),
    approved_by: approvedBy,
  };

  if (reportItem?.rerun_scope) {
    request.rerun_scope_overrides = { [improvementId]: reportItem.rerun_scope };
  }
  if (reportItem?.local_search_roi) {
    request.local_search_roi_overrides = { [improvementId]: reportItem.local_search_roi };
  }
  if (isFilledRecord(reportItem?.config_patch)) {
    request.config_patch_overrides = { [improvementId]: reportItem.config_patch };
  }
  if (reportItem?.suggested_window) {
    request.suggested_window_overrides = { [improvementId]: reportItem.suggested_window };
  }
  if (reportItem?.clip_action) {
    request.clip_action_overrides = { [improvementId]: reportItem.clip_action };
  }
  if (isFilledRecord(reportItem?.follow_cam_rerender_plan)) {
    request.follow_cam_rerender_plan_overrides = { [improvementId]: reportItem.follow_cam_rerender_plan };
  }

  return request;
}

export function buildRejectIntentForStatusItem(
  item: Pick<AIImprovementStatusItem, "id" | "improvement_id" | "candidate_id" | "approval_ids">,
  rejectedBy: string,
): AIImprovementRejectIntent {
  return {
    item_key: statusItemKey(item),
    improvement_id: cleanString(item.improvement_id),
    candidate_id: cleanString(item.candidate_id),
    approval_ids: cleanStringArray(item.approval_ids),
    rejected_by: rejectedBy,
  };
}

export function buildApprovedChildRunRequest(
  parentRunId: string,
  action: Pick<AIApprovedAction, "approval_id" | "approved_action">,
  artifactName?: string | null,
): CreateRunRequest {
  const approvalId = cleanString(action.approval_id);
  if (!approvalId) {
    throw new Error("Approved child recovery requires an exact approval_id.");
  }

  return {
    parent_run_id: parentRunId,
    approved_action_ids: [approvalId],
    approved_actions_artifact_name: cleanString(artifactName),
    notes: `operator-ui queued approved AI improvement action ${approvalId}`,
  };
}

export function buildApprovedHighlightRenderRequest(
  action: Pick<AIApprovedAction, "approval_id">,
): HighlightRenderRequest {
  const approvalId = cleanString(action.approval_id);
  if (!approvalId) {
    throw new Error("Approved highlight render requires an exact approval_id.");
  }
  return {
    approved_action_id: approvalId,
    notes: `operator-ui rendered approved AI highlight action ${approvalId}`,
  };
}

export function isExecutableApprovedAction(action: Pick<AIApprovedAction, "approved_action"> | null | undefined): boolean {
  return EXECUTABLE_APPROVED_ACTIONS.has(cleanString(action?.approved_action) ?? "");
}

export function statusItemCanBeApproved(item: Pick<AIImprovementStatusItem, "improvement_id"> | null | undefined): boolean {
  return cleanString(item?.improvement_id) !== null;
}

export function statusItemNeedsHumanConfirmation(
  item: Pick<AIImprovementStatusItem, "comparison_status" | "promotion_status"> | null | undefined,
): boolean {
  return item?.comparison_status === "warn" || item?.promotion_status === "pending_confirmation";
}

export function buildStatusItemPresentation(
  item: AIImprovementStatusItem,
  reportItem?: Partial<AIImprovementItem> | null,
  reportSummary?: Record<string, unknown> | null,
  approvedAction?: Partial<AIApprovedAction> | null,
): AIImprovementStatusItemPresentation {
  const improvementId = cleanString(item.improvement_id);
  const candidateId = cleanString(item.candidate_id);
  const approvalIds = cleanStringArray(item.approval_ids);
  const consumedApprovalIds = cleanStringArray(item.consumed_approval_ids);
  const frameWindow = item.frame_window ?? reportItemFrameWindow(reportItem);
  const recommendedAction =
    cleanString(item.recommended_action) ?? cleanString(reportItem?.recommended_action) ?? cleanString(approvedAction?.approved_action);
  const approvedActionName = cleanString(item.approved_action) ?? cleanString(approvedAction?.approved_action);
  const localSearchRoi = approvedAction?.local_search_roi ?? reportItem?.local_search_roi ?? null;
  const provenance = mergeRecords(reportItem?.provenance, approvedAction?.provenance);
  const evidenceIds = uniqueStrings([
    ...cleanStringArray(item.evidence_ids),
    ...extractEvidenceIds(reportItem?.evidence),
    cleanString(reportItem?.source_packet_id),
    cleanString(reportItem?.visual_review_id),
    cleanString(reportItem?.camera_motion_event_id),
    cleanString(approvedAction?.source_packet_id),
    cleanString(approvedAction?.visual_review_id),
    cleanString(approvedAction?.camera_motion_event_id),
    cleanString(provenance?.source_packet_id),
    cleanString(provenance?.visual_review_id),
    cleanString(provenance?.camera_motion_event_id),
  ]);
  const confidence = typeof item.confidence === "number" ? item.confidence : typeof reportItem?.confidence === "number" ? reportItem.confidence : null;

  return {
    key: statusItemKey(item),
    improvementId,
    candidateId,
    approvalIds,
    consumedApprovalIds,
    evidenceIds,
    frameWindow,
    frameWindowLabel: frameWindow ? `${frameWindow.start_frame}-${frameWindow.end_frame}` : "n/a",
    roiState: recoveryState(recommendedAction, approvedActionName, localSearchRoi),
    localSearchRoi,
    provenance,
    falsePositiveClass: cleanString(item.false_positive_class) ?? cleanString(reportItem?.false_positive_class),
    confidence,
    confidenceLabel: confidence == null ? "n/a" : confidence.toFixed(2),
    recommendedAction,
    approvedAction: approvedActionName,
    modelTier: modelTierForImprovement(reportItem, reportSummary, approvedAction),
    approvalStatus: cleanString(item.approval_status) ?? "none",
    comparisonStatus: cleanString(item.comparison_status) ?? "none",
    promotionStatus: cleanString(item.promotion_status) ?? "not_promoted",
    needsHumanConfirmation: statusItemNeedsHumanConfirmation(item),
    artifactReferences: Array.isArray(item.artifact_references) ? item.artifact_references.filter(isRecord) : [],
  };
}

export function statusItemKey(
  item: Pick<AIImprovementStatusItem, "id" | "improvement_id" | "candidate_id" | "approval_ids">,
  fallback = "status-item",
): string {
  return (
    cleanString(item.improvement_id) ??
    cleanString(item.id) ??
    cleanString(item.candidate_id) ??
    cleanStringArray(item.approval_ids)[0] ??
    fallback
  );
}

function statusItemApprovalTargetId(item: Pick<AIImprovementStatusItem, "improvement_id">): string | null {
  return cleanString(item.improvement_id);
}

function normalizeProblemGroup(value: string): AIImprovementProblemGroupKey {
  const cleaned = value.trim().toLowerCase();
  if (cleaned === "missing_ball" || cleaned === "missingball" || cleaned === "missing-ball") return "missing_ball";
  if (cleaned === "camera_motion" || cleaned === "camera-motion" || cleaned === "follow_cam" || cleaned === "follow-cam") {
    return "camera_motion";
  }
  if (cleaned === "highlight" || cleaned === "highlights" || cleaned === "highlight_boundary") return "highlights";
  return "noise";
}

function reportItemFrameWindow(reportItem?: Partial<AIImprovementItem> | null): AIFrameWindow | null {
  if (!reportItem) return null;
  if (reportItem.rerun_scope) return reportItem.rerun_scope;
  if (reportItem.suggested_window) return reportItem.suggested_window;
  if (reportItem.start_frame != null && reportItem.end_frame != null) {
    return { start_frame: reportItem.start_frame, end_frame: reportItem.end_frame };
  }
  return null;
}

function recoveryState(
  recommendedAction: string | null,
  approvedAction: string | null,
  localSearchRoi: AILocalSearchRoi | null,
): AIImprovementStatusItemPresentation["roiState"] {
  const action = approvedAction ?? recommendedAction;
  if (action === "mark_ball_not_visible") return "not_visible";
  if (action === "localize_ball_roi" || localSearchRoi) return "localize_ball_roi";
  return "none";
}

function extractEvidenceIds(evidence: unknown): string[] {
  if (!Array.isArray(evidence)) return [];
  return evidence.flatMap((item) => {
    const direct = cleanString(item);
    if (direct) return [direct];
    if (!isRecord(item)) return [];
    return [
      cleanString(item.id),
      cleanString(item.source_packet_id),
      cleanString(item.visual_review_id),
      cleanString(item.camera_motion_event_id),
      cleanString(item.candidate_id),
    ].filter((value): value is string => value !== null);
  });
}

function modelTierForImprovement(
  reportItem?: Partial<AIImprovementItem> | null,
  reportSummary?: Record<string, unknown> | null,
  approvedAction?: Partial<AIApprovedAction> | null,
): string {
  const provenance = isFilledRecord(reportItem?.provenance) ? reportItem.provenance : {};
  const approvalProvenance = isFilledRecord(approvedAction?.provenance) ? approvedAction.provenance : {};
  const modelSelection = isFilledRecord(reportSummary?.model_selection) ? reportSummary.model_selection : {};
  return (
    cleanString(reportItem?.model_tier) ??
    cleanString(reportItem?.model) ??
    cleanString(provenance.model_tier) ??
    cleanString(provenance.model) ??
    cleanString(approvalProvenance.model_tier) ??
    cleanString(approvalProvenance.model) ??
    cleanString(reportSummary?.model_tier) ??
    cleanString(reportSummary?.model) ??
    cleanString(modelSelection.model) ??
    cleanString(modelSelection.source) ??
    cleanString(reportSummary?.provider_model) ??
    "n/a"
  );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

function isFilledRecord(value: unknown): value is Record<string, unknown> {
  return isRecord(value) && Object.keys(value).length > 0;
}

function mergeRecords(...values: unknown[]): Record<string, unknown> | null {
  const merged: Record<string, unknown> = {};
  for (const value of values) {
    if (isFilledRecord(value)) Object.assign(merged, value);
  }
  return Object.keys(merged).length > 0 ? merged : null;
}

function cleanString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function cleanStringArray(value: unknown): string[] {
  return Array.isArray(value)
    ? value.map(cleanString).filter((item): item is string => item !== null)
    : [];
}

function uniqueStrings(values: Array<string | null | undefined>): string[] {
  const seen = new Set<string>();
  const result: string[] = [];
  for (const value of values) {
    const cleaned = cleanString(value);
    if (!cleaned || seen.has(cleaned)) continue;
    seen.add(cleaned);
    result.push(cleaned);
  }
  return result;
}
