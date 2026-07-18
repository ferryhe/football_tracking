from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from football_tracking.ai_contracts import (
    AIApprovedActionName,
    AIClipAction,
    AIFailureTag,
    AIRecommendedAction,
    AIRootCauseModule,
)
from football_tracking.detector_development_common import canonical_sha256

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


class BroadcastCalibrationConfirmation(BaseModel):
    """Three-frame, per-source calibration required by the hybrid broadcast workflow."""

    source_resolution: tuple[int, int]
    confirmed_sample_frames: tuple[int, int, int]
    field_polygon: list[Point2DPayload] = Field(min_length=3)
    exclusion_polygons: list[list[Point2DPayload]] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_confirmation(self) -> "BroadcastCalibrationConfirmation":
        width, height = self.source_resolution
        if width <= 0 or height <= 0:
            raise ValueError("source_resolution values must be positive")
        if any(frame < 0 for frame in self.confirmed_sample_frames):
            raise ValueError("confirmed_sample_frames must be non-negative")
        if tuple(sorted(self.confirmed_sample_frames)) != self.confirmed_sample_frames:
            raise ValueError("confirmed_sample_frames must be strictly increasing")
        if len(set(self.confirmed_sample_frames)) != 3:
            raise ValueError("confirmed_sample_frames must contain three distinct frames")
        for name, polygon in (
            ("field_polygon", self.field_polygon),
            *((f"exclusion_polygons[{index}]", polygon) for index, polygon in enumerate(self.exclusion_polygons)),
        ):
            if len(polygon) < 3:
                raise ValueError(f"{name} must contain at least three points")
            for x, y in polygon:
                if not 0.0 <= x < width or not 0.0 <= y < height:
                    raise ValueError(f"{name} points must lie inside source_resolution")
            doubled_area = sum(
                current[0] * following[1] - following[0] * current[1]
                for current, following in zip(polygon, polygon[1:] + polygon[:1], strict=True)
            )
            if abs(doubled_area) <= 1e-9:
                raise ValueError(f"{name} must have non-zero area")
        return self


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


class BroadcastPreflightState(BaseModel):
    model_config = ConfigDict(extra="allow")

    input_video: str | None = None
    source_resolution: tuple[int, int] | None = None
    source_frame_count: int | None = None
    fps: float | None = None
    source_size_bytes: int | None = None
    source_mtime_ns: int | None = None
    calibration: BroadcastCalibrationConfirmation | None = None
    classifier_status: str | None = None
    selective_policy_status: str | None = None


class BroadcastLastOperationState(BaseModel):
    model_config = ConfigDict(extra="allow")

    operation_run_id: str
    operation: Literal["recompute", "render"]
    status: Literal["queued", "running", "committing", "completed", "failed", "cancelled"]
    recovered: bool = False
    error: str | None = None


class BroadcastOperationRequestState(BaseModel):
    model_config = ConfigDict(extra="allow")

    trajectory_generation_id: str | None = Field(default=None, pattern=r"^trajectory-[0-9a-f]{24}$")
    target_width: int | None = None
    target_height: int | None = None
    bundle_id: str | None = None
    bundle_manifest_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    retry_from_job_id: str | None = None


class BroadcastOperationResultState(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: str | None = None
    trajectory_generation_id: str | None = None
    camera_generation_id: str | None = None
    render_generation_id: str | None = None
    review_evidence_generation_id: str | None = None
    queue_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class BroadcastRunState(BaseModel):
    """Stable public broadcast fields; extra lineage remains forward compatible."""

    model_config = ConfigDict(extra="allow")

    status: str | None = None
    quality_profile: Literal["stable_broadcast"] | None = None
    max_manual_review_windows: int | None = Field(default=None, ge=1, le=30)
    preflight: BroadcastPreflightState | None = None
    blocking_reasons: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    status_generation: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    trajectory_generation_id: str | None = None
    camera_generation_id: str | None = None
    render_generation_id: str | None = None
    operation: Literal["recompute", "render", "review_evidence_import"] | None = None
    operation_status: (
        Literal[
            "queued",
            "running",
            "reconciling",
            "committing",
            "copying",
            "validating",
            "completed",
            "failed",
            "cancelled",
            "blocked",
            "metadata_conflict",
        ]
        | None
    ) = None
    operation_report_status: Literal["available", "missing_after_ready_commit", "conflict"] | None = None
    parent_run_id: str | None = None
    owner_pid: int | None = None
    owner_instance_id: str | None = None
    request: BroadcastOperationRequestState | None = None
    result: BroadcastOperationResultState | None = None
    commit_started: bool = False
    cancel_requested: bool = False
    last_operation: BroadcastLastOperationState | None = None
    metadata_warnings: list[str] = Field(default_factory=list)


class TrialFailureClassification(BaseModel):
    code: Literal[
        "insufficient_evidence",
        "decode_failure",
        "no_raw_candidates",
        "all_candidates_class_rejected",
        "all_candidates_filtered",
        "no_tracklets",
        "all_lost",
        "wrong_or_noisy_candidates",
        "unstable_tracking",
        "acceptable",
    ]
    severity: Literal["none", "high", "blocking"]
    summary: str
    recommended_action: str


class TrialCollectedCount(BaseModel):
    value: int | None = Field(default=None, ge=0)
    status: Literal["collected", "not_collected", "invalid"]

    @model_validator(mode="after")
    def validate_status_value(self) -> "TrialCollectedCount":
        if self.status == "collected" and self.value is None:
            raise ValueError("collected counts require a value")
        if self.status != "collected" and self.value is not None:
            raise ValueError("unavailable counts cannot expose a value")
        return self


class TrialStageReconciliation(BaseModel):
    status: Literal["reconciled", "mismatch", "not_collected"]
    reason_codes: list[str] = Field(default_factory=list)


class TrialDetectionStages(BaseModel):
    schema_version: Literal["2.0"] = "2.0"
    coverage_status: Literal["complete", "invalid", "not_collected"]
    evaluated_frames: TrialCollectedCount
    detected_frames: TrialCollectedCount
    predicted_frames: TrialCollectedCount
    lost_frames: TrialCollectedCount
    raw_candidates: TrialCollectedCount
    class_mapped_candidates: TrialCollectedCount
    filtered_candidates: TrialCollectedCount
    selected_candidates: TrialCollectedCount
    tracklets: TrialCollectedCount
    rejection_reasons: dict[str, int] = Field(default_factory=dict)
    reconciliation: TrialStageReconciliation


class TrialThresholdProfile(BaseModel):
    profile_id: str
    version: str
    algorithm_version: str
    matching_rules: dict[str, Any]
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    thresholds: dict[str, float]


class TrialNumericObservation(BaseModel):
    status: Literal["collected", "not_collected", "invalid"]
    value: int | float | None = None

    @model_validator(mode="after")
    def validate_status_value(self) -> "TrialNumericObservation":
        if self.status == "collected" and self.value is None:
            raise ValueError("collected observations require a value")
        if self.status != "collected" and self.value is not None:
            raise ValueError("unavailable observations cannot expose a value")
        return self


class TrialTrackDiagnostics(BaseModel):
    status: Literal["collected", "not_collected", "invalid"]
    frame_count: TrialNumericObservation
    detected: TrialNumericObservation
    predicted: TrialNumericObservation
    lost: TrialNumericObservation
    detected_ratio: TrialNumericObservation
    predicted_ratio: TrialNumericObservation
    lost_ratio: TrialNumericObservation
    longest_lost_streak: TrialNumericObservation
    false_positive_island_count: TrialNumericObservation
    max_step_px: TrialNumericObservation


class TrialRejectionReasonsObservation(BaseModel):
    status: Literal["collected", "not_collected", "invalid"]
    value: dict[str, int] | None = None

    @model_validator(mode="after")
    def validate_status_value(self) -> "TrialRejectionReasonsObservation":
        if self.status == "collected" and self.value is None:
            raise ValueError("collected rejection reasons require a value")
        if self.status != "collected" and self.value is not None:
            raise ValueError("unavailable rejection reasons cannot expose a value")
        return self


class TrialFollowCamDiagnostics(BaseModel):
    status: Literal["collected", "not_collected", "invalid"]
    max_pan_step_px: TrialNumericObservation
    max_pan_accel_px: TrialNumericObservation
    max_zoom_step_ratio: TrialNumericObservation


class TrialGateDiagnostics(BaseModel):
    raw_track: TrialTrackDiagnostics
    cleaned_track: TrialTrackDiagnostics
    rejection_reasons: TrialRejectionReasonsObservation
    ai_review_trigger_count: TrialNumericObservation
    ai_review_triggers_per_100_frames: TrialNumericObservation
    event_candidate_count: TrialNumericObservation
    event_candidates_per_100_frames: TrialNumericObservation
    follow_cam: TrialFollowCamDiagnostics


class TrialSignalGateV2(BaseModel):
    schema_version: Literal["2.0"] = "2.0"
    status: Literal["insufficient_evidence", "retune_required", "acceptable"]
    coverage_complete: bool
    evidence_available: bool
    trajectory_acceptable: bool
    signal_acceptable: bool
    acceptance_metrics_complete: bool
    acceptance_contract_complete: bool
    quality_acceptable: bool
    operator_confirmation_required: Literal[True] = True
    reason_codes: list[str] = Field(default_factory=list)
    failure_classification: TrialFailureClassification
    threshold_profile: TrialThresholdProfile
    stage_counts: TrialDetectionStages | None = None
    trajectory: dict[str, Any]
    diagnostics: TrialGateDiagnostics
    evidence: dict[str, str]


class TrialDiagnosisResponse(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    run_id: str
    legacy_quality_gate_status: str | None = None
    trial_signal_gate_v2: TrialSignalGateV2
    tuning_schema_version: Literal["1.0"] = "1.0"


class TrialTuningControl(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    section: Literal["detector", "sahi", "filtering", "selection", "tracking", "postprocess"]
    kind: Literal["number", "integer", "boolean", "select", "multi_select"]
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    options: list[str] | None = None
    runtime_impact: Literal["low", "medium", "high"]
    description: str
    description_zh: str

    @model_validator(mode="after")
    def validate_bounds(self) -> "TrialTuningControl":
        if self.kind in {"number", "integer"}:
            if self.minimum is None or self.maximum is None or self.step is None:
                raise ValueError("numeric tuning controls require minimum, maximum, and step")
            if self.minimum >= self.maximum or self.step <= 0:
                raise ValueError("numeric tuning control bounds are invalid")
            if self.options is not None:
                raise ValueError("numeric tuning controls cannot define options")
        elif self.kind in {"select", "multi_select"}:
            if not self.options:
                raise ValueError("select tuning controls require options")
            if self.minimum is not None or self.maximum is not None or self.step is not None:
                raise ValueError("select tuning controls cannot define numeric bounds")
        elif any(value is not None for value in (self.minimum, self.maximum, self.step, self.options)):
            raise ValueError("boolean tuning controls cannot define bounds or options")
        return self


class TrialTuningAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_code: Literal["return_to_field_setup"]
    target_step: Literal["field_setup"]
    reason_code: Literal["field_geometry_requires_new_calibration"]
    affected_paths: list[
        Literal[
            "filtering.roi",
            "scene_bias.ground_zones",
            "scene_bias.negative_rois",
        ]
    ] = Field(min_length=3, max_length=3)
    lineage_constraint: Literal["invalidate_trial_and_downstream_then_create_new_calibration_version"]

    @model_validator(mode="after")
    def validate_affected_paths(self) -> "TrialTuningAction":
        if set(self.affected_paths) != {
            "filtering.roi",
            "scene_bias.ground_zones",
            "scene_bias.negative_rois",
        }:
            raise ValueError("field setup action must declare every affected geometry path")
        return self


class TrialTuningSchemaResponse(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    patch_schema_version: Literal["1.0"] = "1.0"
    controls: list[TrialTuningControl] = Field(min_length=1)
    actions: list[TrialTuningAction] = Field(min_length=1, max_length=1)


class RunRecord(BaseModel):
    run_id: str
    source: str
    status: RunStatus
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None
    config_name: str | None = None
    config_path: str | None = None
    config_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    input_video: str | None = None
    parent_run_id: str | None = None
    output_dir: str
    modules_enabled: dict[str, bool] = Field(default_factory=dict)
    artifacts: list[ArtifactSummary] = Field(default_factory=list)
    stats: dict[str, Any] = Field(default_factory=dict)
    trial_signal_gate_v2: TrialSignalGateV2 | None = None
    broadcast: BroadcastRunState = Field(default_factory=BroadcastRunState)
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


BroadcastReviewActionName = Literal["confirm_ball", "reject_noise", "mark_unknown"]
BroadcastNoiseSubtype = Literal[
    "player_body_or_shoe",
    "field_line_or_mark",
    "sideline_or_spare_ball",
    "equipment_or_background",
    "lighting_shadow_or_blur",
]


class BroadcastReviewAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_id: str = Field(min_length=1, max_length=200)
    review_item_id: str = Field(min_length=1, max_length=200)
    candidate_id: str = Field(min_length=1, max_length=300)
    reviewer_id: str = Field(min_length=1, max_length=200)
    created_at: str | None = None
    action: BroadcastReviewActionName
    noise_subtype: BroadcastNoiseSubtype | None = None

    @model_validator(mode="after")
    def validate_action_payload(self) -> "BroadcastReviewAction":
        if self.action == "reject_noise":
            if self.noise_subtype is None:
                raise ValueError("reject_noise requires noise_subtype")
        elif self.noise_subtype is not None:
            raise ValueError("noise_subtype is only valid for reject_noise")
        return self


class BroadcastReviewActionsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Empty actions are valid only for an evidence-bound queue with zero
    # candidates. The service revalidates that exact queue before publishing.
    queue_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    actions: list[BroadcastReviewAction]

    @model_validator(mode="after")
    def validate_unique_actions(self) -> "BroadcastReviewActionsRequest":
        action_ids = [action.action_id for action in self.actions]
        if len(action_ids) != len(set(action_ids)):
            raise ValueError("review action_id values must be unique")
        candidates = [action.candidate_id for action in self.actions]
        if len(candidates) != len(set(candidates)):
            raise ValueError("each review candidate may be submitted only once")
        return self


class BroadcastTerminalTailReviewEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_video_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    tracking_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    action_signal_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    temporal_chunks_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reported_frame_count: int = Field(ge=1)
    verified_frame_count: int = Field(ge=1)
    gap_frames: int = Field(ge=1)
    gap_seconds: float = Field(gt=0.0)
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class BroadcastTerminalTailReviewState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["not_required", "required", "accepted", "invalid"]
    reason: str | None = None
    evidence: BroadcastTerminalTailReviewEvidence | None = None
    decision: Literal["accept_terminal_shortfall"] | None = None
    reviewer_id: str | None = None
    reviewed_at: str | None = None
    acknowledgement_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class BroadcastTerminalTailReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["accept_terminal_shortfall"]
    reviewer_id: str = Field(min_length=1, max_length=200)
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("reviewer_id")
    @classmethod
    def validate_reviewer_id(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("reviewer_id must not be blank")
        return stripped


class BroadcastTrajectoryRecomputeRequest(BaseModel):
    review_decisions_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class BroadcastRenderRequest(BaseModel):
    trajectory_generation_id: str = Field(pattern=r"^trajectory-[0-9a-f]{24}$")
    target_width: int = Field(default=1920, ge=320, le=7680)
    target_height: int = Field(default=1080, ge=180, le=4320)


class BroadcastOperationDetails(BaseModel):
    model_config = ConfigDict(extra="allow")

    review_decisions_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    terminal_tail_review_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    message: str | None = None


class BroadcastOperationResponse(BaseModel):
    run_id: str
    parent_run_id: str | None = None
    status: Literal["ready", "queued", "completed", "needs_review"]
    reason: str | None = None
    artifact: str | None = None
    generation_id: str | None = None
    details: BroadcastOperationDetails = Field(default_factory=BroadcastOperationDetails)


class BroadcastReviewEvidenceImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bundle_id: str = Field(min_length=1, max_length=96)
    bundle_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    retry_from_job_id: str | None = Field(default=None, min_length=1, max_length=200)


class BroadcastConfigLineageReconfirmationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_run_id: str = Field(min_length=1)
    confirmed_config_name: str = Field(min_length=1)
    confirmed_text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_observed_raw_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    workflow_bindings: dict[str, Any]
    operator_id: str = Field(min_length=1, max_length=200)
    reviewer_id: str = Field(min_length=1, max_length=200)

    @field_validator("target_run_id", "confirmed_config_name")
    @classmethod
    def validate_authority_identity(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("authority identity must be trimmed text")
        return value

    @field_validator("operator_id", "reviewer_id")
    @classmethod
    def validate_identity(cls, value: str) -> str:
        if not value.strip() or value != value.strip():
            raise ValueError("identity must be non-empty trimmed text")
        return value

    @model_validator(mode="after")
    def validate_independent_reviewer(self) -> BroadcastConfigLineageReconfirmationRequest:
        if self.operator_id == self.reviewer_id:
            raise ValueError("operator and independent reviewer must differ")
        return self


class BroadcastConfigLineageReconfirmationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    status: Literal["reconfirmed"]
    generation_id: str = Field(pattern=r"^lineage-[0-9a-f]{24}$")
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmed_text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_raw_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    canonical_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    lineage_generation_id: str = Field(pattern=r"^lineage-[0-9a-f]{24}$")
    historical_raw_snapshot_observed: Literal[False]


class BroadcastConfigLineageReconfirmationChallenge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_run_id: str = Field(min_length=1)
    confirmed_config_name: str = Field(min_length=1)
    confirmed_text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_observed_raw_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    workflow_bindings: dict[str, Any]


ConfigLineageBlockerCode = Literal[
    "confirmed_config_lineage_reconfirmation_required",
    "config_lineage_snapshot_unsafe",
    "config_lineage_snapshot_mismatch",
    "config_lineage_reconfirmation_conflict",
]


class BroadcastConfigLineageBlockerResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["blocked"]
    blocker_code: ConfigLineageBlockerCode
    detail: str
    retryable: bool = False


class BroadcastReviewEvidenceRevokeResponse(BaseModel):
    run_id: str
    status: Literal["revoked"]
    generation_id: str = Field(pattern=r"^review-evidence-[0-9a-f]{24}$")
    queue_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    revoked_at: str


class BroadcastReviewEvidenceProvisionerLimits(BaseModel):
    max_files: int = Field(gt=0)
    max_bundle_bytes: int = Field(gt=0)
    max_single_file_bytes: int = Field(gt=0)


class BroadcastReviewEvidenceRetention(BaseModel):
    policy: Literal["manual-audit-retention-v1"]
    retain_until: str
    automatic_delete: Literal[False]


class BroadcastReviewEvidenceCapacity(BaseModel):
    total_size_bytes: int | None = Field(default=None, ge=0)
    required_free_bytes: int | None = Field(default=None, ge=0)
    available_free_bytes: int | None = Field(default=None, ge=0)
    attempt_quota_bytes: int | None = Field(default=None, ge=0)
    capacity_status: Literal["sufficient", "insufficient"] | None = None
    retention: BroadcastReviewEvidenceRetention | None = None
    provisioner_limits: BroadcastReviewEvidenceProvisionerLimits | None = None


class BroadcastReviewEvidenceBundleSummary(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: Literal["available", "not_applicable", "invalid"]
    bundle_id: str | None = None
    bundle_manifest_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    queue_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    total_size_bytes: int | None = Field(default=None, ge=0)
    required_free_bytes: int | None = Field(default=None, ge=0)
    available_free_bytes: int | None = Field(default=None, ge=0)
    attempt_quota_bytes: int | None = Field(default=None, ge=0)
    capacity_status: Literal["sufficient", "insufficient"] | None = None
    retention: BroadcastReviewEvidenceRetention | None = None
    provisioner_limits: BroadcastReviewEvidenceProvisionerLimits | None = None
    inbox_entry: str
    error_code: str | None = None
    error: str | None = None


class BroadcastReviewEvidenceStateResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    run_id: str
    status: Literal[
        "not_available",
        "available",
        "queued",
        "copying",
        "validating",
        "committing",
        "ready",
        "failed",
        "cancelled",
        "blocked",
    ]
    active_job_id: str | None = None
    retry_from_job_id: str | None = None
    generation_id: str | None = None
    queue_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    stage: str | None = None
    progress_percent: float = Field(default=0.0, ge=0.0, le=100.0)
    blocker_code: str | None = None
    error_code: str | None = None
    recovery_action: str | None = None
    retryable: bool = False
    can_cancel: bool = False
    bundles: list[BroadcastReviewEvidenceBundleSummary] = Field(default_factory=list)
    capacity: BroadcastReviewEvidenceCapacity | None = None
    blocking_reasons: list[str] = Field(default_factory=list)
    message: str | None = None
    config_lineage_reconfirmation: BroadcastConfigLineageReconfirmationChallenge | None = None


class BroadcastReviewEvidenceArtifact(BaseModel):
    model_config = ConfigDict(extra="allow")

    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    shape: list[int] | None = None
    dtype: str | None = None
    color_space: str | None = None


class BroadcastReviewEvidenceArtifacts(BaseModel):
    model_config = ConfigDict(extra="allow")

    tight_tensor: BroadcastReviewEvidenceArtifact
    context_tensor: BroadcastReviewEvidenceArtifact
    review_montage: BroadcastReviewEvidenceArtifact


class BroadcastReviewEvidence(BaseModel):
    model_config = ConfigDict(extra="allow")

    sample_id: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    dataset_version: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifacts: BroadcastReviewEvidenceArtifacts


class BroadcastReviewCandidate(BaseModel):
    model_config = ConfigDict(extra="allow")

    candidate_id: str
    candidate_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    variant_id: str
    frame_index: int = Field(ge=0)
    bbox: tuple[float, float, float, float]
    detector_source: str
    detector_confidence: float = Field(ge=0.0, le=1.0)
    predicted_label: str
    prediction_confidence: float = Field(ge=0.0, le=1.0)
    selective_decision: Literal["accept", "reject", "abstain"]
    decision_reasons: list[str] = Field(default_factory=list)
    review_kind: str
    evidence: BroadcastReviewEvidence


class BroadcastReviewWindow(BaseModel):
    model_config = ConfigDict(extra="allow")

    review_item_id: str
    variant_id: str
    start_frame: int = Field(ge=0)
    end_frame: int = Field(ge=0)
    duration_seconds: float = Field(ge=0.0)
    compliance: Literal["compliant"]
    priority: int = Field(ge=0)
    candidates: list[BroadcastReviewCandidate] = Field(default_factory=list)


class BroadcastReviewWindowsResponse(BaseModel):
    run_id: str
    status: Literal["ready", "needs_review"]
    reason: str | None = None
    queue_sha256: str | None = None
    review_item_count: int = 0
    items: list[BroadcastReviewWindow] = Field(default_factory=list)
    terminal_tail_review: BroadcastTerminalTailReviewState = Field(
        default_factory=lambda: BroadcastTerminalTailReviewState(status="not_required")
    )


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
    pipeline_mode: Literal["standard", "broadcast_hybrid"] = "standard"
    calibration_confirmation: BroadcastCalibrationConfirmation | None = None
    quality_profile: Literal["stable_broadcast"] | None = None
    max_manual_review_windows: int | None = Field(default=None, ge=1, le=30)
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
            if (
                self.pipeline_mode != "standard"
                or self.calibration_confirmation is not None
                or self.quality_profile is not None
                or self.max_manual_review_windows is not None
            ):
                raise ValueError("approved child recovery cannot be combined with broadcast_hybrid fields")
            if not self.parent_run_id:
                raise ValueError("Approved child recovery requires parent_run_id.")
            return self
        if not self.config_name:
            raise ValueError("Create run requires config_name unless approved child recovery fields are provided.")
        if self.pipeline_mode == "broadcast_hybrid":
            if self.quality_profile != "stable_broadcast":
                raise ValueError("broadcast_hybrid requires quality_profile='stable_broadcast'")
            if self.calibration_confirmation is None:
                raise ValueError("broadcast_hybrid requires calibration_confirmation")
            if self.max_manual_review_windows is None:
                self.max_manual_review_windows = 30
        elif (
            self.calibration_confirmation is not None
            or self.quality_profile is not None
            or self.max_manual_review_windows is not None
        ):
            raise ValueError("broadcast calibration/profile fields require pipeline_mode='broadcast_hybrid'")
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
        if (
            has_start
            and has_end
            and self.end_frame is not None
            and self.start_frame is not None
            and self.end_frame < self.start_frame
        ):
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
    approved_action: AIApprovedActionName
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


DetectorAvailabilityStatus = Literal["available", "unavailable", "blocked"]
DetectorObservationStatus = Literal["pass", "fail", "not_run"]
DetectorLifecycleState = Literal[
    "unverified",
    "feasibility_passed",
    "development_candidate",
    "source_segment_qualified",
    "camera_qualified",
    "retired",
]
DetectorProbeStatus = Literal[
    "queued",
    "running",
    "committing",
    "ready",
    "failed",
    "cancelled",
    "blocked",
]


class DetectorLicenseMetadata(BaseModel):
    name: str
    spdx_id: str
    url: str
    reviewed: bool
    approved_for_local_probe: bool


class DetectorLicenseSet(BaseModel):
    dataset: DetectorLicenseMetadata
    model: DetectorLicenseMetadata
    runtime: DetectorLicenseMetadata
    deployment: DetectorLicenseMetadata


class DetectorModelWeights(BaseModel):
    relative_path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0)


class DetectorModelSource(BaseModel):
    project: str
    version: str
    asset_release: str
    weight_url: str
    acquisition_method: str
    access_requirement: str


class DetectorModelEgress(BaseModel):
    frames_leave_local_machine: bool
    destination: str | None
    operator_consent: Literal["not_required", "granted", "required_not_granted"]


class DetectorModelDescriptorView(BaseModel):
    schema_version: str
    artifact_type: Literal["detector_model_descriptor"]
    model_id: str
    version: str
    model_version: str
    display_name: str
    architecture_family: str
    weights: DetectorModelWeights
    source: DetectorModelSource
    checkpoint: dict[str, Any] | None = None
    runtime_contract: dict[str, Any]
    class_names: list[str]
    class_map: dict[str, str]
    expected_input: dict[str, Any]
    execution: dict[str, Any] | None = None
    memory_envelope: dict[str, Any] | None = None
    licenses: DetectorLicenseSet
    egress: DetectorModelEgress
    lifecycle_state: DetectorLifecycleState
    bindings: dict[str, Any]
    descriptor_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    import_manifest_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class DetectorAvailabilityObservation(BaseModel):
    status: DetectorObservationStatus
    reason: str
    installed_runtime: dict[str, str | None] | None = None
    evidence_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class DetectorAvailabilityObservations(BaseModel):
    file: DetectorAvailabilityObservation
    digest: DetectorAvailabilityObservation
    class_map: DetectorAvailabilityObservation
    license: DetectorAvailabilityObservation
    runtime_load: DetectorAvailabilityObservation


class DetectorModelAvailability(BaseModel):
    status: DetectorAvailabilityStatus
    reason_codes: list[str]
    observations: DetectorAvailabilityObservations
    observed_weight_path: str | None = None


class DetectorQualificationView(BaseModel):
    trial_eligible: bool
    source_segment_qualified: bool
    camera_qualified: bool


class DetectorModelRecordView(BaseModel):
    descriptor: DetectorModelDescriptorView
    availability: DetectorModelAvailability
    qualification: DetectorQualificationView
    selectable_for_probe: bool


class DetectorProfileRuntimeView(BaseModel):
    name: str
    installed_version: str | None
    load_smoke: bool


class DetectorProfileAvailabilityView(BaseModel):
    status: DetectorAvailabilityStatus
    reason_codes: list[str]
    runtime: DetectorProfileRuntimeView | None = None


class DetectorProfileSettingsView(BaseModel):
    confidence_threshold: float = Field(ge=0, le=1)
    image_size: int = Field(gt=0)
    use_half: bool
    allowed_labels: list[str]
    top_k: Literal[5]
    slice_height: int | None = Field(default=None, gt=0)
    slice_width: int | None = Field(default=None, gt=0)
    overlap_height_ratio: float | None = Field(default=None, ge=0, lt=1)
    overlap_width_ratio: float | None = Field(default=None, ge=0, lt=1)
    perform_standard_pred: bool | None = None
    postprocess_type: str | None = None
    postprocess_match_metric: str | None = None
    postprocess_match_threshold: float | None = Field(default=None, ge=0, le=1)


class DetectorProfileView(BaseModel):
    schema_version: str
    artifact_type: Literal["detector_profile"]
    profile_id: str
    version: str
    model_id: str
    model_version: str
    model_descriptor_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mode: Literal["direct", "sahi"]
    settings: DetectorProfileSettingsView
    profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    recommended: bool
    availability: DetectorProfileAvailabilityView
    selectable_for_probe: bool


class DetectorCatalogFindingLicense(BaseModel):
    status: Literal["review_required", "unavailable", "unknown"]
    name: str | None
    spdx_id: str | None
    url: str | None
    approved_for_local_probe: bool


class DetectorCatalogFindingLicenseSet(BaseModel):
    dataset: DetectorCatalogFindingLicense
    model: DetectorCatalogFindingLicense
    runtime: DetectorCatalogFindingLicense
    deployment: DetectorCatalogFindingLicense


class DetectorCatalogFindingSource(BaseModel):
    project: str
    version: str | None
    url: str


class DetectorCatalogFindingAccess(BaseModel):
    method: str
    account_or_plan_required: str
    local_weights_validated: bool


class DetectorCatalogFindingEgress(BaseModel):
    frames_leave_local_machine: Literal["unknown_until_access_method_selected"]
    destination: str | None
    operator_consent: Literal["required_before_external_inference"]


class DetectorCatalogFindingAvailability(BaseModel):
    status: Literal["unavailable"]
    reason_codes: list[str] = Field(min_length=1)


class DetectorCatalogFindingView(BaseModel):
    finding_id: str
    display_name: str
    source: DetectorCatalogFindingSource
    architecture_family: str
    access: DetectorCatalogFindingAccess
    licenses: DetectorCatalogFindingLicenseSet
    egress: DetectorCatalogFindingEgress
    selectable: Literal[False]
    availability: DetectorCatalogFindingAvailability


class DetectorModelCatalogResponse(BaseModel):
    schema_version: str
    artifact_type: Literal["ball_detector_development_v1"]
    models: list[DetectorModelRecordView]
    profiles: list[DetectorProfileView]
    catalog_findings: list[DetectorCatalogFindingView]


class DetectorModelImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    package_relative_path: str = Field(min_length=1, max_length=500)
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class DetectorModelImportResponse(BaseModel):
    created: bool
    model: DetectorModelRecordView


DetectorSafeId = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,119}$")]
DetectorFrameIndex = Annotated[int, Field(strict=True, ge=0)]


class DetectorProbeCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    parent_trial_id: DetectorSafeId
    profile_ids: list[DetectorSafeId] = Field(min_length=2, max_length=6)
    frame_indices: list[DetectorFrameIndex] | None = Field(default=None, min_length=1, max_length=50)
    top_k: Literal[5] = 5
    retry_from_job_id: DetectorSafeId | None = None

    @field_validator("profile_ids")
    @classmethod
    def validate_detector_profile_ids(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values) or any(not value or value != value.strip() for value in values):
            raise ValueError("profile_ids must contain unique exact IDs")
        return values

    @field_validator("frame_indices")
    @classmethod
    def validate_detector_frame_indices(cls, values: list[int] | None) -> list[int] | None:
        if values is None:
            return None
        if len(set(values)) != len(values) or any(value < 0 for value in values):
            raise ValueError("frame_indices must contain unique nonnegative integers")
        return sorted(values)


class DetectorProbeStrictView(BaseModel):
    """Exact public evidence contract; response serialization must not drop lineage."""

    model_config = ConfigDict(extra="forbid")


class DetectorProbeCreateResponse(DetectorProbeStrictView):
    job_id: DetectorSafeId
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: DetectorProbeStatus
    status_url: str
    cancel_url: str
    retry_from_job_id: DetectorSafeId | None = None


class DetectorProbeProgressView(DetectorProbeStrictView):
    completed: int = Field(strict=True, ge=0)
    total: int = Field(strict=True, ge=0)
    updated_at: str


class FrozenDetectorProfileBindingView(DetectorProbeStrictView):
    profile_id: DetectorSafeId
    profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_id: DetectorSafeId
    model_version: str
    model_descriptor_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    weights_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    weights_size_bytes: int = Field(strict=True, gt=0)


class DetectorProbeTuningPatchBindingView(DetectorProbeStrictView):
    state: Literal["absent", "versioned"]
    schema_version: Literal["1.0"]
    version_id: str | None
    parent_version_id: str | None
    values_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_tuning_patch_state(self) -> DetectorProbeTuningPatchBindingView:
        if self.state == "absent":
            if (
                self.version_id is not None
                or self.parent_version_id is not None
                or self.values_sha256 != canonical_sha256({})
            ):
                raise ValueError("absent tuning patch cannot have a version identity")
        elif self.version_id is None or not self.version_id.strip():
            raise ValueError("versioned tuning patch requires version_id")
        return self


class DetectorProbeExecutionEnvironmentView(DetectorProbeStrictView):
    device: Literal["cpu", "cuda:0"]
    precision: Literal["fp32"]
    cuda_available: bool
    cuda_device_count: int = Field(strict=True, ge=0)
    cuda_visible_devices: str | None
    cuda_compiled_version: str | None
    cudnn_version: int | None = Field(default=None, strict=True, ge=0)
    gpu_name: str | None
    gpu_compute_capability: str | None
    gpu_total_memory_bytes: int | None = Field(default=None, strict=True, gt=0)
    cuda_driver_version: int | None = Field(default=None, strict=True, ge=0)
    python_implementation: str = Field(min_length=1)
    python_version: str = Field(min_length=1)
    numpy_version: str = Field(min_length=1)
    opencv_version: str = Field(min_length=1)
    pydantic_version: str = Field(min_length=1)
    pydantic_core_version: str = Field(min_length=1)
    opencv_build_information_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    opencv_ffmpeg_enabled: bool | None
    decoder_fingerprint_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_execution_hardware(self) -> DetectorProbeExecutionEnvironmentView:
        gpu_identity_values = (
            self.gpu_name,
            self.gpu_compute_capability,
            self.gpu_total_memory_bytes,
            self.cuda_driver_version,
        )
        if self.device == "cpu":
            if (
                self.cuda_available
                or self.cuda_device_count != 0
                or any(value is not None for value in gpu_identity_values)
            ):
                raise ValueError("CPU execution cannot claim CUDA hardware")
        elif not self.cuda_available or self.cuda_device_count < 1:
            raise ValueError("CUDA execution requires a visible CUDA device")
        decoder_fingerprint = {
            "python_implementation": self.python_implementation,
            "python_version": self.python_version,
            "numpy_version": self.numpy_version,
            "opencv_version": self.opencv_version,
            "opencv_build_information_sha256": self.opencv_build_information_sha256,
            "opencv_ffmpeg_enabled": self.opencv_ffmpeg_enabled,
        }
        if canonical_sha256(decoder_fingerprint) != self.decoder_fingerprint_sha256:
            raise ValueError("decoder fingerprint digest does not match")
        return self


class DetectorProbeExecutionBundleView(DetectorProbeStrictView):
    schema_version: Literal["1.0"]
    installed_runtime: dict[str, str]
    runtime_contract: dict[str, str]
    runtime_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_observation_evidence_sha256s: dict[DetectorSafeId, Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]]
    execution_environment: DetectorProbeExecutionEnvironmentView
    runtime_environment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    code_bundle_files: dict[str, Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]]
    code_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    code_commit: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")] | None
    code_commit_status: Literal["bound", "unbound", "unavailable"]
    code_commit_reason: Literal[
        "code_bundle_differs_from_commit", "repository_commit_unavailable"
    ] | None
    code_commit_blob_files: (
        dict[str, Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]] | None
    )
    code_commit_blob_bundle_sha256: (
        Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None
    )
    code_commit_binding_kind: Literal["exact_or_crlf_to_lf_commit_blob"] | None
    frozen_profiles_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_execution_bundle(self) -> DetectorProbeExecutionBundleView:
        runtime_names = {"sahi", "torch", "ultralytics"}
        if (
            set(self.installed_runtime) != runtime_names
            or set(self.runtime_contract) != runtime_names
            or any(not value for value in self.installed_runtime.values())
            or any(not value for value in self.runtime_contract.values())
        ):
            raise ValueError("execution bundle runtime bindings are incomplete")
        if canonical_sha256(self.runtime_contract) != self.runtime_contract_sha256:
            raise ValueError("execution bundle runtime contract digest does not match")
        if canonical_sha256(self.code_bundle_files) != self.code_bundle_sha256:
            raise ValueError("execution bundle code digest does not match")
        runtime_environment = {
            "installed_runtime": self.installed_runtime,
            "runtime_observation_evidence_sha256s": (self.runtime_observation_evidence_sha256s),
            "execution_environment": self.execution_environment.model_dump(mode="json"),
            "code_bundle_sha256": self.code_bundle_sha256,
            "code_commit": self.code_commit,
            "code_commit_status": self.code_commit_status,
            "code_commit_reason": self.code_commit_reason,
            "code_commit_blob_bundle_sha256": (
                self.code_commit_blob_bundle_sha256
            ),
            "code_commit_binding_kind": self.code_commit_binding_kind,
        }
        if canonical_sha256(runtime_environment) != self.runtime_environment_sha256:
            raise ValueError("execution bundle environment digest does not match")
        if self.code_commit_status == "bound":
            if (
                self.code_commit is None
                or self.code_commit_reason is not None
                or self.code_commit_blob_files is None
                or self.code_commit_blob_bundle_sha256 is None
                or self.code_commit_binding_kind
                != "exact_or_crlf_to_lf_commit_blob"
            ):
                raise ValueError("bound code commit evidence is incomplete")
            if (
                set(self.code_commit_blob_files) != set(self.code_bundle_files)
                or canonical_sha256(self.code_commit_blob_files)
                != self.code_commit_blob_bundle_sha256
            ):
                raise ValueError("bound commit blob digest does not match")
        elif self.code_commit_status == "unbound":
            if (
                self.code_commit is not None
                or self.code_commit_reason != "code_bundle_differs_from_commit"
                or self.code_commit_blob_files is not None
                or self.code_commit_blob_bundle_sha256 is not None
                or self.code_commit_binding_kind is not None
            ):
                raise ValueError("unbound code commit evidence is inconsistent")
        elif (
            self.code_commit is not None
            or self.code_commit_reason != "repository_commit_unavailable"
            or self.code_commit_blob_files is not None
            or self.code_commit_blob_bundle_sha256 is not None
            or self.code_commit_binding_kind is not None
        ):
            raise ValueError("unavailable code commit evidence is inconsistent")
        return self


class FrozenDetectorProbeRequestView(DetectorProbeStrictView):
    parent_trial_id: DetectorSafeId
    source_id: DetectorSafeId
    source_relative_path: str
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_file_identity_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_size_bytes: int = Field(strict=True, gt=0)
    source_width: int = Field(strict=True, gt=0)
    source_height: int = Field(strict=True, gt=0)
    source_frame_count: int = Field(strict=True, gt=0)
    tracking_contract_relative_path: str
    tracking_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    base_config_relative_path: str
    base_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    effective_config_relative_path: str
    effective_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    trial_intent_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    tuning_patch_binding: DetectorProbeTuningPatchBindingView
    tuning_patch_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    profile_ids: list[DetectorSafeId] = Field(min_length=2, max_length=6)
    frozen_profiles_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    profile_sha256s: dict[DetectorSafeId, Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]]
    profile_bindings: list[FrozenDetectorProfileBindingView] = Field(min_length=2, max_length=6)
    execution_bundle: DetectorProbeExecutionBundleView
    execution_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_environment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    frame_indices: list[DetectorFrameIndex] = Field(min_length=1, max_length=50)
    top_k: Literal[5]
    requested_decode_mode: Literal["sequential", "preroll", "direct"]
    retry_from_job_id: DetectorSafeId | None = None

    @model_validator(mode="after")
    def validate_frozen_probe_request(self) -> FrozenDetectorProbeRequestView:
        if self.profile_ids != sorted(self.profile_ids) or len(set(self.profile_ids)) != len(self.profile_ids):
            raise ValueError("frozen profile_ids must be unique and sorted")
        if self.frame_indices != sorted(self.frame_indices) or len(set(self.frame_indices)) != len(self.frame_indices):
            raise ValueError("frozen frame_indices must be unique and sorted")
        if self.frame_indices[-1] >= self.source_frame_count:
            raise ValueError("frozen frame_indices exceed the source frame count")
        binding_ids = [binding.profile_id for binding in self.profile_bindings]
        if binding_ids != self.profile_ids or set(self.profile_sha256s) != set(self.profile_ids):
            raise ValueError("frozen profile bindings do not match profile_ids")
        if any(self.profile_sha256s[binding.profile_id] != binding.profile_sha256 for binding in self.profile_bindings):
            raise ValueError("frozen profile binding digests do not match")
        if canonical_sha256(self.tuning_patch_binding.model_dump(mode="json")) != self.tuning_patch_sha256:
            raise ValueError("frozen tuning-patch digest does not match its exact binding")
        if (
            self.execution_bundle.frozen_profiles_sha256 != self.frozen_profiles_sha256
            or self.execution_bundle.runtime_environment_sha256 != self.runtime_environment_sha256
            or canonical_sha256(self.execution_bundle.model_dump(mode="json")) != self.execution_bundle_sha256
            or set(self.execution_bundle.runtime_observation_evidence_sha256s) != set(self.profile_ids)
        ):
            raise ValueError("frozen execution bundle does not match request lineage")
        return self


class FrozenDetectorProfileView(DetectorProfileView):
    model_config = ConfigDict(extra="forbid")

    model_descriptor: DetectorModelDescriptorView

    @model_validator(mode="after")
    def validate_frozen_profile_lineage(self) -> FrozenDetectorProfileView:
        if (
            self.model_id != self.model_descriptor.model_id
            or self.model_version != self.model_descriptor.version
            or self.model_descriptor_sha256 != self.model_descriptor.descriptor_sha256
        ):
            raise ValueError("frozen detector profile descriptor lineage does not match")
        return self


class DetectorProbeCandidateView(DetectorProbeStrictView):
    frame_index: DetectorFrameIndex
    bbox_source_px: tuple[float, float, float, float]
    confidence: float = Field(ge=0, le=1)
    class_name: Literal["ball"]
    checkpoint_class_name: str = Field(min_length=1)
    source: str = Field(min_length=1)
    coordinate_reason: Literal["direct_source_coordinates", "sahi_tile_offset_applied"]
    merge_reason: Literal["retained_top_k"]

    @model_validator(mode="after")
    def validate_candidate_box(self) -> DetectorProbeCandidateView:
        left, top, right, bottom = self.bbox_source_px
        if left < 0 or top < 0 or right <= left or bottom <= top:
            raise ValueError("detector candidate bbox_source_px is invalid")
        return self


class DetectorProbeProfileEvidenceView(DetectorProbeStrictView):
    profile_id: DetectorSafeId
    profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["completed", "failed", "blocked"]
    latency_ms: float | None = Field(default=None, ge=0)
    candidate_count: int = Field(strict=True, ge=0)
    top_k: Literal[5]
    raw_candidates: list[DetectorProbeCandidateView] = Field(max_length=5)
    display_candidate: DetectorProbeCandidateView | None
    filter_reasons: dict[str, Annotated[int, Field(strict=True, ge=0)]]
    failure_code: str | None
    raw_overlay_artifact_url: str
    raw_overlay_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    raw_overlay_size_bytes: int = Field(strict=True, gt=0)

    @model_validator(mode="after")
    def validate_profile_evidence_state(self) -> DetectorProbeProfileEvidenceView:
        if self.candidate_count < len(self.raw_candidates):
            raise ValueError("candidate_count cannot be smaller than retained raw candidates")
        if self.display_candidate is not None and self.display_candidate not in self.raw_candidates:
            raise ValueError("display_candidate must be one of raw_candidates")
        if self.status == "completed":
            if self.latency_ms is None or self.failure_code is not None:
                raise ValueError("completed profile evidence has invalid completion state")
        elif self.failure_code is None:
            raise ValueError("failed or blocked profile evidence requires failure_code")
        return self


class DetectorProbeMediaIntegrityView(DetectorProbeStrictView):
    path: str | None
    status: Literal["ok", "unavailable"]
    width: int = Field(strict=True, ge=0)
    height: int = Field(strict=True, ge=0)
    mean_luma: float = Field(ge=0)
    std_luma: float = Field(ge=0)
    texture_tile_ratio: float = Field(ge=0, le=1)
    dominant_color_ratio: float = Field(ge=0, le=1)
    gray: bool
    low_information: bool
    likely_corrupt: bool
    reasons: list[str]


class DetectorProbeFrameEvidenceView(DetectorProbeStrictView):
    frame_index: DetectorFrameIndex
    source_width: int = Field(strict=True, gt=0)
    source_height: int = Field(strict=True, gt=0)
    requested_decode_mode: Literal["sequential", "preroll", "direct"]
    effective_decode_mode: Literal["sequential", "preroll_verified", "direct_verified", "sequential_fallback"]
    decoded_frame_position: DetectorFrameIndex
    media_integrity: DetectorProbeMediaIntegrityView
    source_artifact_url: str
    source_frame_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_frame_size_bytes: int = Field(strict=True, gt=0)
    profile_results: list[DetectorProbeProfileEvidenceView] = Field(min_length=2, max_length=6)

    @model_validator(mode="after")
    def validate_frame_evidence(self) -> DetectorProbeFrameEvidenceView:
        if self.decoded_frame_position != self.frame_index:
            raise ValueError("decoded frame position does not match frame_index")
        if self.media_integrity.width != self.source_width or self.media_integrity.height != self.source_height:
            raise ValueError("media integrity dimensions do not match source dimensions")
        if len({profile.profile_id for profile in self.profile_results}) != len(self.profile_results):
            raise ValueError("probe frame contains duplicate profiles")
        for profile in self.profile_results:
            for candidate in [*profile.raw_candidates, profile.display_candidate]:
                if candidate is None:
                    continue
                left, top, right, bottom = candidate.bbox_source_px
                if (
                    candidate.frame_index != self.frame_index
                    or right > self.source_width
                    or bottom > self.source_height
                    or left < 0
                    or top < 0
                ):
                    raise ValueError("probe candidate is outside source-frame coordinates")
        return self


class DetectorProbeSourceBindingView(DetectorProbeStrictView):
    source_id: DetectorSafeId
    relative_path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    file_identity_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(strict=True, gt=0)
    width: int = Field(strict=True, gt=0)
    height: int = Field(strict=True, gt=0)
    frame_count: int = Field(strict=True, gt=0)
    tracking_contract_relative_path: str
    tracking_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class DetectorProbeLineageView(DetectorProbeStrictView):
    parent_trial_id: DetectorSafeId
    base_config_relative_path: str
    base_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    effective_config_relative_path: str
    effective_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    trial_intent_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    tuning_patch_binding: DetectorProbeTuningPatchBindingView
    tuning_patch_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    profile_sha256s: dict[DetectorSafeId, Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]]
    frozen_profiles_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_bundle: DetectorProbeExecutionBundleView
    execution_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_environment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    intent_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    retry_from_job_id: DetectorSafeId | None


class DetectorProbeDecodeView(DetectorProbeStrictView):
    width: int = Field(strict=True, gt=0)
    height: int = Field(strict=True, gt=0)
    frame_count: int = Field(strict=True, gt=0)
    fps: float = Field(gt=0)
    requested_decode_mode: Literal["sequential", "preroll", "direct"]
    effective_decode_mode: Literal["sequential", "preroll_verified", "direct_verified", "sequential_fallback"]
    verified_frame_indices: list[DetectorFrameIndex] = Field(min_length=1, max_length=50)
    position_verification: Literal["opencv_next_frame_index_with_0.25_tolerance"]


class DetectorProbeExecutionView(DetectorProbeStrictView):
    device: Literal["cpu", "cuda:0"]
    precision: Literal["fp32"]


class DetectorProbeArtifactView(DetectorProbeStrictView):
    artifact_id: DetectorSafeId
    relative_path: str
    media_type: Literal["image/jpeg"]
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(strict=True, gt=0)
    width: int = Field(strict=True, gt=0)
    height: int = Field(strict=True, gt=0)


class DetectorProbeReportView(DetectorProbeStrictView):
    schema_version: Literal["1.0"]
    artifact_type: Literal["detector_probe_report"]
    job_id: DetectorSafeId
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source: DetectorProbeSourceBindingView
    lineage: DetectorProbeLineageView
    frozen_profiles: list[FrozenDetectorProfileView] = Field(min_length=2, max_length=6)
    top_k: Literal[5]
    frames: list[DetectorProbeFrameEvidenceView] = Field(min_length=1, max_length=50)
    decode: DetectorProbeDecodeView
    execution: DetectorProbeExecutionView
    artifacts: list[DetectorProbeArtifactView] = Field(min_length=3, max_length=350)
    created_at: str
    report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def _detector_probe_artifact_id(url: str, job_id: str) -> str:
    prefix = f"/api/v1/detector-probes/{job_id}/artifacts/"
    if not url.startswith(prefix):
        raise ValueError("detector probe artifact URL is outside the job allowlist")
    artifact_id = url[len(prefix) :]
    if not artifact_id or "/" in artifact_id or ".." in artifact_id:
        raise ValueError("detector probe artifact URL contains an invalid artifact ID")
    return artifact_id


class DetectorProbeJobResponse(DetectorProbeStrictView):
    schema_version: Literal["1.0"]
    artifact_type: Literal["detector_probe_job"]
    job_id: DetectorSafeId
    idempotency_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    intent_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    frozen_profiles_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: DetectorProbeStatus
    stage: str
    progress: DetectorProbeProgressView
    frozen_request: FrozenDetectorProbeRequestView
    frozen_profiles: list[FrozenDetectorProfileView] = Field(min_length=2, max_length=6)
    retry_from_job_id: DetectorSafeId | None
    error_code: str | None = None
    blocker_code: str | None = None
    recovery_action: str | None = None
    report: DetectorProbeReportView | None
    result_manifest_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    created_at: str
    updated_at: str
    status_url: str
    cancel_url: str
    can_cancel: bool

    @model_validator(mode="after")
    def validate_probe_job_evidence(self) -> DetectorProbeJobResponse:
        expected_status_url = f"/api/v1/detector-probes/{self.job_id}"
        if self.status_url != expected_status_url or self.cancel_url != f"{expected_status_url}/cancel":
            raise ValueError("detector probe control URLs do not match job_id")
        if self.idempotency_key != self.request_sha256:
            raise ValueError("detector probe idempotency key does not match request digest")
        if self.retry_from_job_id != self.frozen_request.retry_from_job_id:
            raise ValueError("detector probe retry lineage does not match frozen request")
        if self.retry_from_job_id == self.job_id:
            raise ValueError("detector probe retry cannot reference itself")
        if (
            self.progress.total != len(self.frozen_request.frame_indices) * len(self.frozen_request.profile_ids)
            or self.progress.completed > self.progress.total
        ):
            raise ValueError("detector probe progress does not match frozen work")
        if self.can_cancel != (self.status in {"queued", "running"}):
            raise ValueError("detector probe cancellation state is inconsistent")
        frozen_by_id = {profile.profile_id: profile for profile in self.frozen_profiles}
        if list(frozen_by_id) != self.frozen_request.profile_ids:
            raise ValueError("frozen profiles do not match the frozen request")
        for binding in self.frozen_request.profile_bindings:
            profile = frozen_by_id[binding.profile_id]
            descriptor = profile.model_descriptor
            if (
                profile.profile_sha256 != binding.profile_sha256
                or profile.model_id != binding.model_id
                or profile.model_version != binding.model_version
                or profile.model_descriptor_sha256 != binding.model_descriptor_sha256
                or descriptor.weights.sha256 != binding.weights_sha256
                or descriptor.weights.size_bytes != binding.weights_size_bytes
            ):
                raise ValueError("frozen profile evidence does not match its binding")
        if self.frozen_profiles_sha256 != self.frozen_request.frozen_profiles_sha256:
            raise ValueError("frozen profile aggregate digest does not match")

        if self.status == "ready":
            if (
                self.report is None
                or self.result_manifest_sha256 is None
                or self.progress.completed != self.progress.total
            ):
                raise ValueError("ready detector probe is missing complete committed evidence")
        elif self.report is not None or self.result_manifest_sha256 is not None:
            raise ValueError("non-ready detector probe cannot publish committed evidence")
        if self.report is None:
            return self

        report = self.report
        frozen = self.frozen_request
        if (
            report.job_id != self.job_id
            or report.request_sha256 != self.request_sha256
            or report.source.source_id != frozen.source_id
            or report.source.relative_path != frozen.source_relative_path
            or report.source.sha256 != frozen.source_sha256
            or report.source.file_identity_sha256 != frozen.source_file_identity_sha256
            or report.source.size_bytes != frozen.source_size_bytes
            or report.source.width != frozen.source_width
            or report.source.height != frozen.source_height
            or report.source.frame_count != frozen.source_frame_count
            or report.source.tracking_contract_relative_path != frozen.tracking_contract_relative_path
            or report.source.tracking_contract_sha256 != frozen.tracking_contract_sha256
        ):
            raise ValueError("detector probe report source lineage does not match frozen request")
        if (
            report.lineage.parent_trial_id != frozen.parent_trial_id
            or report.lineage.base_config_relative_path != frozen.base_config_relative_path
            or report.lineage.base_config_sha256 != frozen.base_config_sha256
            or report.lineage.effective_config_relative_path != frozen.effective_config_relative_path
            or report.lineage.effective_config_sha256 != frozen.effective_config_sha256
            or report.lineage.trial_intent_sha256 != frozen.trial_intent_sha256
            or report.lineage.tuning_patch_binding != frozen.tuning_patch_binding
            or report.lineage.tuning_patch_sha256 != frozen.tuning_patch_sha256
            or report.lineage.profile_sha256s != frozen.profile_sha256s
            or report.lineage.frozen_profiles_sha256 != frozen.frozen_profiles_sha256
            or report.lineage.execution_bundle != frozen.execution_bundle
            or report.lineage.execution_bundle_sha256 != frozen.execution_bundle_sha256
            or report.lineage.runtime_environment_sha256 != frozen.runtime_environment_sha256
            or report.lineage.intent_sha256 != self.intent_sha256
            or report.lineage.retry_from_job_id != self.retry_from_job_id
        ):
            raise ValueError("detector probe report lineage does not match frozen request")
        report_profile_digests = {profile.profile_id: profile.profile_sha256 for profile in report.frozen_profiles}
        if report_profile_digests != frozen.profile_sha256s:
            raise ValueError("report frozen profiles do not match frozen request")
        if (
            report.decode.width != frozen.source_width
            or report.decode.height != frozen.source_height
            or report.decode.frame_count != frozen.source_frame_count
            or report.decode.requested_decode_mode != frozen.requested_decode_mode
            or report.decode.verified_frame_indices != frozen.frame_indices
        ):
            raise ValueError("report decode evidence does not match frozen request")
        if (
            report.execution.device != frozen.execution_bundle.execution_environment.device
            or report.execution.precision != frozen.execution_bundle.execution_environment.precision
        ):
            raise ValueError("report execution evidence does not match frozen environment")
        if [frame.frame_index for frame in report.frames] != frozen.frame_indices:
            raise ValueError("report frame set does not match frozen request")
        artifacts = {artifact.artifact_id: artifact for artifact in report.artifacts}
        if len(artifacts) != len(report.artifacts):
            raise ValueError("detector probe report contains duplicate artifacts")
        referenced_artifact_ids: set[str] = set()
        for frame in report.frames:
            if (
                frame.source_width != frozen.source_width
                or frame.source_height != frozen.source_height
                or frame.requested_decode_mode != frozen.requested_decode_mode
            ):
                raise ValueError("probe frame evidence does not match frozen source")
            frame_profiles = {profile.profile_id: profile for profile in frame.profile_results}
            if list(frame_profiles) != frozen.profile_ids or any(
                frame_profiles[profile_id].profile_sha256 != frozen.profile_sha256s[profile_id]
                for profile_id in frozen.profile_ids
            ):
                raise ValueError("probe frame profiles do not match frozen profiles")
            source_artifact_id = _detector_probe_artifact_id(frame.source_artifact_url, self.job_id)
            source_artifact = artifacts.get(source_artifact_id)
            if (
                source_artifact is None
                or source_artifact.sha256 != frame.source_frame_sha256
                or source_artifact.size_bytes != frame.source_frame_size_bytes
                or source_artifact.width != frame.source_width
                or source_artifact.height != frame.source_height
            ):
                raise ValueError("source-frame artifact evidence does not match artifact manifest")
            referenced_artifact_ids.add(source_artifact_id)
            for profile in frame.profile_results:
                overlay_artifact_id = _detector_probe_artifact_id(profile.raw_overlay_artifact_url, self.job_id)
                overlay_artifact = artifacts.get(overlay_artifact_id)
                if (
                    overlay_artifact is None
                    or overlay_artifact.sha256 != profile.raw_overlay_sha256
                    or overlay_artifact.size_bytes != profile.raw_overlay_size_bytes
                    or overlay_artifact.width != frame.source_width
                    or overlay_artifact.height != frame.source_height
                ):
                    raise ValueError("overlay artifact evidence does not match artifact manifest")
                referenced_artifact_ids.add(overlay_artifact_id)
        if referenced_artifact_ids != set(artifacts):
            raise ValueError("detector probe artifact manifest contains unreferenced evidence")
        return self
