from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

RunStatus = Literal["queued", "running", "completed", "failed", "cancelled"]
AIResponseLanguage = Literal["en", "zh"]
QualityStatus = Literal["pass", "warn", "fail"]
AIReviewPriority = Literal["none", "low", "medium", "high"]
Point2DPayload = tuple[float, float]
QuadPointsPayload = tuple[Point2DPayload, Point2DPayload, Point2DPayload, Point2DPayload]
MatrixRowPayload = tuple[float, float, float]
Matrix3x3Payload = tuple[MatrixRowPayload, MatrixRowPayload, MatrixRowPayload]


class HealthResponse(BaseModel):
    status: str
    active_run_id: str | None = None
    config_count: int
    run_count: int


class ConfigListItem(BaseModel):
    name: str
    path: str
    created_at: str | None = None
    input_video: str | None = None
    output_dir: str | None = None
    detector_model_path: str | None = None
    postprocess_enabled: bool
    follow_cam_enabled: bool
    exists: dict[str, bool]


class InputVideoItem(BaseModel):
    name: str
    path: str
    size_bytes: int
    modified_at: str


class InputCatalogResponse(BaseModel):
    root_dir: str
    videos: list[InputVideoItem] = Field(default_factory=list)


class DeleteResourceResponse(BaseModel):
    name: str
    path: str
    deleted: bool = True


class ApiErrorResponse(BaseModel):
    detail: str


class FieldPreviewRequest(BaseModel):
    input_video: str
    sample_index: int | None = None


class FieldPreviewResponse(BaseModel):
    input_video: str
    preview_data_url: str
    frame_width: int
    frame_height: int
    frame_index: int
    frame_time_seconds: float
    sample_index: int
    sample_count: int


class FieldSuggestionRequest(BaseModel):
    input_video: str
    config_name: str | None = None
    frame_index: int | None = None


class InputQualityRequest(BaseModel):
    input_video: str
    config_name: str | None = None


class InputQualityCheck(BaseModel):
    key: str
    label: str
    score: float = Field(ge=0.0, le=1.0)
    status: QualityStatus
    value: Any
    unit: str
    guidance: str


class InputQualityResponse(BaseModel):
    input_video: str
    frame_width: int
    frame_height: int
    sample_count: int
    overall_score: float = Field(ge=0.0, le=1.0)
    overall_status: QualityStatus
    checks: list[InputQualityCheck]
    recommendations: list[str]


class PitchDimensionsPayload(BaseModel):
    length_m: float
    width_m: float


class FieldCalibrationPayload(BaseModel):
    image_points: QuadPointsPayload
    pitch_points: QuadPointsPayload
    image_to_pitch_matrix: Matrix3x3Payload
    pitch_to_image_matrix: Matrix3x3Payload
    pitch_dimensions: PitchDimensionsPayload
    confidence: Literal["config", "estimated", "low"]
    source: str


class FieldSuggestionResponse(BaseModel):
    input_video: str
    preview_data_url: str
    preview_bounds: tuple[int, int, int, int]
    frame_width: int
    frame_height: int
    frame_index: int
    frame_time_seconds: float
    sample_index: int
    sample_count: int
    field_polygon: list[tuple[int, int]] = Field(default_factory=list)
    expanded_polygon: list[tuple[int, int]] = Field(default_factory=list)
    field_roi: tuple[int, int, int, int]
    expanded_roi: tuple[int, int, int, int]
    confidence: Literal["config", "detected", "fallback"]
    source: str
    field_coverage: float
    calibration: FieldCalibrationPayload | None = None
    config_patch: dict[str, Any] = Field(default_factory=dict)


class ConfigDetail(BaseModel):
    name: str
    path: str
    text: str
    raw: dict[str, Any]
    resolved: dict[str, Any]
    summary: ConfigListItem


class UpdateConfigRequest(BaseModel):
    content: str


class DeriveConfigRequest(BaseModel):
    base_config_name: str
    output_name: str
    patch: dict[str, Any] = Field(default_factory=dict)


class ArtifactSummary(BaseModel):
    name: str
    path: str
    kind: str
    exists: bool
    size_bytes: int | None = None
    content_type: str | None = None


class BallAuditSummary(BaseModel):
    frame_count: int
    source_count: int
    tracklet_count: int
    suspicious_tracklet_count: int
    review_event_count: int
    lost_gap_count: int
    max_step_px: float | None = None


class BallAuditSource(BaseModel):
    name: str
    path: str
    row_count: int
    tracklet_count: int


class BallAuditPoint(BaseModel):
    x: float
    y: float


class BallAuditTracklet(BaseModel):
    id: str
    source: str
    start_frame: int | None = None
    end_frame: int | None = None
    length: int
    status_counts: dict[str, int]
    mean_confidence: float | None = None
    start_point: BallAuditPoint
    end_point: BallAuditPoint
    max_step_px: float | None = None
    flags: list[str] = Field(default_factory=list)
    suspicion_score: float


class BallAuditReviewEvent(BaseModel):
    source: str
    type: str
    severity: Literal["info", "warn", "fail"]
    start_frame: int | None = None
    end_frame: int | None = None
    frame_count: int
    reason: str
    evidence: dict[str, Any] = Field(default_factory=dict)


class BallAuditReport(BaseModel):
    schema_version: str
    generated_at: str
    summary: BallAuditSummary
    sources: list[BallAuditSource] = Field(default_factory=list)
    tracklets: list[BallAuditTracklet] = Field(default_factory=list)
    review_events: list[BallAuditReviewEvent] = Field(default_factory=list)


class AIReviewWindow(BaseModel):
    start_frame: int
    end_frame: int
    reason: str


class AIReviewDecision(BaseModel):
    needs_ai_review: bool
    priority: AIReviewPriority
    reason: str
    trigger_count: int
    recommended_review_windows: list[AIReviewWindow] = Field(default_factory=list)


class AIReviewTrigger(BaseModel):
    id: str
    type: str
    priority: AIReviewPriority
    source: str
    start_frame: int | None = None
    end_frame: int | None = None
    frame_count: int
    reason: str
    evidence: dict[str, Any] = Field(default_factory=dict)


class AIReviewSummary(BaseModel):
    counts_by_type: dict[str, int] = Field(default_factory=dict)
    counts_by_priority: dict[str, int] = Field(default_factory=dict)
    max_trigger_priority: AIReviewPriority


class AIReviewTriggerReport(BaseModel):
    schema_version: str
    generated_at: str
    decision: AIReviewDecision
    triggers: list[AIReviewTrigger] = Field(default_factory=list)
    summary: AIReviewSummary


class RunProgress(BaseModel):
    stage: str
    current_frame: int | None = None
    total_frames: int | None = None
    percent: float = 0.0
    eta_seconds: float | None = None
    elapsed_seconds: float | None = None
    updated_at: str | None = None


class RunRecord(BaseModel):
    run_id: str
    source: str
    status: RunStatus
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None
    config_name: str | None = None
    config_path: str | None = None
    input_video: str | None = None
    parent_run_id: str | None = None
    output_dir: str
    modules_enabled: dict[str, bool] = Field(default_factory=dict)
    artifacts: list[ArtifactSummary] = Field(default_factory=list)
    stats: dict[str, Any] = Field(default_factory=dict)
    progress: RunProgress | None = None
    notes: str | None = None
    error: str | None = None


class AssetGroup(BaseModel):
    group_id: str
    title: str
    input_video: InputVideoItem | None = None
    last_activity_at: str | None = None
    run_count: int = 0
    config_count: int = 0
    output_count: int = 0
    runs: list[RunRecord] = Field(default_factory=list)
    configs: list[ConfigListItem] = Field(default_factory=list)
    outputs: list[RunRecord] = Field(default_factory=list)
    is_unbound: bool = False


class CreateRunRequest(BaseModel):
    config_name: str
    input_video: str | None = None
    parent_run_id: str | None = None
    output_dir_name: str | None = None
    config_patch: dict[str, Any] = Field(default_factory=dict)
    enable_postprocess: bool | None = None
    enable_follow_cam: bool | None = None
    start_frame: int | None = None
    max_frames: int | None = None
    notes: str | None = None


class FollowCamRenderRequest(BaseModel):
    output_dir_name: str | None = None
    output_video_name: str | None = None
    prefer_cleaned_track: bool = True
    draw_ball_marker: bool = False
    draw_frame_text: bool = False
    target_width: int = 1920
    target_height: int = 1080
    notes: str | None = None


class CameraPathResponse(BaseModel):
    columns: list[str]
    offset: int
    limit: int
    total_rows: int
    rows: list[dict[str, Any]]


class AIExplainRequest(BaseModel):
    run_id: str | None = None
    config_name: str | None = None
    focus: str | None = None
    language: AIResponseLanguage = "en"


class AIRecommendRequest(BaseModel):
    run_id: str
    objective: str | None = None
    language: AIResponseLanguage = "en"


class AIConfigDiffRequest(BaseModel):
    base_config_name: str
    patch: dict[str, Any] = Field(default_factory=dict)
    output_name: str | None = None


class AISuggestion(BaseModel):
    title: str
    diagnosis: str
    recommendation: str
    expected_tradeoff: str
    patch: dict[str, Any] = Field(default_factory=dict)
    patch_preview: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    output_name_suggestion: str | None = None


class AIExplainResponse(BaseModel):
    summary: str
    evidence: list[str] = Field(default_factory=list)


class AIConfigDiffResponse(BaseModel):
    base_config_name: str
    output_name: str
    patch: dict[str, Any] = Field(default_factory=dict)
    patch_preview: list[str] = Field(default_factory=list)
