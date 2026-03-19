from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class TrackState(str, Enum):
    """追踪状态机内部状态。"""

    INIT = "INIT"
    TRACKING = "TRACKING"
    PREDICTING = "PREDICTING"
    LOST = "LOST"


class OutputStatus(str, Enum):
    """对外输出状态，只允许使用用户要求的三种值。"""

    DETECTED = "Detected"
    PREDICTED = "Predicted"
    LOST = "Lost"


@dataclass(slots=True)
class Candidate:
    """检测层输出的候选球，不包含任何追踪状态。"""

    frame_index: int
    x1: float
    y1: float
    x2: float
    y2: float
    confidence: float
    label: str = "ball"
    source: str = "detector"

    @property
    def width(self) -> float:
        return max(0.0, self.x2 - self.x1)

    @property
    def height(self) -> float:
        return max(0.0, self.y2 - self.y1)

    @property
    def area(self) -> float:
        return self.width * self.height

    @property
    def aspect_ratio(self) -> float:
        if self.height <= 1e-6:
            return 0.0
        return self.width / self.height

    @property
    def center(self) -> tuple[float, float]:
        return (self.x1 + self.width / 2.0, self.y1 + self.height / 2.0)


@dataclass(slots=True)
class TrackPoint:
    """轨迹点，既可以来自真实检测，也可以来自短时预测。"""

    frame_index: int
    x: float
    y: float
    confidence: float
    status: OutputStatus


@dataclass(slots=True)
class CandidateScore:
    """保存每个候选的评分明细，便于逐帧 debug。"""

    candidate: Candidate
    total_score: float
    distance_score: float
    direction_score: float
    velocity_score: float
    acceleration_penalty: float
    trajectory_length_bonus: float
    confidence_score: float
    scene_bonus: float
    scene_zone: str | None
    reason: str

    def to_debug_dict(self) -> dict[str, Any]:
        return {
            "candidate_center": [round(self.candidate.center[0], 2), round(self.candidate.center[1], 2)],
            "candidate_bbox": [
                round(self.candidate.x1, 2),
                round(self.candidate.y1, 2),
                round(self.candidate.x2, 2),
                round(self.candidate.y2, 2),
            ],
            "confidence": round(self.candidate.confidence, 4),
            "label": self.candidate.label,
            "source": self.candidate.source,
            "total_score": round(self.total_score, 4),
            "distance_score": round(self.distance_score, 4),
            "direction_score": round(self.direction_score, 4),
            "velocity_score": round(self.velocity_score, 4),
            "acceleration_penalty": round(self.acceleration_penalty, 4),
            "trajectory_length_bonus": round(self.trajectory_length_bonus, 4),
            "confidence_score": round(self.confidence_score, 4),
            "scene_bonus": round(self.scene_bonus, 4),
            "scene_zone": self.scene_zone,
            "reason": self.reason,
        }


@dataclass(slots=True)
class TrackerContext:
    """选择层所需的历史上下文。"""

    state: TrackState
    last_position: tuple[float, float] | None = None
    predicted_position: tuple[float, float] | None = None
    last_detected_position: tuple[float, float] | None = None
    gating_radius: float | None = None
    velocity: tuple[float, float] = (0.0, 0.0)
    acceleration: tuple[float, float] = (0.0, 0.0)
    history_length: int = 0
    lost_frames: int = 0


@dataclass(slots=True)
class SelectionDecision:
    """选择层输出，表示本帧最终决定追踪哪个候选。"""

    selected_candidate: Candidate | None
    selected_score: float
    selected_reason: str
    candidate_scores: list[CandidateScore] = field(default_factory=list)


@dataclass(slots=True)
class TrackResult:
    """追踪层输出，既用于渲染，也用于 CSV 导出。"""

    frame_index: int
    output_status: OutputStatus
    state: TrackState
    point: TrackPoint | None
    confidence: float
    reason: str
    lost_frames: int
    raw_candidate_count: int
    filtered_candidate_count: int
    selected_score: float = 0.0
    selected_candidate_scores: list[CandidateScore] = field(default_factory=list)
    filter_rejection_counts: dict[str, int] = field(default_factory=dict)
    filter_rejections: list[dict[str, Any]] = field(default_factory=list)
    reacquire_attempted: bool = False
    reacquire_candidate_count: int = 0
    reacquire_window: list[int] | None = None

    def to_debug_dict(self) -> dict[str, Any]:
        return {
            "frame": self.frame_index,
            "status": self.output_status.value,
            "state": self.state.value,
            "point": None
            if self.point is None
            else {
                "x": round(self.point.x, 2),
                "y": round(self.point.y, 2),
                "confidence": round(self.point.confidence, 4),
            },
            "confidence": round(self.confidence, 4),
            "reason": self.reason,
            "lost_frames": self.lost_frames,
            "raw_candidate_count": self.raw_candidate_count,
            "filtered_candidate_count": self.filtered_candidate_count,
            "selected_score": round(self.selected_score, 4),
            "candidate_scores": [item.to_debug_dict() for item in self.selected_candidate_scores],
            "filter_rejection_counts": self.filter_rejection_counts,
            "filter_rejections": self.filter_rejections,
            "reacquire_attempted": self.reacquire_attempted,
            "reacquire_candidate_count": self.reacquire_candidate_count,
            "reacquire_window": self.reacquire_window,
        }
