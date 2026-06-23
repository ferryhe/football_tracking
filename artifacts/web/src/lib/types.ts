export type RunStatus = "queued" | "running" | "completed" | "failed" | "cancelled";

export interface HealthResponse {
  status: string;
  active_run_id: string | null;
  config_count: number;
  run_count: number;
}

export interface ConfigListItem {
  name: string;
  path: string;
  created_at: string | null;
  input_video: string | null;
  output_dir: string | null;
  detector_model_path: string | null;
  postprocess_enabled: boolean;
  follow_cam_enabled: boolean;
  exists: Record<string, boolean>;
}

export interface ConfigDetail {
  name: string;
  path: string;
  text: string;
  raw: Record<string, unknown>;
  resolved: Record<string, unknown>;
  summary: ConfigListItem;
}

export interface UpdateConfigRequest {
  content: string;
}

export interface DeriveConfigRequest {
  base_config_name: string;
  output_name: string;
  patch?: Record<string, unknown>;
}

export interface InputVideoItem {
  name: string;
  path: string;
  size_bytes: number;
  modified_at: string;
}

export interface InputCatalogResponse {
  root_dir: string;
  videos: InputVideoItem[];
}

export interface ArtifactSummary {
  name: string;
  path: string;
  kind: string;
  exists: boolean;
  size_bytes: number | null;
  content_type: string | null;
}

export interface RunProgress {
  stage: string;
  current_frame: number | null;
  total_frames: number | null;
  percent: number;
  eta_seconds: number | null;
  elapsed_seconds: number | null;
  updated_at: string | null;
}

export type AICandidateLifecycleStage =
  | "review_only"
  | "proposed"
  | "approved"
  | "pending_execution"
  | "executed"
  | "compared"
  | "gated"
  | "finalized";

export type AICandidateLifecycleComparisonStatus = "pass" | "warn" | "fail" | "unavailable" | "none";

export type AICandidateLifecyclePromotionStatus =
  | "not_promoted"
  | "pending_confirmation"
  | "promoted"
  | "rejected"
  | "blocked";

export type AICandidateLifecycleResolutionStatus = "none" | "resolved_not_visible" | "candidate_output";

export type AICandidateLifecycleBlockingReason =
  | "missing_evidence"
  | "unsafe_window"
  | "unsupported_type"
  | "missing_candidate_id"
  | "missing_comparison"
  | "failed_quality_gate"
  | "pending_api_execution"
  | "pending_human_confirmation"
  | (string & {});

export interface AICandidateLifecycleCandidate {
  candidate_id?: string | null;
  problem_type?: string | null;
  improvement_ids?: string[];
  approval_ids?: string[];
  artifact_paths?: string[];
  stage?: AICandidateLifecycleStage;
  comparison_status?: AICandidateLifecycleComparisonStatus;
  promotion_status?: AICandidateLifecyclePromotionStatus;
  resolution_status?: AICandidateLifecycleResolutionStatus;
  blocking_reasons?: AICandidateLifecycleBlockingReason[];
}

export interface AICandidateLifecycleSummary {
  stage?: AICandidateLifecycleStage;
  comparison_status?: AICandidateLifecycleComparisonStatus;
  promotion_status?: AICandidateLifecyclePromotionStatus;
  resolution_status?: AICandidateLifecycleResolutionStatus;
  blocking_reasons?: AICandidateLifecycleBlockingReason[];
  candidate_count?: number;
  approved_action_count?: number;
  comparison_report_count?: number;
}

export interface AICandidateLifecycleReport {
  schema_version?: string;
  generated_at?: string | null;
  output_dir?: string | null;
  summary?: AICandidateLifecycleSummary;
  candidates?: AICandidateLifecycleCandidate[];
  artifacts?: Record<string, string>;
}

export interface RunRecord {
  run_id: string;
  source: string;
  status: RunStatus;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  config_name: string | null;
  config_path: string | null;
  input_video: string | null;
  parent_run_id: string | null;
  output_dir: string;
  modules_enabled: Record<string, boolean>;
  artifacts: ArtifactSummary[];
  stats: Record<string, unknown> & {
    ai_candidate_lifecycle?: AICandidateLifecycleSummary | AICandidateLifecycleReport | null;
  };
  ai_candidate_lifecycle?: AICandidateLifecycleReport | null;
  progress?: RunProgress | null;
  notes: string | null;
  error: string | null;
}

export interface AssetGroup {
  group_id: string;
  title: string;
  input_video: InputVideoItem | null;
  last_activity_at: string | null;
  run_count: number;
  config_count: number;
  output_count: number;
  runs: RunRecord[];
  configs: ConfigListItem[];
  outputs: RunRecord[];
  is_unbound: boolean;
}

export interface FieldPreviewResponse {
  input_video: string;
  preview_data_url: string;
  frame_width: number;
  frame_height: number;
  frame_index: number;
  frame_time_seconds: number;
  sample_index: number;
  sample_count: number;
}

export interface FieldSuggestionResponse {
  input_video: string;
  preview_data_url: string;
  preview_bounds: [number, number, number, number];
  frame_width: number;
  frame_height: number;
  frame_index: number;
  frame_time_seconds: number;
  sample_index: number;
  sample_count: number;
  field_polygon: [number, number][];
  expanded_polygon: [number, number][];
  field_roi: [number, number, number, number];
  expanded_roi: [number, number, number, number];
  confidence: "config" | "detected" | "fallback";
  source: string;
  field_coverage: number;
  config_patch: Record<string, unknown>;
}

export interface AISuggestion {
  title: string;
  diagnosis: string;
  recommendation: string;
  expected_tradeoff: string;
  patch: Record<string, unknown>;
  patch_preview: string[];
  evidence: string[];
  output_name_suggestion: string | null;
}

export interface AIExplainResponse {
  summary: string;
  evidence: string[];
}

export interface AIFrameWindow {
  start_frame: number;
  end_frame: number;
}

export interface AILocalSearchRoi {
  coordinate_space: "image";
  frame: number;
  x: number;
  y: number;
  width: number;
  height: number;
  confidence: number;
}

export interface AIImproveRequest {
  run_id: string;
  objective?: string;
  model?: string;
  dry_run?: boolean;
  max_items?: number;
  language?: string;
}

export interface AIImprovementItem extends Record<string, unknown> {
  id: string;
  priority?: string;
  area: string;
  failure_tags?: string[];
  root_cause_module?: string;
  diagnosis?: string;
  recommended_action: AIRecommendedAction;
  config_patch?: Record<string, unknown>;
  evidence?: unknown[];
  confidence?: number;
  start_frame?: number | null;
  end_frame?: number | null;
  rerun_scope?: AIFrameWindow | null;
  likely_ball_region?: Record<string, unknown> | null;
  local_search_roi?: AILocalSearchRoi | null;
  false_positive_class?: string | null;
  candidate_id?: string | null;
  suggested_window?: AIFrameWindow | null;
  clip_action?: AIClipAction | null;
  camera_motion_event_id?: string | null;
  camera_motion_severity?: string | null;
  evidence_payload?: Record<string, unknown>;
  follow_cam_rerender_plan?: Record<string, unknown> | null;
  provenance?: Record<string, unknown>;
}

export interface AIHighlightAdjustment extends Record<string, unknown> {
  candidate_id: string;
  current_window: AIFrameWindow;
  suggested_window: AIFrameWindow;
  core_window?: AIFrameWindow | null;
  render_window?: AIFrameWindow | null;
  buffer_policy?: Record<string, unknown> | null;
  boundary_warnings?: string[];
  warnings?: string[];
  reason: string;
  confidence?: number;
  clip_action?: AIClipAction | null;
}

export interface AIImproveResponse {
  summary: Record<string, unknown>;
  artifact_name: string;
  artifact_path: string;
  improvements: AIImprovementItem[];
  highlight_adjustments: AIHighlightAdjustment[];
}

export interface AIImproveReportArtifact extends Record<string, unknown> {
  summary: Record<string, unknown>;
  improvements: AIImprovementItem[];
  highlight_adjustments?: AIHighlightAdjustment[];
}

export type AIRecommendedAction =
  | "targeted_rerun"
  | "tighten_noise_filter"
  | "loosen_ball_recovery"
  | "split_packet"
  | "manual_review"
  | "reject_noise"
  | "adjust_highlight_window"
  | "adjust_follow_cam"
  | "tracking_rerun_before_follow_cam"
  | "render_suggested_highlight"
  | "localize_ball_roi"
  | "noise_filter_adjustment"
  | "human_review_camera_motion";

export type AIClipAction = "extend_tail" | "trim_head" | "trim_tail" | "split" | "keep";

export interface AIImproveApprovalRequest {
  improvement_ids: string[];
  approved_by: string;
  rerun_scope_overrides?: Record<string, AIFrameWindow>;
  local_search_roi_overrides?: Record<string, AILocalSearchRoi>;
  config_patch_overrides?: Record<string, Record<string, unknown>>;
  suggested_window_overrides?: Record<string, AIFrameWindow>;
  clip_action_overrides?: Record<string, AIClipAction>;
  follow_cam_rerender_plan_overrides?: Record<string, Record<string, unknown>>;
}

export interface AIApprovedAction extends Record<string, unknown> {
  approval_id: string;
  improvement_id: string;
  approved_action: AIRecommendedAction;
  approval_source?: string;
  approved_at?: string;
  approved_by?: string;
  provenance?: Record<string, unknown>;
  rerun_scope?: AIFrameWindow | null;
  local_search_roi?: AILocalSearchRoi | null;
  config_patch?: Record<string, unknown>;
  suggested_window?: AIFrameWindow | null;
  clip_action?: AIClipAction | null;
  follow_cam_rerender_plan?: Record<string, unknown> | null;
  source_packet_id?: string | null;
  visual_review_id?: string | null;
  candidate_id?: string | null;
  camera_motion_event_id?: string | null;
  camera_motion_severity?: string | null;
  start_frame?: number | null;
  end_frame?: number | null;
}

export interface AIApprovalArtifactSummary {
  name: string | null;
  path: string | null;
  exists: boolean;
}

export interface AIImproveApprovalResponse {
  schema_version: string;
  generated_at: string;
  run_id: string;
  source_report: string;
  approved_by: string;
  artifact_name: string;
  artifact_path: string;
  config_patch_artifact_name?: string | null;
  config_patch_artifact_path?: string | null;
  follow_cam_rerender_plan_artifact_name?: string | null;
  follow_cam_rerender_plan_artifact_path?: string | null;
  approved_actions: AIApprovedAction[];
  summary: Record<string, unknown> & {
    artifacts?: Record<string, AIApprovalArtifactSummary>;
  };
  warnings?: string[];
}

export interface AIApprovedActionsArtifact extends Record<string, unknown> {
  schema_version: string;
  generated_at: string;
  run_id: string;
  source_report: string;
  approved_by: string;
  approved_actions: AIApprovedAction[];
  warnings?: string[];
}

interface CreateRunCommon {
  input_video?: string | null;
  output_dir_name?: string | null;
  config_patch?: Record<string, unknown>;
  enable_postprocess?: boolean | null;
  enable_follow_cam?: boolean | null;
  start_frame?: number | null;
  max_frames?: number | null;
  notes?: string | null;
}

type StandardCreateRunRequest = CreateRunCommon & {
  config_name: string;
  parent_run_id?: string | null;
  approved_action_ids?: never;
  approved_actions_artifact_name?: never;
};

type ApprovedChildRunByActionIdsRequest = CreateRunCommon & {
  config_name?: string | null;
  parent_run_id: string;
  approved_action_ids: string[];
  approved_actions_artifact_name?: string | null;
};

type ApprovedChildRunByArtifactRequest = CreateRunCommon & {
  config_name?: string | null;
  parent_run_id: string;
  approved_action_ids?: string[];
  approved_actions_artifact_name: string;
};

export type CreateRunRequest =
  | StandardCreateRunRequest
  | ApprovedChildRunByActionIdsRequest
  | ApprovedChildRunByArtifactRequest;

export interface FollowCamRenderRequest {
  output_dir_name?: string | null;
  output_video_name?: string | null;
  prefer_cleaned_track?: boolean;
  draw_ball_marker?: boolean;
  draw_frame_text?: boolean;
  target_width?: number;
  target_height?: number;
  notes?: string | null;
}

export interface EventCandidateWindow {
  start_frame: number;
  end_frame: number;
}

export interface EventCandidate {
  id: string;
  type: string;
  label?: "candidate";
  start_frame: number;
  end_frame: number;
  frame_count: number;
  score: number;
  reason: string;
  render_window: EventCandidateWindow;
  evidence?: Record<string, unknown>;
}

export interface EventCandidateReport {
  schema_version: string;
  source: {
    name: string;
    path: string | null;
    row_count: number;
  };
  summary: {
    frame_count: number;
    detected_frame_count: number;
    candidate_count: number;
    counts_by_type?: Record<string, number>;
    min_frame: number | null;
    max_frame: number | null;
  };
  candidates: EventCandidate[];
}

interface HighlightRenderBase {
  pre_roll_frames?: number;
  post_roll_frames?: number;
  output_dir_name?: string | null;
  output_video_name?: string | null;
  notes?: string | null;
}

type CandidateHighlightRenderRequest = HighlightRenderBase & {
  candidate_id: string;
  approved_action_id?: never;
  start_frame?: never;
  end_frame?: never;
};

type ApprovedHighlightRenderRequest = HighlightRenderBase & {
  approved_action_id: string;
  candidate_id?: never;
  start_frame?: never;
  end_frame?: never;
};

type FrameWindowHighlightRenderRequest = HighlightRenderBase & {
  start_frame: number;
  end_frame: number;
  candidate_id?: never;
  approved_action_id?: never;
};

export type HighlightRenderRequest =
  | CandidateHighlightRenderRequest
  | ApprovedHighlightRenderRequest
  | FrameWindowHighlightRenderRequest;
