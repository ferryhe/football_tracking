export type RunStatus = "queued" | "running" | "completed" | "failed";

export interface HealthResponse {
  status: string;
  active_run_id: string | null;
  config_count: number;
  run_count: number;
}

export interface ConfigListItem {
  name: string;
  path: string;
  input_video: string | null;
  output_dir: string | null;
  detector_model_path: string | null;
  postprocess_enabled: boolean;
  follow_cam_enabled: boolean;
  exists: Record<string, boolean>;
}

export interface InputVideoItem {
  name: string;
  path: string;
  size_bytes: number;
  modified_at: string;
}

export interface InputCatalog {
  root_dir: string;
  videos: InputVideoItem[];
}

export type FieldPoint = [number, number];

export interface FieldPreview {
  input_video: string;
  preview_data_url: string;
  frame_width: number;
  frame_height: number;
  frame_index: number;
  frame_time_seconds: number;
  sample_index: number;
  sample_count: number;
}

export interface FieldSuggestion extends FieldPreview {
  input_video: string;
  preview_bounds: [number, number, number, number];
  field_polygon: FieldPoint[];
  expanded_polygon: FieldPoint[];
  field_roi: [number, number, number, number];
  expanded_roi: [number, number, number, number];
  confidence: "config" | "detected" | "fallback";
  source: string;
  field_coverage: number;
  config_patch: Record<string, unknown>;
  accepted?: boolean;
}

export interface ConfigDetail {
  name: string;
  path: string;
  raw: Record<string, unknown>;
  resolved: Record<string, unknown>;
  summary: ConfigListItem;
}

export interface ArtifactSummary {
  name: string;
  path: string;
  kind: string;
  exists: boolean;
  size_bytes?: number | null;
  content_type?: string | null;
}

export interface RunRecord {
  run_id: string;
  source: string;
  status: RunStatus;
  created_at: string;
  started_at?: string | null;
  completed_at?: string | null;
  config_name?: string | null;
  config_path?: string | null;
  input_video?: string | null;
  output_dir: string;
  modules_enabled: Record<string, boolean>;
  artifacts: ArtifactSummary[];
  stats: Record<string, unknown>;
  notes?: string | null;
  error?: string | null;
}

export interface CameraPathResponse {
  columns: string[];
  offset: number;
  limit: number;
  total_rows: number;
  rows: Record<string, string>[];
}

export interface AssistantSuggestion {
  title: string;
  diagnosis: string;
  recommendation: string;
  expected_tradeoff?: string;
  patch?: Record<string, unknown>;
  patchPreview: string[];
  evidence: string[];
  outputNameSuggestion?: string | null;
}

export interface AIExplainResponse {
  summary: string;
  evidence: string[];
}

export interface AIConfigDiffResponse {
  base_config_name: string;
  output_name: string;
  patch: Record<string, unknown>;
  patch_preview: string[];
}
