from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from football_tracking.ai_contracts import AIClipAction, AIFailureTag, AIRecommendedAction, AIRootCauseModule

RunStatus = Literal["queued", "running", "completed", "failed", "cancelled"]
AIResponseLanguage = Literal["en", "zh"]
QualityStatus = Literal["pass", "warn", "fail"]
AIReviewPriority = Literal["none", "low", "medium", "high"]
CandidateLifecycleStage = Literal[
    "review_only",
    "proposed",
    "approved",
    "pending_execution",
    "executed",
    "compared",
    "gated",
    "finalized",
]
CandidateComparisonStatus = Literal["pass", "warn", "fail", "unavailable", "none"]
CandidatePromotionStatus = Literal["not_promoted", "pending_confirmation", "promoted", "rejected", "blocked"]
CandidateResolutionStatus = Literal["none", "resolved_not_visible", "candidate_output"]
AIArtifactAvailabilityStatus = Literal["available", "unavailable", "error"]
CandidateBlockingReason = Literal[
    "missing_evidence",
    "unsafe_window",
    "unsupported_type",
    "missing_candidate_id",
    "missing_comparison",
    "failed_quality_gate",
    "pending_api_execution",
    "pending_human_confirmation",
]
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


class PlayerTracksSource(BaseModel):
    path: str
    status: Literal["missing", "empty", "loaded"]
    detection_count: int
    malformed_line_count: int


class PlayerTracksSummary(BaseModel):
    frame_count: int
    detection_count: int
    track_count: int
    active_track_count: int
    mean_track_length: float
    longest_track_length: int
    teams: dict[str, int] = Field(default_factory=dict)


class PlayerTrackPoint(BaseModel):
    x: float
    y: float


class PlayerTrackSample(BaseModel):
    frame: int
    bbox: tuple[float, float, float, float]
    foot_point: PlayerTrackPoint
    confidence: float
    label: str
    team: str


class PlayerTrack(BaseModel):
    id: str
    start_frame: int
    end_frame: int
    length: int
    team: str
    mean_confidence: float
    first_foot_point: PlayerTrackPoint
    last_foot_point: PlayerTrackPoint
    max_step_px: float | None = None
    samples: list[PlayerTrackSample] = Field(default_factory=list)


class PlayerTracksReport(BaseModel):
    schema_version: str
    generated_at: str
    source: PlayerTracksSource
    summary: PlayerTracksSummary
    tracks: list[PlayerTrack] = Field(default_factory=list)


class EventCandidateSource(BaseModel):
    name: str
    path: str | None = None
    row_count: int


class EventCandidateSummary(BaseModel):
    frame_count: int
    detected_frame_count: int
    candidate_count: int
    counts_by_type: dict[str, int] = Field(default_factory=dict)
    min_frame: int | None = None
    max_frame: int | None = None


class EventCandidateWindow(BaseModel):
    start_frame: int = Field(ge=0)
    end_frame: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_order(self) -> "EventCandidateWindow":
        if self.end_frame < self.start_frame:
            raise ValueError("EventCandidateWindow requires end_frame to be greater than or equal to start_frame.")
        return self


class EventCandidateBufferPolicy(BaseModel):
    fps: float = Field(gt=0)
    fps_source: str
    pre_buffer_seconds: float = Field(ge=0)
    post_buffer_seconds: float = Field(ge=0)
    pre_buffer_frames: int = Field(ge=0)
    post_buffer_frames: int = Field(ge=0)
    min_post_event_frames: int = Field(ge=0)
    min_tail_frames: int = Field(ge=0)


class EventCandidate(BaseModel):
    id: str
    type: str
    label: Literal["candidate"] = "candidate"
    start_frame: int
    end_frame: int
    frame_count: int
    score: float
    reason: str
    core_window: EventCandidateWindow
    render_window: EventCandidateWindow
    buffer_policy: EventCandidateBufferPolicy
    evidence: dict[str, Any] = Field(default_factory=dict)


class EventCandidateReport(BaseModel):
    schema_version: str
    source: EventCandidateSource
    summary: EventCandidateSummary
    candidates: list[EventCandidate] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class AICandidateLifecycleSummary(BaseModel):
    stage: CandidateLifecycleStage = "review_only"
    comparison_status: CandidateComparisonStatus = "none"
    promotion_status: CandidatePromotionStatus = "not_promoted"
    resolution_status: CandidateResolutionStatus = "none"
    blocking_reasons: list[CandidateBlockingReason] = Field(default_factory=list)
    candidate_count: int = 0
    approved_action_count: int = 0
    comparison_report_count: int = 0


class AICandidateLifecycleCandidate(BaseModel):
    candidate_id: str | None = None
    problem_type: str | None = None
    improvement_ids: list[str] = Field(default_factory=list)
    approval_ids: list[str] = Field(default_factory=list)
    artifact_paths: list[str] = Field(default_factory=list)
    stage: CandidateLifecycleStage = "review_only"
    comparison_status: CandidateComparisonStatus = "none"
    promotion_status: CandidatePromotionStatus = "not_promoted"
    resolution_status: CandidateResolutionStatus = "none"
    blocking_reasons: list[CandidateBlockingReason] = Field(default_factory=list)


class AICandidateLifecycleReport(BaseModel):
    schema_version: str = "1.0"
    generated_at: str | None = None
    output_dir: str | None = None
    summary: AICandidateLifecycleSummary = Field(default_factory=AICandidateLifecycleSummary)
    candidates: list[AICandidateLifecycleCandidate] = Field(default_factory=list)
    artifacts: dict[str, str] = Field(default_factory=dict)


class AIImprovementArtifactStatus(BaseModel):
    name: str
    category: str
    status: AIArtifactAvailabilityStatus
    summary: str
    path: str | None = None
    exists: bool = False
    size_bytes: int | None = None
    content_type: str | None = None
    problem_type: str | None = None
    candidate_id: str | None = None


class AIImprovementArtifactReference(BaseModel):
    name: str
    status: AIArtifactAvailabilityStatus
    path: str | None = None
    category: str | None = None


class AIImprovementManifestStatus(BaseModel):
    status: str
    artifact_status: AIArtifactAvailabilityStatus = "unavailable"
    summary: str | None = None
    path: str | None = None


class AIImprovementStatusItem(BaseModel):
    id: str | None = None
    improvement_id: str | None = None
    candidate_id: str | None = None
    approval_ids: list[str] = Field(default_factory=list)
    frame_window: AIFrameWindow | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    confidence: float | None = None
    false_positive_class: str | None = None
    recommended_action: str | None = None
    approved_action: str | None = None
    approval_status: str = "none"
    consumed_approval_ids: list[str] = Field(default_factory=list)
    comparison_status: CandidateComparisonStatus = "none"
    promotion_status: CandidatePromotionStatus = "not_promoted"
    artifact_references: list[AIImprovementArtifactReference] = Field(default_factory=list)


class AIImprovementStatusResponse(BaseModel):
    schema_version: str = "1.0"
    run_id: str
    output_dir: str
    artifacts: list[AIImprovementArtifactStatus]
    items_by_problem_type: dict[str, list[AIImprovementStatusItem]]
    final_manifest_status: AIImprovementManifestStatus
    final_selected_artifacts: list[dict[str, Any]]
    final_selected_artifact_candidate_ids: list[str]


class RunProgress(BaseModel):
    stage: str
    current_frame: int | None = None
    total_frames: int | None = None
    chunk_index: int | None = None
    chunk_count: int | None = None
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
    ai_candidate_lifecycle: AICandidateLifecycleReport = Field(default_factory=AICandidateLifecycleReport)
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
    config_name: str | None = None
    input_video: str | None = None
    parent_run_id: str | None = None
    output_dir_name: str | None = None
    config_patch: dict[str, Any] = Field(default_factory=dict)
    enable_postprocess: bool | None = None
    enable_follow_cam: bool | None = None
    start_frame: int | None = None
    max_frames: int | None = None
    approved_action_ids: list[str] = Field(default_factory=list)
    approved_actions_artifact_name: str | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def validate_create_mode(self) -> "CreateRunRequest":
        if self.config_name is not None:
            self.config_name = self.config_name.strip() or None
        if self.parent_run_id is not None:
            self.parent_run_id = self.parent_run_id.strip() or None
        if self.approved_actions_artifact_name is not None:
            self.approved_actions_artifact_name = self.approved_actions_artifact_name.strip() or None
        approved_ids = [str(item).strip() for item in self.approved_action_ids if str(item).strip()]
        self.approved_action_ids = approved_ids
        has_approved_artifact = bool(self.approved_actions_artifact_name)
        is_approved_child = bool(approved_ids or has_approved_artifact)
        if is_approved_child:
            if not self.parent_run_id:
                raise ValueError("Approved child recovery requires parent_run_id.")
            return self
        if not self.config_name:
            raise ValueError("Create run requires config_name unless approved child recovery fields are provided.")
        return self


class FollowCamRenderRequest(BaseModel):
    output_dir_name: str | None = None
    output_video_name: str | None = None
    prefer_cleaned_track: bool = True
    draw_ball_marker: bool = False
    draw_frame_text: bool = False
    target_width: int = 1920
    target_height: int = 1080
    notes: str | None = None


class HighlightRenderRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "oneOf": [
                {
                    "required": ["candidate_id"],
                    "not": {
                        "anyOf": [
                            {"required": ["approved_action_id"]},
                            {"required": ["start_frame"]},
                            {"required": ["end_frame"]},
                        ]
                    },
                },
                {
                    "required": ["approved_action_id"],
                    "not": {
                        "anyOf": [
                            {"required": ["candidate_id"]},
                            {"required": ["start_frame"]},
                            {"required": ["end_frame"]},
                        ]
                    },
                },
                {
                    "required": ["start_frame", "end_frame"],
                    "not": {
                        "anyOf": [
                            {"required": ["candidate_id"]},
                            {"required": ["approved_action_id"]},
                        ]
                    },
                },
            ],
        }
    )

    candidate_id: str | None = None
    approved_action_id: str | None = None
    start_frame: int | None = Field(default=None, ge=0)
    end_frame: int | None = Field(default=None, ge=0)
    pre_roll_frames: int | None = Field(default=None, ge=0)
    post_roll_frames: int | None = Field(default=None, ge=0)
    output_dir_name: str | None = None
    output_video_name: str | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def validate_selection(self) -> "HighlightRenderRequest":
        has_candidate = bool(self.candidate_id)
        has_approved_action = bool(self.approved_action_id)
        has_start = self.start_frame is not None
        has_end = self.end_frame is not None
        explicit_window = has_start and has_end
        if not has_candidate and not has_approved_action and not (has_start and has_end):
            raise ValueError("Highlight render requires candidate_id, approved_action_id, or start_frame/end_frame.")
        mode_count = sum(1 for selected in (has_candidate, has_approved_action, explicit_window) if selected)
        if mode_count != 1:
            raise ValueError(
                "Highlight render requires exactly one of candidate_id, approved_action_id, or start_frame/end_frame."
            )
        if has_start != has_end:
            raise ValueError("Highlight render frame window requires both start_frame and end_frame.")
        if has_start and has_end and self.end_frame is not None and self.start_frame is not None and self.end_frame < self.start_frame:
            raise ValueError("Highlight render requires end_frame to be greater than or equal to start_frame.")
        return self


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


class AIImproveRequest(BaseModel):
    run_id: str
    objective: str | None = None
    model: str | None = None
    dry_run: bool = False
    max_items: int = Field(default=20, ge=1, le=100)
    language: AIResponseLanguage = "en"


class AIImproveSummary(BaseModel):
    status: Literal["ok", "needs_rerun", "unavailable", "error"]
    primary_issue: str | None = None
    improvement_count: int = 0
    targeted_rerun_count: int = 0
    config_patch_count: int = 0
    highlight_adjustment_count: int = 0
    camera_improvement_count: int = 0
    camera_severity_counts: dict[str, int] = Field(default_factory=dict)
    camera_action_counts: dict[str, int] = Field(default_factory=dict)


class AIFrameWindow(BaseModel):
    start_frame: int = Field(ge=0)
    end_frame: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_order(self) -> "AIFrameWindow":
        if self.end_frame < self.start_frame:
            raise ValueError("end_frame must be greater than or equal to start_frame.")
        return self


class AILikelyBallRegion(BaseModel):
    description: str
    frame: int | None = Field(default=None, ge=0)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class AILocalSearchRoi(BaseModel):
    coordinate_space: Literal["image"]
    frame: int = Field(ge=0)
    x: float = Field(ge=0.0)
    y: float = Field(ge=0.0)
    width: float = Field(gt=0.0)
    height: float = Field(gt=0.0)
    confidence: float = Field(ge=0.0, le=1.0)


class AIImprovementItem(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    priority: str
    area: str
    failure_tags: list[AIFailureTag] = Field(default_factory=list)
    root_cause_module: AIRootCauseModule
    diagnosis: str = ""
    recommended_action: AIRecommendedAction
    config_patch: dict[str, Any] = Field(default_factory=dict)
    evidence: list[Any] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    start_frame: int | None = Field(default=None, ge=0)
    end_frame: int | None = Field(default=None, ge=0)
    rerun_scope: AIFrameWindow | None = None
    likely_ball_region: AILikelyBallRegion | None = None
    local_search_roi: AILocalSearchRoi | None = None
    false_positive_class: str | None = None
    candidate_id: str | None = None
    suggested_window: AIFrameWindow | None = None
    clip_action: AIClipAction | None = None
    camera_motion_event_id: str | None = None
    camera_motion_severity: str | None = None
    evidence_payload: dict[str, Any] = Field(default_factory=dict)
    follow_cam_rerender_plan: dict[str, Any] | None = None


class AIHighlightAdjustment(BaseModel):
    model_config = ConfigDict(extra="allow")

    candidate_id: str
    current_window: AIFrameWindow
    suggested_window: AIFrameWindow
    reason: str
    confidence: float = Field(ge=0.0, le=1.0)
    clip_action: AIClipAction | None = None


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


class AIImproveResponse(BaseModel):
    summary: AIImproveSummary
    artifact_name: str
    artifact_path: str
    improvements: list[AIImprovementItem]
    highlight_adjustments: list[AIHighlightAdjustment]


class AIImproveApprovalRequest(BaseModel):
    improvement_ids: list[str] = Field(min_length=1)
    approved_by: str = "operator"
    rerun_scope_overrides: dict[str, AIFrameWindow] = Field(default_factory=dict)
    local_search_roi_overrides: dict[str, AILocalSearchRoi] = Field(default_factory=dict)
    config_patch_overrides: dict[str, dict[str, Any]] = Field(default_factory=dict)
    suggested_window_overrides: dict[str, AIFrameWindow] = Field(default_factory=dict)
    clip_action_overrides: dict[str, AIClipAction] = Field(default_factory=dict)
    follow_cam_rerender_plan_overrides: dict[str, dict[str, Any]] = Field(default_factory=dict)


class AIApprovedAction(BaseModel):
    model_config = ConfigDict(extra="allow")

    approval_id: str
    improvement_id: str
    approved_action: AIRecommendedAction
    approval_source: str
    approved_at: str
    approved_by: str
    provenance: dict[str, Any] = Field(default_factory=dict)
    rerun_scope: AIFrameWindow | None = None
    local_search_roi: AILocalSearchRoi | None = None
    config_patch: dict[str, Any] = Field(default_factory=dict)
    suggested_window: AIFrameWindow | None = None
    clip_action: AIClipAction | None = None
    follow_cam_rerender_plan: dict[str, Any] | None = None
    source_packet_id: str | None = None
    visual_review_id: str | None = None
    candidate_id: str | None = None
    false_positive_class: str | None = None
    camera_motion_event_id: str | None = None
    camera_motion_severity: str | None = None
    start_frame: int | None = Field(default=None, ge=0)
    end_frame: int | None = Field(default=None, ge=0)


class AIApprovalArtifactSummary(BaseModel):
    name: str | None = None
    path: str | None = None
    exists: bool = False


class AIImproveApprovalSummary(BaseModel):
    approved_action_count: int = 0
    approved_action_counts: dict[str, int] = Field(default_factory=dict)
    targeted_rerun_count: int = 0
    config_patch_count: int = 0
    highlight_action_count: int = 0
    follow_cam_action_count: int = 0
    requires_execution: bool = False
    requires_high_recall_rerun: bool = False
    requires_tracking_rerun: bool = False
    requires_follow_cam_rerender: bool = False
    requires_highlight_render: bool = False
    artifacts: dict[str, AIApprovalArtifactSummary] = Field(default_factory=dict)


class AIImproveApprovalResponse(BaseModel):
    schema_version: str
    generated_at: str
    run_id: str
    source_report: str
    approved_by: str
    artifact_name: str
    artifact_path: str
    config_patch_artifact_name: str | None = None
    config_patch_artifact_path: str | None = None
    follow_cam_rerender_plan_artifact_name: str | None = None
    follow_cam_rerender_plan_artifact_path: str | None = None
    approved_actions: list[AIApprovedAction]
    summary: AIImproveApprovalSummary
    warnings: list[str] = Field(default_factory=list)
