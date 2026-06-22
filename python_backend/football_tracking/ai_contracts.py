from __future__ import annotations

from typing import Literal, TypeAlias

AIFailureTag: TypeAlias = Literal[
    "ball_lost",
    "foot_confusion",
    "shoe_confusion",
    "sideline_confusion",
    "wall_background_drift",
    "large_jump_after_reacquire",
    "camera_catchup_spike",
    "black_frames",
    "post_roll_too_short",
    "highlight_boundary_unclear",
    "unknown",
]

AIRootCauseModule: TypeAlias = Literal[
    "detection",
    "selection",
    "reacquisition",
    "postprocess",
    "stitching",
    "packetization",
    "event_scoring",
    "follow_cam",
    "rendering",
    "unknown",
]

AIRecommendedAction: TypeAlias = Literal[
    "targeted_rerun",
    "localize_ball_roi",
    "noise_filter_adjustment",
    "tighten_noise_filter",
    "loosen_ball_recovery",
    "split_packet",
    "manual_review",
    "reject_noise",
    "adjust_highlight_window",
    "adjust_follow_cam",
    "tracking_rerun_before_follow_cam",
    "human_review_camera_motion",
    "render_suggested_highlight",
]

AIClipAction: TypeAlias = Literal[
    "extend_tail",
    "trim_head",
    "trim_tail",
    "split",
    "keep",
]

AI_FAILURE_TAGS = set(AIFailureTag.__args__)
AI_ROOT_CAUSE_MODULES = set(AIRootCauseModule.__args__)
AI_RECOMMENDED_ACTIONS = set(AIRecommendedAction.__args__)
AI_CLIP_ACTIONS = set(AIClipAction.__args__)
