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
  stats: Record<string, unknown>;
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

export interface CreateRunRequest {
  config_name: string;
  input_video?: string | null;
  parent_run_id?: string | null;
  output_dir_name?: string | null;
  config_patch?: Record<string, unknown>;
  enable_postprocess?: boolean | null;
  enable_follow_cam?: boolean | null;
  start_frame?: number | null;
  max_frames?: number | null;
  notes?: string | null;
}

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

export interface HighlightRenderRequest {
  candidate_id?: string | null;
  start_frame?: number | null;
  end_frame?: number | null;
  pre_roll_frames?: number;
  post_roll_frames?: number;
  output_dir_name?: string | null;
  output_video_name?: string | null;
  notes?: string | null;
}
