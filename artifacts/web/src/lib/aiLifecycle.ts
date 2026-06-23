import type {
  AICandidateLifecycleBlockingReason,
  AICandidateLifecycleCandidate,
  AICandidateLifecycleComparisonStatus,
  AICandidateLifecyclePromotionStatus,
  AICandidateLifecycleReport,
  AICandidateLifecycleResolutionStatus,
  AICandidateLifecycleSummary,
  AICandidateLifecycleStage,
  AIApprovedAction,
  AIImprovementItem,
  RunRecord,
} from "./types";

export type LifecycleOperatorState =
  | "review_only"
  | "suggested"
  | "approved"
  | "waiting_to_execute"
  | "executed_candidate"
  | "passed_comparison"
  | "warned_comparison"
  | "failed_comparison"
  | "promoted_final_output"
  | "rejected_or_blocked"
  | "resolved_not_visible";

export type LifecycleTone = "muted" | "info" | "approved" | "pending" | "success" | "warning" | "danger";

export interface LifecycleCandidatePresentation {
  state: LifecycleOperatorState;
  label: string;
  tone: LifecycleTone;
  problemLabel: string | null;
  blockingLabels: string[];
  evidence: string[];
  isFinal: boolean;
  isPublishable: boolean;
  hasConcreteApproval: boolean;
}

export interface LifecycleSummaryPresentation extends LifecycleCandidatePresentation {
  candidateCount: number;
  approvedActionCount: number;
  comparisonReportCount: number;
}

export interface LifecycleCandidateIndex {
  byCandidateId: Map<string, AICandidateLifecycleCandidate>;
  byImprovementId: Map<string, AICandidateLifecycleCandidate>;
  byApprovalId: Map<string, AICandidateLifecycleCandidate>;
  candidates: AICandidateLifecycleCandidate[];
}

export const LIFECYCLE_OPERATOR_STATE_LABELS: Record<LifecycleOperatorState, string> = {
  review_only: "AI review only",
  suggested: "suggested",
  approved: "approved",
  waiting_to_execute: "waiting to execute",
  executed_candidate: "executed candidate",
  passed_comparison: "passed comparison",
  warned_comparison: "warned comparison",
  failed_comparison: "failed comparison",
  promoted_final_output: "promoted final output",
  rejected_or_blocked: "rejected or blocked",
  resolved_not_visible: "resolved not visible",
};

const OPERATOR_STATE_TONES: Record<LifecycleOperatorState, LifecycleTone> = {
  review_only: "muted",
  suggested: "info",
  approved: "approved",
  waiting_to_execute: "pending",
  executed_candidate: "info",
  passed_comparison: "success",
  warned_comparison: "warning",
  failed_comparison: "danger",
  promoted_final_output: "success",
  rejected_or_blocked: "danger",
  resolved_not_visible: "success",
};

const BLOCKING_REASON_LABELS: Record<string, string> = {
  missing_evidence: "Missing evidence",
  unsafe_window: "Unsafe window",
  unsupported_type: "Unsupported candidate type",
  missing_candidate_id: "Missing candidate ID",
  missing_comparison: "Missing comparison report",
  failed_quality_gate: "Failed quality gate",
  pending_api_execution: "Waiting for execution",
  pending_human_confirmation: "Waiting for operator confirmation",
};

const PROBLEM_TYPE_LABELS: Record<string, string> = {
  missing_ball: "Missing ball",
  noise: "Noise",
  follow_cam: "Follow-cam",
  camera_motion: "Camera motion",
  highlight: "Highlight",
};

const STATUS_LABELS: Record<
  AICandidateLifecycleComparisonStatus | AICandidateLifecyclePromotionStatus | AICandidateLifecycleResolutionStatus,
  string
> = {
  pass: "pass",
  warn: "warn",
  fail: "fail",
  unavailable: "unavailable",
  none: "none",
  not_promoted: "not promoted",
  pending_confirmation: "pending confirmation",
  promoted: "promoted",
  rejected: "rejected",
  blocked: "blocked",
  resolved_not_visible: "resolved not visible",
  candidate_output: "candidate output",
};

const TERMINAL_BLOCKING_REASONS = new Set<string>([
  "missing_evidence",
  "unsafe_window",
  "unsupported_type",
  "missing_candidate_id",
  "missing_comparison",
  "failed_quality_gate",
]);

export function getRunLifecycle(
  run: Pick<RunRecord, "ai_candidate_lifecycle" | "stats"> | null | undefined,
): AICandidateLifecycleReport | null {
  if (!run) return null;
  if (isLifecycleReport(run.ai_candidate_lifecycle)) return run.ai_candidate_lifecycle;
  const statsLifecycle = run.stats?.ai_candidate_lifecycle;
  if (isLifecycleReport(statsLifecycle)) return statsLifecycle;
  return isLifecycleSummary(statsLifecycle) ? lifecycleReportFromSummary(statsLifecycle) : null;
}

export function buildLifecycleCandidateIndex(lifecycle: AICandidateLifecycleReport | null | undefined): LifecycleCandidateIndex {
  const byCandidateId = new Map<string, AICandidateLifecycleCandidate>();
  const byImprovementId = new Map<string, AICandidateLifecycleCandidate>();
  const byApprovalId = new Map<string, AICandidateLifecycleCandidate>();
  const candidates = (lifecycle?.candidates ?? []).filter(isLifecycleCandidate);

  for (const candidate of candidates) {
    addToMap(byCandidateId, candidate.candidate_id, candidate);
    for (const improvementId of candidate.improvement_ids ?? []) addToMap(byImprovementId, improvementId, candidate);
    for (const approvalId of candidate.approval_ids ?? []) addToMap(byApprovalId, approvalId, candidate);
  }

  return { byCandidateId, byImprovementId, byApprovalId, candidates };
}

export function resolveImprovementLifecycleCandidate(
  item: Pick<AIImprovementItem, "id" | "candidate_id">,
  index: LifecycleCandidateIndex,
  approvedAction: Pick<AIApprovedAction, "approval_id" | "candidate_id" | "improvement_id"> | null | undefined,
): AICandidateLifecycleCandidate | null {
  const itemCandidateId = cleanString(item.candidate_id);
  if (itemCandidateId) {
    const byCandidate = index.byCandidateId.get(itemCandidateId);
    if (byCandidate) return byCandidate;
  }

  const actionCandidateId = cleanString(approvedAction?.candidate_id);
  if (actionCandidateId) {
    const byActionCandidate = index.byCandidateId.get(actionCandidateId);
    if (byActionCandidate) return byActionCandidate;
  }

  const improvementId = cleanString(item.id);
  if (improvementId) {
    const byImprovement = index.byImprovementId.get(improvementId);
    if (byImprovement) return byImprovement;
  }

  const actionImprovementId = cleanString(approvedAction?.improvement_id);
  if (actionImprovementId) {
    const byActionImprovement = index.byImprovementId.get(actionImprovementId);
    if (byActionImprovement) return byActionImprovement;
  }

  const approvalId = cleanString(approvedAction?.approval_id);
  return approvalId ? index.byApprovalId.get(approvalId) ?? null : null;
}

export function presentLifecycleSummary(lifecycle: AICandidateLifecycleReport | null | undefined): LifecycleSummaryPresentation {
  const summary = lifecycle?.summary;
  const candidateLike: AICandidateLifecycleCandidate = {
    stage: summary?.stage ?? "review_only",
    comparison_status: summary?.comparison_status ?? "none",
    promotion_status: summary?.promotion_status ?? "not_promoted",
    resolution_status: summary?.resolution_status ?? "none",
    blocking_reasons: summary?.blocking_reasons ?? [],
    approval_ids: [],
    improvement_ids: [],
    artifact_paths: [],
  };
  return {
    ...presentLifecycleCandidate(candidateLike),
    candidateCount: summary?.candidate_count ?? lifecycle?.candidates?.length ?? 0,
    approvedActionCount: summary?.approved_action_count ?? 0,
    comparisonReportCount: summary?.comparison_report_count ?? 0,
  };
}

export function presentLifecycleCandidate(
  candidate: AICandidateLifecycleCandidate | null | undefined,
): LifecycleCandidatePresentation {
  const normalized = normalizeCandidate(candidate);
  const state = lifecycleOperatorState(normalized);
  const label = lifecycleOperatorStateLabel(state);
  const blockingLabels = (normalized.blocking_reasons ?? []).map(lifecycleBlockingReasonLabel);
  const problemLabel = lifecycleProblemTypeLabel(normalized.problem_type);
  const evidence = lifecycleEvidence(normalized, problemLabel);
  return {
    state,
    label,
    tone: OPERATOR_STATE_TONES[state],
    problemLabel,
    blockingLabels,
    evidence,
    isFinal: state === "promoted_final_output" || state === "resolved_not_visible",
    isPublishable: state === "promoted_final_output",
    hasConcreteApproval: hasCandidateConcreteApproval(normalized),
  };
}

export function lifecycleOperatorStateLabel(
  state: LifecycleOperatorState,
  labels: Partial<Record<LifecycleOperatorState, string>> = LIFECYCLE_OPERATOR_STATE_LABELS,
): string {
  return labels[state] ?? LIFECYCLE_OPERATOR_STATE_LABELS[state];
}

export function lifecycleProblemTypeLabel(problemType: string | null | undefined): string | null {
  const cleaned = cleanString(problemType);
  if (!cleaned) return null;
  return PROBLEM_TYPE_LABELS[cleaned] ?? humanizeIdentifier(cleaned);
}

export function lifecycleBlockingReasonLabel(reason: AICandidateLifecycleBlockingReason | string): string {
  return BLOCKING_REASON_LABELS[reason] ?? humanizeIdentifier(reason);
}

export function lifecycleStatusLabel(status: string | null | undefined): string | null {
  const cleaned = cleanString(status);
  if (!cleaned) return null;
  return STATUS_LABELS[cleaned as keyof typeof STATUS_LABELS] ?? humanizeIdentifier(cleaned);
}

export function hasConcreteApprovalId(
  value: Pick<AIApprovedAction, "approval_id"> | { approval_id?: unknown } | string | null | undefined,
): boolean {
  if (typeof value === "string") return cleanString(value) !== null;
  return cleanString(value?.approval_id) !== null;
}

export function inferAIImprovementProblemType(item: AIImprovementItem): string | null {
  const explicitProblemType = cleanString(item.problem_type);
  if (explicitProblemType) return explicitProblemType;

  const action = cleanString(item.recommended_action)?.toLowerCase() ?? "";
  const area = cleanString(item.area)?.toLowerCase() ?? "";
  const rootCause = cleanString(item.root_cause_module)?.toLowerCase() ?? "";
  const tags = cleanStringArray(item.failure_tags).join(" ").toLowerCase();
  const text = `${action} ${tags} ${area} ${rootCause}`;

  if (
    action === "targeted_rerun" ||
    action === "localize_ball_roi" ||
    text.includes("missing_ball") ||
    text.includes("ball_lost") ||
    text.includes("lost_gap") ||
    text.includes("missing ball") ||
    text.includes("missing")
  ) {
    return "missing_ball";
  }
  if (
    action === "adjust_follow_cam" ||
    action === "tracking_rerun_before_follow_cam" ||
    action === "human_review_camera_motion" ||
    text.includes("follow_cam") ||
    text.includes("follow cam") ||
    text.includes("camera")
  ) {
    return "follow_cam";
  }
  if (
    action === "adjust_highlight_window" ||
    action === "render_suggested_highlight" ||
    text.includes("highlight") ||
    text.includes("post_roll") ||
    text.includes("clip boundary")
  ) {
    return "highlight";
  }
  if (
    action === "tighten_noise_filter" ||
    action === "reject_noise" ||
    action === "noise_filter_adjustment" ||
    text.includes("noise") ||
    text.includes("false positive") ||
    text.includes("false_positive") ||
    text.includes("filter")
  ) {
    return "noise";
  }
  return null;
}

export function createProposedLifecycleCandidate(
  item: AIImprovementItem,
  problemType: string | null,
): AICandidateLifecycleCandidate {
  return {
    candidate_id: cleanString(item.candidate_id),
    problem_type: problemType,
    improvement_ids: [item.id].filter(Boolean),
    approval_ids: [],
    artifact_paths: [],
    stage: "proposed",
    comparison_status: "none",
    promotion_status: "not_promoted",
    resolution_status: "none",
    blocking_reasons: [],
  };
}

function lifecycleOperatorState(candidate: RequiredLifecycleCandidate): LifecycleOperatorState {
  if (candidate.promotion_status === "rejected" || candidate.promotion_status === "blocked") return "rejected_or_blocked";
  if (candidate.comparison_status === "fail") return "failed_comparison";
  if (candidate.comparison_status === "unavailable") return "rejected_or_blocked";
  if (hasTerminalBlockingReason(candidate.blocking_reasons)) return "rejected_or_blocked";
  if (candidate.resolution_status === "resolved_not_visible") return "resolved_not_visible";
  if (isPublishablePromotedCandidate(candidate)) return "promoted_final_output";
  if (candidate.comparison_status === "warn") return "warned_comparison";
  if (candidate.comparison_status === "pass") return "passed_comparison";
  if (candidate.stage === "pending_execution" || candidate.blocking_reasons.includes("pending_api_execution")) {
    return "waiting_to_execute";
  }
  if (candidate.stage === "executed" || candidate.stage === "compared" || candidate.stage === "gated" || candidate.stage === "finalized") {
    return "executed_candidate";
  }
  if (candidate.stage === "approved") return "approved";
  if (candidate.stage === "proposed") return "suggested";
  return "review_only";
}

function lifecycleEvidence(candidate: RequiredLifecycleCandidate, problemLabel: string | null): string[] {
  const evidence: string[] = [];
  if (problemLabel) evidence.push(`Problem: ${problemLabel}`);
  if (candidate.candidate_id) evidence.push(`Candidate: ${candidate.candidate_id}`);
  if (candidate.improvement_ids.length > 0) evidence.push(`Improvement: ${candidate.improvement_ids.join(", ")}`);
  if (candidate.approval_ids.length > 0) evidence.push(`Approval: ${candidate.approval_ids.join(", ")}`);
  if (candidate.comparison_status !== "none") {
    evidence.push(`Comparison: ${STATUS_LABELS[candidate.comparison_status]}`);
  }
  if (candidate.promotion_status !== "not_promoted") {
    evidence.push(`Promotion: ${STATUS_LABELS[candidate.promotion_status]}`);
  }
  if (candidate.resolution_status !== "none") {
    evidence.push(`Resolution: ${STATUS_LABELS[candidate.resolution_status]}`);
  }
  if (candidate.artifact_paths.length > 0) {
    evidence.push(`${candidate.artifact_paths.length} candidate artifact${candidate.artifact_paths.length === 1 ? "" : "s"}`);
  }
  return evidence;
}

function hasCandidateConcreteApproval(candidate: RequiredLifecycleCandidate): boolean {
  return candidate.approval_ids.some((approvalId) => hasConcreteApprovalId(approvalId));
}

function isPublishablePromotedCandidate(candidate: RequiredLifecycleCandidate): boolean {
  return (
    candidate.promotion_status === "promoted" &&
    (candidate.comparison_status === "pass" || candidate.comparison_status === "warn") &&
    !hasTerminalBlockingReason(candidate.blocking_reasons)
  );
}

function hasTerminalBlockingReason(reasons: string[]): boolean {
  return reasons.some((reason) => TERMINAL_BLOCKING_REASONS.has(reason));
}

type RequiredLifecycleCandidate = Required<
  Pick<
    AICandidateLifecycleCandidate,
    | "candidate_id"
    | "problem_type"
    | "improvement_ids"
    | "approval_ids"
    | "artifact_paths"
    | "stage"
    | "comparison_status"
    | "promotion_status"
    | "resolution_status"
    | "blocking_reasons"
  >
>;

function normalizeCandidate(candidate: AICandidateLifecycleCandidate | null | undefined): RequiredLifecycleCandidate {
  return {
    candidate_id: cleanString(candidate?.candidate_id),
    problem_type: cleanString(candidate?.problem_type),
    improvement_ids: cleanStringArray(candidate?.improvement_ids),
    approval_ids: cleanStringArray(candidate?.approval_ids),
    artifact_paths: cleanStringArray(candidate?.artifact_paths),
    stage: validStage(candidate?.stage) ? candidate.stage : "review_only",
    comparison_status: validComparisonStatus(candidate?.comparison_status) ? candidate.comparison_status : "none",
    promotion_status: validPromotionStatus(candidate?.promotion_status) ? candidate.promotion_status : "not_promoted",
    resolution_status: validResolutionStatus(candidate?.resolution_status) ? candidate.resolution_status : "none",
    blocking_reasons: cleanStringArray(candidate?.blocking_reasons),
  };
}

function isLifecycleReport(value: unknown): value is AICandidateLifecycleReport {
  return isRecord(value) && (Array.isArray(value.candidates) || isRecord(value.summary));
}

function isLifecycleSummary(value: unknown): value is AICandidateLifecycleSummary {
  return (
    isRecord(value) &&
    !Array.isArray(value.candidates) &&
    (validStage(value.stage) ||
      validComparisonStatus(value.comparison_status) ||
      validPromotionStatus(value.promotion_status) ||
      validResolutionStatus(value.resolution_status) ||
      Array.isArray(value.blocking_reasons))
  );
}

function lifecycleReportFromSummary(summary: AICandidateLifecycleSummary): AICandidateLifecycleReport {
  return {
    summary,
    candidates: [],
  };
}

function isLifecycleCandidate(value: unknown): value is AICandidateLifecycleCandidate {
  return isRecord(value);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

function addToMap(
  map: Map<string, AICandidateLifecycleCandidate>,
  key: string | null | undefined,
  candidate: AICandidateLifecycleCandidate,
): void {
  const cleaned = cleanString(key);
  if (cleaned && !map.has(cleaned)) map.set(cleaned, candidate);
}

function cleanString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function cleanStringArray(value: unknown): string[] {
  return Array.isArray(value)
    ? value.map(cleanString).filter((item): item is string => item !== null)
    : [];
}

function humanizeIdentifier(value: string): string {
  const spaced = value.replace(/[_-]+/g, " ").trim();
  if (!spaced) return value;
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

function validStage(value: unknown): value is AICandidateLifecycleStage {
  return (
    value === "review_only" ||
    value === "proposed" ||
    value === "approved" ||
    value === "pending_execution" ||
    value === "executed" ||
    value === "compared" ||
    value === "gated" ||
    value === "finalized"
  );
}

function validComparisonStatus(value: unknown): value is AICandidateLifecycleComparisonStatus {
  return value === "pass" || value === "warn" || value === "fail" || value === "unavailable" || value === "none";
}

function validPromotionStatus(value: unknown): value is AICandidateLifecyclePromotionStatus {
  return (
    value === "not_promoted" ||
    value === "pending_confirmation" ||
    value === "promoted" ||
    value === "rejected" ||
    value === "blocked"
  );
}

function validResolutionStatus(value: unknown): value is AICandidateLifecycleResolutionStatus {
  return value === "none" || value === "resolved_not_visible" || value === "candidate_output";
}
