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
    selection: SelectionConfig
    tracking: TrackingConfig
    output: OutputConfig
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


def load_config(config_path: Path) -> AppConfig:
    """从 YAML 加载配置，并将相对路径解析为绝对路径。"""
    if not config_path.exists():
        raise FileNotFoundError(f"配置文件不存在: {config_path}")

    config_path = config_path.resolve()
    base_dir = config_path.parent.parent

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

    runtime_raw = raw.get("runtime", {})
    raw_max_frames = runtime_raw.get("max_frames")
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
        selection=selection,
        tracking=tracking,
        output=output,
        runtime=runtime,
        mock=mock,
    )
