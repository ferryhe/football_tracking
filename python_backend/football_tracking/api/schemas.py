from __future__ import annotations

import math
from copy import deepcopy
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from football_tracking.ai_contracts import (
    AIApprovedActionName,
    AIClipAction,
    AIFailureTag,
    AIRecommendedAction,
    AIRootCauseModule,
)
from football_tracking.ball_frame_evidence import (
    BallFrameEvidenceError,
    build_detector_probe_inherited_evidence_authority,
    build_detector_probe_result_manifest_authority,
    validate_detector_probe_job_authority,
    verify_detector_probe_review_proxy_inheritance,
    verify_frame_evidence_package,
)
from football_tracking.detector_audited_authority import (
    AUDITED_T2_LEGACY_PROBE_BINDINGS as _AUDITED_T2_LEGACY_REPORT_BINDINGS,
)
from football_tracking.detector_development_common import canonical_sha256
from football_tracking.review_proxy_mapping import (
    ReviewProxyError,
    validate_review_proxy_manifest,
)

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
    code_commit_reason: Literal["code_bundle_differs_from_commit", "repository_commit_unavailable"] | None
    code_commit_blob_files: dict[str, Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]] | None
    code_commit_blob_bundle_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None
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
            "code_commit_blob_bundle_sha256": (self.code_commit_blob_bundle_sha256),
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
                or self.code_commit_binding_kind != "exact_or_crlf_to_lf_commit_blob"
            ):
                raise ValueError("bound code commit evidence is incomplete")
            if (
                set(self.code_commit_blob_files) != set(self.code_bundle_files)
                or canonical_sha256(self.code_commit_blob_files) != self.code_commit_blob_bundle_sha256
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


class DetectorProbeInheritedEvidenceView(DetectorProbeStrictView):
    parent_probe_job_id: DetectorSafeId
    parent_probe_request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent_probe_intent_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent_probe_semantic_intent_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent_probe_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent_probe_result_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent_probe_record_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent_execution_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent_runtime_environment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_frame_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class DetectorProbeRepairEvidenceView(DetectorProbeStrictView):
    schema_version: Literal["1.0"]
    repair_id: DetectorSafeId
    repair_request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    repair_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    repair_result_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    proxy_media_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    proxy_size_bytes: int = Field(strict=True, gt=0)
    repair_execution_binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    repair_code_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    repair_runtime_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    repair_decoder_fingerprint_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sampled_frame_sha256s: dict[
        Annotated[str, Field(pattern=r"^(?:0|[1-9][0-9]*)$")],
        Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")],
    ]


class DetectorProbeContinuationExecutionBindingView(DetectorProbeStrictView):
    schema_version: Literal["1.0"]
    artifact_type: Literal["detector_review_proxy_continuation_execution_binding"]
    code_files: dict[str, Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]]
    code_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime: dict[str, str]
    runtime_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_continuation_execution_binding(
        self,
    ) -> "DetectorProbeContinuationExecutionBindingView":
        if not self.code_files or canonical_sha256(self.code_files) != self.code_bundle_sha256:
            raise ValueError("continuation code-bundle digest changed")
        if not self.runtime or canonical_sha256(self.runtime) != self.runtime_sha256:
            raise ValueError("continuation runtime digest changed")
        payload = self.model_dump(mode="json")
        payload.pop("binding_sha256")
        if canonical_sha256(payload) != self.binding_sha256:
            raise ValueError("continuation execution binding digest changed")
        return self


class DetectorProbeReviewProxyUpgradeBindingView(DetectorProbeStrictView):
    schema_version: Literal["1.0"]
    retry_kind: Literal["review_proxy_decode_upgrade"]
    inherited_evidence: DetectorProbeInheritedEvidenceView
    repair_evidence: DetectorProbeRepairEvidenceView
    continuation_execution_binding: DetectorProbeContinuationExecutionBindingView
    binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_upgrade_binding(self) -> "DetectorProbeReviewProxyUpgradeBindingView":
        payload = self.model_dump(mode="json")
        payload.pop("binding_sha256")
        if canonical_sha256(payload) != self.binding_sha256:
            raise ValueError("review-proxy upgrade binding digest changed")
        if self.inherited_evidence.parent_probe_job_id == self.repair_evidence.repair_id:
            raise ValueError("review-proxy repair cannot reuse the parent job identity")
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
    annotation_sampling_manifest_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    retry_kind: Literal["review_proxy_decode_upgrade"] | None = None
    review_proxy_upgrade: DetectorProbeReviewProxyUpgradeBindingView | None = None

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
        if (self.retry_kind is None) != (self.review_proxy_upgrade is None):
            raise ValueError("review-proxy retry kind and authority must bind together")
        if self.review_proxy_upgrade is not None and (
            self.retry_from_job_id != self.review_proxy_upgrade.inherited_evidence.parent_probe_job_id
        ):
            raise ValueError("review-proxy retry does not bind its exact parent")
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
    filter_reasons: dict[str, Annotated[int, Field(strict=True, gt=0)]]
    failure_code: str | None
    raw_overlay_artifact_url: str
    raw_overlay_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    raw_overlay_size_bytes: int = Field(strict=True, gt=0)

    @model_validator(mode="after")
    def validate_profile_evidence_state(self) -> DetectorProbeProfileEvidenceView:
        deduplicated_count = self.candidate_count - self.filter_reasons.get("duplicate_suppressed_iou", 0)
        if (
            deduplicated_count < 0
            or len(self.raw_candidates) != min(deduplicated_count, 5)
            or self.filter_reasons.get("top_k_limit", 0) != max(0, deduplicated_count - 5)
        ):
            raise ValueError("candidate count/rejection accounting is inconsistent")
        expected_display = self.raw_candidates[0] if self.raw_candidates else None
        if self.display_candidate != expected_display:
            raise ValueError("display_candidate must be the exact first raw candidate")
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
    decoder_reported_pos_msec: float | None = Field(default=None, allow_inf_nan=False)
    decoder_timing_observation_method: (
        Literal[
            "opencv_cap_prop_pos_msec_after_verified_frame_read",
            "verified_review_proxy_frame_index_mapping_v1",
        ]
        | None
    ) = None
    media_integrity: DetectorProbeMediaIntegrityView
    source_artifact_url: str
    source_frame_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_frame_size_bytes: int = Field(strict=True, gt=0)
    proxy_artifact_url: str | None = None
    proxy_frame_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    proxy_frame_size_bytes: int | None = Field(default=None, strict=True, gt=0)
    profile_results: list[DetectorProbeProfileEvidenceView] = Field(min_length=2, max_length=6)

    @model_validator(mode="after")
    def validate_frame_evidence(self) -> DetectorProbeFrameEvidenceView:
        if self.decoded_frame_position != self.frame_index:
            raise ValueError("decoded frame position does not match frame_index")
        if self.media_integrity.width != self.source_width or self.media_integrity.height != self.source_height:
            raise ValueError("media integrity dimensions do not match source dimensions")
        if len({profile.profile_id for profile in self.profile_results}) != len(self.profile_results):
            raise ValueError("probe frame contains duplicate profiles")
        if (
            len(
                {
                    self.proxy_artifact_url is None,
                    self.proxy_frame_sha256 is None,
                    self.proxy_frame_size_bytes is None,
                }
            )
            != 1
        ):
            raise ValueError("proxy frame artifact binding is incomplete")
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
    semantic_intent_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    retry_from_job_id: DetectorSafeId | None
    retry_kind: Literal["review_proxy_decode_upgrade"] | None = None
    review_proxy_upgrade: DetectorProbeReviewProxyUpgradeBindingView | None = None


class DetectorProbeDecodeView(DetectorProbeStrictView):
    width: int = Field(strict=True, gt=0)
    height: int = Field(strict=True, gt=0)
    frame_count: int = Field(strict=True, gt=0)
    fps: float = Field(gt=0)
    requested_decode_mode: Literal["sequential", "preroll", "direct"]
    effective_decode_mode: Literal["sequential", "preroll_verified", "direct_verified", "sequential_fallback"]
    verified_frame_indices: list[DetectorFrameIndex] = Field(min_length=1, max_length=50)
    position_verification: Literal[
        "opencv_next_frame_index_with_0.25_tolerance",
        "verified_review_proxy_frame_index_mapping_v1",
    ]
    frame_timing_observations: list["DetectorProbeFrameTimingObservationView"] | None = Field(
        default=None,
        min_length=1,
        max_length=50,
    )


class DetectorProbeFrameTimingObservationView(DetectorProbeStrictView):
    frame_index: DetectorFrameIndex
    decoder_reported_pos_msec: float = Field(allow_inf_nan=False)
    observation_method: Literal[
        "opencv_cap_prop_pos_msec_after_verified_frame_read",
        "verified_review_proxy_frame_index_mapping_v1",
    ]


class DetectorProbeExecutionView(DetectorProbeStrictView):
    device: Literal["cpu", "cuda:0"]
    precision: Literal["fp32"]


class DetectorReviewProxyMediaView(DetectorProbeStrictView):
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(strict=True, gt=0)
    width: int = Field(strict=True, gt=0)
    height: int = Field(strict=True, gt=0)
    fps: float = Field(gt=0, allow_inf_nan=False)
    frame_count: int = Field(strict=True, gt=0)
    codec: str = Field(min_length=1)


class DetectorReviewProxySourceView(DetectorReviewProxyMediaView):
    file_identity_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class DetectorReviewProxyMappingIntegrityView(DetectorProbeStrictView):
    status: Literal["ok"]
    gray: Literal[False]
    low_information: Literal[False]
    likely_corrupt: Literal[False]


class DetectorReviewProxyMappingView(DetectorProbeStrictView):
    source_frame_index: DetectorFrameIndex
    source_timing_status: Literal["observed", "not_collected"]
    source_decoder_pos_msec: float | None = Field(default=None, allow_inf_nan=False)
    proxy_frame_index: DetectorFrameIndex
    proxy_timing_basis: Literal["verified_cfr_frame_index_time_v1"]
    proxy_cfr_time_msec: float = Field(allow_inf_nan=False)
    source_frame_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    proxy_frame_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    media_integrity: DetectorReviewProxyMappingIntegrityView

    @model_validator(mode="after")
    def validate_source_timing_status(self) -> "DetectorReviewProxyMappingView":
        if (self.source_timing_status == "not_collected") != (self.source_decoder_pos_msec is None):
            raise ValueError("review-proxy source timing status is inconsistent")
        return self


class DetectorReviewProxyCoordinateTransformView(DetectorProbeStrictView):
    kind: Literal["uniform_source_to_proxy_scale_v1"]
    scale_x: float = Field(gt=0, allow_inf_nan=False)
    scale_y: float = Field(gt=0, allow_inf_nan=False)
    source_origin: tuple[Literal[0.0], Literal[0.0]]
    proxy_origin: tuple[Literal[0.0], Literal[0.0]]


class DetectorReviewProxyManifestView(DetectorProbeStrictView):
    schema_version: Literal["1.0"]
    artifact_type: Literal["ball_review_proxy"]
    source: DetectorReviewProxySourceView
    proxy: DetectorReviewProxyMediaView
    decoder_fingerprint_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    requested_decode_mode: Literal["sequential", "preroll", "direct"]
    effective_decode_mode: Literal[
        "sequential",
        "preroll_verified",
        "direct_verified",
        "sequential_fallback",
    ]
    map_time_tolerance_msec: float = Field(ge=0, allow_inf_nan=False)
    declared_offset_msec: float = Field(allow_inf_nan=False)
    coordinate_transform: DetectorReviewProxyCoordinateTransformView
    expected_frame_indices: list[DetectorFrameIndex] = Field(min_length=1, max_length=50)
    mappings: list[DetectorReviewProxyMappingView] = Field(min_length=1, max_length=50)
    mapping_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    integrity_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


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
    review_proxy_manifest: DetectorReviewProxyManifestView | None = None
    execution: DetectorProbeExecutionView
    artifacts: list[DetectorProbeArtifactView] = Field(min_length=3, max_length=400)
    created_at: str
    report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="before")
    @classmethod
    def validate_timing_contract_generation(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        binding = _AUDITED_T2_LEGACY_REPORT_BINDINGS.get(value.get("job_id"))
        lineage = value.get("lineage")
        legacy = (
            binding is not None
            and value.get("report_sha256") == binding["report_sha256"]
            and isinstance(lineage, dict)
            and lineage.get("execution_bundle_sha256") == binding["execution_bundle_sha256"]
        )
        frames = value.get("frames")
        decode = value.get("decode")
        if legacy:
            if (
                "review_proxy_manifest" in value
                or not isinstance(frames, list)
                or any(
                    not isinstance(frame, dict)
                    or "decoder_reported_pos_msec" in frame
                    or "decoder_timing_observation_method" in frame
                    for frame in frames
                )
                or not isinstance(decode, dict)
                or "frame_timing_observations" in decode
            ):
                raise ValueError("audited legacy T2 report timing shape changed")
            return value
        proxy_manifest = value.get("review_proxy_manifest")
        proxy_mappings = proxy_manifest.get("mappings") if isinstance(proxy_manifest, dict) else None
        proxy_source_timing_not_collected = bool(
            isinstance(proxy_mappings, list)
            and proxy_mappings
            and all(
                isinstance(mapping, dict)
                and mapping.get("source_timing_status") == "not_collected"
                and mapping.get("source_decoder_pos_msec") is None
                for mapping in proxy_mappings
            )
        )
        if proxy_source_timing_not_collected:
            if (
                not isinstance(frames, list)
                or any(
                    not isinstance(frame, dict)
                    or frame.get("decoder_reported_pos_msec") is not None
                    or frame.get("decoder_timing_observation_method") is not None
                    for frame in frames
                )
                or not isinstance(decode, dict)
                or decode.get("frame_timing_observations") is not None
            ):
                raise ValueError("proxy child must preserve absent historical source timing")
            return value
        if (
            "review_proxy_manifest" not in value
            or not isinstance(frames, list)
            or any(
                not isinstance(frame, dict)
                or frame.get("decoder_reported_pos_msec") is None
                or frame.get("decoder_timing_observation_method") is None
                for frame in frames
            )
            or not isinstance(decode, dict)
            or decode.get("frame_timing_observations") is None
        ):
            raise ValueError("current T2 report requires complete decoder timing evidence")
        return value


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
    semantic_intent_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
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
            or report.lineage.semantic_intent_sha256 != self.semantic_intent_sha256
            or report.lineage.retry_from_job_id != self.retry_from_job_id
            or report.lineage.retry_kind != frozen.retry_kind
            or report.lineage.review_proxy_upgrade != frozen.review_proxy_upgrade
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
        timing_observations = report.decode.frame_timing_observations
        legacy_binding = _AUDITED_T2_LEGACY_REPORT_BINDINGS.get(report.job_id)
        legacy = (
            legacy_binding is not None
            and report.report_sha256 == legacy_binding["report_sha256"]
            and report.lineage.execution_bundle_sha256 == legacy_binding["execution_bundle_sha256"]
        )
        proxy_source_timing_not_collected = bool(
            report.review_proxy_manifest is not None
            and report.review_proxy_manifest.mappings
            and all(
                mapping.source_timing_status == "not_collected" and mapping.source_decoder_pos_msec is None
                for mapping in report.review_proxy_manifest.mappings
            )
        )
        if legacy:
            if timing_observations is not None or any(
                frame.decoder_reported_pos_msec is not None or frame.decoder_timing_observation_method is not None
                for frame in report.frames
            ):
                raise ValueError("audited legacy T2 report timing shape changed")
            paired_frames = ((frame, None) for frame in report.frames)
        elif proxy_source_timing_not_collected:
            if timing_observations is not None or any(
                frame.decoder_reported_pos_msec is not None or frame.decoder_timing_observation_method is not None
                for frame in report.frames
            ):
                raise ValueError("proxy child cannot invent inherited source decoder timing")
            paired_frames = ((frame, None) for frame in report.frames)
        else:
            if (
                timing_observations is None
                or [observation.frame_index for observation in timing_observations] != frozen.frame_indices
            ):
                raise ValueError("report frame timing set does not match frozen request")
            paired_frames = zip(report.frames, timing_observations, strict=True)
        artifacts = {artifact.artifact_id: artifact for artifact in report.artifacts}
        if len(artifacts) != len(report.artifacts):
            raise ValueError("detector probe report contains duplicate artifacts")
        referenced_artifact_ids: set[str] = set()
        proxy_mappings = (
            {mapping.source_frame_index: mapping for mapping in report.review_proxy_manifest.mappings}
            if report.review_proxy_manifest is not None
            else {}
        )
        for frame, timing in paired_frames:
            if (
                frame.source_width != frozen.source_width
                or frame.source_height != frozen.source_height
                or frame.requested_decode_mode != frozen.requested_decode_mode
                or (
                    timing is not None
                    and (
                        timing.frame_index != frame.frame_index
                        or timing.decoder_reported_pos_msec != frame.decoder_reported_pos_msec
                        or timing.observation_method != frame.decoder_timing_observation_method
                    )
                )
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
            proxy_mapping = proxy_mappings.get(frame.frame_index)
            if proxy_mapping is None:
                if (
                    frame.proxy_artifact_url is not None
                    or frame.proxy_frame_sha256 is not None
                    or frame.proxy_frame_size_bytes is not None
                ):
                    raise ValueError("direct probe frame cannot publish proxy evidence")
            else:
                if (
                    frame.proxy_artifact_url is None
                    or frame.proxy_frame_sha256 != proxy_mapping.proxy_frame_sha256
                    or frame.proxy_frame_size_bytes is None
                ):
                    raise ValueError("proxy frame mapping is not bound to retained evidence")
                proxy_artifact_id = _detector_probe_artifact_id(frame.proxy_artifact_url, self.job_id)
                proxy_artifact = artifacts.get(proxy_artifact_id)
                if (
                    proxy_artifact is None
                    or proxy_artifact.sha256 != frame.proxy_frame_sha256
                    or proxy_artifact.size_bytes != frame.proxy_frame_size_bytes
                    or proxy_artifact.width != report.review_proxy_manifest.proxy.width
                    or proxy_artifact.height != report.review_proxy_manifest.proxy.height
                ):
                    raise ValueError("proxy-frame artifact evidence does not match its mapping")
                referenced_artifact_ids.add(proxy_artifact_id)
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


DetectorReviewProxyRepairStatus = Literal[
    "queued",
    "running",
    "committing",
    "ready",
    "failed",
    "blocked",
    "cancelled",
]
DetectorReviewProxyRepairStage = Literal[
    "proxy_queued",
    "queued",
    "running",
    "verifying_source",
    "transcoding",
    "independent_verification",
    "recovered_after_restart",
    "proxy_committing",
    "proxy_ready",
    "continuation_intent",
    "child_probe_ready",
    "replacement_session_ready",
    "groups_published",
    "ready",
    "failed",
    "blocked",
    "cancelled",
]
DetectorReviewProxyRecoveryAction = Literal["retry", "resume"]

_DETECTOR_REVIEW_PROXY_STAGE_RANKS = {
    "proxy_queued": 0,
    "queued": 0,
    "running": 0,
    "verifying_source": 0,
    "transcoding": 0,
    "independent_verification": 0,
    "recovered_after_restart": 0,
    "proxy_committing": 0,
    "failed": 0,
    "blocked": 0,
    "cancelled": 0,
    "proxy_ready": 1,
    "continuation_intent": 2,
    "child_probe_ready": 3,
    "replacement_session_ready": 4,
    "groups_published": 5,
    "ready": 6,
}
_DETECTOR_REVIEW_PROXY_PRE_SIDE_EFFECT_RETRYABLE_CODES = frozenset(
    {
        "cancelled",
        "continuation_child_plan_changed",
        "path_unavailable",
        "repair_execution_binding_changed",
        "review_proxy_child_terminal",
        "review_proxy_continuation_failed",
        "review_proxy_failed",
        "review_proxy_worker_died",
        "review_proxy_worker_timeout",
        "service_shutting_down",
        "source_changed",
    }
)
_DETECTOR_REVIEW_PROXY_SAME_ATTEMPT_RESUMABLE_CODES = frozenset(
    {
        "invalid_review_proxy_repair_evidence",
    }
)


class DetectorReviewProxyRepairCreateRequest(DetectorProbeStrictView):
    model_config = ConfigDict(extra="forbid", strict=True)

    blocked_session_id: DetectorSafeId


class DetectorReviewProxyRepairRetryRequest(DetectorProbeStrictView):
    model_config = ConfigDict(extra="forbid", strict=True)


class DetectorReviewProxyRepairEligibilityView(DetectorProbeStrictView):
    eligible: Literal[True]
    action: Literal["generate_verified_review_proxy"]
    blocker_code: Literal["review_proxy_required"]


class DetectorReviewProxyRepairAuthorityView(DetectorProbeStrictView):
    blocked_session_id: DetectorSafeId
    blocked_session_request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    blocked_session_record_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent_probe_job_id: DetectorSafeId
    development_probe_job_ids: list[DetectorSafeId] = Field(min_length=1, max_length=7)
    parent_probe_request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent_probe_intent_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent_probe_semantic_intent_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent_probe_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent_probe_result_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent_probe_record_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent_execution_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent_runtime_environment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_frame_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_id: DetectorSafeId
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_file_identity_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_size_bytes: int = Field(strict=True, gt=0)
    source_width: int = Field(strict=True, gt=0)
    source_height: int = Field(strict=True, gt=0)
    source_frame_count: int = Field(strict=True, gt=0)
    source_fps: float = Field(gt=0, allow_inf_nan=False)
    locked_profile_id: DetectorSafeId
    locked_profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    frame_indices: list[DetectorFrameIndex] = Field(min_length=1, max_length=50)
    sampling_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    temporal_groups_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    replacement_request_authority_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("frame_indices")
    @classmethod
    def validate_repair_frame_indices(cls, values: list[int]) -> list[int]:
        if values != sorted(set(values)):
            raise ValueError("repair frame_indices must be sorted and unique")
        return values

    @model_validator(mode="after")
    def validate_repair_authority(self) -> "DetectorReviewProxyRepairAuthorityView":
        if (
            any(index >= self.source_frame_count for index in self.frame_indices)
            or len(set(self.development_probe_job_ids)) != len(self.development_probe_job_ids)
            or self.development_probe_job_ids[-1] != self.parent_probe_job_id
        ):
            raise ValueError("repair frame_indices must exist in the source")
        return self


class DetectorReviewProxyRepairProgressView(DetectorProbeStrictView):
    stage_completed: int = Field(strict=True, ge=0, le=6)
    stage_total: Literal[6]
    source_frames_completed: int = Field(strict=True, ge=0)
    source_frames_total: int = Field(strict=True, gt=0)
    updated_at: str

    @model_validator(mode="after")
    def validate_repair_progress(self) -> "DetectorReviewProxyRepairProgressView":
        if self.stage_completed > self.stage_total or self.source_frames_completed > self.source_frames_total:
            raise ValueError("repair progress exceeds its frozen work")
        return self


class DetectorReviewProxyRepairProxyResultView(DetectorProbeStrictView):
    review_proxy_id: DetectorSafeId
    review_proxy_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    proxy_media_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    proxy_size_bytes: int = Field(strict=True, gt=0)
    proxy_width: Literal[2560]
    proxy_height: Literal[720]
    proxy_frame_count: int = Field(strict=True, gt=0)
    proxy_fps: float = Field(gt=0, allow_inf_nan=False)
    mapping_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sampled_artifact_count: int = Field(strict=True, gt=0, le=50)
    encoder_binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    repair_execution_binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    repair_code_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    repair_runtime_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    repair_decoder_fingerprint_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class DetectorReviewProxyRepairChildProbeView(DetectorProbeStrictView):
    job_id: DetectorSafeId
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    intent_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    semantic_intent_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    resource_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    frozen_profiles_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    result_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_environment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    continuation_execution_binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    continuation_code_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    continuation_runtime_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    retry_from_job_id: DetectorSafeId
    retry_kind: Literal["review_proxy_decode_upgrade"]
    status_url: str
    report_url: str

    @model_validator(mode="after")
    def validate_child_probe_urls(self) -> "DetectorReviewProxyRepairChildProbeView":
        base = f"/api/v1/detector-probes/{self.job_id}"
        if self.status_url != base or self.report_url != base:
            raise ValueError("child probe URLs do not bind the exact child job")
        return self


class DetectorReviewProxyRepairSessionView(DetectorProbeStrictView):
    session_id: DetectorSafeId
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["annotating"]
    retry_from_session_id: DetectorSafeId
    retry_mode: Literal["review_proxy_decode_upgrade"]
    attempt_family_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    development_probe_job_ids: list[DetectorSafeId] = Field(min_length=2, max_length=8)
    status_url: str

    @model_validator(mode="after")
    def validate_replacement_session_url(self) -> "DetectorReviewProxyRepairSessionView":
        if self.status_url != f"/api/v1/ball-annotation-sessions/{self.session_id}":
            raise ValueError("replacement session URL does not bind the exact session")
        return self


class DetectorReviewProxyRepairResultView(DetectorProbeStrictView):
    proxy: DetectorReviewProxyRepairProxyResultView
    child_probe: DetectorReviewProxyRepairChildProbeView
    replacement_session: DetectorReviewProxyRepairSessionView
    parent_probe_record_sha256_after: str = Field(pattern=r"^[0-9a-f]{64}$")


class DetectorReviewProxyRepairJobResponse(DetectorProbeStrictView):
    schema_version: Literal["1.0"]
    artifact_type: Literal["detector_review_proxy_repair_job"]
    repair_id: DetectorSafeId
    attempt_root_repair_id: DetectorSafeId
    attempt_number: int = Field(strict=True, ge=1)
    retry_from_repair_id: DetectorSafeId | None
    idempotency_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: DetectorReviewProxyRepairStatus
    stage: DetectorReviewProxyRepairStage
    preset_id: Literal["h264-cfr-720p-v1"]
    eligibility: DetectorReviewProxyRepairEligibilityView
    authority: DetectorReviewProxyRepairAuthorityView
    progress: DetectorReviewProxyRepairProgressView
    can_cancel: StrictBool
    can_retry: StrictBool
    result: DetectorReviewProxyRepairResultView | None
    error_code: str | None
    blocker_code: str | None
    recovery_action: DetectorReviewProxyRecoveryAction | None
    created_at: str
    updated_at: str
    status_url: str
    cancel_url: str
    retry_url: str

    @model_validator(mode="after")
    def validate_repair_job(self) -> "DetectorReviewProxyRepairJobResponse":
        base_url = f"/api/v1/detector-review-proxy-repairs/{self.repair_id}"
        if (
            self.status_url != base_url
            or self.cancel_url != f"{base_url}/cancel"
            or self.retry_url != f"{base_url}/retry"
        ):
            raise ValueError("repair control URLs do not match repair_id")
        if (
            self.attempt_number == 1
            and (self.attempt_root_repair_id != self.repair_id or self.retry_from_repair_id is not None)
            or self.attempt_number > 1
            and (
                self.attempt_root_repair_id == self.repair_id
                or self.retry_from_repair_id is None
                or self.retry_from_repair_id == self.repair_id
            )
        ):
            raise ValueError("repair retry lineage is inconsistent")
        if self.idempotency_key != self.request_sha256:
            raise ValueError("repair idempotency key does not match request digest")
        if self.progress.source_frames_total != self.authority.source_frame_count:
            raise ValueError("repair progress does not match source authority")
        rank = _DETECTOR_REVIEW_PROXY_STAGE_RANKS[self.stage]
        if (
            self.progress.stage_total != 6
            or self.progress.stage_completed != rank
            or self.progress.updated_at != self.updated_at
            or (rank >= 1 and self.progress.source_frames_completed != self.progress.source_frames_total)
        ):
            raise ValueError("repair progress does not match its durable stage")
        status_stage_valid = (
            self.status == "queued"
            and self.stage in {"proxy_queued", "queued", "recovered_after_restart"}
            or self.status == "running"
            and self.stage
            in {
                "queued",
                "running",
                "verifying_source",
                "transcoding",
                "independent_verification",
                "recovered_after_restart",
            }
            or self.status == "committing"
            and (self.stage == "proxy_committing" or 1 <= rank <= 5)
            or self.status == "ready"
            and self.stage == "ready"
            or self.status == "failed"
            and (self.stage == "failed" or 1 <= rank <= 5)
            or self.status == "blocked"
            and (self.stage == "blocked" or 1 <= rank <= 5)
            or self.status == "cancelled"
            and self.stage == "cancelled"
        )
        if not status_stage_valid:
            raise ValueError("repair status and durable stage are inconsistent")
        if self.can_cancel != (self.status in {"queued", "running"}):
            raise ValueError("repair cancellation state is inconsistent")
        if self.status == "ready":
            if self.result is None:
                raise ValueError("ready repair is missing committed continuation evidence")
            if (
                self.result.parent_probe_record_sha256_after != self.authority.parent_probe_record_sha256
                or self.result.child_probe.retry_from_job_id != self.authority.parent_probe_job_id
                or self.result.replacement_session.retry_from_session_id != self.authority.blocked_session_id
                or self.result.replacement_session.development_probe_job_ids
                != [
                    *self.authority.development_probe_job_ids,
                    self.result.child_probe.job_id,
                ]
                or self.result.proxy.proxy_frame_count != self.authority.source_frame_count
                or not math.isclose(
                    self.result.proxy.proxy_fps,
                    self.authority.source_fps,
                    rel_tol=0.0,
                    abs_tol=1e-9,
                )
                or self.result.proxy.sampled_artifact_count != len(self.authority.frame_indices)
            ):
                raise ValueError("repair result does not match frozen authority")
        elif self.result is not None:
            raise ValueError("non-ready repair cannot publish continuation evidence")
        active = self.status in {"queued", "running", "committing"}
        terminal_failure = self.status in {"failed", "blocked"}
        retryable_failure = bool(
            terminal_failure and rank <= 2 and self.error_code in _DETECTOR_REVIEW_PROXY_PRE_SIDE_EFFECT_RETRYABLE_CODES
        )
        resumable_failure = bool(
            terminal_failure
            and (
                3 <= rank <= 5
                or rank in {1, 2}
                and self.error_code in _DETECTOR_REVIEW_PROXY_SAME_ATTEMPT_RESUMABLE_CODES
            )
        )
        expected_can_retry = self.status == "cancelled" or retryable_failure
        expected_recovery_action = "resume" if resumable_failure else "retry" if retryable_failure else None
        if (
            self.can_retry is not expected_can_retry
            or self.recovery_action != expected_recovery_action
            or active
            and (self.result is not None or self.error_code is not None or self.blocker_code is not None)
            or self.status == "ready"
            and (
                self.error_code is not None
                or self.blocker_code is not None
                or self.recovery_action is not None
                or self.can_cancel
                or self.can_retry
                or self.progress.source_frames_completed != self.progress.source_frames_total
            )
            or self.status == "cancelled"
            and (
                self.error_code is not None
                or self.blocker_code is not None
                or self.recovery_action is not None
                or self.result is not None
                or self.can_cancel
            )
            or terminal_failure
            and (
                not isinstance(self.error_code, str)
                or not self.error_code
                or (self.status == "failed" and self.blocker_code is not None)
                or (
                    self.status == "blocked"
                    and (
                        not isinstance(self.blocker_code, str)
                        or not self.blocker_code
                        or self.blocker_code != self.error_code
                    )
                )
            )
        ):
            raise ValueError("repair failure and recovery lifecycle is inconsistent")
        return self


BallSha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
BallDataRole = Literal["development", "check"]
BallAnnotationSessionStatus = Literal[
    "sampling_locked",
    "check_probe_queued",
    "check_probe_running",
    "check_probe_committing",
    "annotating",
    "finalizing",
    "blocked",
    "finalized",
]


class BallAnnotationStrictView(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def _canonical_ball_propagation_report(
    report: "BallSealedPropagationReportView",
    *,
    include_report_sha256: bool,
) -> dict[str, Any]:
    payload = report.model_dump(mode="json")
    if not include_report_sha256:
        payload.pop("report_sha256")
    for collection_name in ("frame_results", "suggestions"):
        for row in payload[collection_name]:
            if row["human_confirmation"] is None:
                row.pop("human_confirmation")
            if row["human_decision"] is None:
                row.pop("human_decision")
    return payload


def _canonical_ball_sampling_manifest(
    manifest: dict[str, Any],
) -> dict[str, Any]:
    for field_name in ("selection_authority", "candidate_universe_authority"):
        if manifest.get(field_name) is None:
            manifest.pop(field_name, None)
    for collection_name in ("groups", "excluded_development_groups"):
        for group in manifest[collection_name]:
            if group.get("pre_reveal_lighting_stratum") is None:
                group.pop("pre_reveal_lighting_stratum", None)
    return manifest


class BallApiErrorDetail(BallAnnotationStrictView):
    code: str
    message: str


class BallApiErrorResponse(BallAnnotationStrictView):
    detail: BallApiErrorDetail


class BallScaleApplicabilityRequest(BallAnnotationStrictView):
    stratum: Literal["near", "mid", "far"]
    status: Literal["applicable", "not_applicable"]
    evidence_note: str = Field(min_length=3, max_length=500)


class BallFrameIntervalRequest(BallAnnotationStrictView):
    start_frame: int = Field(ge=0)
    end_frame: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_order(self) -> "BallFrameIntervalRequest":
        if self.end_frame < self.start_frame:
            raise ValueError("lighting frame interval end must not precede start")
        return self


class BallLightingApplicabilityRequest(BallAnnotationStrictView):
    stratum: Literal["bright_sun", "shadow", "backlight", "twilight", "artificial_light"]
    status: Literal["applicable", "not_applicable"]
    evidence_note: str = Field(min_length=3, max_length=500)
    quota: int = Field(
        ge=0,
        le=50,
        description=("Development/not-applicable uses 0; every applicable check lighting stratum requires at least 3."),
    )
    frame_intervals: list[BallFrameIntervalRequest] = Field(max_length=32)

    @model_validator(mode="after")
    def validate_sampling_authority(self) -> "BallLightingApplicabilityRequest":
        if self.status == "applicable" and self.quota == 0:
            if self.frame_intervals:
                raise ValueError("zero-quota lighting cannot declare frame intervals")
        elif self.status == "applicable" and not self.frame_intervals:
            raise ValueError("applicable lighting requires pre-reveal frame intervals")
        elif self.status == "not_applicable" and (self.quota != 0 or self.frame_intervals):
            raise ValueError("not-applicable lighting cannot receive quota or intervals")
        return self


class BallStrataApplicabilityRequest(BallAnnotationStrictView):
    scale: list[BallScaleApplicabilityRequest] = Field(min_length=3, max_length=3)
    lighting: list[BallLightingApplicabilityRequest] = Field(min_length=5, max_length=5)

    @model_validator(mode="after")
    def validate_complete_strata(self) -> "BallStrataApplicabilityRequest":
        if {row.stratum for row in self.scale} != {"near", "mid", "far"}:
            raise ValueError("scale applicability must declare near, mid and far exactly once")
        if {row.stratum for row in self.lighting} != {
            "bright_sun",
            "shadow",
            "backlight",
            "twilight",
            "artificial_light",
        }:
            raise ValueError("lighting applicability must declare every lighting stratum exactly once")
        if not any(row.status == "applicable" for row in self.scale) or not any(
            row.status == "applicable" for row in self.lighting
        ):
            raise ValueError("at least one scale and lighting stratum must be applicable")
        return self


class BallAnnotationSessionCreateRequest(BallAnnotationStrictView):
    model_config = ConfigDict(extra="forbid", strict=True)

    data_role: BallDataRole
    development_probe_job_ids: list[DetectorSafeId] = Field(min_length=1, max_length=8)
    locked_profile_id: DetectorSafeId
    target_frame_count: int | None = Field(default=None, ge=20, le=50)
    sampling_profile_id: Literal["tiny_ball_temporal_groups_v1"]
    metric_profile_id: Literal["tiny_ball_feasibility_metric_v1"]
    operator_id: DetectorSafeId
    strata_applicability: BallStrataApplicabilityRequest
    retry_from_session_id: DetectorSafeId | None = None
    development_package_session_id: DetectorSafeId | None = None
    development_package_sha256: BallSha256 | None = None

    @model_validator(mode="after")
    def validate_role_target(self) -> "BallAnnotationSessionCreateRequest":
        if len(set(self.development_probe_job_ids)) != len(self.development_probe_job_ids):
            raise ValueError("development_probe_job_ids must be unique")
        if self.data_role == "check" and self.target_frame_count is None:
            raise ValueError("check sessions require target_frame_count 20-50")
        if self.data_role == "development" and self.target_frame_count is not None:
            raise ValueError("development target_frame_count must be null")
        if self.data_role == "development" and (
            self.development_package_session_id is not None or self.development_package_sha256 is not None
        ):
            raise ValueError("development sessions cannot bind a development package")
        if self.data_role == "check" and (
            self.development_package_session_id is None or self.development_package_sha256 is None
        ):
            raise ValueError("check sessions require a finalized development package binding")
        lighting_quota = sum(row.quota for row in self.strata_applicability.lighting)
        if self.data_role == "development" and lighting_quota != 0:
            raise ValueError("development sessions do not freeze check lighting quotas")
        if self.data_role == "development" and any(row.frame_intervals for row in self.strata_applicability.lighting):
            raise ValueError("development sessions do not freeze check lighting intervals")
        if self.data_role == "check" and lighting_quota != self.target_frame_count:
            raise ValueError("check lighting quotas must sum to target_frame_count")
        if self.data_role == "check" and any(
            row.status == "applicable" and (row.quota < 3 or not row.frame_intervals)
            for row in self.strata_applicability.lighting
        ):
            raise ValueError("every applicable check lighting stratum requires quota >= 3 and intervals")
        return self


class BallPointSourcePx(BallAnnotationStrictView):
    x: float = Field(ge=0, allow_inf_nan=False)
    y: float = Field(ge=0, allow_inf_nan=False)


class BallBoxSourcePx(BallAnnotationStrictView):
    left: float = Field(ge=0, allow_inf_nan=False)
    top: float = Field(ge=0, allow_inf_nan=False)
    right: float = Field(gt=0, allow_inf_nan=False)
    bottom: float = Field(gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_geometry(self) -> "BallBoxSourcePx":
        if self.right <= self.left or self.bottom <= self.top:
            raise ValueError("source-pixel box must have positive area")
        return self


class BallAnnotationPayload(BallAnnotationStrictView):
    point_source_px: BallPointSourcePx | None
    bbox_source_px: BallBoxSourcePx | None
    presence: Literal["present", "absent", "unknown"]
    visibility: Literal["visible", "partial", "unresolvable", "not_applicable"]
    training_use: Literal["positive", "background", "excluded"]
    annotation_state: Literal["suggested", "confirmed"]
    scale_stratum: Literal["near", "mid", "far", "not_applicable"]
    lighting_tag: Literal[
        "bright_sun",
        "shadow",
        "backlight",
        "twilight",
        "artificial_light",
        "not_applicable",
    ]
    motion_occlusion_tags: list[
        Literal[
            "ground",
            "airborne",
            "motion_blurred",
            "occluded",
            "reappearance",
            "stationary",
        ]
    ] = Field(max_length=6)
    provenance: Literal[
        "manual_human_annotation",
        "detector_candidate_human_confirmed",
        "propagation_suggestion_human_confirmed",
        "suggestion_dismissed_manual",
    ]

    @model_validator(mode="after")
    def validate_annotation_state(self) -> "BallAnnotationPayload":
        if len(self.motion_occlusion_tags) != len(set(self.motion_occlusion_tags)):
            raise ValueError("motion_occlusion_tags must be unique")
        if self.point_source_px is not None and self.bbox_source_px is not None:
            center_x = (self.bbox_source_px.left + self.bbox_source_px.right) / 2.0
            center_y = (self.bbox_source_px.top + self.bbox_source_px.bottom) / 2.0
            if abs(self.point_source_px.x - center_x) > 0.5 or abs(self.point_source_px.y - center_y) > 0.5:
                raise ValueError("point and bounding-box centers are inconsistent")
        if self.annotation_state == "suggested" and self.training_use != "excluded":
            raise ValueError("suggested annotations cannot be training truth")
        if self.presence == "absent":
            if (
                self.point_source_px is not None
                or self.bbox_source_px is not None
                or self.visibility != "not_applicable"
                or self.training_use not in {"background", "excluded"}
                or self.scale_stratum != "not_applicable"
            ):
                raise ValueError("absent annotation fields are inconsistent")
        elif self.presence == "unknown":
            if (
                self.point_source_px is not None
                or self.bbox_source_px is not None
                or self.visibility not in {"unresolvable", "not_applicable"}
                or self.training_use != "excluded"
                or self.scale_stratum != "not_applicable"
            ):
                raise ValueError("unknown annotation fields are inconsistent")
        elif self.visibility == "unresolvable":
            if (
                self.point_source_px is not None
                or self.bbox_source_px is not None
                or self.training_use != "excluded"
                or self.scale_stratum != "not_applicable"
            ):
                raise ValueError("unresolvable annotation fields are inconsistent")
        else:
            if (
                self.visibility not in {"visible", "partial"}
                or (self.point_source_px is None and self.bbox_source_px is None)
                or self.scale_stratum == "not_applicable"
                or self.training_use == "background"
            ):
                raise ValueError("localizable annotation fields are inconsistent")
            if self.training_use == "positive" and (
                self.annotation_state != "confirmed" or self.bbox_source_px is None
            ):
                raise ValueError("positive annotation requires a confirmed box")
        return self


class BallAnnotationRevisionRequest(BallAnnotationStrictView):
    model_config = ConfigDict(extra="forbid", strict=True)

    mutation_id: DetectorSafeId
    expected_revision: int = Field(ge=0)
    operation: Literal["set", "delete", "undo"]
    undo_revision: int | None = Field(default=None, gt=0)
    annotation: BallAnnotationPayload | None
    suggestion_kind: Literal["detector_candidate", "propagation"] | None = None
    suggestion_id: DetectorSafeId | None = None
    accepted_suggestion_job_id: DetectorSafeId | None = None
    accepted_suggestion_sha256: BallSha256 | None = None
    dismissed_suggestion_kind: Literal["detector_candidate", "propagation"] | None = None
    dismissed_suggestion_id: DetectorSafeId | None = None
    dismissed_suggestion_job_id: DetectorSafeId | None = None
    dismissed_suggestion_sha256: BallSha256 | None = None

    @model_validator(mode="after")
    def validate_operation_payload(self) -> "BallAnnotationRevisionRequest":
        if self.operation == "set" and self.annotation is None:
            raise ValueError("set requires annotation")
        if self.operation != "set" and self.annotation is not None:
            raise ValueError("delete/undo cannot carry annotation")
        if self.operation == "undo" and self.undo_revision is None:
            raise ValueError("undo requires undo_revision")
        if self.operation != "undo" and self.undo_revision is not None:
            raise ValueError("undo_revision is only valid for undo")
        accepted = (
            self.suggestion_kind is not None,
            self.suggestion_id is not None,
            self.accepted_suggestion_job_id is not None,
            self.accepted_suggestion_sha256 is not None,
        )
        dismissed = (
            self.dismissed_suggestion_kind is not None,
            self.dismissed_suggestion_id is not None,
            self.dismissed_suggestion_job_id is not None,
            self.dismissed_suggestion_sha256 is not None,
        )
        if any(accepted) and not all(accepted):
            raise ValueError("accepted suggestion kind, identity, job and digest must bind together")
        if any(dismissed) and not all(dismissed):
            raise ValueError("dismissed suggestion kind, identity, job and digest must bind together")
        if any(accepted) and any(dismissed):
            raise ValueError("one revision cannot accept and dismiss a suggestion")
        if self.operation != "set" and (any(accepted) or any(dismissed)):
            raise ValueError("suggestion decisions are only valid for set")
        if self.operation == "set" and self.annotation is not None:
            expected_provenance = (
                "detector_candidate_human_confirmed"
                if self.suggestion_kind == "detector_candidate"
                else "propagation_suggestion_human_confirmed"
                if self.suggestion_kind == "propagation"
                else "suggestion_dismissed_manual"
                if self.dismissed_suggestion_kind is not None
                else "manual_human_annotation"
            )
            if self.annotation.annotation_state != "confirmed" or self.annotation.provenance != expected_provenance:
                raise ValueError("annotation state or provenance conflicts with suggestion decision")
        return self


class BallSourceBindingView(BallAnnotationStrictView):
    source_id: DetectorSafeId
    sha256: BallSha256
    file_identity_sha256: BallSha256
    size_bytes: int = Field(gt=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    frame_count: int = Field(gt=0)
    tracking_contract_sha256: BallSha256
    relative_path: str
    tracking_contract_relative_path: str
    fps: float = Field(gt=0)


class BallProfileBindingView(BallAnnotationStrictView):
    profile_id: DetectorSafeId
    profile_sha256: BallSha256
    model_id: DetectorSafeId
    model_version: str
    model_descriptor_sha256: BallSha256
    weights_sha256: BallSha256


class BallDecodeBindingView(BallAnnotationStrictView):
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    frame_count: int = Field(gt=0)
    fps: float = Field(gt=0)
    requested_decode_mode: Literal["sequential", "preroll", "direct"]
    effective_decode_mode: Literal[
        "sequential",
        "preroll_verified",
        "direct_verified",
        "sequential_fallback",
    ]
    position_verification: Literal[
        "opencv_next_frame_index_with_0.25_tolerance",
        "verified_review_proxy_frame_index_mapping_v1",
    ]


class BallAnnotationLineageView(BallAnnotationStrictView):
    parent_trial_id: DetectorSafeId
    development_probe_job_ids: list[DetectorSafeId] = Field(min_length=1, max_length=8)
    development_probe_report_sha256s: dict[DetectorSafeId, BallSha256]
    development_probe_result_manifest_sha256s: dict[DetectorSafeId, BallSha256]
    development_probe_execution_bundle_sha256s: dict[DetectorSafeId, BallSha256]
    development_probe_frozen_profiles_sha256s: dict[DetectorSafeId, BallSha256]
    decode: BallDecodeBindingView
    runtime_environment_sha256: BallSha256

    @model_validator(mode="after")
    def validate_probe_bindings(self) -> "BallAnnotationLineageView":
        expected = set(self.development_probe_job_ids)
        if (
            set(self.development_probe_report_sha256s) != expected
            or set(self.development_probe_result_manifest_sha256s) != expected
            or set(self.development_probe_execution_bundle_sha256s) != expected
            or set(self.development_probe_frozen_profiles_sha256s) != expected
        ):
            raise ValueError("development probe lineage maps must bind every job exactly")
        return self


class BallSamplingTemporalGroupView(BallAnnotationStrictView):
    group_id: BallSha256
    profile_id: Literal["tiny_ball_temporal_groups_v1"]
    source_sha256: BallSha256
    seed_frame_index: int = Field(ge=0)
    start_frame: int = Field(ge=0)
    end_frame: int = Field(ge=0)
    derivative_family: list[int] = Field(min_length=2, max_length=2)
    canonical_moment_id: BallSha256
    derivative_family_id: BallSha256
    ancestry_profile: Literal["source-proxy-crop-tile-propagation-closure-v1"]
    frame_index: int = Field(ge=0)
    pre_reveal_lighting_stratum: Literal["bright_sun", "shadow", "backlight", "twilight", "artificial_light"] | None = (
        None
    )


class BallApplicabilityEvidenceView(BallAnnotationStrictView):
    declared_before_reveal: Literal[True]
    note: str = Field(min_length=3, max_length=500)
    evidence_sha256: BallSha256


class BallScaleApplicabilityView(BallAnnotationStrictView):
    stratum: Literal["near", "mid", "far"]
    status: Literal["applicable", "not_applicable"]
    evidence: BallApplicabilityEvidenceView


class BallLightingApplicabilityView(BallAnnotationStrictView):
    stratum: Literal["bright_sun", "shadow", "backlight", "twilight", "artificial_light"]
    status: Literal["applicable", "not_applicable"]
    quota: int = Field(ge=0, le=50)
    frame_intervals: list[BallFrameIntervalRequest] = Field(max_length=32)
    evidence: BallApplicabilityEvidenceView


class BallStrataApplicabilityView(BallAnnotationStrictView):
    scale: list[BallScaleApplicabilityView] = Field(min_length=3, max_length=3)
    lighting: list[BallLightingApplicabilityView] = Field(min_length=5, max_length=5)

    @model_validator(mode="after")
    def validate_complete_strata(self) -> "BallStrataApplicabilityView":
        if [row.stratum for row in self.scale] != ["near", "mid", "far"]:
            raise ValueError("scale applicability must use canonical stratum order")
        expected_lighting = [
            "bright_sun",
            "shadow",
            "backlight",
            "twilight",
            "artificial_light",
        ]
        if [row.stratum for row in self.lighting] != expected_lighting:
            raise ValueError("lighting applicability must use canonical stratum order")
        for row in self.lighting:
            if row.status == "not_applicable" and (row.quota != 0 or row.frame_intervals):
                raise ValueError("not-applicable lighting cannot receive sampling authority")
            if row.status == "applicable" and (row.quota == 0) != (not row.frame_intervals):
                raise ValueError("lighting quota and intervals must bind together")
        return self


class BallSamplingScaleAuthorityView(BallAnnotationStrictView):
    stratum: Literal["near", "mid", "far"]
    status: Literal["applicable", "not_applicable"]


class BallSamplingLightingAuthorityView(BallAnnotationStrictView):
    stratum: Literal["bright_sun", "shadow", "backlight", "twilight", "artificial_light"]
    status: Literal["applicable", "not_applicable"]
    quota: int = Field(ge=0, le=50)
    frame_intervals: list[BallFrameIntervalRequest] = Field(max_length=32)


class BallSamplingSelectionAuthorityView(BallAnnotationStrictView):
    schema_version: Literal["1.0"]
    artifact_type: Literal["ball_annotation_sampling_selection_authority"]
    attempt_family_sha256: BallSha256
    development_package_sha256: BallSha256
    source_sha256: BallSha256
    locked_profile_id: DetectorSafeId
    locked_profile_sha256: BallSha256
    sampling_profile_id: Literal["tiny_ball_temporal_groups_v1"]
    metric_profile_id: Literal["tiny_ball_feasibility_metric_v1"]
    metric_profile_sha256: BallSha256
    target_frame_count: int = Field(ge=20, le=50)
    scale_applicability: list[BallSamplingScaleAuthorityView] = Field(min_length=3, max_length=3)
    lighting_applicability: list[BallSamplingLightingAuthorityView] = Field(min_length=5, max_length=5)

    @model_validator(mode="after")
    def validate_canonical_authority(self) -> "BallSamplingSelectionAuthorityView":
        if [row.stratum for row in self.scale_applicability] != [
            "near",
            "mid",
            "far",
        ]:
            raise ValueError("selection scale authority is not canonical")
        if [row.stratum for row in self.lighting_applicability] != [
            "bright_sun",
            "shadow",
            "backlight",
            "twilight",
            "artificial_light",
        ]:
            raise ValueError("selection lighting authority is not canonical")
        return self


class BallCandidateUniverseLightingView(BallAnnotationStrictView):
    stratum: Literal["bright_sun", "shadow", "backlight", "twilight", "artificial_light"]
    quota: int = Field(ge=3, le=50)
    frame_intervals: list[BallFrameIntervalRequest] = Field(min_length=1, max_length=32)


class BallCandidateUniverseExcludedGroupView(BallAnnotationStrictView):
    group_id: BallSha256
    profile_id: Literal["tiny_ball_temporal_groups_v1"]
    source_sha256: BallSha256
    seed_frame_index: int = Field(ge=0)
    start_frame: int = Field(ge=0)
    end_frame: int = Field(ge=0)
    derivative_family: list[int] = Field(min_length=2, max_length=2)
    canonical_moment_id: BallSha256
    derivative_family_id: BallSha256
    ancestry_profile: Literal["source-proxy-crop-tile-propagation-closure-v1"]


class BallCandidateUniverseAuthorityView(BallAnnotationStrictView):
    schema_version: Literal["1.0"]
    artifact_type: Literal["ball_annotation_candidate_universe"]
    source_sha256: BallSha256
    start_frame: int = Field(ge=0)
    end_frame: int = Field(ge=0)
    candidate_frame_count: int = Field(gt=0)
    grouping_profile_id: Literal["tiny_ball_temporal_groups_v1"]
    selection_profile_id: Literal["tiny_ball_temporal_block_hash_v1"]
    lighting_strata: list[BallCandidateUniverseLightingView] = Field(min_length=1, max_length=5)
    excluded_temporal_groups: list[BallCandidateUniverseExcludedGroupView]

    @model_validator(mode="after")
    def validate_universe_bounds(self) -> "BallCandidateUniverseAuthorityView":
        if self.end_frame < self.start_frame:
            raise ValueError("candidate universe end precedes start")
        if self.candidate_frame_count != self.end_frame - self.start_frame + 1:
            raise ValueError("candidate universe count does not match bounds")
        expected_order = [
            name
            for name in (
                "bright_sun",
                "shadow",
                "backlight",
                "twilight",
                "artificial_light",
            )
            if name in {row.stratum for row in self.lighting_strata}
        ]
        if [row.stratum for row in self.lighting_strata] != expected_order:
            raise ValueError("candidate lighting authority is not canonical")
        if len(self.excluded_temporal_groups) != len({row.group_id for row in self.excluded_temporal_groups}):
            raise ValueError("candidate universe contains duplicate excluded groups")
        return self


class BallSamplingManifestView(BallAnnotationStrictView):
    schema_version: Literal["1.0"]
    artifact_type: Literal["ball_annotation_sampling_manifest"]
    profile_id: Literal["tiny_ball_temporal_groups_v1"]
    selection_profile_id: Literal["development_probe_frames_v1", "tiny_ball_temporal_block_hash_v1"]
    scale_stratification_mode: Literal["post_reveal_support_gate_only"]
    lighting_stratification_mode: Literal[
        "not_applicable_development_evidence",
        "predeclared_frame_intervals_and_quota_v1",
    ]
    selection_seed_sha256: BallSha256
    candidate_universe_sha256: BallSha256
    candidate_universe_start_frame: int = Field(ge=0)
    candidate_universe_end_frame: int = Field(ge=0)
    selection_authority: BallSamplingSelectionAuthorityView | None = None
    candidate_universe_authority: BallCandidateUniverseAuthorityView | None = None
    metric_profile_id: Literal["tiny_ball_feasibility_metric_v1"]
    metric_profile_sha256: BallSha256
    data_role: BallDataRole
    target_frame_count: int = Field(gt=0)
    frame_indices: list[int] = Field(min_length=1)
    groups: list[BallSamplingTemporalGroupView] = Field(min_length=1)
    excluded_development_groups: list[BallSamplingTemporalGroupView]
    locked_before_probe: bool
    source_sha256: BallSha256
    locked_profile_id: DetectorSafeId
    locked_profile_sha256: BallSha256
    strata_applicability: BallStrataApplicabilityView
    manifest_sha256: BallSha256

    @model_validator(mode="after")
    def validate_role_authority(self) -> "BallSamplingManifestView":
        if self.candidate_universe_end_frame < self.candidate_universe_start_frame:
            raise ValueError("candidate universe end precedes start")
        if (
            self.frame_indices != sorted(set(self.frame_indices))
            or len(self.frame_indices) != len(self.groups)
            or self.target_frame_count != len(self.frame_indices)
        ):
            raise ValueError("sampling frame set is not canonical")
        for frame_index, group in zip(self.frame_indices, self.groups, strict=True):
            if (
                group.frame_index != frame_index
                or group.seed_frame_index != frame_index
                or group.source_sha256 != self.source_sha256
                or not self.candidate_universe_start_frame <= frame_index <= self.candidate_universe_end_frame
            ):
                raise ValueError("sampling group does not match the frozen frame set")
        if self.data_role == "development":
            if (
                self.selection_profile_id != "development_probe_frames_v1"
                or self.lighting_stratification_mode != "not_applicable_development_evidence"
                or self.locked_before_probe
                or self.selection_authority is not None
                or self.candidate_universe_authority is not None
                or any(group.pre_reveal_lighting_stratum is not None for group in self.groups)
            ):
                raise ValueError("development sampling authority shape is invalid")
        else:
            selection = self.selection_authority
            universe = self.candidate_universe_authority
            if (
                self.selection_profile_id != "tiny_ball_temporal_block_hash_v1"
                or self.lighting_stratification_mode != "predeclared_frame_intervals_and_quota_v1"
                or not self.locked_before_probe
                or selection is None
                or universe is None
                or not 20 <= self.target_frame_count <= 50
                or any(group.pre_reveal_lighting_stratum is None for group in self.groups)
            ):
                raise ValueError("check sampling authority shape is invalid")
            assert selection is not None and universe is not None
            expected_scale = [{"stratum": row.stratum, "status": row.status} for row in self.strata_applicability.scale]
            expected_lighting = [
                {
                    "stratum": row.stratum,
                    "status": row.status,
                    "quota": row.quota,
                    "frame_intervals": [interval.model_dump(mode="json") for interval in row.frame_intervals],
                }
                for row in self.strata_applicability.lighting
            ]
            if (
                selection.source_sha256 != self.source_sha256
                or selection.locked_profile_id != self.locked_profile_id
                or selection.locked_profile_sha256 != self.locked_profile_sha256
                or selection.sampling_profile_id != self.profile_id
                or selection.metric_profile_id != self.metric_profile_id
                or selection.metric_profile_sha256 != self.metric_profile_sha256
                or selection.target_frame_count != self.target_frame_count
                or [row.model_dump(mode="json") for row in selection.scale_applicability] != expected_scale
                or [row.model_dump(mode="json") for row in selection.lighting_applicability] != expected_lighting
                or self.selection_seed_sha256 != canonical_sha256(selection.model_dump(mode="json"))
            ):
                raise ValueError("selection authority does not bind the manifest")
            expected_universe_lighting = [
                {
                    "stratum": row.stratum,
                    "quota": row.quota,
                    "frame_intervals": [interval.model_dump(mode="json") for interval in row.frame_intervals],
                }
                for row in self.strata_applicability.lighting
                if row.quota > 0
            ]
            if (
                universe.source_sha256 != self.source_sha256
                or universe.start_frame != self.candidate_universe_start_frame
                or universe.end_frame != self.candidate_universe_end_frame
                or [row.model_dump(mode="json") for row in universe.lighting_strata] != expected_universe_lighting
                or self.candidate_universe_sha256 != canonical_sha256(universe.model_dump(mode="json"))
            ):
                raise ValueError("candidate universe authority does not bind the manifest")
        manifest = self.model_dump(mode="json", exclude_none=True)
        manifest.pop("manifest_sha256")
        if self.manifest_sha256 != canonical_sha256(manifest):
            raise ValueError("sampling manifest digest does not match its contents")
        return self


class BallCheckProbeAuthorityView(BallAnnotationStrictView):
    job_id: DetectorSafeId
    request_sha256: BallSha256
    intent_sha256: BallSha256
    result_manifest_sha256: BallSha256
    report_sha256: BallSha256
    parent_trial_id: DetectorSafeId
    runtime_environment_sha256: BallSha256
    execution_bundle_sha256: BallSha256
    frozen_profiles_sha256: BallSha256
    locked_profile: BallProfileBindingView
    control_profile: BallProfileBindingView


BallFeasibilityStatus = Literal[
    "not_applicable",
    "insufficient_evidence",
    "feasibility_passed",
    "feasibility_failed",
]


class BallAnnotationFinalPackagePointerView(BallAnnotationStrictView):
    result_url: str
    manifest_sha256: BallSha256
    package_sha256: BallSha256
    report_sha256: BallSha256
    status: BallFeasibilityStatus


class BallTruePresentationTimestampView(BallAnnotationStrictView):
    status: Literal["not_collected"]
    value_seconds: None
    method: None


class BallSuggestedCandidateAuthorityView(BallAnnotationStrictView):
    candidate_id: DetectorSafeId
    profile_id: DetectorSafeId
    rank: int = Field(ge=1, le=5)
    bbox_source_px: BallBoxSourcePx
    confidence: float = Field(ge=0, le=1)
    annotation_state: Literal["suggested"]
    training_use: Literal["excluded"]
    truth_status: Literal["unconfirmed_suggestion"]
    suggestion_job_id: DetectorSafeId
    suggestion_sha256: BallSha256

    @model_validator(mode="after")
    def validate_suggestion_authority(self) -> "BallSuggestedCandidateAuthorityView":
        payload = self.model_dump(
            mode="json",
            exclude={"suggestion_job_id", "suggestion_sha256", "decision"},
        )
        if canonical_sha256(payload) != self.suggestion_sha256:
            raise ValueError("detector suggestion digest does not match its authority")
        return self


class BallSuggestedCandidateView(BallSuggestedCandidateAuthorityView):
    decision: Literal["pending", "accepted", "dismissed"]


class BallDetectorCandidateDecisionView(BallAnnotationStrictView):
    decision: Literal["accepted_human_annotation", "dismissed_manual_annotation"]
    revision_id: DetectorSafeId
    revision: int = Field(gt=0)
    operator_id: DetectorSafeId
    decided_at: str


class BallDetectorCandidateOriginView(BallAnnotationStrictView):
    probe_job_id: DetectorSafeId
    probe_report_sha256: BallSha256
    probe_result_manifest_sha256: BallSha256
    source_artifact_id: DetectorSafeId
    candidate_evidence_sha256: BallSha256


class BallDetectorCandidateReviewMediaView(BallAnnotationStrictView):
    probe_job_id: DetectorSafeId
    probe_report_sha256: BallSha256
    probe_result_manifest_sha256: BallSha256
    source_artifact_id: DetectorSafeId
    proxy_binding_sha256: BallSha256 | None


class BallDetectorCandidateEvidenceView(BallAnnotationStrictView):
    frame_index: int = Field(ge=0)
    candidate_origin: BallDetectorCandidateOriginView
    review_media: BallDetectorCandidateReviewMediaView
    candidate: BallSuggestedCandidateAuthorityView
    candidate_sha256: BallSha256
    decision: BallDetectorCandidateDecisionView | None

    @model_validator(mode="after")
    def validate_candidate_binding(self) -> "BallDetectorCandidateEvidenceView":
        if (
            self.candidate_origin.probe_job_id != self.candidate.suggestion_job_id
            or self.candidate_sha256 != self.candidate.suggestion_sha256
        ):
            raise ValueError("detector candidate digest does not match its contents")
        return self


class BallTemporalDerivativeView(BallAnnotationStrictView):
    artifact_type: Literal["propagation"]
    artifact_id: DetectorSafeId
    inheritance_rule: Literal["inherit-source-group-without-regrouping-v1"]


class BallInheritedTemporalGroupView(BallAnnotationStrictView):
    group_id: BallSha256
    profile_id: Literal["tiny_ball_temporal_groups_v1"]
    source_sha256: BallSha256
    seed_frame_index: int = Field(ge=0)
    start_frame: int = Field(ge=0)
    end_frame: int = Field(ge=0)
    derivative_family: list[int] = Field(min_length=2, max_length=2)
    canonical_moment_id: BallSha256
    derivative_family_id: BallSha256
    ancestry_profile: Literal["source-proxy-crop-tile-propagation-closure-v1"]
    derivative: BallTemporalDerivativeView
    derivative_binding_sha256: BallSha256


class BallPropagationSelfCheckView(BallAnnotationStrictView):
    match_score: float = Field(ge=-1, le=1)
    backward_match_score: float = Field(ge=-1, le=1)
    forward_backward_error_px: float = Field(ge=0)
    step_displacement_px: float = Field(ge=0)


class BallPropagationHumanConfirmationView(BallAnnotationStrictView):
    revision_id: DetectorSafeId
    revision: int = Field(gt=0)
    operator_id: DetectorSafeId
    center_error_px: float = Field(ge=0)
    iou: float | None = Field(default=None, ge=0, le=1)
    corrected: bool
    confirmed_at: str


class BallPropagationHumanDecisionView(BallAnnotationStrictView):
    decision: Literal["dismissed_manual_annotation"]
    revision_id: DetectorSafeId
    revision: int = Field(gt=0)
    operator_id: DetectorSafeId
    decided_at: str


class BallPropagationSuggestionView(BallAnnotationStrictView):
    suggestion_id: DetectorSafeId
    frame_index: int = Field(ge=0)
    temporal_group_id: BallSha256
    temporal_group: BallInheritedTemporalGroupView
    point_source_px: BallPointSourcePx | None
    bbox_source_px: BallBoxSourcePx | None
    presence: Literal["present"]
    visibility: Literal["visible", "partial"]
    training_use: Literal["excluded"]
    annotation_state: Literal["suggested"]
    provenance: Literal["tiny_ball_bounded_template_flow_v1"]
    source_frame_sha256: BallSha256
    self_check: BallPropagationSelfCheckView
    suggestion_job_id: DetectorSafeId
    suggestion_sha256: BallSha256
    pending_human_confirmation: bool
    human_confirmation: BallPropagationHumanConfirmationView | None = None
    human_decision: BallPropagationHumanDecisionView | None = None

    @model_validator(mode="after")
    def validate_human_decision(self) -> "BallPropagationSuggestionView":
        decided = self.human_confirmation is not None or self.human_decision is not None
        if self.human_confirmation is not None and self.human_decision is not None:
            raise ValueError("propagation suggestion cannot be confirmed and dismissed")
        if self.pending_human_confirmation == decided:
            raise ValueError("propagation suggestion decision state is inconsistent")
        payload = self.model_dump(
            mode="json",
            exclude={
                "suggestion_job_id",
                "suggestion_sha256",
                "pending_human_confirmation",
                "human_confirmation",
                "human_decision",
            },
        )
        if canonical_sha256(payload) != self.suggestion_sha256:
            raise ValueError("propagation suggestion digest does not match its authority")
        return self


class BallAnnotationFrameView(BallAnnotationStrictView):
    frame_index: int = Field(ge=0)
    source_frame_sha256: BallSha256
    source_frame_size_bytes: int = Field(gt=0)
    suggested_candidates: list[BallSuggestedCandidateView] = Field(max_length=5)
    source_timing_status: Literal["observed", "not_collected"]
    decoder_reported_pos_msec: float | None = Field(default=None, allow_inf_nan=False)
    decoder_time_seconds: float | None = Field(default=None, allow_inf_nan=False)
    display_time_seconds: float = Field(ge=0, allow_inf_nan=False)
    true_presentation_timestamp: BallTruePresentationTimestampView
    proxy_binding: "BallReviewProxyFrameBindingView | None"
    temporal_group_id: BallSha256
    frame_url: str
    annotation_revision: int = Field(ge=0)
    annotation_etag: BallSha256
    current_annotation: BallAnnotationPayload | None
    frame_role: Literal["primary_sample", "propagation_target"]
    primary_sample: StrictBool
    propagation_job_ids: list[DetectorSafeId] = Field(max_length=20)
    propagation_suggestions: list[BallPropagationSuggestionView] = Field(max_length=20)

    @model_validator(mode="after")
    def validate_source_timing(self) -> "BallAnnotationFrameView":
        if self.source_timing_status == "not_collected":
            if (
                self.decoder_reported_pos_msec is not None
                or self.decoder_time_seconds is not None
                or self.proxy_binding is None
            ):
                raise ValueError("uncollected source timing cannot publish a decoder observation")
        elif (
            self.decoder_reported_pos_msec is None
            or self.decoder_time_seconds is None
            or abs(self.decoder_time_seconds - self.decoder_reported_pos_msec / 1000.0) > 1e-9
        ):
            raise ValueError("observed source decoder timing is inconsistent")
        if self.proxy_binding is not None:
            source_frame = self.proxy_binding.source_frame
            proxy_frame = self.proxy_binding.proxy_frame
            if (
                source_frame.frame_index != self.frame_index
                or proxy_frame.frame_index != self.frame_index
                or source_frame.sha256 != self.source_frame_sha256
                or source_frame.timing_status != self.source_timing_status
                or source_frame.decoder_reported_pos_msec != self.decoder_reported_pos_msec
            ):
                raise ValueError("public frame proxy authority does not match source frame")
        return self


class BallAnnotationProgressView(BallAnnotationStrictView):
    annotated_frames: int = Field(ge=0)
    total_frames: int = Field(ge=0)
    unconfirmed_suggestions: int = Field(ge=0)
    primary_annotated_frames: int = Field(ge=0)
    primary_total_frames: int = Field(ge=0)
    supplemental_annotated_frames: int = Field(ge=0)
    supplemental_total_frames: int = Field(ge=0)
    unconfirmed_propagation_suggestions: int = Field(ge=0)


class BallAnnotationRetryLineageView(BallAnnotationStrictView):
    mode: Literal[
        "same_authority",
        "worker_runtime_reexecution",
        "review_proxy_decode_upgrade",
    ]
    previous_session_id: DetectorSafeId
    previous_error_code: str | None
    previous_blocker_code: str | None
    previous_lineage_sha256: BallSha256
    current_lineage_sha256: BallSha256
    sampling_manifest_sha256: BallSha256


class BallDevelopmentPackageBindingView(BallAnnotationStrictView):
    session_id: DetectorSafeId
    package_sha256: BallSha256
    attempt_family_sha256: BallSha256


class BallReviewProxyRepairCapabilityView(BallAnnotationStrictView):
    eligible: Literal[True]
    action: Literal["generate_verified_review_proxy"]
    create_url: Literal["/api/v1/detector-review-proxy-repairs"]
    parent_probe_job_id: DetectorSafeId
    parent_probe_report_sha256: BallSha256
    parent_probe_result_manifest_sha256: BallSha256
    parent_probe_record_sha256: BallSha256
    blocked_session_record_sha256: BallSha256


class BallAnnotationSessionResponse(BallAnnotationStrictView):
    schema_version: Literal["1.0"]
    artifact_type: Literal["ball_annotation_session"]
    session_id: DetectorSafeId
    idempotency_key: BallSha256
    request_sha256: BallSha256
    data_role: BallDataRole
    status: BallAnnotationSessionStatus
    stage: str
    source: BallSourceBindingView
    lineage: BallAnnotationLineageView
    locked_profile: BallProfileBindingView
    control_profile_id: DetectorSafeId
    control_profile: BallProfileBindingView
    sampling_profile_id: Literal["tiny_ball_temporal_groups_v1"]
    metric_profile_id: Literal["tiny_ball_feasibility_metric_v1"]
    metric_profile_sha256: BallSha256
    sampling_manifest: BallSamplingManifestView
    operator_id: DetectorSafeId
    applicable_scale_strata: list[Literal["near", "mid", "far"]]
    applicable_lighting_strata: list[Literal["bright_sun", "shadow", "backlight", "twilight", "artificial_light"]]
    retry_from_session_id: DetectorSafeId | None
    retry_lineage: BallAnnotationRetryLineageView | None
    attempt_family_sha256: BallSha256
    development_package_binding: BallDevelopmentPackageBindingView | None
    check_probe_job_id: DetectorSafeId | None
    check_probe_authority: BallCheckProbeAuthorityView | None
    frames: list[BallAnnotationFrameView] = Field(max_length=70)
    final_package: BallAnnotationFinalPackagePointerView | None
    error_code: str | None
    blocker_code: str | None
    review_proxy_repair: BallReviewProxyRepairCapabilityView | None = None
    created_at: str
    updated_at: str
    progress: BallAnnotationProgressView

    @model_validator(mode="after")
    def validate_session_authority(self) -> "BallAnnotationSessionResponse":
        if (
            self.control_profile_id != self.control_profile.profile_id
            or self.sampling_manifest.data_role != self.data_role
            or self.sampling_manifest.source_sha256 != self.source.sha256
            or self.sampling_manifest.locked_profile_id != self.locked_profile.profile_id
            or self.sampling_manifest.locked_profile_sha256 != self.locked_profile.profile_sha256
            or self.sampling_manifest.metric_profile_id != self.metric_profile_id
            or self.sampling_manifest.metric_profile_sha256 != self.metric_profile_sha256
        ):
            raise ValueError("session authority does not bind the sampling manifest")
        expected_scale = [
            row.stratum for row in self.sampling_manifest.strata_applicability.scale if row.status == "applicable"
        ]
        expected_lighting = [
            row.stratum for row in self.sampling_manifest.strata_applicability.lighting if row.status == "applicable"
        ]
        if self.applicable_scale_strata != expected_scale or self.applicable_lighting_strata != expected_lighting:
            raise ValueError("session applicability lists do not match the manifest")
        if (self.retry_from_session_id is None) != (self.retry_lineage is None):
            raise ValueError("retry identity and lineage must bind together")
        if self.retry_lineage is not None and (
            self.retry_lineage.previous_session_id != self.retry_from_session_id
            or self.retry_lineage.sampling_manifest_sha256 != self.sampling_manifest.manifest_sha256
        ):
            raise ValueError("retry lineage does not bind the current session")
        if self.data_role == "development":
            if (
                self.development_package_binding is not None
                or self.check_probe_job_id is not None
                or self.check_probe_authority is not None
            ):
                raise ValueError("development session authority shape is invalid")
            if self.retry_lineage is not None and self.retry_lineage.mode != "review_proxy_decode_upgrade":
                raise ValueError("development retries are limited to review-proxy upgrades")
        else:
            binding = self.development_package_binding
            selection = self.sampling_manifest.selection_authority
            authority = self.check_probe_authority
            if (
                binding is None
                or selection is None
                or self.check_probe_job_id is None
                or binding.attempt_family_sha256 != self.attempt_family_sha256
                or selection.attempt_family_sha256 != self.attempt_family_sha256
                or selection.development_package_sha256 != binding.package_sha256
            ):
                raise ValueError("check session authority shape is invalid")
            pre_ready = self.status in {
                "sampling_locked",
                "check_probe_queued",
                "check_probe_running",
                "check_probe_committing",
            }
            blocked_before_ready = self.status == "blocked" and authority is None
            if pre_ready or blocked_before_ready:
                if authority is not None or self.frames:
                    raise ValueError("pre-ready check session cannot expose probe authority")
            elif (
                authority is None
                or authority.job_id != self.check_probe_job_id
                or authority.locked_profile != self.locked_profile
                or authority.control_profile != self.control_profile
            ):
                raise ValueError("ready check session lacks bound probe authority")
        if (self.status == "finalized") != (self.final_package is not None):
            raise ValueError("final package pointer must match finalized state")
        frame_indices = [frame.frame_index for frame in self.frames]
        if len(frame_indices) != len(set(frame_indices)):
            raise ValueError("session frame identities must be unique")
        if any(
            (frame.primary_sample and frame.frame_role != "primary_sample")
            or (not frame.primary_sample and frame.frame_role != "propagation_target")
            or (not frame.primary_sample and bool(frame.suggested_candidates))
            for frame in self.frames
        ):
            raise ValueError("session frame roles or detector candidates are inconsistent")
        primary_frames = [frame for frame in self.frames if frame.primary_sample]
        supplemental_frames = [frame for frame in self.frames if frame.frame_role == "propagation_target"]
        if self.frames and (
            sorted(frame.frame_index for frame in primary_frames) != self.sampling_manifest.frame_indices
            or any(frame.frame_index in self.sampling_manifest.frame_indices for frame in supplemental_frames)
        ):
            raise ValueError("session primary and supplemental frame sets are inconsistent")
        if (
            self.progress.annotated_frames != sum(frame.current_annotation is not None for frame in self.frames)
            or self.progress.total_frames != len(self.frames)
            or self.progress.unconfirmed_suggestions
            != sum(candidate.decision == "pending" for frame in self.frames for candidate in frame.suggested_candidates)
            or self.progress.primary_annotated_frames
            != sum(frame.current_annotation is not None for frame in primary_frames)
            or self.progress.primary_total_frames != len(primary_frames)
            or self.progress.supplemental_annotated_frames
            != sum(frame.current_annotation is not None for frame in supplemental_frames)
            or self.progress.supplemental_total_frames != len(supplemental_frames)
            or self.progress.unconfirmed_propagation_suggestions
            != sum(
                suggestion.pending_human_confirmation
                for frame in self.frames
                for suggestion in frame.propagation_suggestions
            )
        ):
            raise ValueError("session progress does not match public frame decision state")
        return self


class BallAnnotationRevisionResponse(BallAnnotationStrictView):
    schema_version: Literal["1.0"]
    artifact_type: Literal["ball_annotation_revision"]
    revision_id: DetectorSafeId
    session_id: DetectorSafeId
    frame_index: int = Field(ge=0)
    revision: int = Field(gt=0)
    operation: Literal["set", "delete", "undo"]
    mutation_id: DetectorSafeId
    expected_revision: int = Field(ge=0)
    supersedes_revision: int | None
    undo_revision: int | None
    accepted_suggestion_kind: Literal["detector_candidate", "propagation"] | None
    accepted_suggestion_id: DetectorSafeId | None
    accepted_suggestion_job_id: DetectorSafeId | None
    accepted_suggestion_sha256: BallSha256 | None
    dismissed_suggestion_kind: Literal["detector_candidate", "propagation"] | None
    dismissed_suggestion_id: DetectorSafeId | None
    dismissed_suggestion_job_id: DetectorSafeId | None
    dismissed_suggestion_sha256: BallSha256 | None
    effective_annotation: BallAnnotationPayload | None
    operator_id: DetectorSafeId
    annotation_etag: BallSha256
    created_at: str

    @model_validator(mode="after")
    def validate_suggestion_binding(self) -> "BallAnnotationRevisionResponse":
        accepted = (
            self.accepted_suggestion_kind is not None,
            self.accepted_suggestion_id is not None,
            self.accepted_suggestion_job_id is not None,
            self.accepted_suggestion_sha256 is not None,
        )
        dismissed = (
            self.dismissed_suggestion_kind is not None,
            self.dismissed_suggestion_id is not None,
            self.dismissed_suggestion_job_id is not None,
            self.dismissed_suggestion_sha256 is not None,
        )
        if any(accepted) and not all(accepted):
            raise ValueError("accepted suggestion kind, identity, job and digest must bind together")
        if any(dismissed) and not all(dismissed):
            raise ValueError("dismissed suggestion kind, identity, job and digest must bind together")
        if any(accepted) and any(dismissed):
            raise ValueError("one revision cannot accept and dismiss a suggestion")
        if self.operation == "set" and self.effective_annotation is not None:
            expected_provenance = (
                "detector_candidate_human_confirmed"
                if self.accepted_suggestion_kind == "detector_candidate"
                else "propagation_suggestion_human_confirmed"
                if self.accepted_suggestion_kind == "propagation"
                else "suggestion_dismissed_manual"
                if self.dismissed_suggestion_kind is not None
                else "manual_human_annotation"
            )
            if self.effective_annotation.provenance != expected_provenance:
                raise ValueError("revision provenance does not match suggestion audit")
        return self


class BallPropagationCreateRequest(BallAnnotationStrictView):
    model_config = ConfigDict(extra="forbid", strict=True)

    mutation_id: DetectorSafeId
    seed_frame_index: int = Field(ge=0)
    radius_frames: int = Field(ge=1, le=2)
    expected_seed_revision: int = Field(gt=0)


class BallPropagationSeedBindingView(BallAnnotationStrictView):
    frame_index: int = Field(ge=0)
    annotation_revision: int = Field(gt=0)
    annotation_etag: BallSha256
    annotation_sha256: BallSha256
    source_frame_sha256: BallSha256
    temporal_group_id: BallSha256
    sampling_manifest_sha256: BallSha256
    tracker_profile_sha256: BallSha256


class BallPropagationTrackerProfileView(BallAnnotationStrictView):
    profile_id: Literal["tiny_ball_bounded_template_flow_v1"]
    version: Literal["1.0"]
    radius_frames_max: Literal[2]
    search_radius_source_px: Literal[24]
    minimum_match_score: float = Field(ge=0, le=1)
    minimum_backward_match_score: float = Field(ge=0, le=1)
    maximum_forward_backward_error_px: float = Field(gt=0)
    profile_sha256: BallSha256


class BallPropagationFrameResultView(BallAnnotationStrictView):
    frame_index: int = Field(ge=0)
    direction: Literal["backward", "forward"]
    status: Literal["success", "failed"]
    failure_code: str | None
    source_frame_sha256: BallSha256
    suggestion_id: DetectorSafeId | None
    match_score: float | None = Field(default=None, ge=-1, le=1)
    backward_match_score: float | None = Field(default=None, ge=-1, le=1)
    forward_backward_error_px: float | None = Field(default=None, ge=0)
    step_displacement_px: float | None = Field(default=None, ge=0)
    pending_human_confirmation: bool
    human_confirmation: BallPropagationHumanConfirmationView | None = None
    human_decision: BallPropagationHumanDecisionView | None = None

    @model_validator(mode="after")
    def validate_result_state(self) -> "BallPropagationFrameResultView":
        decided = self.human_confirmation is not None or self.human_decision is not None
        if self.human_confirmation is not None and self.human_decision is not None:
            raise ValueError("propagation frame cannot be confirmed and dismissed")
        if self.status == "success":
            if (
                self.failure_code is not None
                or self.suggestion_id is None
                or self.match_score is None
                or self.backward_match_score is None
                or self.forward_backward_error_px is None
                or self.step_displacement_px is None
                or self.pending_human_confirmation == decided
            ):
                raise ValueError("successful propagation result is incomplete")
        elif self.failure_code is None or self.suggestion_id is not None or self.pending_human_confirmation or decided:
            raise ValueError("failed propagation result carries success state")
        return self


class BallPropagationSummaryView(BallAnnotationStrictView):
    attempted_by_direction: dict[Literal["backward", "forward"], int]
    succeeded_by_direction: dict[Literal["backward", "forward"], int]
    attempted_frame_count: int = Field(ge=0, le=4)
    succeeded_frame_count: int = Field(ge=0, le=4)
    self_check_coverage: float = Field(ge=0, le=1)
    self_checked_max_safe_window_frames: int = Field(ge=0, le=2)
    human_validated_frame_count: int = Field(ge=0, le=4)
    human_dismissed_frame_count: int = Field(ge=0, le=4)
    pending_human_confirmation_count: int = Field(ge=0, le=4)
    human_validated_center_error_px: float | None = Field(default=None, ge=0)
    human_validated_iou: float | None = Field(default=None, ge=0, le=1)
    human_validated_safe_span_frames: int | None = Field(default=None, ge=0, le=2)
    pending_human_confirmation: bool


class BallPropagationJobResponse(BallAnnotationStrictView):
    schema_version: Literal["1.0"]
    artifact_type: Literal["ball_propagation_job"]
    job_id: DetectorSafeId
    session_id: DetectorSafeId
    intent_sha256: BallSha256
    mutation_id: DetectorSafeId
    seed_frame_index: int = Field(ge=0)
    expected_seed_revision: int = Field(gt=0)
    radius_frames: int = Field(ge=1, le=2)
    seed_binding: BallPropagationSeedBindingView
    target_frame_indices: list[int] = Field(min_length=1, max_length=4)
    tracker_profile: BallPropagationTrackerProfileView
    neighbor_probe_job_id: DetectorSafeId | None
    status: Literal["queued", "waiting_probe", "committing", "ready", "failed", "blocked", "cancelled"]
    stage: str
    frame_results: list[BallPropagationFrameResultView] = Field(max_length=4)
    summary: BallPropagationSummaryView | None
    suggestions: list[BallPropagationSuggestionView] = Field(max_length=4)
    error_code: str | None
    neighbor_probe_cancel_status: (
        Literal[
            "not_started",
            "cancelled",
            "cancel_requested",
            "already_terminal",
            "cancel_failed",
        ]
        | None
    )
    neighbor_probe_cancel_error_code: str | None
    created_at: str
    updated_at: str
    status_url: str
    cancel_url: str


class BallPropagationDecisionCountsView(BallAnnotationStrictView):
    confirmed: int = Field(ge=0, le=4)
    dismissed: int = Field(ge=0, le=4)
    pending: int = Field(ge=0, le=4)


class BallSealedPropagationReportView(BallAnnotationStrictView):
    schema_version: Literal["1.0"]
    artifact_type: Literal["ball_propagation_report"]
    job_id: DetectorSafeId
    session_id: DetectorSafeId
    intent_sha256: BallSha256
    mutation_id: DetectorSafeId
    seed_frame_index: int = Field(ge=0)
    expected_seed_revision: int = Field(gt=0)
    radius_frames: int = Field(ge=1, le=2)
    seed_binding: BallPropagationSeedBindingView
    seed_binding_sha256: BallSha256
    target_frame_indices: list[int] = Field(min_length=1, max_length=4)
    tracker_profile: BallPropagationTrackerProfileView
    tracker_profile_sha256: BallSha256
    neighbor_probe_job_id: DetectorSafeId
    neighbor_probe_report_sha256: BallSha256
    neighbor_probe_result_manifest_sha256: BallSha256
    frame_results: list[BallPropagationFrameResultView] = Field(min_length=1, max_length=4)
    suggestions: list[BallPropagationSuggestionView] = Field(max_length=4)
    summary: BallPropagationSummaryView
    decision_counts: BallPropagationDecisionCountsView
    created_at: str
    updated_at: str
    report_sha256: BallSha256

    @model_validator(mode="after")
    def validate_sealed_report(self) -> "BallSealedPropagationReportView":
        if (
            self.target_frame_indices != sorted(set(self.target_frame_indices))
            or [row.frame_index for row in self.frame_results] != self.target_frame_indices
            or self.seed_binding.frame_index != self.seed_frame_index
            or self.seed_binding.annotation_revision != self.expected_seed_revision
            or self.seed_binding.tracker_profile_sha256 != self.tracker_profile_sha256
            or self.tracker_profile.profile_sha256 != self.tracker_profile_sha256
            or canonical_sha256(self.seed_binding.model_dump(mode="json")) != self.seed_binding_sha256
        ):
            raise ValueError("sealed propagation authority is inconsistent")
        successes = [row for row in self.frame_results if row.status == "success"]
        confirmed = sum(row.human_confirmation is not None for row in successes)
        dismissed = sum(row.human_decision is not None for row in successes)
        pending = sum(row.pending_human_confirmation for row in successes)
        if (
            self.decision_counts.confirmed != confirmed
            or self.decision_counts.dismissed != dismissed
            or self.decision_counts.pending != pending
            or self.summary.succeeded_frame_count != len(successes)
            or self.summary.human_validated_frame_count != confirmed
            or self.summary.human_dismissed_frame_count != dismissed
            or self.summary.pending_human_confirmation_count != pending
            or self.summary.pending_human_confirmation != (pending > 0)
        ):
            raise ValueError("sealed propagation decision accounting is inconsistent")
        suggestions = {row.suggestion_id: row for row in self.suggestions}
        successful_ids = {row.suggestion_id for row in successes if row.suggestion_id is not None}
        if set(suggestions) != successful_ids:
            raise ValueError("sealed propagation suggestions do not match successful results")
        for result in successes:
            suggestion = suggestions[result.suggestion_id]
            if (
                suggestion.frame_index != result.frame_index
                or suggestion.source_frame_sha256 != result.source_frame_sha256
                or suggestion.pending_human_confirmation != result.pending_human_confirmation
                or suggestion.human_confirmation != result.human_confirmation
                or suggestion.human_decision != result.human_decision
            ):
                raise ValueError("sealed propagation suggestion audit does not match its result")
        report = _canonical_ball_propagation_report(self, include_report_sha256=False)
        if canonical_sha256(report) != self.report_sha256:
            raise ValueError("sealed propagation report digest does not match its contents")
        return self


class BallAnnotationFinalizeRequest(BallAnnotationStrictView):
    model_config = ConfigDict(extra="forbid", strict=True)

    mutation_id: DetectorSafeId


class BallEffectiveAnnotationView(BallAnnotationPayload):
    frame_index: int = Field(ge=0)


class BallSealedAnnotationRevisionView(BallAnnotationStrictView):
    schema_version: Literal["1.0"]
    artifact_type: Literal["ball_annotation_revision"]
    revision_id: DetectorSafeId
    session_id: DetectorSafeId
    frame_index: int = Field(ge=0)
    revision: int = Field(gt=0)
    operation: Literal["set", "delete", "undo"]
    mutation_id: DetectorSafeId
    mutation_sha256: BallSha256
    expected_revision: int = Field(ge=0)
    supersedes_revision: int | None
    undo_revision: int | None
    accepted_suggestion_kind: Literal["detector_candidate", "propagation"] | None
    accepted_suggestion_id: DetectorSafeId | None
    accepted_suggestion_job_id: DetectorSafeId | None
    accepted_suggestion_sha256: BallSha256 | None
    dismissed_suggestion_kind: Literal["detector_candidate", "propagation"] | None
    dismissed_suggestion_id: DetectorSafeId | None
    dismissed_suggestion_job_id: DetectorSafeId | None
    dismissed_suggestion_sha256: BallSha256 | None
    previous_effective_annotation: BallAnnotationPayload | None
    effective_annotation: BallAnnotationPayload | None
    operator_id: DetectorSafeId
    annotation_etag: BallSha256
    created_at: str

    @model_validator(mode="after")
    def validate_suggestion_binding(self) -> "BallSealedAnnotationRevisionView":
        accepted = (
            self.accepted_suggestion_kind is not None,
            self.accepted_suggestion_id is not None,
            self.accepted_suggestion_job_id is not None,
            self.accepted_suggestion_sha256 is not None,
        )
        dismissed = (
            self.dismissed_suggestion_kind is not None,
            self.dismissed_suggestion_id is not None,
            self.dismissed_suggestion_job_id is not None,
            self.dismissed_suggestion_sha256 is not None,
        )
        if any(accepted) and not all(accepted):
            raise ValueError("accepted suggestion kind, identity, job and digest must bind together")
        if any(dismissed) and not all(dismissed):
            raise ValueError("dismissed suggestion kind, identity, job and digest must bind together")
        if any(accepted) and any(dismissed):
            raise ValueError("one revision cannot accept and dismiss a suggestion")
        return self


class BallFrameEvidenceSourceView(BallAnnotationStrictView):
    sha256: BallSha256
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class BallFrameEvidenceJpegView(BallAnnotationStrictView):
    sha256: BallSha256
    size_bytes: int = Field(gt=0)
    media_type: Literal["image/jpeg"]


class BallImmutableFrameMediaView(BallAnnotationStrictView):
    frame_index: int = Field(ge=0)
    relative_path: str = Field(pattern=r"^frames/[0-9]{9}\.jpg$")
    sha256: BallSha256
    size_bytes: int = Field(gt=0)
    media_type: Literal["image/jpeg"]
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class BallCanonicalTemporalGroupView(BallAnnotationStrictView):
    group_id: BallSha256
    profile_id: Literal["tiny_ball_temporal_groups_v1"]
    source_sha256: BallSha256
    seed_frame_index: int = Field(ge=0)
    start_frame: int = Field(ge=0)
    end_frame: int = Field(ge=0)
    derivative_family: list[int] = Field(min_length=2, max_length=2)
    canonical_moment_id: BallSha256
    derivative_family_id: BallSha256
    ancestry_profile: Literal["source-proxy-crop-tile-propagation-closure-v1"]


class BallCrossDecodeObservationView(BallAnnotationStrictView):
    effective_decode_mode: Literal[
        "sequential",
        "preroll_verified",
        "direct_verified",
        "sequential_fallback",
    ]
    decoded_frame_position: int = Field(ge=0)
    decoder_reported_pos_msec: float = Field(allow_inf_nan=False)
    source_frame_jpeg_sha256: BallSha256


class BallCrossDecodeVerificationView(BallAnnotationStrictView):
    method: Literal["decoder_pos_msec_and_frame_digest_agreement_v1"]
    tolerance_msec: float = Field(ge=0, allow_inf_nan=False)
    observations: list[BallCrossDecodeObservationView] = Field(min_length=2, max_length=4)
    verification_sha256: BallSha256

    @model_validator(mode="after")
    def validate_cross_decode(self) -> "BallCrossDecodeVerificationView":
        if len({row.effective_decode_mode for row in self.observations}) < 2:
            raise ValueError("cross-decode verification requires distinct decode modes")
        first = self.observations[0]
        if any(
            row.decoded_frame_position != first.decoded_frame_position
            or row.source_frame_jpeg_sha256 != first.source_frame_jpeg_sha256
            or abs(row.decoder_reported_pos_msec - first.decoder_reported_pos_msec) > self.tolerance_msec
            for row in self.observations[1:]
        ):
            raise ValueError("cross-decode observations do not agree")
        evidence = self.model_dump(mode="json")
        evidence.pop("verification_sha256")
        if canonical_sha256(evidence) != self.verification_sha256:
            raise ValueError("cross-decode verification digest is invalid")
        return self


class BallSourceFrameTimingBindingView(BallAnnotationStrictView):
    schema_version: Literal["1.0"]
    artifact_type: Literal["ball_source_frame_timing_binding"]
    timing_profile_id: Literal[
        "verified_decoder_pos_msec_after_frame_position_v1",
        "source_pos_msec_not_collected_proxy_cfr_verified_v1",
    ]
    timing_status: Literal["observed", "not_collected"]
    source_sha256: BallSha256
    runtime_environment_sha256: BallSha256
    source_frame_jpeg_sha256: BallSha256
    frame_index: int = Field(ge=0)
    decoded_frame_position: int = Field(ge=0)
    fps: float = Field(gt=0, allow_inf_nan=False)
    effective_decode_mode: Literal[
        "sequential",
        "preroll_verified",
        "direct_verified",
        "sequential_fallback",
    ]
    decoder_reported_pos_msec: float | None = Field(allow_inf_nan=False)
    decoder_time_seconds: float | None = Field(allow_inf_nan=False)
    decoder_timing_observation_method: Literal["opencv_cap_prop_pos_msec_after_verified_frame_read"] | None
    display_time_seconds: float = Field(ge=0, allow_inf_nan=False)
    display_time_derivation: Literal["frame_index_divided_by_fps_for_display_only_not_source_pts"]
    true_presentation_timestamp: BallTruePresentationTimestampView
    position_verification: Literal[
        "opencv_next_frame_index_with_0.25_tolerance",
        "verified_review_proxy_frame_index_mapping_v1",
    ]
    cross_decode_verification: BallCrossDecodeVerificationView | None
    timing_binding_sha256: BallSha256

    @model_validator(mode="after")
    def validate_timing_binding(self) -> "BallSourceFrameTimingBindingView":
        if self.decoded_frame_position != self.frame_index:
            raise ValueError("decoder timing fields are inconsistent")
        if self.timing_status == "not_collected":
            if (
                self.timing_profile_id != "source_pos_msec_not_collected_proxy_cfr_verified_v1"
                or self.decoder_reported_pos_msec is not None
                or self.decoder_time_seconds is not None
                or self.decoder_timing_observation_method is not None
                or self.cross_decode_verification is not None
                or self.position_verification != "verified_review_proxy_frame_index_mapping_v1"
            ):
                raise ValueError("uncollected source timing claims observations")
        elif (
            self.timing_profile_id != "verified_decoder_pos_msec_after_frame_position_v1"
            or self.decoder_reported_pos_msec is None
            or self.decoder_time_seconds is None
            or self.decoder_timing_observation_method is None
            or self.position_verification != "opencv_next_frame_index_with_0.25_tolerance"
            or abs(self.decoder_time_seconds - self.decoder_reported_pos_msec / 1000.0) > 1e-9
        ):
            raise ValueError("observed source timing fields are inconsistent")
        if self.cross_decode_verification is not None:
            first = self.cross_decode_verification.observations[0]
            if (
                first.decoded_frame_position != self.frame_index
                or first.source_frame_jpeg_sha256 != self.source_frame_jpeg_sha256
            ):
                raise ValueError("cross-decode evidence does not bind this frame")
        binding = self.model_dump(mode="json")
        binding.pop("timing_binding_sha256")
        if canonical_sha256(binding) != self.timing_binding_sha256:
            raise ValueError("source timing binding digest is invalid")
        return self


class BallProxyMediaBindingView(BallAnnotationStrictView):
    sha256: BallSha256
    size_bytes: int = Field(gt=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class BallProxyMappedFrameView(BallAnnotationStrictView):
    frame_index: int = Field(ge=0)
    timing_status: Literal["observed", "not_collected"]
    decoder_reported_pos_msec: float | None = Field(default=None, allow_inf_nan=False)
    sha256: BallSha256

    @model_validator(mode="after")
    def validate_mapped_timing(self) -> "BallProxyMappedFrameView":
        if (self.timing_status == "not_collected") != (self.decoder_reported_pos_msec is None):
            raise ValueError("mapped frame timing status is inconsistent")
        return self


class BallProxyCfrMappedFrameView(BallAnnotationStrictView):
    frame_index: int = Field(ge=0)
    timing_basis: Literal["verified_cfr_frame_index_time_v1"]
    cfr_time_msec: float = Field(allow_inf_nan=False)
    sha256: BallSha256


class BallProxyTimeMappingView(BallAnnotationStrictView):
    method: Literal[
        "explicit_per_frame_decoder_pos_msec_map_v1",
        "exact_frame_index_to_verified_proxy_cfr_v1",
    ]
    source_timing_status: Literal["observed", "not_collected"]
    proxy_timing_basis: Literal["verified_cfr_frame_index_time_v1"]
    declared_offset_msec: float = Field(allow_inf_nan=False)
    observed_offset_msec: float | None = Field(default=None, allow_inf_nan=False)
    residual_msec: float | None = Field(default=None, allow_inf_nan=False)
    tolerance_msec: float = Field(ge=0, allow_inf_nan=False)


class BallReviewProxyFrameBindingView(BallAnnotationStrictView):
    schema_version: Literal["1.0"]
    artifact_type: Literal["ball_review_proxy_frame_binding"]
    proxy: BallProxyMediaBindingView
    map_sha256: BallSha256
    source_frame: BallProxyMappedFrameView
    proxy_frame: BallProxyCfrMappedFrameView
    map_time_tolerance_msec: float = Field(ge=0, allow_inf_nan=False)
    declared_offset_msec: float = Field(allow_inf_nan=False)
    time_mapping: BallProxyTimeMappingView
    binding_sha256: BallSha256

    @model_validator(mode="after")
    def validate_proxy_mapping(self) -> "BallReviewProxyFrameBindingView":
        source_not_collected = self.source_frame.timing_status == "not_collected"
        observed_offset = (
            None
            if source_not_collected
            else self.proxy_frame.cfr_time_msec - self.source_frame.decoder_reported_pos_msec
        )
        if (
            self.source_frame.frame_index != self.proxy_frame.frame_index
            or self.map_time_tolerance_msec != self.time_mapping.tolerance_msec
            or self.declared_offset_msec != self.time_mapping.declared_offset_msec
            or self.time_mapping.source_timing_status != self.source_frame.timing_status
            or self.time_mapping.proxy_timing_basis != self.proxy_frame.timing_basis
            or (
                source_not_collected
                and (
                    self.time_mapping.method != "exact_frame_index_to_verified_proxy_cfr_v1"
                    or self.time_mapping.observed_offset_msec is not None
                    or self.time_mapping.residual_msec is not None
                )
            )
            or (
                not source_not_collected
                and (
                    self.time_mapping.method != "explicit_per_frame_decoder_pos_msec_map_v1"
                    or abs(observed_offset - self.time_mapping.observed_offset_msec) > 1e-9
                    or abs(self.time_mapping.residual_msec - (observed_offset - self.declared_offset_msec)) > 1e-9
                    or abs(self.time_mapping.residual_msec) > self.map_time_tolerance_msec
                )
            )
        ):
            raise ValueError("review proxy time mapping is inconsistent")
        binding = self.model_dump(mode="json")
        binding.pop("binding_sha256")
        if canonical_sha256(binding) != self.binding_sha256:
            raise ValueError("review proxy binding digest is invalid")
        return self


class BallSourceFrameProbeEvidenceView(BallAnnotationStrictView):
    schema_version: Literal["1.0"]
    artifact_type: Literal["ball_source_frame_probe_evidence"]
    probe_job_id: DetectorSafeId
    probe_report_sha256: BallSha256
    probe_result_manifest_sha256: BallSha256
    artifact_id: DetectorSafeId
    artifact_sha256: BallSha256
    artifact_size_bytes: int = Field(gt=0)
    artifact_media_type: Literal["image/jpeg"]
    binding_sha256: BallSha256


class BallSupplementalPropagationEvidenceView(BallAnnotationStrictView):
    schema_version: Literal["1.0"]
    artifact_type: Literal["ball_supplemental_propagation_evidence"]
    propagation_job_id: DetectorSafeId
    propagation_report_sha256: BallSha256
    neighbor_probe_job_id: DetectorSafeId
    neighbor_probe_report_sha256: BallSha256
    neighbor_probe_result_manifest_sha256: BallSha256
    neighbor_artifact_id: DetectorSafeId
    neighbor_artifact_sha256: BallSha256
    neighbor_artifact_size_bytes: int = Field(gt=0)
    propagation_intent_sha256: BallSha256
    seed_binding_sha256: BallSha256
    tracker_profile_sha256: BallSha256
    propagation_frame_result_sha256: BallSha256
    suggestion_id: DetectorSafeId | None
    suggestion_sha256: BallSha256 | None
    temporal_group_derivative_binding_sha256: BallSha256
    binding_sha256: BallSha256


class BallSealedFrameEvidenceView(BallAnnotationStrictView):
    schema_version: Literal["1.0"]
    artifact_type: Literal["ball_sealed_frame_evidence"]
    frame_index: int = Field(ge=0)
    frame_role: Literal["primary", "supplemental"]
    source: BallFrameEvidenceSourceView
    source_frame_jpeg: BallFrameEvidenceJpegView
    temporal_group: BallCanonicalTemporalGroupView | BallInheritedTemporalGroupView
    probe_evidence: BallSourceFrameProbeEvidenceView
    timing_binding: BallSourceFrameTimingBindingView
    proxy_binding: BallReviewProxyFrameBindingView | None
    propagation_evidence: BallSupplementalPropagationEvidenceView | None
    effective_revision: int = Field(gt=0)
    effective_annotation_sha256: BallSha256
    revision_chain_sha256: BallSha256
    frame_evidence_sha256: BallSha256

    @model_validator(mode="after")
    def validate_frame_role(self) -> "BallSealedFrameEvidenceView":
        if (
            self.source_frame_jpeg.sha256 != self.timing_binding.source_frame_jpeg_sha256
            or self.source.sha256 != self.timing_binding.source_sha256
            or self.frame_index != self.timing_binding.frame_index
            or self.probe_evidence.artifact_sha256 != self.source_frame_jpeg.sha256
        ):
            raise ValueError("sealed frame media and timing authority do not bind")
        if self.timing_binding.timing_status == "not_collected":
            if (
                self.proxy_binding is None
                or self.proxy_binding.source_frame.timing_status != "not_collected"
                or self.proxy_binding.source_frame.decoder_reported_pos_msec is not None
                or self.proxy_binding.source_frame.frame_index != self.frame_index
                or self.proxy_binding.source_frame.sha256 != self.source_frame_jpeg.sha256
                or self.proxy_binding.proxy_frame.frame_index != self.frame_index
                or self.proxy_binding.proxy_frame.timing_basis != "verified_cfr_frame_index_time_v1"
            ):
                raise ValueError("uncollected source timing lacks exact verified proxy CFR evidence")
        if self.frame_role == "primary":
            if self.propagation_evidence is not None or not isinstance(
                self.temporal_group, BallCanonicalTemporalGroupView
            ):
                raise ValueError("primary frame evidence has supplemental authority")
        elif (
            self.propagation_evidence is None
            or not isinstance(self.temporal_group, BallInheritedTemporalGroupView)
            or self.temporal_group.derivative.artifact_id != self.propagation_evidence.neighbor_artifact_id
            or self.temporal_group.derivative_binding_sha256
            != self.propagation_evidence.temporal_group_derivative_binding_sha256
        ):
            raise ValueError("supplemental frame evidence lacks propagation authority")
        evidence = self.model_dump(mode="json")
        evidence.pop("frame_evidence_sha256")
        if canonical_sha256(evidence) != self.frame_evidence_sha256:
            raise ValueError("sealed frame evidence digest is invalid")
        return self


class BallDatasetExpansionValidationEvidenceView(BallAnnotationStrictView):
    all_frames_human_confirmed: Literal[True]
    all_primary_roles_complete: Literal[True]
    all_supplemental_roles_complete: Literal[True]
    exact_frame_media_sha256: BallSha256
    frame_evidence_sha256: BallSha256
    revision_chain_sha256: BallSha256
    pending_detector_candidate_count: int = Field(ge=0)
    pending_propagation_suggestion_count: int = Field(ge=0)
    pending_suggestion_decision_count: int = Field(ge=0)
    localizable_positive_seed_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_pending_total(
        self,
    ) -> "BallDatasetExpansionValidationEvidenceView":
        if (
            self.pending_suggestion_decision_count
            != self.pending_detector_candidate_count + self.pending_propagation_suggestion_count
            or self.pending_detector_candidate_count != 0
            or self.pending_propagation_suggestion_count != 0
            or self.pending_suggestion_decision_count != 0
        ):
            raise ValueError("final annotation evidence cannot contain pending suggestion decisions")
        return self


class BallDatasetExpansionEligibilityView(BallAnnotationStrictView):
    eligible: bool
    reasons: list[
        Literal[
            "check_role_is_evaluation_only",
            "pending_suggestion_decisions",
            "no_localizable_positive_seed",
        ]
    ]
    validation_evidence: BallDatasetExpansionValidationEvidenceView

    @model_validator(mode="after")
    def validate_eligibility_evidence(
        self,
    ) -> "BallDatasetExpansionEligibilityView":
        if len(self.reasons) != len(set(self.reasons)):
            raise ValueError("dataset expansion reasons must be unique")
        if self.eligible != (not self.reasons):
            raise ValueError("dataset expansion eligibility must match reasons")
        pending = self.validation_evidence.pending_suggestion_decision_count
        if ("pending_suggestion_decisions" in self.reasons) != (pending > 0):
            raise ValueError("pending suggestion reason must match evidence")
        has_check_reason = "check_role_is_evaluation_only" in self.reasons
        no_seed = self.validation_evidence.localizable_positive_seed_count == 0
        if ("no_localizable_positive_seed" in self.reasons) != (no_seed and not has_check_reason):
            raise ValueError("no-seed reason must match development evidence")
        expected_order = [
            reason
            for reason in (
                "check_role_is_evaluation_only",
                "pending_suggestion_decisions",
                "no_localizable_positive_seed",
            )
            if reason in self.reasons
        ]
        if self.reasons != expected_order:
            raise ValueError("dataset expansion reasons are not canonical")
        return self


class BallHistoricalProbeAuthorityView(BallAnnotationStrictView):
    probe_job_id: DetectorSafeId
    probe_report_sha256: BallSha256
    probe_result_manifest_sha256: BallSha256
    probe_report: dict[str, Any]
    probe_result_manifest: dict[str, Any]
    source_frame_evidence_sha256: BallSha256
    candidate_evidence_sha256: BallSha256

    @model_validator(mode="after")
    def validate_historical_authority(
        self,
    ) -> "BallHistoricalProbeAuthorityView":
        try:
            result_manifest, result_manifest_sha256 = build_detector_probe_result_manifest_authority(self.probe_report)
            digests = build_detector_probe_inherited_evidence_authority(self.probe_report)
        except BallFrameEvidenceError as exc:
            raise ValueError("historical probe authority report is invalid") from exc
        if (
            self.probe_report.get("job_id") != self.probe_job_id
            or self.probe_report.get("report_sha256") != self.probe_report_sha256
            or self.probe_result_manifest != result_manifest
            or result_manifest_sha256 != self.probe_result_manifest_sha256
            or digests["source_frame_evidence_sha256"] != self.source_frame_evidence_sha256
            or digests["candidate_evidence_sha256"] != self.candidate_evidence_sha256
        ):
            raise ValueError("historical probe authority changed from report/result manifest")
        return self


class BallFrameReviewProxyAuthorityView(BallAnnotationStrictView):
    probe_job_id: DetectorSafeId
    probe_report_sha256: BallSha256
    probe_result_manifest_sha256: BallSha256
    probe_report: dict[str, Any]
    probe_result_manifest: dict[str, Any]
    review_proxy_manifest: DetectorReviewProxyManifestView
    historical_probe_authority: BallHistoricalProbeAuthorityView | None = None

    @model_validator(mode="after")
    def validate_proxy_authority(self) -> "BallFrameReviewProxyAuthorityView":
        report = self.probe_report
        try:
            result_manifest, result_manifest_sha256 = build_detector_probe_result_manifest_authority(report)
        except BallFrameEvidenceError as exc:
            raise ValueError("frame proxy authority child report is invalid") from exc
        if (
            report.get("job_id") != self.probe_job_id
            or report.get("report_sha256") != self.probe_report_sha256
            or self.probe_result_manifest != result_manifest
            or result_manifest_sha256 != self.probe_result_manifest_sha256
        ):
            raise ValueError("frame proxy authority changed from child report/result manifest")
        try:
            manifest = validate_review_proxy_manifest(self.review_proxy_manifest.model_dump(mode="json"))
        except ReviewProxyError as exc:
            raise ValueError("frame proxy authority manifest is not canonical") from exc
        if report.get("review_proxy_manifest") != manifest:
            raise ValueError("frame proxy manifest changed from child report")
        upgrade = report.get("lineage", {}).get("review_proxy_upgrade")
        historical = self.historical_probe_authority
        if isinstance(upgrade, dict):
            if historical is None:
                raise ValueError("frame proxy child lacks historical evidence authority")
            try:
                inherited = verify_detector_probe_review_proxy_inheritance(
                    report,
                    historical.probe_report,
                    parent_probe_result_manifest_sha256=(historical.probe_result_manifest_sha256),
                )
            except BallFrameEvidenceError as exc:
                raise ValueError("frame proxy child changed inherited historical evidence") from exc
            if (
                inherited["source_frame_evidence_sha256"] != historical.source_frame_evidence_sha256
                or inherited["candidate_evidence_sha256"] != historical.candidate_evidence_sha256
            ):
                raise ValueError("frame proxy child inherited evidence digests are invalid")
        elif historical is not None:
            raise ValueError("ordinary frame proxy cannot publish historical child authority")
        return self


class BallDetectorProbeAuthorityView(BallAnnotationStrictView):
    schema_version: Literal["1.0"]
    artifact_type: Literal["detector_probe_job_authority"]
    job_id: DetectorSafeId
    request_sha256: BallSha256
    intent_sha256: BallSha256
    semantic_intent_sha256: BallSha256 | None
    resource_sha256: BallSha256
    frozen_profiles_sha256: BallSha256
    execution_bundle_sha256: BallSha256
    runtime_environment_sha256: BallSha256
    retry_from_job_id: DetectorSafeId | None
    retry_kind: str | None
    frozen_request: dict[str, Any]
    frozen_profiles: list[dict[str, Any]]
    probe_report_sha256: BallSha256
    probe_result_manifest_sha256: BallSha256
    probe_report: dict[str, Any]
    probe_result_manifest: dict[str, Any]
    probe_job_record: dict[str, Any]
    canonical_job_record_sha256: BallSha256
    audit_anchor_kind: Literal["audited_t2_legacy", "embedded_job_record"]
    job_record_authority_sha256: BallSha256

    @model_validator(mode="after")
    def validate_probe_job_authority(
        self,
    ) -> "BallDetectorProbeAuthorityView":
        try:
            validate_detector_probe_job_authority(self.model_dump(mode="json"))
        except BallFrameEvidenceError as exc:
            raise ValueError(str(exc)) from exc
        return self


class BallAnnotationNormalizedSessionRequestView(BallAnnotationSessionCreateRequest):
    strata_applicability: BallStrataApplicabilityView
    applicable_scale_strata: list[Literal["near", "mid", "far"]]
    applicable_lighting_strata: list[
        Literal[
            "bright_sun",
            "shadow",
            "backlight",
            "twilight",
            "artificial_light",
        ]
    ]

    @model_validator(mode="after")
    def validate_derived_applicability(
        self,
    ) -> "BallAnnotationNormalizedSessionRequestView":
        expected_scales = [row.stratum for row in self.strata_applicability.scale if row.status == "applicable"]
        expected_lights = [row.stratum for row in self.strata_applicability.lighting if row.status == "applicable"]
        if self.applicable_scale_strata != expected_scales or self.applicable_lighting_strata != expected_lights:
            raise ValueError("normalized session applicability is invalid")
        return self


class BallAnnotationSessionRequestAuthorityView(BallAnnotationStrictView):
    schema_version: Literal["1.0"]
    artifact_type: Literal["ball_annotation_session_request_authority"]
    session_id: DetectorSafeId
    request_sha256: BallSha256
    normalized_request: BallAnnotationNormalizedSessionRequestView
    authority_sha256: BallSha256

    @model_validator(mode="after")
    def validate_session_request_authority(
        self,
    ) -> "BallAnnotationSessionRequestAuthorityView":
        body = self.model_dump(mode="json")
        body.pop("authority_sha256")
        if (
            canonical_sha256(self.normalized_request.model_dump(mode="json")) != self.request_sha256
            or canonical_sha256(body) != self.authority_sha256
        ):
            raise ValueError("session request authority digest is invalid")
        return self


class BallAnnotationPackageView(BallAnnotationStrictView):
    schema_version: Literal["1.0"]
    artifact_type: Literal["ball_annotation_package"]
    session_id: DetectorSafeId
    session_request_authority: BallAnnotationSessionRequestAuthorityView
    data_role: BallDataRole
    source: BallSourceBindingView
    lineage: BallAnnotationLineageView
    detector_probe_authorities: list[BallDetectorProbeAuthorityView] = Field(max_length=8)
    frame_review_proxy_authority: BallFrameReviewProxyAuthorityView | None
    attempt_family_sha256: BallSha256
    development_package_binding: BallDevelopmentPackageBindingView | None
    operator_id: DetectorSafeId
    locked_profile: BallProfileBindingView
    control_profile_id: DetectorSafeId
    control_profile: BallProfileBindingView
    sampling_profile_id: Literal["tiny_ball_temporal_groups_v1"]
    metric_profile_id: Literal["tiny_ball_feasibility_metric_v1"]
    metric_profile_sha256: BallSha256
    sampling_manifest: BallSamplingManifestView
    check_probe_job_id: DetectorSafeId | None
    check_probe_authority: BallCheckProbeAuthorityView | None
    detector_candidate_evidence: list[BallDetectorCandidateEvidenceView]
    detector_candidate_evidence_sha256: BallSha256
    effective_annotations: list[BallEffectiveAnnotationView] = Field(min_length=1)
    revision_chain: list[BallSealedAnnotationRevisionView] = Field(min_length=1)
    supplemental_frame_indices: list[int] = Field(max_length=20)
    frame_evidence: list[BallSealedFrameEvidenceView] = Field(min_length=1, max_length=70)
    frame_evidence_sha256: BallSha256
    frame_media: list[BallImmutableFrameMediaView] = Field(min_length=1, max_length=70)
    frame_media_sha256: BallSha256
    propagation_reports: list[BallSealedPropagationReportView] = Field(max_length=20)
    propagation_reports_sha256: BallSha256
    created_at: str
    training_eligible: Literal[False]
    may_seed_dataset_expansion: bool
    dataset_expansion_eligibility: BallDatasetExpansionEligibilityView
    qualification_eligible: Literal[False]
    pr4a_pr4b_truth_compatible: Literal[False]
    package_sha256: BallSha256

    @model_validator(mode="before")
    @classmethod
    def validate_raw_package_evidence_authority(
        cls,
        value: Any,
    ) -> Any:
        canonical_value = deepcopy(value)
        if isinstance(canonical_value, dict):
            manifest = canonical_value.get("sampling_manifest")
            if isinstance(manifest, dict):
                _canonical_ball_sampling_manifest(manifest)
            propagation_reports = canonical_value.get("propagation_reports")
            if isinstance(propagation_reports, list):
                for report in propagation_reports:
                    if not isinstance(report, dict):
                        continue
                    for collection_name in ("frame_results", "suggestions"):
                        collection = report.get(collection_name)
                        if not isinstance(collection, list):
                            continue
                        for row in collection:
                            if not isinstance(row, dict):
                                continue
                            if row.get("human_confirmation") is None:
                                row.pop("human_confirmation", None)
                            if row.get("human_decision") is None:
                                row.pop("human_decision", None)
        try:
            verify_frame_evidence_package(canonical_value)
        except BallFrameEvidenceError as exc:
            raise ValueError(f"annotation package evidence authority is invalid: {exc}") from exc
        return value

    @model_validator(mode="after")
    def validate_package_role_and_eligibility(
        self,
    ) -> "BallAnnotationPackageView":
        eligible = self.dataset_expansion_eligibility.eligible
        if self.may_seed_dataset_expansion != eligible:
            raise ValueError("may_seed_dataset_expansion must match eligibility")
        has_check_reason = "check_role_is_evaluation_only" in self.dataset_expansion_eligibility.reasons
        if self.data_role == "development":
            if (
                self.development_package_binding is not None
                or self.check_probe_job_id is not None
                or self.check_probe_authority is not None
                or has_check_reason
            ):
                raise ValueError("development package authority shape is invalid")
        else:
            if (
                self.development_package_binding is None
                or self.check_probe_job_id is None
                or self.check_probe_authority is None
                or self.check_probe_authority.job_id != self.check_probe_job_id
                or self.development_package_binding.attempt_family_sha256 != self.attempt_family_sha256
                or eligible
                or self.may_seed_dataset_expansion
                or not has_check_reason
                or self.dataset_expansion_eligibility.validation_evidence.pending_detector_candidate_count != 0
            ):
                raise ValueError("check package authority shape is invalid")
        if (
            self.control_profile_id != self.control_profile.profile_id
            or self.sampling_manifest.data_role != self.data_role
            or self.sampling_manifest.source_sha256 != self.source.sha256
            or self.sampling_manifest.locked_profile_id != self.locked_profile.profile_id
            or self.sampling_manifest.locked_profile_sha256 != self.locked_profile.profile_sha256
            or self.sampling_manifest.metric_profile_sha256 != self.metric_profile_sha256
        ):
            raise ValueError("package authority does not bind its sampling manifest")
        actual_probe_job_ids = [authority.job_id for authority in self.detector_probe_authorities]
        if len(set(actual_probe_job_ids)) != len(actual_probe_job_ids):
            raise ValueError("detector probe authorities differ from exact package lineage")
        if self.data_role == "development":
            expected_probe_job_ids = self.lineage.development_probe_job_ids
        else:
            if not self.detector_probe_authorities or actual_probe_job_ids[-1] != self.check_probe_job_id:
                raise ValueError("detector probe authorities differ from exact package lineage")
            current_authority = self.detector_probe_authorities[-1]
            current_lineage = current_authority.probe_report.get("lineage")
            proxy_upgrade = current_lineage.get("review_proxy_upgrade") if isinstance(current_lineage, dict) else None
            if proxy_upgrade is None:
                expected_probe_job_ids = [self.check_probe_job_id]
            else:
                parent_job_id = current_authority.retry_from_job_id
                if parent_job_id is None or current_authority.retry_kind != "review_proxy_decode_upgrade":
                    raise ValueError("check review proxy retry authority is invalid")
                expected_probe_job_ids = [parent_job_id, self.check_probe_job_id]
        if actual_probe_job_ids != expected_probe_job_ids:
            raise ValueError("detector probe authorities differ from exact package lineage")
        detector_probe_authorities_by_job = {
            authority.job_id: authority for authority in self.detector_probe_authorities
        }
        if self.data_role == "development":
            for lineage_values, authority_field in (
                (
                    self.lineage.development_probe_report_sha256s,
                    "probe_report_sha256",
                ),
                (
                    self.lineage.development_probe_result_manifest_sha256s,
                    "probe_result_manifest_sha256",
                ),
                (
                    self.lineage.development_probe_execution_bundle_sha256s,
                    "execution_bundle_sha256",
                ),
                (
                    self.lineage.development_probe_frozen_profiles_sha256s,
                    "frozen_profiles_sha256",
                ),
            ):
                if any(
                    lineage_values[job_id]
                    != getattr(
                        detector_probe_authorities_by_job[job_id],
                        authority_field,
                    )
                    for job_id in actual_probe_job_ids
                ):
                    raise ValueError("detector probe authority differs from exact lineage digest maps")
        else:
            assert self.check_probe_authority is not None
            sealed_check = self.detector_probe_authorities[-1]
            if (
                self.check_probe_authority.job_id != sealed_check.job_id
                or self.check_probe_authority.request_sha256 != sealed_check.request_sha256
                or self.check_probe_authority.intent_sha256 != sealed_check.intent_sha256
                or self.check_probe_authority.report_sha256 != sealed_check.probe_report_sha256
                or self.check_probe_authority.result_manifest_sha256 != sealed_check.probe_result_manifest_sha256
                or self.check_probe_authority.execution_bundle_sha256 != sealed_check.execution_bundle_sha256
                or self.check_probe_authority.runtime_environment_sha256 != sealed_check.runtime_environment_sha256
                or self.check_probe_authority.frozen_profiles_sha256 != sealed_check.frozen_profiles_sha256
            ):
                raise ValueError("check probe authority differs from exact job authority")
            if len(self.detector_probe_authorities) == 2:
                sealed_parent = self.detector_probe_authorities[0]
                try:
                    verify_detector_probe_review_proxy_inheritance(
                        sealed_check.probe_report,
                        sealed_parent.probe_report,
                        parent_probe_result_manifest_sha256=(sealed_parent.probe_result_manifest_sha256),
                    )
                except BallFrameEvidenceError as exc:
                    raise ValueError("check review proxy inheritance is invalid") from exc
        candidate_order = [(row.frame_index, row.candidate.rank) for row in self.detector_candidate_evidence]
        if candidate_order != sorted(candidate_order):
            raise ValueError("detector candidate evidence is not canonically ordered")
        revisions_by_id = {row.revision_id: row for row in self.revision_chain}
        frame_evidence_by_index = {row.frame_index: row for row in self.frame_evidence}
        for evidence in self.detector_candidate_evidence:
            frame_evidence = frame_evidence_by_index.get(evidence.frame_index)
            proxy_sha256 = (
                canonical_sha256(frame_evidence.proxy_binding.model_dump(mode="json"))
                if frame_evidence is not None and frame_evidence.proxy_binding is not None
                else None
            )
            expected_media_job_id = (
                self.frame_review_proxy_authority.probe_job_id
                if proxy_sha256 is not None and self.frame_review_proxy_authority is not None
                else (frame_evidence.probe_evidence.probe_job_id if frame_evidence is not None else None)
            )
            if (
                frame_evidence is None
                or frame_evidence.frame_role != "primary"
                or evidence.review_media.probe_job_id != frame_evidence.probe_evidence.probe_job_id
                or evidence.review_media.probe_report_sha256 != frame_evidence.probe_evidence.probe_report_sha256
                or evidence.review_media.probe_result_manifest_sha256
                != frame_evidence.probe_evidence.probe_result_manifest_sha256
                or evidence.review_media.source_artifact_id != frame_evidence.probe_evidence.artifact_id
                or evidence.review_media.probe_job_id != expected_media_job_id
                or evidence.review_media.proxy_binding_sha256 != proxy_sha256
            ):
                raise ValueError("detector candidate origin or review media is inconsistent")
            origin = evidence.candidate_origin
            media = evidence.review_media
            origin_authority = detector_probe_authorities_by_job.get(origin.probe_job_id)
            media_authority = detector_probe_authorities_by_job.get(media.probe_job_id)
            if (
                origin_authority is None
                or media_authority is None
                or (self.data_role == "development" and origin_authority.audit_anchor_kind != "audited_t2_legacy")
                or origin_authority.probe_report_sha256 != origin.probe_report_sha256
                or origin_authority.probe_result_manifest_sha256 != origin.probe_result_manifest_sha256
                or media_authority.probe_report_sha256 != media.probe_report_sha256
                or media_authority.probe_result_manifest_sha256 != media.probe_result_manifest_sha256
            ):
                raise ValueError("detector candidate lacks exact audited origin authority")
            historical = (
                self.frame_review_proxy_authority.historical_probe_authority
                if self.frame_review_proxy_authority is not None
                else None
            )
            if origin.source_artifact_id != media.source_artifact_id:
                raise ValueError("detector candidate origin artifact differs from review media")
            if self.data_role == "development":
                if (
                    origin.probe_job_id not in self.lineage.development_probe_job_ids
                    or media.probe_job_id not in self.lineage.development_probe_job_ids
                    or self.lineage.development_probe_report_sha256s.get(origin.probe_job_id)
                    != origin.probe_report_sha256
                    or self.lineage.development_probe_report_sha256s.get(media.probe_job_id)
                    != media.probe_report_sha256
                    or self.lineage.development_probe_result_manifest_sha256s.get(origin.probe_job_id)
                    != origin.probe_result_manifest_sha256
                    or self.lineage.development_probe_result_manifest_sha256s.get(media.probe_job_id)
                    != media.probe_result_manifest_sha256
                ):
                    raise ValueError("detector candidate provenance changed from lineage")
            elif self.check_probe_authority is not None and (
                media.probe_job_id != self.check_probe_authority.job_id
                or media.probe_report_sha256 != self.check_probe_authority.report_sha256
                or media.probe_result_manifest_sha256 != self.check_probe_authority.result_manifest_sha256
            ):
                raise ValueError("check review media changed from check authority")
            if historical is None:
                if (
                    origin.probe_job_id != media.probe_job_id
                    or origin.probe_report_sha256 != media.probe_report_sha256
                    or origin.probe_result_manifest_sha256 != media.probe_result_manifest_sha256
                ):
                    raise ValueError("direct candidate origin differs from review media")
            elif (
                origin.probe_job_id != historical.probe_job_id
                or origin.probe_report_sha256 != historical.probe_report_sha256
                or origin.probe_result_manifest_sha256 != historical.probe_result_manifest_sha256
                or origin.candidate_evidence_sha256 != historical.candidate_evidence_sha256
                or media.probe_job_id != self.frame_review_proxy_authority.probe_job_id
                or media.probe_report_sha256 != self.frame_review_proxy_authority.probe_report_sha256
                or media.probe_result_manifest_sha256 != self.frame_review_proxy_authority.probe_result_manifest_sha256
            ):
                raise ValueError("proxy candidate provenance changed from inheritance")
            decision = evidence.decision
            if decision is None:
                continue
            revision = revisions_by_id.get(decision.revision_id)
            expected_kind = revision.accepted_suggestion_kind if revision is not None else None
            expected_id = revision.accepted_suggestion_id if revision is not None else None
            expected_job = revision.accepted_suggestion_job_id if revision is not None else None
            expected_sha = revision.accepted_suggestion_sha256 if revision is not None else None
            if decision.decision == "dismissed_manual_annotation" and revision is not None:
                expected_kind = revision.dismissed_suggestion_kind
                expected_id = revision.dismissed_suggestion_id
                expected_job = revision.dismissed_suggestion_job_id
                expected_sha = revision.dismissed_suggestion_sha256
            if (
                revision is None
                or revision.revision != decision.revision
                or revision.operator_id != decision.operator_id
                or expected_kind != "detector_candidate"
                or expected_id != evidence.candidate.candidate_id
                or expected_job != evidence.candidate_origin.probe_job_id
                or expected_sha != evidence.candidate_sha256
            ):
                raise ValueError("detector candidate decision lacks bound revision audit")
        pending_detector = sum(row.decision is None for row in self.detector_candidate_evidence)
        if (
            pending_detector != 0
            or self.dataset_expansion_eligibility.validation_evidence.pending_propagation_suggestion_count != 0
            or self.dataset_expansion_eligibility.validation_evidence.pending_suggestion_decision_count != 0
            or any(report.decision_counts.pending != 0 for report in self.propagation_reports)
            or any(
                suggestion.pending_human_confirmation
                for report in self.propagation_reports
                for suggestion in report.suggestions
            )
            or any(
                row.pending_human_confirmation for report in self.propagation_reports for row in report.frame_results
            )
            or self.dataset_expansion_eligibility.validation_evidence.pending_detector_candidate_count != 0
            or canonical_sha256([row.model_dump(mode="json") for row in self.detector_candidate_evidence])
            != self.detector_candidate_evidence_sha256
        ):
            raise ValueError("detector candidate evidence does not bind eligibility")
        primary_indices = self.sampling_manifest.frame_indices
        effective_indices = [row.frame_index for row in self.effective_annotations]
        media_indices = [row.frame_index for row in self.frame_media]
        evidence_indices = [row.frame_index for row in self.frame_evidence]
        expected_indices = sorted([*primary_indices, *self.supplemental_frame_indices])
        if (
            primary_indices != sorted(set(primary_indices))
            or self.supplemental_frame_indices != sorted(set(self.supplemental_frame_indices))
            or set(primary_indices) & set(self.supplemental_frame_indices)
            or effective_indices != expected_indices
            or media_indices != expected_indices
            or evidence_indices != expected_indices
        ):
            raise ValueError("package frame collections do not bind the sampled roles")
        reports_by_job = {row.job_id: row for row in self.propagation_reports}
        if len(reports_by_job) != len(self.propagation_reports):
            raise ValueError("package propagation report identities are duplicated")
        producing_jobs: set[str] = set()
        for evidence in self.frame_evidence:
            if evidence.frame_role == "supplemental":
                assert evidence.propagation_evidence is not None
                producing_jobs.add(evidence.propagation_evidence.propagation_job_id)
                report = reports_by_job.get(evidence.propagation_evidence.propagation_job_id)
                if (
                    report is None
                    or report.session_id != self.session_id
                    or report.report_sha256 != evidence.propagation_evidence.propagation_report_sha256
                ):
                    raise ValueError("supplemental frame does not bind its sealed report")
        if producing_jobs != set(reports_by_job):
            raise ValueError("package contains unreferenced propagation reports")
        proxy_rows = [row for row in self.frame_evidence if row.proxy_binding is not None]
        if self.frame_review_proxy_authority is None:
            if proxy_rows:
                raise ValueError("proxy frame rows lack frozen child authority")
        else:
            authority = self.frame_review_proxy_authority
            sealed_child_authority = detector_probe_authorities_by_job.get(authority.probe_job_id)
            manifest = authority.review_proxy_manifest
            rows_by_index = {row.frame_index: row for row in proxy_rows}
            if self.data_role == "development":
                proxy_authority_invalid = (
                    authority.probe_job_id not in self.lineage.development_probe_job_ids
                    or self.lineage.development_probe_report_sha256s.get(authority.probe_job_id)
                    != authority.probe_report_sha256
                    or self.lineage.development_probe_result_manifest_sha256s.get(authority.probe_job_id)
                    != authority.probe_result_manifest_sha256
                )
            else:
                proxy_authority_invalid = (
                    self.check_probe_authority is None
                    or authority.probe_job_id != self.check_probe_authority.job_id
                    or authority.probe_report_sha256 != self.check_probe_authority.report_sha256
                    or authority.probe_result_manifest_sha256 != self.check_probe_authority.result_manifest_sha256
                )
            if (
                proxy_authority_invalid
                or sealed_child_authority is None
                or sealed_child_authority.probe_report != authority.probe_report
                or sealed_child_authority.probe_result_manifest != authority.probe_result_manifest
                or manifest.source.sha256 != self.source.sha256
                or manifest.source.file_identity_sha256 != self.source.file_identity_sha256
                or manifest.source.size_bytes != self.source.size_bytes
                or manifest.source.width != self.source.width
                or manifest.source.height != self.source.height
                or manifest.source.frame_count != self.source.frame_count
                or not math.isclose(
                    manifest.source.fps,
                    self.source.fps,
                    rel_tol=0.0,
                    abs_tol=1e-12,
                )
            ):
                raise ValueError("proxy authority changed from development lineage")
            historical = authority.historical_probe_authority
            if historical is not None:
                sealed_parent_authority = detector_probe_authorities_by_job.get(historical.probe_job_id)
                if (
                    sealed_parent_authority is None
                    or sealed_parent_authority.probe_report != historical.probe_report
                    or sealed_parent_authority.probe_result_manifest != historical.probe_result_manifest
                ):
                    raise ValueError("historical proxy authority differs from exact parent job authority")
            if sorted(rows_by_index) != manifest.expected_frame_indices:
                raise ValueError("proxy frame rows differ from child authority")
            for mapping in manifest.mappings:
                row = rows_by_index[mapping.source_frame_index]
                proxy = row.proxy_binding
                assert proxy is not None
                if (
                    row.probe_evidence.probe_job_id != authority.probe_job_id
                    or row.probe_evidence.probe_report_sha256 != authority.probe_report_sha256
                    or row.probe_evidence.probe_result_manifest_sha256 != authority.probe_result_manifest_sha256
                    or proxy.map_sha256 != manifest.mapping_sha256
                    or proxy.proxy.sha256 != manifest.proxy.sha256
                    or proxy.proxy.size_bytes != manifest.proxy.size_bytes
                    or proxy.proxy.width != manifest.proxy.width
                    or proxy.proxy.height != manifest.proxy.height
                    or proxy.source_frame.frame_index != mapping.source_frame_index
                    or proxy.source_frame.timing_status != mapping.source_timing_status
                    or proxy.source_frame.decoder_reported_pos_msec != mapping.source_decoder_pos_msec
                    or proxy.source_frame.sha256 != mapping.source_frame_sha256
                    or proxy.proxy_frame.frame_index != mapping.proxy_frame_index
                    or proxy.proxy_frame.timing_basis != mapping.proxy_timing_basis
                    or proxy.proxy_frame.cfr_time_msec != mapping.proxy_cfr_time_msec
                    or proxy.proxy_frame.sha256 != mapping.proxy_frame_sha256
                ):
                    raise ValueError("proxy frame row changed from child authority")
        if (
            canonical_sha256([row.model_dump(mode="json") for row in self.frame_evidence]) != self.frame_evidence_sha256
            or canonical_sha256([row.model_dump(mode="json") for row in self.frame_media]) != self.frame_media_sha256
            or canonical_sha256(
                [
                    _canonical_ball_propagation_report(row, include_report_sha256=True)
                    for row in self.propagation_reports
                ]
            )
            != self.propagation_reports_sha256
        ):
            raise ValueError("package collection digests do not match their contents")
        package = self.model_dump(mode="json")
        package.pop("package_sha256")
        _canonical_ball_sampling_manifest(package["sampling_manifest"])
        package["propagation_reports"] = [
            _canonical_ball_propagation_report(row, include_report_sha256=True) for row in self.propagation_reports
        ]
        if canonical_sha256(package) != self.package_sha256:
            raise ValueError("annotation package digest does not match its contents")
        return self


class BallFeasibilityAuthorizationsView(BallAnnotationStrictView):
    may_expand_to_100_300_boxes: bool
    trial_eligible: Literal[False]
    source_segment_qualified: Literal[False]
    camera_qualified: Literal[False]
    production_approved: Literal[False]
    full_run_authorized: Literal[False]


class BallDevelopmentSealedEvidenceView(BallAnnotationStrictView):
    annotation_package_sha256: BallSha256
    sampling_manifest_sha256: BallSha256
    check_probe_job_id: None
    check_probe_report_sha256: None
    attempt_family_sha256: BallSha256
    dataset_expansion_eligibility: BallDatasetExpansionEligibilityView


class BallCheckSealedEvidenceView(BallAnnotationStrictView):
    annotation_package_sha256: BallSha256
    sampling_manifest_sha256: BallSha256
    sampling_lock_sha256: BallSha256
    check_probe_job_id: DetectorSafeId
    check_probe_report_sha256: BallSha256
    attempt_family_sha256: BallSha256
    development_annotation_session_id: DetectorSafeId
    development_annotation_package_sha256: BallSha256
    dataset_expansion_eligibility: BallDatasetExpansionEligibilityView


class BallDevelopmentFeasibilityReportView(BallAnnotationStrictView):
    schema_version: Literal["1.0"]
    artifact_type: Literal["ball_feasibility_report"]
    session_id: DetectorSafeId
    attempt_family_sha256: BallSha256
    development_package_binding: None
    status: Literal["not_applicable"]
    reason: Literal["development_package_is_not_one_time_check_evidence"]
    sealed_evidence: BallDevelopmentSealedEvidenceView
    authorizations: BallFeasibilityAuthorizationsView
    report_sha256: BallSha256

    @model_validator(mode="after")
    def validate_development_report(
        self,
    ) -> "BallDevelopmentFeasibilityReportView":
        if (
            self.sealed_evidence.attempt_family_sha256 != self.attempt_family_sha256
            or self.authorizations.may_expand_to_100_300_boxes
        ):
            raise ValueError("development feasibility authority is inconsistent")
        report = self.model_dump(mode="json")
        report.pop("report_sha256")
        if canonical_sha256(report) != self.report_sha256:
            raise ValueError("development feasibility report digest is invalid")
        return self


class BallFeasibilityMatchingRuleView(BallAnnotationStrictView):
    name: Literal["confirmed-box-center-region-v1"]
    minimum_radius_source_px: Literal[4.0]
    confirmed_box_diagonal_multiplier: Literal[0.75]
    source_height_cap_divisor: Literal[45.0]
    one_to_one: Literal[True]


class BallFeasibilityApparentSizeRuleView(BallAnnotationStrictView):
    name: Literal["source-height-bound-ball-diagonal-v1"]
    plausible_diagonal_min_source_px: Literal[1.0]
    far_max_source_height_divisor: Literal[80.0]
    mid_max_source_height_divisor: Literal[40.0]
    near_max_source_height_multiplier: Literal[0.075]
    aspect_ratio_min: Literal[0.25]
    aspect_ratio_max: Literal[4.0]


class BallFeasibilityIntervalsView(BallAnnotationStrictView):
    confidence: Literal[0.95]
    recall: Literal["one-sided-wilson-score-v1"]
    false_candidates: Literal["bounded-hoeffding-upper-v1"]
    false_candidate_range: list[Literal[0.0, 5.0]] = Field(min_length=2, max_length=2)


class BallFeasibilityMetricProfileView(BallAnnotationStrictView):
    profile_id: Literal["tiny_ball_feasibility_metric_v1"]
    candidate_budget: Literal[5]
    top1_recall_target: Literal[0.6]
    top5_recall_target: Literal[0.8]
    minimum_total_frames: Literal[20]
    maximum_total_frames: Literal[50]
    minimum_localizable_positives: Literal[15]
    minimum_confirmed_absent: Literal[5]
    minimum_applicable_stratum_positives: Literal[3]
    exploratory_small_n_threshold: Literal[10]
    apparent_size_rule: BallFeasibilityApparentSizeRuleView
    matching_rule: BallFeasibilityMatchingRuleView
    intervals: BallFeasibilityIntervalsView


class BallFeasibilityComputedSourceBoundsView(BallAnnotationStrictView):
    source_height_px: int = Field(gt=0)
    plausible_diagonal_min_source_px: float = Field(gt=0, allow_inf_nan=False)
    far_diagonal_max_source_px: float = Field(gt=0, allow_inf_nan=False)
    mid_diagonal_max_source_px: float = Field(gt=0, allow_inf_nan=False)
    near_diagonal_max_source_px: float = Field(gt=0, allow_inf_nan=False)
    plausible_diagonal_max_source_px: float = Field(gt=0, allow_inf_nan=False)
    aspect_ratio_min: float = Field(gt=0, allow_inf_nan=False)
    aspect_ratio_max: float = Field(gt=0, allow_inf_nan=False)
    matching_radius_cap_source_px: float = Field(gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_bounds(self) -> "BallFeasibilityComputedSourceBoundsView":
        if not (
            0
            < self.far_diagonal_max_source_px
            <= self.mid_diagonal_max_source_px
            <= self.near_diagonal_max_source_px
            == self.plausible_diagonal_max_source_px
            and self.plausible_diagonal_min_source_px <= self.plausible_diagonal_max_source_px
            and self.aspect_ratio_min < self.aspect_ratio_max
        ):
            raise ValueError("computed source-pixel bounds are inconsistent")
        expected = {
            "plausible_diagonal_min_source_px": 1.0,
            "far_diagonal_max_source_px": self.source_height_px / 80.0,
            "mid_diagonal_max_source_px": self.source_height_px / 40.0,
            "near_diagonal_max_source_px": self.source_height_px * 0.075,
            "plausible_diagonal_max_source_px": self.source_height_px * 0.075,
            "aspect_ratio_min": 0.25,
            "aspect_ratio_max": 4.0,
            "matching_radius_cap_source_px": max(4.0, self.source_height_px / 45.0),
        }
        observed = self.model_dump(mode="json")
        if any(abs(observed[name] - value) > 1e-12 for name, value in expected.items()):
            raise ValueError("computed source-pixel bounds differ from metric v1")
        return self


class BallFeasibilityScaleSupportView(BallAnnotationStrictView):
    near: int = Field(ge=0)
    mid: int = Field(ge=0)
    far: int = Field(ge=0)


class BallFeasibilityLightingSupportView(BallAnnotationStrictView):
    bright_sun: int = Field(ge=0)
    shadow: int = Field(ge=0)
    backlight: int = Field(ge=0)
    twilight: int = Field(ge=0)
    artificial_light: int = Field(ge=0)


class BallFeasibilitySupportView(BallAnnotationStrictView):
    total_frames: int = Field(ge=0)
    localizable_positives: int = Field(ge=0)
    confirmed_absent: int = Field(ge=0)
    excluded_or_unresolvable: int = Field(ge=0)
    scale: BallFeasibilityScaleSupportView
    lighting: BallFeasibilityLightingSupportView
    applicable_scale_strata: list[Literal["near", "mid", "far"]]
    applicable_lighting_strata: list[
        Literal[
            "bright_sun",
            "shadow",
            "backlight",
            "twilight",
            "artificial_light",
        ]
    ]
    missing: list[str]


def _ball_feasibility_wilson_lower(successes: int, total: int) -> float:
    if total <= 0:
        return 0.0
    z = 1.6448536269514722
    point = successes / total
    denominator = 1.0 + z * z / total
    center = point + z * z / (2.0 * total)
    spread = z * math.sqrt((point * (1.0 - point) + z * z / (4.0 * total)) / total)
    return max(0.0, (center - spread) / denominator)


def _ball_feasibility_hoeffding_upper(point: float, total: int) -> float:
    if total <= 0:
        return 5.0
    radius = 5.0 * math.sqrt(math.log(1.0 / 0.05) / (2.0 * total))
    return min(5.0, point + radius)


class BallFeasibilityRawMetricView(BallAnnotationStrictView):
    numerator: int = Field(ge=0)
    denominator: int = Field(ge=0)


class BallFeasibilityRecallMetricView(BallAnnotationStrictView):
    raw: BallFeasibilityRawMetricView
    point_estimate: float = Field(ge=0, le=1, allow_inf_nan=False)
    one_sided_95_lower: float = Field(ge=0, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_recall(self) -> "BallFeasibilityRecallMetricView":
        if self.raw.numerator > self.raw.denominator:
            raise ValueError("recall numerator exceeds denominator")
        point = self.raw.numerator / self.raw.denominator if self.raw.denominator else 0.0
        expected_lower = _ball_feasibility_wilson_lower(self.raw.numerator, self.raw.denominator)
        if abs(self.point_estimate - point) > 1e-12 or abs(self.one_sided_95_lower - expected_lower) > 1e-12:
            raise ValueError("recall estimates do not match raw counts")
        return self


class BallFeasibilityUpperMetricView(BallAnnotationStrictView):
    raw: BallFeasibilityRawMetricView
    point_estimate: float = Field(ge=0, le=5, allow_inf_nan=False)
    one_sided_95_upper: float = Field(ge=0, le=5, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_bounded_upper(self) -> "BallFeasibilityUpperMetricView":
        if self.raw.numerator > 5 * self.raw.denominator:
            raise ValueError("bounded false-candidate numerator exceeds range")
        point = self.raw.numerator / self.raw.denominator if self.raw.denominator else 0.0
        expected_upper = _ball_feasibility_hoeffding_upper(point, self.raw.denominator)
        if abs(self.point_estimate - point) > 1e-12 or abs(self.one_sided_95_upper - expected_upper) > 1e-12:
            raise ValueError("false-candidate estimates do not match raw counts")
        return self


class BallFeasibilityPointMetricView(BallAnnotationStrictView):
    raw: BallFeasibilityRawMetricView
    point_estimate: float = Field(ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_point_estimate(self) -> "BallFeasibilityPointMetricView":
        point = self.raw.numerator / self.raw.denominator if self.raw.denominator else 0.0
        if abs(self.point_estimate - point) > 1e-12:
            raise ValueError("point estimate does not match raw counts")
        return self


class BallFeasibilityStratumSupportView(BallAnnotationStrictView):
    localizable_positives: int = Field(ge=0)
    confirmed_absent: int = Field(ge=0)
    evaluable_frames: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_support(self) -> "BallFeasibilityStratumSupportView":
        if self.evaluable_frames != (self.localizable_positives + self.confirmed_absent):
            raise ValueError("stratum support denominator is inconsistent")
        return self


class BallFeasibilityCandidateTotalsView(BallAnnotationStrictView):
    false: int = Field(ge=0)
    scored: int = Field(ge=0)
    raw: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_totals(self) -> "BallFeasibilityCandidateTotalsView":
        if self.false > self.scored or self.scored > self.raw:
            raise ValueError("stratum candidate totals are inconsistent")
        return self


class BallFeasibilityStratumMetricView(BallAnnotationStrictView):
    support: BallFeasibilityStratumSupportView
    top1_recall: BallFeasibilityRecallMetricView
    top5_recall: BallFeasibilityRecallMetricView
    candidate_totals: BallFeasibilityCandidateTotalsView
    false_candidates_per_evaluable_frame: BallFeasibilityUpperMetricView
    exploratory_small_n: bool

    @model_validator(mode="after")
    def validate_denominators(self) -> "BallFeasibilityStratumMetricView":
        positives = self.support.localizable_positives
        evaluable = self.support.evaluable_frames
        if (
            self.top1_recall.raw.denominator != positives
            or self.top5_recall.raw.denominator != positives
            or self.false_candidates_per_evaluable_frame.raw.denominator != evaluable
            or self.false_candidates_per_evaluable_frame.raw.numerator != self.candidate_totals.false
            or self.top1_recall.raw.numerator > self.top5_recall.raw.numerator
            or self.candidate_totals.false != self.candidate_totals.scored - self.top5_recall.raw.numerator
            or self.exploratory_small_n != (positives < 10)
        ):
            raise ValueError("stratum metric denominators are inconsistent")
        return self


class BallFeasibilityScaleMetricsView(BallAnnotationStrictView):
    near: BallFeasibilityStratumMetricView
    mid: BallFeasibilityStratumMetricView
    far: BallFeasibilityStratumMetricView


class BallFeasibilityLightingMetricsView(BallAnnotationStrictView):
    bright_sun: BallFeasibilityStratumMetricView
    shadow: BallFeasibilityStratumMetricView
    backlight: BallFeasibilityStratumMetricView
    twilight: BallFeasibilityStratumMetricView
    artificial_light: BallFeasibilityStratumMetricView


class BallFeasibilityMotionOcclusionMetricsView(BallAnnotationStrictView):
    none: BallFeasibilityStratumMetricView
    ground: BallFeasibilityStratumMetricView
    airborne: BallFeasibilityStratumMetricView
    motion_blurred: BallFeasibilityStratumMetricView
    occluded: BallFeasibilityStratumMetricView
    reappearance: BallFeasibilityStratumMetricView
    stationary: BallFeasibilityStratumMetricView


class BallFeasibilityStrataMetricsView(BallAnnotationStrictView):
    scale: BallFeasibilityScaleMetricsView
    lighting: BallFeasibilityLightingMetricsView
    motion_occlusion: BallFeasibilityMotionOcclusionMetricsView


class BallFeasibilityMetricsView(BallAnnotationStrictView):
    top1_recall: BallFeasibilityRecallMetricView
    top5_recall: BallFeasibilityRecallMetricView
    false_candidates_per_evaluable_frame: BallFeasibilityUpperMetricView
    candidates_per_evaluable_frame: BallFeasibilityPointMetricView
    raw_candidates_per_evaluable_frame: BallFeasibilityPointMetricView

    @model_validator(mode="after")
    def validate_metric_family(self) -> "BallFeasibilityMetricsView":
        positive_denominator = self.top1_recall.raw.denominator
        evaluable_denominator = self.false_candidates_per_evaluable_frame.raw.denominator
        if (
            self.top5_recall.raw.denominator != positive_denominator
            or self.top1_recall.raw.numerator > self.top5_recall.raw.numerator
            or self.candidates_per_evaluable_frame.raw.denominator != evaluable_denominator
            or self.raw_candidates_per_evaluable_frame.raw.denominator != evaluable_denominator
            or self.candidates_per_evaluable_frame.raw.numerator > self.raw_candidates_per_evaluable_frame.raw.numerator
            or self.candidates_per_evaluable_frame.raw.numerator > 5 * evaluable_denominator
            or self.false_candidates_per_evaluable_frame.raw.numerator
            != self.candidates_per_evaluable_frame.raw.numerator - self.top5_recall.raw.numerator
        ):
            raise ValueError("feasibility metric family is internally inconsistent")
        return self


class BallFeasibilityCandidateDiagnosticView(BallAnnotationStrictView):
    rank: int = Field(ge=1, le=5)
    matched: bool
    center_distance_source_px: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    iou: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    evaluation_radius_source_px: float | None = Field(default=None, gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_match_evidence(self) -> "BallFeasibilityCandidateDiagnosticView":
        measured = (
            self.center_distance_source_px is not None,
            self.iou is not None,
            self.evaluation_radius_source_px is not None,
        )
        if any(measured) and not all(measured):
            raise ValueError("candidate measurement tuple must be complete")
        if not any(measured):
            if self.matched:
                raise ValueError("unmeasured candidate cannot be matched")
            return self
        assert self.center_distance_source_px is not None
        assert self.evaluation_radius_source_px is not None
        if self.evaluation_radius_source_px <= 0 or self.matched != (
            self.center_distance_source_px <= self.evaluation_radius_source_px
        ):
            raise ValueError("candidate match does not follow the evaluation radius")
        return self


BallFeasibilityDiagnosticCode = Annotated[
    str,
    Field(
        pattern=(
            r"^(bbox_diagonal_below_minimum|bbox_diagonal_above_maximum|"
            r"bbox_aspect_ratio_out_of_bounds|"
            r"scale_stratum_mismatch:(near|mid|far|not_applicable):(near|mid|far)|"
            r"lighting_stratum_mismatch:(bright_sun|shadow|backlight|twilight|"
            r"artificial_light|not_applicable):(bright_sun|shadow|backlight|"
            r"twilight|artificial_light))$"
        )
    ),
]


class BallFeasibilityFrameView(BallAnnotationStrictView):
    frame_index: int = Field(ge=0)
    presence: Literal["present", "absent", "unknown"]
    metric_eligible: bool
    scored_candidate_count: int = Field(ge=0, le=5)
    raw_candidate_count: int = Field(ge=0)
    top1_hit: bool | None
    top5_hit: bool | None
    candidate_diagnostics: list[BallFeasibilityCandidateDiagnosticView] = Field(max_length=5)
    observed_lighting_tag: Literal[
        "bright_sun",
        "shadow",
        "backlight",
        "twilight",
        "artificial_light",
        "not_applicable",
    ]
    frozen_lighting_stratum: Literal["bright_sun", "shadow", "backlight", "twilight", "artificial_light"]
    observed_scale_stratum: Literal["near", "mid", "far", "not_applicable"]
    derived_scale_stratum: Literal["near", "mid", "far"] | None
    bbox_diagonal_source_px: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    bbox_aspect_ratio: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    motion_occlusion_tags: list[
        Literal[
            "ground",
            "airborne",
            "motion_blurred",
            "occluded",
            "reappearance",
            "stationary",
        ]
    ] = Field(max_length=6)
    diagnostic_codes: list[BallFeasibilityDiagnosticCode] = Field(max_length=8)

    @model_validator(mode="after")
    def validate_frame_metric_state(self) -> "BallFeasibilityFrameView":
        if self.raw_candidate_count < self.scored_candidate_count:
            raise ValueError("raw candidate count is below scored Top-5 count")
        if len(self.motion_occlusion_tags) != len(set(self.motion_occlusion_tags)):
            raise ValueError("motion/occlusion tags must be unique")
        if len(self.diagnostic_codes) != len(set(self.diagnostic_codes)):
            raise ValueError("diagnostic codes must be unique")
        if self.metric_eligible:
            expected_ranks = list(range(1, self.scored_candidate_count + 1))
            observed_ranks = [row.rank for row in self.candidate_diagnostics]
            matched = [row.matched for row in self.candidate_diagnostics]
            expected_top1 = bool(matched and matched[0])
            expected_top5 = any(matched)
            if (
                self.top1_hit is None
                or self.top5_hit is None
                or len(self.candidate_diagnostics) != self.scored_candidate_count
                or observed_ranks != expected_ranks
                or self.presence == "unknown"
                or self.top1_hit != expected_top1
                or self.top5_hit != expected_top5
                or (self.presence == "absent" and any(matched))
            ):
                raise ValueError("eligible frame metric evidence is inconsistent")
        elif self.top1_hit is not None or self.top5_hit is not None or self.candidate_diagnostics:
            raise ValueError("ineligible frame cannot contribute metric evidence")
        return self


def _ball_feasibility_false_candidate_count(
    frame: BallFeasibilityFrameView,
) -> int:
    if frame.presence == "present":
        return frame.scored_candidate_count - int(bool(frame.top5_hit))
    return frame.scored_candidate_count


def _ball_feasibility_stratum_metric_payload(
    frames: list[BallFeasibilityFrameView],
    *,
    exploratory_threshold: int,
) -> dict[str, Any]:
    positives = [row for row in frames if row.presence == "present"]
    absent = [row for row in frames if row.presence == "absent"]
    positive_count = len(positives)
    evaluable_count = len(frames)
    top1_hits = sum(bool(row.top1_hit) for row in positives)
    top5_hits = sum(bool(row.top5_hit) for row in positives)
    false_total = sum(_ball_feasibility_false_candidate_count(row) for row in frames)
    scored_total = sum(row.scored_candidate_count for row in frames)
    raw_total = sum(row.raw_candidate_count for row in frames)
    false_point = false_total / evaluable_count if evaluable_count else 0.0
    return {
        "support": {
            "localizable_positives": positive_count,
            "confirmed_absent": len(absent),
            "evaluable_frames": evaluable_count,
        },
        "top1_recall": {
            "raw": {"numerator": top1_hits, "denominator": positive_count},
            "point_estimate": top1_hits / positive_count if positive_count else 0.0,
            "one_sided_95_lower": _ball_feasibility_wilson_lower(top1_hits, positive_count),
        },
        "top5_recall": {
            "raw": {"numerator": top5_hits, "denominator": positive_count},
            "point_estimate": top5_hits / positive_count if positive_count else 0.0,
            "one_sided_95_lower": _ball_feasibility_wilson_lower(top5_hits, positive_count),
        },
        "candidate_totals": {
            "false": false_total,
            "scored": scored_total,
            "raw": raw_total,
        },
        "false_candidates_per_evaluable_frame": {
            "raw": {"numerator": false_total, "denominator": evaluable_count},
            "point_estimate": false_point,
            "one_sided_95_upper": _ball_feasibility_hoeffding_upper(false_point, evaluable_count),
        },
        "exploratory_small_n": positive_count < exploratory_threshold,
    }


def _ball_feasibility_strata_metrics_payload(
    eligible: list[BallFeasibilityFrameView],
    *,
    exploratory_threshold: int,
) -> dict[str, Any]:
    scale = {
        stratum: _ball_feasibility_stratum_metric_payload(
            [row for row in eligible if row.presence == "present" and row.observed_scale_stratum == stratum],
            exploratory_threshold=exploratory_threshold,
        )
        for stratum in ("near", "mid", "far")
    }
    lighting = {
        stratum: _ball_feasibility_stratum_metric_payload(
            [row for row in eligible if row.observed_lighting_tag == stratum],
            exploratory_threshold=exploratory_threshold,
        )
        for stratum in (
            "bright_sun",
            "shadow",
            "backlight",
            "twilight",
            "artificial_light",
        )
    }
    motion_occlusion = {
        stratum: _ball_feasibility_stratum_metric_payload(
            [
                row
                for row in eligible
                if row.presence == "present"
                and ((stratum == "none" and not row.motion_occlusion_tags) or stratum in row.motion_occlusion_tags)
            ],
            exploratory_threshold=exploratory_threshold,
        )
        for stratum in (
            "none",
            "ground",
            "airborne",
            "motion_blurred",
            "occluded",
            "reappearance",
            "stationary",
        )
    }
    return {
        "scale": scale,
        "lighting": lighting,
        "motion_occlusion": motion_occlusion,
    }


def _ball_feasibility_metrics_payload(
    eligible: list[BallFeasibilityFrameView],
) -> dict[str, Any]:
    positives = [row for row in eligible if row.presence == "present"]
    positive_count = len(positives)
    evaluable_count = len(eligible)
    top1_hits = sum(bool(row.top1_hit) for row in positives)
    top5_hits = sum(bool(row.top5_hit) for row in positives)
    false_total = sum(_ball_feasibility_false_candidate_count(row) for row in eligible)
    scored_total = sum(row.scored_candidate_count for row in eligible)
    raw_total = sum(row.raw_candidate_count for row in eligible)
    false_point = false_total / evaluable_count if evaluable_count else 0.0
    return {
        "top1_recall": {
            "raw": {"numerator": top1_hits, "denominator": positive_count},
            "point_estimate": top1_hits / positive_count if positive_count else 0.0,
            "one_sided_95_lower": _ball_feasibility_wilson_lower(top1_hits, positive_count),
        },
        "top5_recall": {
            "raw": {"numerator": top5_hits, "denominator": positive_count},
            "point_estimate": top5_hits / positive_count if positive_count else 0.0,
            "one_sided_95_lower": _ball_feasibility_wilson_lower(top5_hits, positive_count),
        },
        "false_candidates_per_evaluable_frame": {
            "raw": {"numerator": false_total, "denominator": evaluable_count},
            "point_estimate": false_point,
            "one_sided_95_upper": _ball_feasibility_hoeffding_upper(false_point, evaluable_count),
        },
        "candidates_per_evaluable_frame": {
            "raw": {"numerator": scored_total, "denominator": evaluable_count},
            "point_estimate": scored_total / evaluable_count if evaluable_count else 0.0,
        },
        "raw_candidates_per_evaluable_frame": {
            "raw": {"numerator": raw_total, "denominator": evaluable_count},
            "point_estimate": raw_total / evaluable_count if evaluable_count else 0.0,
        },
    }


def _ball_feasibility_expected_diagnostic_codes(
    frame: BallFeasibilityFrameView,
    bounds: BallFeasibilityComputedSourceBoundsView,
) -> list[str]:
    expected: list[str] = []
    if frame.observed_lighting_tag != frame.frozen_lighting_stratum:
        expected.append(f"lighting_stratum_mismatch:{frame.frozen_lighting_stratum}:{frame.observed_lighting_tag}")
    diagonal = frame.bbox_diagonal_source_px
    aspect_ratio = frame.bbox_aspect_ratio
    if frame.presence != "present" and frame.observed_scale_stratum != "not_applicable":
        raise ValueError("non-present frame cannot claim an observed scale")
    if (diagonal is None) != (aspect_ratio is None):
        raise ValueError("frame box diagnostics must be present together")
    if diagonal is None:
        if frame.derived_scale_stratum is not None or (
            frame.presence == "present" and frame.observed_scale_stratum != "not_applicable"
        ):
            raise ValueError("frame without a measured box cannot derive scale")
        return expected
    if frame.presence != "present":
        raise ValueError("non-present frame cannot expose measured box diagnostics")
    if frame.observed_scale_stratum == "not_applicable":
        raise ValueError("measured present frame requires an observed scale")
    if diagonal < bounds.plausible_diagonal_min_source_px:
        expected.append("bbox_diagonal_below_minimum")
    if diagonal > bounds.plausible_diagonal_max_source_px:
        expected.append("bbox_diagonal_above_maximum")
    if not bounds.aspect_ratio_min <= aspect_ratio <= bounds.aspect_ratio_max:
        expected.append("bbox_aspect_ratio_out_of_bounds")
    derived_scale: Literal["near", "mid", "far"] | None = None
    tolerance = 1e-9
    if bounds.plausible_diagonal_min_source_px <= diagonal <= bounds.plausible_diagonal_max_source_px + tolerance:
        if diagonal <= bounds.far_diagonal_max_source_px + tolerance:
            derived_scale = "far"
        elif diagonal <= bounds.mid_diagonal_max_source_px + tolerance:
            derived_scale = "mid"
        else:
            derived_scale = "near"
    if frame.derived_scale_stratum != derived_scale:
        raise ValueError("derived scale does not match measured box diagnostics")
    if derived_scale is not None and frame.observed_scale_stratum != derived_scale:
        expected.append(f"scale_stratum_mismatch:{frame.observed_scale_stratum}:{derived_scale}")
    return expected


class BallFeasibilityContradictionView(BallAnnotationStrictView):
    frame_index: int = Field(ge=0)
    diagnostic_codes: list[BallFeasibilityDiagnosticCode] = Field(min_length=1, max_length=8)


class BallFeasibilityResolutionView(BallAnnotationStrictView):
    requires_new_attempt: bool
    reason_codes: list[
        Literal[
            "annotation_plausibility_contradiction",
            "scale_strata_mismatch",
            "lighting_strata_mismatch",
        ]
    ] = Field(max_length=3)
    raw_annotation_plausibility_contradiction_count: int = Field(ge=0)
    raw_scale_mismatch_count: int = Field(ge=0)
    raw_lighting_mismatch_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_resolution(self) -> "BallFeasibilityResolutionView":
        expected = [
            code
            for code, count in (
                (
                    "annotation_plausibility_contradiction",
                    self.raw_annotation_plausibility_contradiction_count,
                ),
                ("scale_strata_mismatch", self.raw_scale_mismatch_count),
                ("lighting_strata_mismatch", self.raw_lighting_mismatch_count),
            )
            if count > 0
        ]
        if self.reason_codes != expected or self.requires_new_attempt != bool(expected):
            raise ValueError("feasibility resolution reasons are inconsistent")
        return self


class BallCheckFeasibilityReportView(BallAnnotationStrictView):
    schema_version: Literal["1.0"]
    artifact_type: Literal["ball_feasibility_report"]
    session_id: DetectorSafeId
    source_sha256: BallSha256
    locked_profile_id: DetectorSafeId
    locked_profile_sha256: BallSha256
    sampling_manifest_sha256: BallSha256
    metric_profile: BallFeasibilityMetricProfileView
    metric_profile_sha256: BallSha256
    attempt_family_sha256: BallSha256
    development_package_binding: BallDevelopmentPackageBindingView
    computed_source_px_bounds: BallFeasibilityComputedSourceBoundsView
    status: Literal["insufficient_evidence", "feasibility_passed", "feasibility_failed"]
    support: BallFeasibilitySupportView
    metrics: BallFeasibilityMetricsView
    strata_metrics: BallFeasibilityStrataMetricsView
    frames: list[BallFeasibilityFrameView] = Field(min_length=20, max_length=50)
    contradictions: list[BallFeasibilityContradictionView] = Field(max_length=50)
    resolution: BallFeasibilityResolutionView
    authorizations: BallFeasibilityAuthorizationsView
    limitations: list[
        Literal[
            "one_time_directional_feasibility_only",
            "small_support_is_exploratory",
            "revealed_group_must_be_retired_for_all_profiles",
        ]
    ] = Field(min_length=3, max_length=3)
    sealed_evidence: BallCheckSealedEvidenceView
    report_sha256: BallSha256

    @model_validator(mode="after")
    def validate_check_report(self) -> "BallCheckFeasibilityReportView":
        if [row.frame_index for row in self.frames] != sorted({row.frame_index for row in self.frames}):
            raise ValueError("feasibility frame evidence is not canonical")
        expected_resolution_counts = {
            "raw_annotation_plausibility_contradiction_count": 0,
            "raw_scale_mismatch_count": 0,
            "raw_lighting_mismatch_count": 0,
        }
        size_codes = {
            "bbox_diagonal_below_minimum",
            "bbox_diagonal_above_maximum",
            "bbox_aspect_ratio_out_of_bounds",
        }
        matching_rule = self.metric_profile.matching_rule
        minimum_radius = matching_rule.minimum_radius_source_px
        source_radius_cap = max(
            minimum_radius,
            self.computed_source_px_bounds.source_height_px / matching_rule.source_height_cap_divisor,
        )
        for frame in self.frames:
            expected_codes = _ball_feasibility_expected_diagnostic_codes(frame, self.computed_source_px_bounds)
            if frame.diagnostic_codes != expected_codes:
                raise ValueError("frame diagnostic codes do not match frozen observations")
            expected_eligible = not expected_codes and (
                frame.presence == "absent"
                or (frame.presence == "present" and frame.bbox_diagonal_source_px is not None)
            )
            if frame.metric_eligible != expected_eligible:
                raise ValueError("frame metric eligibility does not match diagnostic evidence")
            if frame.presence == "present" and frame.bbox_diagonal_source_px is not None:
                expected_radius = min(
                    max(
                        minimum_radius,
                        frame.bbox_diagonal_source_px * matching_rule.confirmed_box_diagonal_multiplier,
                    ),
                    source_radius_cap,
                )
                if any(
                    diagnostic.evaluation_radius_source_px is not None
                    and not math.isclose(
                        diagnostic.evaluation_radius_source_px,
                        expected_radius,
                        rel_tol=0.0,
                        abs_tol=1e-12,
                    )
                    for diagnostic in frame.candidate_diagnostics
                ):
                    raise ValueError("candidate evaluation radius differs from metric authority")
            expected_resolution_counts["raw_annotation_plausibility_contradiction_count"] += int(
                any(code in size_codes for code in expected_codes)
            )
            expected_resolution_counts["raw_scale_mismatch_count"] += sum(
                code.startswith("scale_stratum_mismatch:") for code in expected_codes
            )
            expected_resolution_counts["raw_lighting_mismatch_count"] += sum(
                code.startswith("lighting_stratum_mismatch:") for code in expected_codes
            )
        expected_reason_codes = [
            reason
            for reason, count_name in (
                (
                    "annotation_plausibility_contradiction",
                    "raw_annotation_plausibility_contradiction_count",
                ),
                ("scale_strata_mismatch", "raw_scale_mismatch_count"),
                ("lighting_strata_mismatch", "raw_lighting_mismatch_count"),
            )
            if expected_resolution_counts[count_name] > 0
        ]
        expected_resolution = {
            "requires_new_attempt": bool(expected_reason_codes),
            "reason_codes": expected_reason_codes,
            **expected_resolution_counts,
        }
        if self.resolution.model_dump(mode="json") != expected_resolution:
            raise ValueError("feasibility resolution does not match frame diagnostics")
        eligible = [row for row in self.frames if row.metric_eligible]
        positives = [row for row in eligible if row.presence == "present"]
        absent = [row for row in eligible if row.presence == "absent"]
        scale_strata = ("near", "mid", "far")
        lighting_strata = (
            "bright_sun",
            "shadow",
            "backlight",
            "twilight",
            "artificial_light",
        )
        if self.support.applicable_scale_strata != [
            stratum for stratum in scale_strata if stratum in self.support.applicable_scale_strata
        ] or self.support.applicable_lighting_strata != [
            stratum for stratum in lighting_strata if stratum in self.support.applicable_lighting_strata
        ]:
            raise ValueError("applicable strata are not canonical")
        expected_scale_support = {
            stratum: sum(row.observed_scale_stratum == stratum for row in positives) for stratum in scale_strata
        }
        expected_lighting_support = {
            stratum: sum(row.observed_lighting_tag == stratum for row in positives) for stratum in lighting_strata
        }
        expected_missing: list[str] = []
        if not (
            self.metric_profile.minimum_total_frames <= len(self.frames) <= self.metric_profile.maximum_total_frames
        ):
            expected_missing.append("total_frame_support")
        if len(positives) < self.metric_profile.minimum_localizable_positives:
            expected_missing.append("localizable_positive_support")
        if len(absent) < self.metric_profile.minimum_confirmed_absent:
            expected_missing.append("confirmed_absent_support")
        for stratum in self.support.applicable_scale_strata:
            if expected_scale_support[stratum] < self.metric_profile.minimum_applicable_stratum_positives:
                expected_missing.append(f"scale:{stratum}")
        for stratum in scale_strata:
            if stratum not in self.support.applicable_scale_strata and expected_scale_support[stratum] > 0:
                expected_missing.append(f"applicability_contradiction:scale:{stratum}")
        for stratum in self.support.applicable_lighting_strata:
            if expected_lighting_support[stratum] < self.metric_profile.minimum_applicable_stratum_positives:
                expected_missing.append(f"lighting:{stratum}")
        for stratum in lighting_strata:
            if stratum not in self.support.applicable_lighting_strata and expected_lighting_support[stratum] > 0:
                expected_missing.append(f"applicability_contradiction:lighting:{stratum}")
        if expected_resolution_counts["raw_annotation_plausibility_contradiction_count"]:
            expected_missing.append("annotation_plausibility_contradiction")
        if expected_resolution_counts["raw_scale_mismatch_count"]:
            expected_missing.append("scale_strata_mismatch")
        if expected_resolution_counts["raw_lighting_mismatch_count"]:
            expected_missing.append("lighting_strata_mismatch")
        expected_metrics = _ball_feasibility_metrics_payload(eligible)
        expected_strata_metrics = _ball_feasibility_strata_metrics_payload(
            eligible,
            exploratory_threshold=self.metric_profile.exploratory_small_n_threshold,
        )
        expected_status = (
            "insufficient_evidence"
            if expected_missing
            else "feasibility_passed"
            if expected_metrics["top1_recall"]["point_estimate"] >= self.metric_profile.top1_recall_target
            and expected_metrics["top5_recall"]["point_estimate"] >= self.metric_profile.top5_recall_target
            else "feasibility_failed"
        )
        expected_support = {
            "total_frames": len(self.frames),
            "localizable_positives": len(positives),
            "confirmed_absent": len(absent),
            "excluded_or_unresolvable": len(self.frames) - len(eligible),
            "scale": expected_scale_support,
            "lighting": expected_lighting_support,
            "applicable_scale_strata": self.support.applicable_scale_strata,
            "applicable_lighting_strata": self.support.applicable_lighting_strata,
            "missing": expected_missing,
        }
        if (
            self.metric_profile_sha256 != canonical_sha256(self.metric_profile.model_dump(mode="json"))
            or self.support.model_dump(mode="json") != expected_support
            or self.metrics.model_dump(mode="json") != expected_metrics
            or self.strata_metrics.model_dump(mode="json") != expected_strata_metrics
            or self.status != expected_status
            or self.sealed_evidence.sampling_manifest_sha256 != self.sampling_manifest_sha256
            or self.sealed_evidence.attempt_family_sha256 != self.attempt_family_sha256
            or self.sealed_evidence.development_annotation_session_id != self.development_package_binding.session_id
            or self.sealed_evidence.development_annotation_package_sha256
            != self.development_package_binding.package_sha256
            or self.sealed_evidence.dataset_expansion_eligibility.reasons != ["check_role_is_evaluation_only"]
            or self.sealed_evidence.dataset_expansion_eligibility.validation_evidence.pending_suggestion_decision_count
            != 0
            or self.development_package_binding.attempt_family_sha256 != self.attempt_family_sha256
            or self.authorizations.may_expand_to_100_300_boxes != (expected_status == "feasibility_passed")
            or self.limitations
            != [
                "one_time_directional_feasibility_only",
                "small_support_is_exploratory",
                "revealed_group_must_be_retired_for_all_profiles",
            ]
        ):
            raise ValueError("check feasibility authority is inconsistent")
        frame_contradictions = [
            {
                "frame_index": row.frame_index,
                "diagnostic_codes": row.diagnostic_codes,
            }
            for row in self.frames
            if row.diagnostic_codes
        ]
        if [row.model_dump(mode="json") for row in self.contradictions] != frame_contradictions:
            raise ValueError("contradiction summary does not match frame diagnostics")
        report = self.model_dump(mode="json")
        report.pop("report_sha256")
        if canonical_sha256(report) != self.report_sha256:
            raise ValueError("check feasibility report digest is invalid")
        return self


class BallAnnotationFinalResultResponse(BallAnnotationStrictView):
    package: BallAnnotationPackageView
    feasibility_report: BallDevelopmentFeasibilityReportView | BallCheckFeasibilityReportView

    @model_validator(mode="after")
    def validate_package_report_binding(self) -> "BallAnnotationFinalResultResponse":
        package = self.package
        report = self.feasibility_report
        if (
            report.session_id != package.session_id
            or report.sealed_evidence.annotation_package_sha256 != package.package_sha256
            or report.sealed_evidence.sampling_manifest_sha256 != package.sampling_manifest.manifest_sha256
            or report.attempt_family_sha256 != package.attempt_family_sha256
        ):
            raise ValueError("final package and feasibility report authority differ")
        if package.data_role == "development":
            if not isinstance(report, BallDevelopmentFeasibilityReportView):
                raise ValueError("development package requires development report")
            if (
                report.sealed_evidence.check_probe_job_id is not None
                or report.sealed_evidence.check_probe_report_sha256 is not None
                or report.sealed_evidence.dataset_expansion_eligibility != package.dataset_expansion_eligibility
            ):
                raise ValueError("development report does not bind package eligibility")
        else:
            if not isinstance(report, BallCheckFeasibilityReportView):
                raise ValueError("check package requires check report")
            binding = package.development_package_binding
            authority = package.check_probe_authority
            expected_scale_strata = [
                row.stratum
                for row in package.sampling_manifest.strata_applicability.scale
                if row.status == "applicable"
            ]
            expected_lighting_strata = [
                row.stratum
                for row in package.sampling_manifest.strata_applicability.lighting
                if row.status == "applicable"
            ]
            if (
                binding is None
                or authority is None
                or report.development_package_binding != binding
                or report.sealed_evidence.development_annotation_session_id != binding.session_id
                or report.sealed_evidence.development_annotation_package_sha256 != binding.package_sha256
                or report.sealed_evidence.check_probe_job_id != package.check_probe_job_id
                or report.sealed_evidence.check_probe_job_id != authority.job_id
                or report.sealed_evidence.check_probe_report_sha256 != authority.report_sha256
                or report.sealed_evidence.dataset_expansion_eligibility != package.dataset_expansion_eligibility
                or report.source_sha256 != package.source.sha256
                or report.locked_profile_id != package.locked_profile.profile_id
                or report.locked_profile_sha256 != package.locked_profile.profile_sha256
                or report.computed_source_px_bounds.source_height_px != package.source.height
                or report.support.applicable_scale_strata != expected_scale_strata
                or report.support.applicable_lighting_strata != expected_lighting_strata
            ):
                raise ValueError("check report does not bind package probe authority")
            primary_indices = package.sampling_manifest.frame_indices
            if [frame.frame_index for frame in report.frames] != primary_indices:
                raise ValueError("check report frame set differs from primary sampling authority")
            annotations_by_frame = {annotation.frame_index: annotation for annotation in package.effective_annotations}
            groups_by_frame = {group.frame_index: group for group in package.sampling_manifest.groups}
            for frame in report.frames:
                annotation = annotations_by_frame.get(frame.frame_index)
                group = groups_by_frame.get(frame.frame_index)
                if annotation is None or group is None:
                    raise ValueError("check report frame lacks package truth authority")
                if annotation.bbox_source_px is None:
                    expected_diagonal = None
                    expected_aspect_ratio = None
                else:
                    width = annotation.bbox_source_px.right - annotation.bbox_source_px.left
                    height = annotation.bbox_source_px.bottom - annotation.bbox_source_px.top
                    expected_diagonal = math.hypot(width, height)
                    expected_aspect_ratio = width / height
                diagonal_matches = (frame.bbox_diagonal_source_px is None and expected_diagonal is None) or (
                    frame.bbox_diagonal_source_px is not None
                    and expected_diagonal is not None
                    and math.isclose(
                        frame.bbox_diagonal_source_px,
                        expected_diagonal,
                        rel_tol=0.0,
                        abs_tol=1e-12,
                    )
                )
                aspect_ratio_matches = (frame.bbox_aspect_ratio is None and expected_aspect_ratio is None) or (
                    frame.bbox_aspect_ratio is not None
                    and expected_aspect_ratio is not None
                    and math.isclose(
                        frame.bbox_aspect_ratio,
                        expected_aspect_ratio,
                        rel_tol=0.0,
                        abs_tol=1e-12,
                    )
                )
                if (
                    frame.presence != annotation.presence
                    or frame.observed_lighting_tag != annotation.lighting_tag
                    or frame.observed_scale_stratum != annotation.scale_stratum
                    or frame.motion_occlusion_tags != annotation.motion_occlusion_tags
                    or frame.frozen_lighting_stratum != group.pre_reveal_lighting_stratum
                    or not diagonal_matches
                    or not aspect_ratio_matches
                ):
                    raise ValueError("check report frame differs from package annotation authority")
        return self
