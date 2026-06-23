from __future__ import annotations

from football_tracking.ai_contracts import AI_EXECUTABLE_ACTIONS, AI_PROBLEM_TYPES
from football_tracking.ai_improvement_prompt_contract import (
    ACTION_SCHEMA_HINTS,
    LANE_PROMPT_CONTRACTS,
    PUBLIC_EXECUTABLE_ACTIONS,
    build_prompt_contract_text,
)

EXPECTED_PUBLIC_EXECUTABLE_ACTIONS = (
    "localize_ball_roi",
    "rerun_ball_window",
    "mark_ball_not_visible",
    "noise_filter_adjustment",
    "tighten_noise_filter",
    "reject_noise",
    "adjust_follow_cam",
    "tracking_rerun_before_follow_cam",
    "adjust_highlight_window",
    "render_suggested_highlight",
)


def test_public_executable_action_set_is_closed_and_current() -> None:
    contract_text = build_prompt_contract_text()

    assert PUBLIC_EXECUTABLE_ACTIONS == EXPECTED_PUBLIC_EXECUTABLE_ACTIONS
    assert AI_EXECUTABLE_ACTIONS == set(EXPECTED_PUBLIC_EXECUTABLE_ACTIONS)
    assert set(ACTION_SCHEMA_HINTS) == set(EXPECTED_PUBLIC_EXECUTABLE_ACTIONS)
    assert "targeted_rerun" not in PUBLIC_EXECUTABLE_ACTIONS
    for action in EXPECTED_PUBLIC_EXECUTABLE_ACTIONS:
        assert action in contract_text


def test_prompt_contract_defines_four_improvement_lanes() -> None:
    contract_text = build_prompt_contract_text()

    assert set(LANE_PROMPT_CONTRACTS) == {"missing_ball", "noise", "follow_cam", "highlight"}
    assert AI_PROBLEM_TYPES == {"missing_ball", "noise", "follow_cam", "highlight"}
    for lane in LANE_PROMPT_CONTRACTS:
        assert lane in contract_text


def test_executable_candidate_schema_fields_are_model_facing() -> None:
    contract_text = build_prompt_contract_text()

    for field in (
        "candidate_id",
        "approval_id",
        "approval-ready source id",
        "problem_type",
        "recommended_action",
        "source_packet_id",
        "visual_review_id",
        "event_candidate_id",
        "start_frame",
        "end_frame",
        "evidence",
        "expected_artifact",
        "comparison_criteria",
    ):
        assert field in contract_text


def test_missing_ball_contract_requires_full_long_gap_coverage_and_bounded_roi() -> None:
    contract_text = build_prompt_contract_text()

    assert "2049-2544" in contract_text
    assert "2079" in contract_text
    assert "full window" in contract_text
    assert "list uncovered subwindows" in contract_text
    assert "bounded-window-only" in contract_text
    assert "must not expand into broad full-video SAHI" in contract_text


def test_follow_cam_contract_distinguishes_tracking_rerun_from_camera_tuning() -> None:
    contract_text = build_prompt_contract_text()

    assert "Lost/Predicted" in contract_text
    assert "tracking_rerun_before_follow_cam" in contract_text
    assert "stable tracking with a camera-only issue" in contract_text
    assert "adjust_follow_cam" in contract_text


def test_highlight_contract_preserves_core_window_and_required_tail() -> None:
    contract_text = build_prompt_contract_text()

    assert "core_window" in contract_text
    assert "required post-event tail" in contract_text
    assert "unless source-video end clamps it" in contract_text


def test_model_policy_keeps_executable_suggestions_on_strong_model() -> None:
    contract_text = build_prompt_contract_text()

    assert "strong model" in contract_text
    assert "executable candidate suggestions" in contract_text
    assert "Smaller or mini models only" in contract_text
    assert "low-risk tagging" in contract_text
    assert "dry-run smoke" in contract_text
    assert "review-only summaries" in contract_text
