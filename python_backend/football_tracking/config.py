from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(slots=True)
class LoggingConfig:
    level: str = "INFO"
    save_debug_jsonl: bool = True


@dataclass(slots=True)
class DetectorConfig:
    model_path: Path
    device: str = "cuda:0"
    confidence_threshold: float = 0.15
    image_size: int = 1280
    use_half: bool = True
    allowed_labels: list[str] = field(default_factory=lambda: ["sports ball", "ball"])


@dataclass(slots=True)
class SahiConfig:
    slice_height: int = 720
    slice_width: int = 1280
    overlap_height_ratio: float = 0.20
    overlap_width_ratio: float = 0.20
    perform_standard_pred: bool = False
    postprocess_type: str = "NMS"
    postprocess_match_metric: str = "IOS"
    postprocess_match_threshold: float = 0.50
    verbose: int = 0


@dataclass(slots=True)
class FilteringConfig:
    min_confidence: float = 0.15
    min_width: float = 4.0
    max_width: float = 80.0
    min_height: float = 4.0
    max_height: float = 80.0
    min_aspect_ratio: float = 0.50
    max_aspect_ratio: float = 1.80
    roi: tuple[int, int, int, int] | None = None


@dataclass(slots=True)
class RelaxedFilteringConfig:
    min_confidence: float | None = None
    min_width: float | None = None
    min_height: float | None = None


@dataclass(slots=True)
class SceneZoneConfig:
    name: str
    roi: tuple[int, int, int, int] | None = None
    points: tuple[tuple[int, int], ...] = ()
    active_states: tuple[str, ...] = ()
    frame_range: tuple[int, int] | None = None
    relaxed_filtering: RelaxedFilteringConfig = field(default_factory=RelaxedFilteringConfig)
    selection_bonus: float = 0.0


@dataclass(slots=True)
class SceneBiasConfig:
    enabled: bool = False
    ground_zones: list[SceneZoneConfig] = field(default_factory=list)
    positive_rois: list[SceneZoneConfig] = field(default_factory=list)
    negative_rois: list[SceneZoneConfig] = field(default_factory=list)
    dynamic_air_recovery: "DynamicAirRecoveryConfig" = field(default_factory=lambda: DynamicAirRecoveryConfig())


@dataclass(slots=True)
class DynamicAirRecoveryConfig:
    enabled: bool = False
    profile: str = "fisheye_180_indoor"
    active_states: tuple[str, ...] = ("PREDICTING", "LOST")
    relaxed_filtering: RelaxedFilteringConfig = field(default_factory=RelaxedFilteringConfig)
    selection_bonus: float = 0.0
    reacquire_enabled: bool = True
    reacquire_confidence_threshold: float = 0.10
    reacquire_image_size: int = 1536
    burst_enabled: bool = True
    burst_frames: int = 6
    burst_confidence_threshold: float = 0.05
    burst_image_size: int = 1920
    burst_window_scale: float = 1.35
    low_quality_reject_enabled: bool = True
    low_quality_reject_confidence: float = 0.22
    low_quality_reject_score: float = 0.30
    low_quality_reject_min_lost_frames: int = 4
    tentative_reacquire_enabled: bool = True
    tentative_reacquire_min_lost_frames: int = 4
    tentative_reacquire_confidence_threshold: float = 0.30
    tentative_reacquire_score_threshold: float = 0.38
    tentative_reacquire_confirmation_radius: float = 140.0
    tentative_reacquire_max_age: int = 1
    gap_aware_jump_gate_enabled: bool = True
    gap_aware_short_lost_frames: int = 3
    gap_aware_short_jump_distance: float = 260.0
    gap_aware_long_jump_distance: float = 520.0
    gap_aware_high_confidence_bypass: float = 0.42
    edge_reentry_enabled: bool = True
    edge_reentry_min_lost_frames: int = 2
    edge_reentry_max_lost_frames: int = 80
    edge_reentry_margin_x_ratio: float = 0.08
    edge_reentry_margin_y_ratio: float = 0.08
    edge_reentry_expand_x: float = 260.0
    edge_reentry_expand_y: float = 240.0
    edge_reentry_high_confidence_bypass: float = 0.45
    isolated_far_jump_enabled: bool = False
    isolated_far_jump_min_lost_frames: int = 1
    isolated_far_jump_distance: float = 320.0
    isolated_far_jump_high_confidence_bypass: float = 0.50
    isolated_far_jump_confirmation_radius: float = 180.0
    true_out_of_view_enabled: bool = False
    true_out_of_view_min_lost_frames: int = 4
    true_out_of_view_min_empty_frames: int = 3
    true_out_of_view_edge_margin_x_ratio: float = 0.08
    true_out_of_view_edge_margin_y_ratio: float = 0.08
    true_out_of_view_high_confidence_bypass: float = 0.55
    true_out_of_view_jump_distance: float = 260.0
    true_out_of_view_confirmation_radius: float = 180.0
    ground_exit_enabled: bool = True
    ground_exit_min_lost_frames: int = 8


@dataclass(slots=True)
class PostprocessConfig:
    enabled: bool = False
    max_detected_island_length: int = 2
    stable_segment_min_length: int = 4
    min_jump_distance: float = 260.0
    nuisance_zone_jump_distance: float = 180.0
    low_confidence_threshold: float = 0.45
    save_cleaned_video: bool = True
    save_cleaned_csv: bool = True
    save_cleaned_debug_jsonl: bool = True
    cleaned_video_name: str = "annotated.cleaned.mp4"
    cleaned_csv_name: str = "ball_track.cleaned.csv"
    cleaned_debug_jsonl_name: str = "debug.cleaned.jsonl"
    cleanup_report_name: str = "cleanup_report.json"
    nuisance_zones: list[SceneZoneConfig] = field(default_factory=list)
    protected_ranges: list[tuple[int, int]] = field(default_factory=list)


@dataclass(slots=True)
class FollowCamConfig:
    enabled: bool = False
    prefer_cleaned_track: bool = True
    output_video_name: str = "follow_cam.mp4"
    camera_path_name: str = "camera_path.csv"
    report_name: str = "follow_cam_report.json"
    target_width: int = 1920
    target_height: int = 1080
    min_crop_height: int = 900
    max_crop_height: int = 1260
    home_center_x_ratio: float = 0.50
    home_center_y_ratio: float = 0.66
    ball_screen_x_ratio: float = 0.50
    ball_screen_y_ratio: float = 0.58
    dead_zone_ratio_x: float = 0.12
    dead_zone_ratio_y: float = 0.10
    pan_smoothing: float = 0.35
    glide_pan_smoothing: float = 0.12
    glide_max_pan_per_frame_x: float = 32.0
    glide_max_pan_per_frame_y: float = 18.0
    catch_up_pan_smoothing: float = 0.24
    catch_up_max_pan_per_frame_x: float = 64.0
    catch_up_max_pan_per_frame_y: float = 36.0
    catch_up_trigger_ratio_x: float = 0.26
    catch_up_trigger_ratio_y: float = 0.22
    catch_up_release_ratio_x: float = 0.18
    catch_up_release_ratio_y: float = 0.15
    zoom_smoothing: float = 0.20
    zoom_in_smoothing: float = 0.05
    zoom_out_smoothing: float = 0.08
    max_zoom_in_per_frame: float = 10.0
    max_zoom_out_per_frame: float = 18.0
    zoom_deadband_height: float = 80.0
    zoom_out_confirm_frames: int = 4
    zoom_in_confirm_frames: int = 8
    zoom_reverse_confirm_frames: int = 14
    zoom_hold_frames_after_change: int = 10
    velocity_smoothing: float = 0.35
    max_pan_per_frame_x: float = 90.0
    max_pan_per_frame_y: float = 55.0
    look_ahead_gain: float = 0.20
    look_ahead_max_px: float = 180.0
    speed_zoom_out_start: float = 22.0
    speed_zoom_out_end: float = 85.0
    predicted_zoom_out_bonus: float = 0.12
    lost_zoom_out_bonus: float = 0.30
    low_confidence_zoom_out_start: float = 0.40
    low_confidence_zoom_out_end: float = 0.18
    predicted_pan_decay: float = 0.75
    lost_hold_frames: int = 10
    lost_recenter_frames: int = 28
    recenter_smoothing: float = 0.08
    draw_ball_marker: bool = True
    draw_frame_text: bool = True


@dataclass(slots=True)
class SelectionWeights:
    distance_score: float = 0.32
    direction_score: float = 0.20
    velocity_score: float = 0.18
    acceleration_penalty: float = 0.14
    trajectory_length_bonus: float = 0.10
    confidence: float = 0.06


@dataclass(slots=True)
class SelectionConfig:
    min_accept_score: float = 0.18
    stable_history_length: int = 12
    weights: SelectionWeights = field(default_factory=SelectionWeights)


@dataclass(slots=True)
class TrackingConfig:
    max_lost_frames: int = 8
    match_distance: float = 120.0
    max_speed: float = 160.0
    max_acceleration: float = 120.0
    min_history_for_tracking: int = 3
    history_size: int = 128
    prediction_mode: str = "constant_velocity"
    predicted_confidence_decay: float = 0.90
    prediction_velocity_decay: float = 0.96
    prediction_boundary_extra_decay: float = 0.85
    prediction_boundary_margin_ratio: float = 0.08
    kalman_enabled: bool = True
    kalman_model: str = "ca"
    process_noise_base: float = 4.0
    process_noise_lost_multiplier: float = 1.35
    process_noise_reacquire_multiplier: float = 1.75
    measurement_noise_base: float = 36.0
    measurement_noise_low_conf_multiplier: float = 3.0
    gate_sigma_scale: float = 3.0
    gate_radius_min: float = 90.0
    gate_radius_max: float = 260.0
    velocity_blend_after_reacquire: float = 0.35
    speed_cap_after_reacquire: float = 180.0
    reacquire_stabilization_frames: int = 4
    reacquire_stabilization_gate_scale: float = 0.70
    reacquire_stabilization_velocity_decay: float = 0.88
    out_of_view_enabled: bool = False
    out_of_view_top_margin_ratio: float = 0.10
    out_of_view_side_margin_ratio: float = 0.06
    out_of_view_bottom_margin_ratio: float = 0.06
    out_of_view_velocity_threshold: float = 18.0
    out_of_view_prediction_limit: int = 4
    out_of_view_extra_decay: float = 0.60


@dataclass(slots=True)
class OutputConfig:
    video_name: str = "annotated.mp4"
    frame_dir: str = "frames"
    csv_name: str = "ball_track.csv"
    debug_jsonl_name: str = "debug.jsonl"
    video_codec: str = "mp4v"
    frame_image_ext: str = ".jpg"
    save_video: bool = True
    save_frames: bool = True
    save_csv: bool = True
    save_debug_jsonl: bool = True
    draw_radius: int = 18
    draw_thickness: int = 4
    frame_text_scale: float = 1.0
    frame_text_thickness: int = 2
    draw_status_text: bool = True


@dataclass(slots=True)
class RuntimeConfig:
    use_gpu_if_available: bool = True
    enable_cudnn_benchmark: bool = True
    opencv_threads: int = 2
    capture_backend: str = "CAP_FFMPEG"
    start_frame: int = 0
    max_frames: int | None = None


@dataclass(slots=True)
class MockConfig:
    enabled: bool = False
    scenario: str = "A"
    frame_width: int = 1280
    frame_height: int = 720
    fps: float = 20.0
    frame_count: int = 12
    ball_box_size: int = 16
    background_color: int = 0


@dataclass(slots=True)
class AppConfig:
    input_video: Path
    output_dir: Path
    logging: LoggingConfig
    detector: DetectorConfig
    sahi: SahiConfig
    filtering: FilteringConfig
    scene_bias: SceneBiasConfig
    selection: SelectionConfig
    tracking: TrackingConfig
    output: OutputConfig
    postprocess: PostprocessConfig
    follow_cam: FollowCamConfig
    runtime: RuntimeConfig
    mock: MockConfig


def _resolve_path(base_dir: Path, raw_path: str) -> Path:
    path = Path(raw_path)
    if path.is_absolute():
        return path
    return (base_dir / path).resolve()


def _to_roi(raw_roi: Any) -> tuple[int, int, int, int] | None:
    if raw_roi in (None, "", []):
        return None
    if not isinstance(raw_roi, list) or len(raw_roi) != 4:
        raise ValueError("ROI 必须为 [x1, y1, x2, y2] 或 null")
    return tuple(int(value) for value in raw_roi)


def _to_points(raw_points: Any) -> tuple[tuple[int, int], ...]:
    if raw_points in (None, "", []):
        return ()
    if not isinstance(raw_points, list) or len(raw_points) < 3:
        raise ValueError("points 必须为至少 3 个点的列表")

    points: list[tuple[int, int]] = []
    for raw_point in raw_points:
        if not isinstance(raw_point, list) or len(raw_point) != 2:
            raise ValueError("polygon 点必须为 [x, y]")
        points.append((int(raw_point[0]), int(raw_point[1])))
    return tuple(points)


def _to_frame_range(raw_frame_range: Any) -> tuple[int, int] | None:
    if raw_frame_range in (None, "", []):
        return None
    if not isinstance(raw_frame_range, list) or len(raw_frame_range) != 2:
        raise ValueError("frame_range 必须为 [start_frame, end_frame] 或 null")
    start_frame = int(raw_frame_range[0])
    end_frame = int(raw_frame_range[1])
    if end_frame < start_frame:
        raise ValueError("frame_range 的 end_frame 不能小于 start_frame")
    return (start_frame, end_frame)


def _to_active_states(raw_active_states: Any) -> tuple[str, ...]:
    if raw_active_states in (None, "", []):
        return ()
    if not isinstance(raw_active_states, list):
        raise ValueError("active_states 必须为 TrackState 列表或 null")
    return tuple(str(value).upper() for value in raw_active_states)


def _to_frame_ranges(raw_frame_ranges: Any) -> list[tuple[int, int]]:
    if raw_frame_ranges in (None, "", []):
        return []
    if not isinstance(raw_frame_ranges, list):
        raise ValueError("protected_ranges 必须为 [[start, end], ...] 列表或 null")
    return [_to_frame_range(raw_frame_range) for raw_frame_range in raw_frame_ranges]


def _to_relaxed_filtering(raw_relaxed_filtering: Any) -> RelaxedFilteringConfig:
    if raw_relaxed_filtering in (None, "", []):
        return RelaxedFilteringConfig()
    if not isinstance(raw_relaxed_filtering, dict):
        raise ValueError("relaxed_filtering 必须为 dict 或 null")
    return RelaxedFilteringConfig(
        min_confidence=(
            None
            if raw_relaxed_filtering.get("min_confidence") in (None, "")
            else float(raw_relaxed_filtering.get("min_confidence"))
        ),
        min_width=(
            None
            if raw_relaxed_filtering.get("min_width") in (None, "")
            else float(raw_relaxed_filtering.get("min_width"))
        ),
        min_height=(
            None
            if raw_relaxed_filtering.get("min_height") in (None, "")
            else float(raw_relaxed_filtering.get("min_height"))
        ),
    )


def _to_scene_zones(raw_zones: Any) -> list[SceneZoneConfig]:
    if raw_zones in (None, "", []):
        return []
    if not isinstance(raw_zones, list):
        raise ValueError("scene_bias 区域配置必须为 list")

    zones: list[SceneZoneConfig] = []
    for index, raw_zone in enumerate(raw_zones):
        if not isinstance(raw_zone, dict):
            raise ValueError(f"scene_bias 区域 #{index} 必须为 dict")
        roi = _to_roi(raw_zone.get("roi"))
        points = _to_points(raw_zone.get("points"))
        if roi is None and not points:
            raise ValueError(f"scene_bias 区域 {raw_zone.get('name', index)} 必须配置 roi 或 points")
        zones.append(
            SceneZoneConfig(
                name=str(raw_zone.get("name", f"zone_{index}")),
                roi=roi,
                points=points,
                active_states=_to_active_states(raw_zone.get("active_states")),
                frame_range=_to_frame_range(raw_zone.get("frame_range")),
                relaxed_filtering=_to_relaxed_filtering(raw_zone.get("relaxed_filtering")),
                selection_bonus=float(raw_zone.get("selection_bonus", 0.0)),
            )
        )
    return zones


def _to_dynamic_air_recovery(raw_dynamic_air_recovery: Any) -> DynamicAirRecoveryConfig:
    if raw_dynamic_air_recovery in (None, "", []):
        return DynamicAirRecoveryConfig()
    if not isinstance(raw_dynamic_air_recovery, dict):
        raise ValueError("dynamic_air_recovery 必须为 dict 或 null")
    return DynamicAirRecoveryConfig(
        enabled=bool(raw_dynamic_air_recovery.get("enabled", False)),
        profile=str(raw_dynamic_air_recovery.get("profile", "fisheye_180_indoor")),
        active_states=_to_active_states(raw_dynamic_air_recovery.get("active_states", ["PREDICTING", "LOST"])),
        relaxed_filtering=_to_relaxed_filtering(raw_dynamic_air_recovery.get("relaxed_filtering")),
        selection_bonus=float(raw_dynamic_air_recovery.get("selection_bonus", 0.0)),
        reacquire_enabled=bool(raw_dynamic_air_recovery.get("reacquire_enabled", True)),
        reacquire_confidence_threshold=float(raw_dynamic_air_recovery.get("reacquire_confidence_threshold", 0.10)),
        reacquire_image_size=int(raw_dynamic_air_recovery.get("reacquire_image_size", 1536)),
        burst_enabled=bool(raw_dynamic_air_recovery.get("burst_enabled", True)),
        burst_frames=int(raw_dynamic_air_recovery.get("burst_frames", 6)),
        burst_confidence_threshold=float(raw_dynamic_air_recovery.get("burst_confidence_threshold", 0.05)),
        burst_image_size=int(raw_dynamic_air_recovery.get("burst_image_size", 1920)),
        burst_window_scale=float(raw_dynamic_air_recovery.get("burst_window_scale", 1.35)),
        low_quality_reject_enabled=bool(raw_dynamic_air_recovery.get("low_quality_reject_enabled", True)),
        low_quality_reject_confidence=float(raw_dynamic_air_recovery.get("low_quality_reject_confidence", 0.22)),
        low_quality_reject_score=float(raw_dynamic_air_recovery.get("low_quality_reject_score", 0.30)),
        low_quality_reject_min_lost_frames=int(raw_dynamic_air_recovery.get("low_quality_reject_min_lost_frames", 4)),
        tentative_reacquire_enabled=bool(raw_dynamic_air_recovery.get("tentative_reacquire_enabled", True)),
        tentative_reacquire_min_lost_frames=int(raw_dynamic_air_recovery.get("tentative_reacquire_min_lost_frames", 4)),
        tentative_reacquire_confidence_threshold=float(
            raw_dynamic_air_recovery.get("tentative_reacquire_confidence_threshold", 0.30)
        ),
        tentative_reacquire_score_threshold=float(
            raw_dynamic_air_recovery.get("tentative_reacquire_score_threshold", 0.38)
        ),
        tentative_reacquire_confirmation_radius=float(
            raw_dynamic_air_recovery.get("tentative_reacquire_confirmation_radius", 140.0)
        ),
        tentative_reacquire_max_age=int(raw_dynamic_air_recovery.get("tentative_reacquire_max_age", 1)),
        gap_aware_jump_gate_enabled=bool(raw_dynamic_air_recovery.get("gap_aware_jump_gate_enabled", True)),
        gap_aware_short_lost_frames=int(raw_dynamic_air_recovery.get("gap_aware_short_lost_frames", 3)),
        gap_aware_short_jump_distance=float(raw_dynamic_air_recovery.get("gap_aware_short_jump_distance", 260.0)),
        gap_aware_long_jump_distance=float(raw_dynamic_air_recovery.get("gap_aware_long_jump_distance", 520.0)),
        gap_aware_high_confidence_bypass=float(
            raw_dynamic_air_recovery.get("gap_aware_high_confidence_bypass", 0.42)
        ),
        edge_reentry_enabled=bool(raw_dynamic_air_recovery.get("edge_reentry_enabled", True)),
        edge_reentry_min_lost_frames=int(raw_dynamic_air_recovery.get("edge_reentry_min_lost_frames", 2)),
        edge_reentry_max_lost_frames=int(raw_dynamic_air_recovery.get("edge_reentry_max_lost_frames", 80)),
        edge_reentry_margin_x_ratio=float(raw_dynamic_air_recovery.get("edge_reentry_margin_x_ratio", 0.08)),
        edge_reentry_margin_y_ratio=float(raw_dynamic_air_recovery.get("edge_reentry_margin_y_ratio", 0.08)),
        edge_reentry_expand_x=float(raw_dynamic_air_recovery.get("edge_reentry_expand_x", 260.0)),
        edge_reentry_expand_y=float(raw_dynamic_air_recovery.get("edge_reentry_expand_y", 240.0)),
        edge_reentry_high_confidence_bypass=float(
            raw_dynamic_air_recovery.get("edge_reentry_high_confidence_bypass", 0.45)
        ),
        isolated_far_jump_enabled=bool(raw_dynamic_air_recovery.get("isolated_far_jump_enabled", False)),
        isolated_far_jump_min_lost_frames=int(raw_dynamic_air_recovery.get("isolated_far_jump_min_lost_frames", 1)),
        isolated_far_jump_distance=float(raw_dynamic_air_recovery.get("isolated_far_jump_distance", 320.0)),
        isolated_far_jump_high_confidence_bypass=float(
            raw_dynamic_air_recovery.get("isolated_far_jump_high_confidence_bypass", 0.50)
        ),
        isolated_far_jump_confirmation_radius=float(
            raw_dynamic_air_recovery.get("isolated_far_jump_confirmation_radius", 180.0)
        ),
        true_out_of_view_enabled=bool(raw_dynamic_air_recovery.get("true_out_of_view_enabled", False)),
        true_out_of_view_min_lost_frames=int(raw_dynamic_air_recovery.get("true_out_of_view_min_lost_frames", 4)),
        true_out_of_view_min_empty_frames=int(raw_dynamic_air_recovery.get("true_out_of_view_min_empty_frames", 3)),
        true_out_of_view_edge_margin_x_ratio=float(
            raw_dynamic_air_recovery.get("true_out_of_view_edge_margin_x_ratio", 0.08)
        ),
        true_out_of_view_edge_margin_y_ratio=float(
            raw_dynamic_air_recovery.get("true_out_of_view_edge_margin_y_ratio", 0.08)
        ),
        true_out_of_view_high_confidence_bypass=float(
            raw_dynamic_air_recovery.get("true_out_of_view_high_confidence_bypass", 0.55)
        ),
        true_out_of_view_jump_distance=float(raw_dynamic_air_recovery.get("true_out_of_view_jump_distance", 260.0)),
        true_out_of_view_confirmation_radius=float(
            raw_dynamic_air_recovery.get("true_out_of_view_confirmation_radius", 180.0)
        ),
        ground_exit_enabled=bool(raw_dynamic_air_recovery.get("ground_exit_enabled", True)),
        ground_exit_min_lost_frames=int(raw_dynamic_air_recovery.get("ground_exit_min_lost_frames", 8)),
    )


def _to_postprocess(raw_postprocess: Any) -> PostprocessConfig:
    if raw_postprocess in (None, "", []):
        return PostprocessConfig()
    if not isinstance(raw_postprocess, dict):
        raise ValueError("postprocess 必须为 dict 或 null")
    return PostprocessConfig(
        enabled=bool(raw_postprocess.get("enabled", False)),
        max_detected_island_length=int(raw_postprocess.get("max_detected_island_length", 2)),
        stable_segment_min_length=int(raw_postprocess.get("stable_segment_min_length", 4)),
        min_jump_distance=float(raw_postprocess.get("min_jump_distance", 260.0)),
        nuisance_zone_jump_distance=float(raw_postprocess.get("nuisance_zone_jump_distance", 180.0)),
        low_confidence_threshold=float(raw_postprocess.get("low_confidence_threshold", 0.45)),
        save_cleaned_video=bool(raw_postprocess.get("save_cleaned_video", True)),
        save_cleaned_csv=bool(raw_postprocess.get("save_cleaned_csv", True)),
        save_cleaned_debug_jsonl=bool(raw_postprocess.get("save_cleaned_debug_jsonl", True)),
        cleaned_video_name=str(raw_postprocess.get("cleaned_video_name", "annotated.cleaned.mp4")),
        cleaned_csv_name=str(raw_postprocess.get("cleaned_csv_name", "ball_track.cleaned.csv")),
        cleaned_debug_jsonl_name=str(raw_postprocess.get("cleaned_debug_jsonl_name", "debug.cleaned.jsonl")),
        cleanup_report_name=str(raw_postprocess.get("cleanup_report_name", "cleanup_report.json")),
        nuisance_zones=_to_scene_zones(raw_postprocess.get("nuisance_zones")),
        protected_ranges=_to_frame_ranges(raw_postprocess.get("protected_ranges")),
    )


def _to_follow_cam(raw_follow_cam: Any) -> FollowCamConfig:
    if raw_follow_cam in (None, "", []):
        return FollowCamConfig()
    if not isinstance(raw_follow_cam, dict):
        raise ValueError("follow_cam 蹇呴』涓?dict 鎴?null")
    return FollowCamConfig(
        enabled=bool(raw_follow_cam.get("enabled", False)),
        prefer_cleaned_track=bool(raw_follow_cam.get("prefer_cleaned_track", True)),
        output_video_name=str(raw_follow_cam.get("output_video_name", "follow_cam.mp4")),
        camera_path_name=str(raw_follow_cam.get("camera_path_name", "camera_path.csv")),
        report_name=str(raw_follow_cam.get("report_name", "follow_cam_report.json")),
        target_width=int(raw_follow_cam.get("target_width", 1920)),
        target_height=int(raw_follow_cam.get("target_height", 1080)),
        min_crop_height=int(raw_follow_cam.get("min_crop_height", 900)),
        max_crop_height=int(raw_follow_cam.get("max_crop_height", 1260)),
        home_center_x_ratio=float(raw_follow_cam.get("home_center_x_ratio", 0.50)),
        home_center_y_ratio=float(raw_follow_cam.get("home_center_y_ratio", 0.66)),
        ball_screen_x_ratio=float(raw_follow_cam.get("ball_screen_x_ratio", 0.50)),
        ball_screen_y_ratio=float(raw_follow_cam.get("ball_screen_y_ratio", 0.58)),
        dead_zone_ratio_x=float(raw_follow_cam.get("dead_zone_ratio_x", 0.12)),
        dead_zone_ratio_y=float(raw_follow_cam.get("dead_zone_ratio_y", 0.10)),
        pan_smoothing=float(raw_follow_cam.get("pan_smoothing", 0.35)),
        glide_pan_smoothing=float(raw_follow_cam.get("glide_pan_smoothing", raw_follow_cam.get("pan_smoothing", 0.12))),
        glide_max_pan_per_frame_x=float(
            raw_follow_cam.get("glide_max_pan_per_frame_x", raw_follow_cam.get("max_pan_per_frame_x", 32.0))
        ),
        glide_max_pan_per_frame_y=float(
            raw_follow_cam.get("glide_max_pan_per_frame_y", raw_follow_cam.get("max_pan_per_frame_y", 18.0))
        ),
        catch_up_pan_smoothing=float(
            raw_follow_cam.get("catch_up_pan_smoothing", raw_follow_cam.get("pan_smoothing", 0.24))
        ),
        catch_up_max_pan_per_frame_x=float(
            raw_follow_cam.get("catch_up_max_pan_per_frame_x", raw_follow_cam.get("max_pan_per_frame_x", 64.0))
        ),
        catch_up_max_pan_per_frame_y=float(
            raw_follow_cam.get("catch_up_max_pan_per_frame_y", raw_follow_cam.get("max_pan_per_frame_y", 36.0))
        ),
        catch_up_trigger_ratio_x=float(raw_follow_cam.get("catch_up_trigger_ratio_x", 0.26)),
        catch_up_trigger_ratio_y=float(raw_follow_cam.get("catch_up_trigger_ratio_y", 0.22)),
        catch_up_release_ratio_x=float(raw_follow_cam.get("catch_up_release_ratio_x", 0.18)),
        catch_up_release_ratio_y=float(raw_follow_cam.get("catch_up_release_ratio_y", 0.15)),
        zoom_smoothing=float(raw_follow_cam.get("zoom_smoothing", 0.20)),
        zoom_in_smoothing=float(raw_follow_cam.get("zoom_in_smoothing", raw_follow_cam.get("zoom_smoothing", 0.05))),
        zoom_out_smoothing=float(raw_follow_cam.get("zoom_out_smoothing", raw_follow_cam.get("zoom_smoothing", 0.08))),
        max_zoom_in_per_frame=float(raw_follow_cam.get("max_zoom_in_per_frame", 10.0)),
        max_zoom_out_per_frame=float(raw_follow_cam.get("max_zoom_out_per_frame", 18.0)),
        zoom_deadband_height=float(raw_follow_cam.get("zoom_deadband_height", 80.0)),
        zoom_out_confirm_frames=int(raw_follow_cam.get("zoom_out_confirm_frames", 4)),
        zoom_in_confirm_frames=int(raw_follow_cam.get("zoom_in_confirm_frames", 8)),
        zoom_reverse_confirm_frames=int(raw_follow_cam.get("zoom_reverse_confirm_frames", 14)),
        zoom_hold_frames_after_change=int(raw_follow_cam.get("zoom_hold_frames_after_change", 10)),
        velocity_smoothing=float(raw_follow_cam.get("velocity_smoothing", 0.35)),
        max_pan_per_frame_x=float(raw_follow_cam.get("max_pan_per_frame_x", 90.0)),
        max_pan_per_frame_y=float(raw_follow_cam.get("max_pan_per_frame_y", 55.0)),
        look_ahead_gain=float(raw_follow_cam.get("look_ahead_gain", 0.20)),
        look_ahead_max_px=float(raw_follow_cam.get("look_ahead_max_px", 180.0)),
        speed_zoom_out_start=float(raw_follow_cam.get("speed_zoom_out_start", 22.0)),
        speed_zoom_out_end=float(raw_follow_cam.get("speed_zoom_out_end", 85.0)),
        predicted_zoom_out_bonus=float(raw_follow_cam.get("predicted_zoom_out_bonus", 0.12)),
        lost_zoom_out_bonus=float(raw_follow_cam.get("lost_zoom_out_bonus", 0.30)),
        low_confidence_zoom_out_start=float(raw_follow_cam.get("low_confidence_zoom_out_start", 0.40)),
        low_confidence_zoom_out_end=float(raw_follow_cam.get("low_confidence_zoom_out_end", 0.18)),
        predicted_pan_decay=float(raw_follow_cam.get("predicted_pan_decay", 0.75)),
        lost_hold_frames=int(raw_follow_cam.get("lost_hold_frames", 10)),
        lost_recenter_frames=int(raw_follow_cam.get("lost_recenter_frames", 28)),
        recenter_smoothing=float(raw_follow_cam.get("recenter_smoothing", 0.08)),
        draw_ball_marker=bool(raw_follow_cam.get("draw_ball_marker", True)),
        draw_frame_text=bool(raw_follow_cam.get("draw_frame_text", True)),
    )


def load_config(config_path: Path) -> AppConfig:
    """从 YAML 加载配置，并将相对路径解析为绝对路径。"""
    if not config_path.exists():
        raise FileNotFoundError(f"配置文件不存在: {config_path}")

    config_path = config_path.resolve()
    base_dir = config_path.parent.parent
    for parent in config_path.parents:
        if parent.name == "config":
            base_dir = parent.parent
            break

    with config_path.open("r", encoding="utf-8") as file:
        raw = yaml.safe_load(file) or {}

    detector_raw = raw.get("detector", {})
    detector = DetectorConfig(
        model_path=_resolve_path(base_dir, detector_raw.get("model_path", "./weights/football_ball_yolo.pt")),
        device=detector_raw.get("device", "cuda:0"),
        confidence_threshold=float(detector_raw.get("confidence_threshold", 0.15)),
        image_size=int(detector_raw.get("image_size", 1280)),
        use_half=bool(detector_raw.get("use_half", True)),
        allowed_labels=list(detector_raw.get("allowed_labels", ["sports ball", "ball"])),
    )

    sahi_raw = raw.get("sahi", {})
    sahi = SahiConfig(
        slice_height=int(sahi_raw.get("slice_height", 720)),
        slice_width=int(sahi_raw.get("slice_width", 1280)),
        overlap_height_ratio=float(sahi_raw.get("overlap_height_ratio", 0.2)),
        overlap_width_ratio=float(sahi_raw.get("overlap_width_ratio", 0.2)),
        perform_standard_pred=bool(sahi_raw.get("perform_standard_pred", False)),
        postprocess_type=str(sahi_raw.get("postprocess_type", "NMS")),
        postprocess_match_metric=str(sahi_raw.get("postprocess_match_metric", "IOS")),
        postprocess_match_threshold=float(sahi_raw.get("postprocess_match_threshold", 0.5)),
        verbose=int(sahi_raw.get("verbose", 0)),
    )

    filtering_raw = raw.get("filtering", {})
    filtering = FilteringConfig(
        min_confidence=float(filtering_raw.get("min_confidence", 0.15)),
        min_width=float(filtering_raw.get("min_width", 4)),
        max_width=float(filtering_raw.get("max_width", 80)),
        min_height=float(filtering_raw.get("min_height", 4)),
        max_height=float(filtering_raw.get("max_height", 80)),
        min_aspect_ratio=float(filtering_raw.get("min_aspect_ratio", 0.5)),
        max_aspect_ratio=float(filtering_raw.get("max_aspect_ratio", 1.8)),
        roi=_to_roi(filtering_raw.get("roi")),
    )

    scene_bias_raw = raw.get("scene_bias", {})
    scene_bias = SceneBiasConfig(
        enabled=bool(scene_bias_raw.get("enabled", False)),
        ground_zones=_to_scene_zones(scene_bias_raw.get("ground_zones")),
        positive_rois=_to_scene_zones(scene_bias_raw.get("positive_rois")),
        negative_rois=_to_scene_zones(scene_bias_raw.get("negative_rois")),
        dynamic_air_recovery=_to_dynamic_air_recovery(scene_bias_raw.get("dynamic_air_recovery")),
    )

    weights_raw = raw.get("selection", {}).get("weights", {})
    selection = SelectionConfig(
        min_accept_score=float(raw.get("selection", {}).get("min_accept_score", 0.18)),
        stable_history_length=int(raw.get("selection", {}).get("stable_history_length", 12)),
        weights=SelectionWeights(
            distance_score=float(weights_raw.get("distance_score", 0.32)),
            direction_score=float(weights_raw.get("direction_score", 0.20)),
            velocity_score=float(weights_raw.get("velocity_score", 0.18)),
            acceleration_penalty=float(weights_raw.get("acceleration_penalty", 0.14)),
            trajectory_length_bonus=float(weights_raw.get("trajectory_length_bonus", 0.10)),
            confidence=float(weights_raw.get("confidence", 0.06)),
        ),
    )

    tracking_raw = raw.get("tracking", {})
    tracking = TrackingConfig(
        max_lost_frames=int(tracking_raw.get("max_lost_frames", 8)),
        match_distance=float(tracking_raw.get("match_distance", 120.0)),
        max_speed=float(tracking_raw.get("max_speed", 160.0)),
        max_acceleration=float(tracking_raw.get("max_acceleration", 120.0)),
        min_history_for_tracking=int(tracking_raw.get("min_history_for_tracking", 3)),
        history_size=int(tracking_raw.get("history_size", 128)),
        prediction_mode=str(tracking_raw.get("prediction_mode", "constant_velocity")),
        predicted_confidence_decay=float(tracking_raw.get("predicted_confidence_decay", 0.90)),
        prediction_velocity_decay=float(tracking_raw.get("prediction_velocity_decay", 0.96)),
        prediction_boundary_extra_decay=float(tracking_raw.get("prediction_boundary_extra_decay", 0.85)),
        prediction_boundary_margin_ratio=float(tracking_raw.get("prediction_boundary_margin_ratio", 0.08)),
        kalman_enabled=bool(tracking_raw.get("kalman_enabled", True)),
        kalman_model=str(tracking_raw.get("kalman_model", "ca")),
        process_noise_base=float(tracking_raw.get("process_noise_base", 4.0)),
        process_noise_lost_multiplier=float(tracking_raw.get("process_noise_lost_multiplier", 1.35)),
        process_noise_reacquire_multiplier=float(tracking_raw.get("process_noise_reacquire_multiplier", 1.75)),
        measurement_noise_base=float(tracking_raw.get("measurement_noise_base", 36.0)),
        measurement_noise_low_conf_multiplier=float(tracking_raw.get("measurement_noise_low_conf_multiplier", 3.0)),
        gate_sigma_scale=float(tracking_raw.get("gate_sigma_scale", 3.0)),
        gate_radius_min=float(tracking_raw.get("gate_radius_min", 90.0)),
        gate_radius_max=float(tracking_raw.get("gate_radius_max", 260.0)),
        velocity_blend_after_reacquire=float(tracking_raw.get("velocity_blend_after_reacquire", 0.35)),
        speed_cap_after_reacquire=float(tracking_raw.get("speed_cap_after_reacquire", 180.0)),
        reacquire_stabilization_frames=int(tracking_raw.get("reacquire_stabilization_frames", 4)),
        reacquire_stabilization_gate_scale=float(tracking_raw.get("reacquire_stabilization_gate_scale", 0.70)),
        reacquire_stabilization_velocity_decay=float(tracking_raw.get("reacquire_stabilization_velocity_decay", 0.88)),
        out_of_view_enabled=bool(tracking_raw.get("out_of_view_enabled", False)),
        out_of_view_top_margin_ratio=float(tracking_raw.get("out_of_view_top_margin_ratio", 0.10)),
        out_of_view_side_margin_ratio=float(tracking_raw.get("out_of_view_side_margin_ratio", 0.06)),
        out_of_view_bottom_margin_ratio=float(tracking_raw.get("out_of_view_bottom_margin_ratio", 0.06)),
        out_of_view_velocity_threshold=float(tracking_raw.get("out_of_view_velocity_threshold", 18.0)),
        out_of_view_prediction_limit=int(tracking_raw.get("out_of_view_prediction_limit", 4)),
        out_of_view_extra_decay=float(tracking_raw.get("out_of_view_extra_decay", 0.60)),
    )

    output_raw = raw.get("output", {})
    output = OutputConfig(
        video_name=str(output_raw.get("video_name", "annotated.mp4")),
        frame_dir=str(output_raw.get("frame_dir", "frames")),
        csv_name=str(output_raw.get("csv_name", "ball_track.csv")),
        debug_jsonl_name=str(output_raw.get("debug_jsonl_name", "debug.jsonl")),
        video_codec=str(output_raw.get("video_codec", "mp4v")),
        frame_image_ext=str(output_raw.get("frame_image_ext", ".jpg")),
        save_video=bool(output_raw.get("save_video", True)),
        save_frames=bool(output_raw.get("save_frames", True)),
        save_csv=bool(output_raw.get("save_csv", True)),
        save_debug_jsonl=bool(output_raw.get("save_debug_jsonl", True)),
        draw_radius=int(output_raw.get("draw_radius", 18)),
        draw_thickness=int(output_raw.get("draw_thickness", 4)),
        frame_text_scale=float(output_raw.get("frame_text_scale", 1.0)),
        frame_text_thickness=int(output_raw.get("frame_text_thickness", 2)),
        draw_status_text=bool(output_raw.get("draw_status_text", True)),
    )

    postprocess = _to_postprocess(raw.get("postprocess"))
    follow_cam = _to_follow_cam(raw.get("follow_cam"))

    runtime_raw = raw.get("runtime", {})
    raw_start_frame = runtime_raw.get("start_frame", 0)
    raw_max_frames = runtime_raw.get("max_frames")
    start_frame = int(raw_start_frame or 0)
    if start_frame < 0:
        start_frame = 0
    max_frames = None
    if raw_max_frames not in (None, ""):
        max_frames = int(raw_max_frames)
        if max_frames <= 0:
            max_frames = None

    runtime = RuntimeConfig(
        use_gpu_if_available=bool(runtime_raw.get("use_gpu_if_available", True)),
        enable_cudnn_benchmark=bool(runtime_raw.get("enable_cudnn_benchmark", True)),
        opencv_threads=int(runtime_raw.get("opencv_threads", 2)),
        capture_backend=str(runtime_raw.get("capture_backend", "CAP_FFMPEG")),
        start_frame=start_frame,
        max_frames=max_frames,
    )

    mock_raw = raw.get("mock", {})
    mock = MockConfig(
        enabled=bool(mock_raw.get("enabled", False)),
        scenario=str(mock_raw.get("scenario", "A")).upper(),
        frame_width=int(mock_raw.get("frame_width", 1280)),
        frame_height=int(mock_raw.get("frame_height", 720)),
        fps=float(mock_raw.get("fps", 20.0)),
        frame_count=int(mock_raw.get("frame_count", 12)),
        ball_box_size=int(mock_raw.get("ball_box_size", 16)),
        background_color=int(mock_raw.get("background_color", 0)),
    )

    logging_raw = raw.get("logging", {})
    logging_config = LoggingConfig(
        level=str(logging_raw.get("level", "INFO")),
        save_debug_jsonl=bool(logging_raw.get("save_debug_jsonl", True)),
    )

    return AppConfig(
        input_video=_resolve_path(base_dir, raw.get("input_video", "./data/input.mp4")),
        output_dir=_resolve_path(base_dir, raw.get("output_dir", "./outputs/run_001")),
        logging=logging_config,
        detector=detector,
        sahi=sahi,
        filtering=filtering,
        scene_bias=scene_bias,
        selection=selection,
        tracking=tracking,
        output=output,
        postprocess=postprocess,
        follow_cam=follow_cam,
        runtime=runtime,
        mock=mock,
    )
