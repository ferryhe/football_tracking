from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

AI_IMPROVEMENT_PROBLEM_TYPES = ("missing_ball", "noise", "follow_cam", "highlight")

PUBLIC_EXECUTABLE_ACTIONS = (
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

LEGACY_EXECUTABLE_ACTION_ALIASES = MappingProxyType({"targeted_rerun": "rerun_ball_window"})

ACTION_PROBLEM_TYPES = MappingProxyType(
    {
        "localize_ball_roi": "missing_ball",
        "rerun_ball_window": "missing_ball",
        "mark_ball_not_visible": "missing_ball",
        "noise_filter_adjustment": "noise",
        "tighten_noise_filter": "noise",
        "reject_noise": "noise",
        "adjust_follow_cam": "follow_cam",
        "tracking_rerun_before_follow_cam": "follow_cam",
        "adjust_highlight_window": "highlight",
        "render_suggested_highlight": "highlight",
    }
)

ACTION_SCHEMA_HINTS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "localize_ball_roi": (
            "candidate_id",
            "approval_id or approval-ready source id",
            "problem_type=missing_ball",
            "recommended_action=localize_ball_roi",
            "source_packet_id or visual_review_id",
            "start_frame",
            "end_frame",
            "local_search_roi",
            "expected_artifact",
            "comparison_criteria",
        ),
        "rerun_ball_window": (
            "candidate_id",
            "approval_id or approval-ready source id",
            "problem_type=missing_ball",
            "recommended_action=rerun_ball_window",
            "source_packet_id or visual_review_id",
            "rerun_scope.start_frame",
            "rerun_scope.end_frame",
            "expected_artifact",
            "comparison_criteria",
        ),
        "mark_ball_not_visible": (
            "candidate_id",
            "approval_id or approval-ready source id",
            "problem_type=missing_ball",
            "recommended_action=mark_ball_not_visible",
            "source_packet_id or visual_review_id",
            "start_frame",
            "end_frame",
            "likely_ball_region.description=not visible",
            "expected_artifact",
            "comparison_criteria",
        ),
        "noise_filter_adjustment": (
            "candidate_id",
            "approval_id or approval-ready source id",
            "problem_type=noise",
            "recommended_action=noise_filter_adjustment",
            "source_packet_id or visual_review_id",
            "start_frame",
            "end_frame",
            "false_positive_class",
            "config_patch",
            "expected_artifact",
            "comparison_criteria",
        ),
        "tighten_noise_filter": (
            "candidate_id",
            "approval_id or approval-ready source id",
            "problem_type=noise",
            "recommended_action=tighten_noise_filter",
            "source_packet_id or visual_review_id",
            "start_frame",
            "end_frame",
            "false_positive_class",
            "expected_artifact",
            "comparison_criteria",
        ),
        "reject_noise": (
            "candidate_id",
            "approval_id or approval-ready source id",
            "problem_type=noise",
            "recommended_action=reject_noise",
            "source_packet_id or visual_review_id",
            "start_frame",
            "end_frame",
            "false_positive_class",
            "expected_artifact",
            "comparison_criteria",
        ),
        "adjust_follow_cam": (
            "candidate_id",
            "approval_id or approval-ready source id",
            "problem_type=follow_cam",
            "recommended_action=adjust_follow_cam",
            "camera_motion_event_id",
            "start_frame",
            "end_frame",
            "config_patch or follow_cam_rerender_plan",
            "expected_artifact",
            "comparison_criteria",
        ),
        "tracking_rerun_before_follow_cam": (
            "candidate_id",
            "approval_id or approval-ready source id",
            "problem_type=follow_cam",
            "recommended_action=tracking_rerun_before_follow_cam",
            "camera_motion_event_id",
            "rerun_scope.start_frame",
            "rerun_scope.end_frame",
            "expected_artifact",
            "comparison_criteria",
        ),
        "adjust_highlight_window": (
            "candidate_id",
            "approval_id or approval-ready source id",
            "problem_type=highlight",
            "recommended_action=adjust_highlight_window",
            "event_candidate_id",
            "suggested_window.start_frame",
            "suggested_window.end_frame",
            "clip_action",
            "evidence",
            "expected_artifact",
            "comparison_criteria",
        ),
        "render_suggested_highlight": (
            "candidate_id",
            "approval_id or approval-ready source id",
            "problem_type=highlight",
            "recommended_action=render_suggested_highlight",
            "event_candidate_id",
            "suggested_window.start_frame",
            "suggested_window.end_frame",
            "clip_action",
            "evidence",
            "expected_artifact",
            "comparison_criteria",
        ),
    }
)

MODEL_POLICY_TEXT = (
    "Model policy: use a stronger model, specifically the configured strong model, for executable candidate "
    "suggestions, missing-ball localization, long-gap reasoning, visual localization, and any candidate-producing "
    "visual reasoning. Smaller or mini models only for low-risk tagging, operator labels, dry-run smoke, or "
    "review-only summaries; they must not manufacture executable approvals or candidate artifacts."
)

LANE_PROMPT_CONTRACTS: Mapping[str, str] = MappingProxyType(
    {
        "missing_ball": (
            "missing_ball lane: use rerun_ball_window, localize_ball_roi, or mark_ball_not_visible. "
            "Missing-ball suggestions must cover the entire lost gap with full-window coverage, or cover the full "
            "window end to end, or explain uncovered subwindows and list uncovered subwindows with explicit "
            "uncovered subranges when coverage is partial. The long right-bottom gap 2049-2544 cannot be closed by only checking around 2079; that is "
            "partial evidence, not full closure. localize_ball_roi is bounded-window-only and must not expand into "
            "broad full-video SAHI. localize_ball_roi and any local_search_roi require a traceable source_packet_id "
            "or visual_review_id from supplied packet evidence; executable localize_ball_roi also requires "
            "ai_visual_review or equivalent vision-reviewed wide/crop evidence. not_visible is acceptable only when "
            "packet or visual evidence shows the ball is hidden, off-frame, or impossible to identify."
        ),
        "noise": (
            "noise lane: spatial split or high-recall false positives should produce bounded cleanup suggestions, "
            "not broad full-video SAHI. Noise suggestions must include false_positive_class, bounded "
            "start_frame/end_frame, evidence ids, and accepted classes extra_ball, shoe_confusion, foot_confusion, "
            "player_head, advertising_board, sideline_confusion, wall_background_drift, unknown_false_positive, or "
            "unknown. Do not put these false-positive classes in failure_tags. Use noise_filter_adjustment with a "
            "safe config_patch, tighten_noise_filter for bounded threshold tightening, or reject_noise for confirmed "
            "false positives."
        ),
        "follow_cam": (
            "follow_cam lane: distinguish tracking recovery from camera tuning. Lost/Predicted or tracking "
            "instability near camera events => tracking_rerun_before_follow_cam; stable tracking with a camera-only "
            "issue => adjust_follow_cam. Camera-motion suggestions must use adjust_follow_cam for stable Detected "
            "tracking with sudden camera movement, tracking_rerun_before_follow_cam when the camera event overlaps "
            "Lost/Predicted or nearby tracking issues, or human_review_camera_motion when evidence is ambiguous; "
            "continuous stable high-speed play is evidence, not an automatic failure."
        ),
        "highlight": (
            "highlight lane: preserve highlight candidate.core_window and the required post-event tail unless "
            "source-video end clamps it. Highlight suggestions must include candidate.core_window, preserve "
            "candidate.buffer_policy.min_tail_frames, respect source-video boundary constraints, and do not trim "
            "result tail after a shot or goal. Use a path-safe candidate_id for the output candidate and "
            "event_candidate_id for the source event candidate from event_candidates.json. The default buffer may be adjusted, but the event core_window and "
            "available required post-event tail must remain inside the render window unless source-video end clamps it."
        ),
    }
)


def build_prompt_contract_text() -> str:
    action_list = ", ".join(PUBLIC_EXECUTABLE_ACTIONS)
    schema_hints = " ".join(f"{action}: {', '.join(fields)}." for action, fields in ACTION_SCHEMA_HINTS.items())
    lanes = " ".join(LANE_PROMPT_CONTRACTS[lane] for lane in AI_IMPROVEMENT_PROBLEM_TYPES)
    return (
        "AI improvement means candidate-producing, approval-ready improvement when evidence is sufficient; "
        "ordinary review advice remains review_only and non-mutating. "
        f"Public executable approved_action set is closed: {action_list}. "
        "Each improvement must include priority, area, failure_tags, root_cause_module, recommended_action, "
        "confidence. Every executable candidate must be bounded, traceable, and comparable: include candidate_id, "
        "approval_id or approval-ready source id, problem_type, recommended_action, source_packet_id or "
        "visual_review_id when packet or visual evidence is used, a bounded start_frame and end_frame or equivalent "
        "rerun_scope/suggested_window, evidence, expected_artifact, and comparison_criteria. "
        "Use rerun_ball_window for executable missing-ball reruns; legacy targeted_rerun may appear in older "
        "artifacts but should be treated as rerun_ball_window and is not part of the public executable set. "
        f"{schema_hints} {lanes} {MODEL_POLICY_TEXT} "
        "config_patch is advisory only and may only suggest known fields under follow_cam, postprocess, "
        "scene_bias.dynamic_air_recovery, selection, or tracking. "
        "Do not include image base64 or claim files that are not present in the supplied context."
    )


def build_ai_improvement_instructions(language: str | None) -> str:
    language_instruction = (
        "Write human-readable fields in Simplified Chinese."
        if language == "zh"
        else "Write human-readable fields in English."
    )
    return (
        "You are diagnosing football tracking run artifacts and producing an advisory improvement report. "
        "Return strict JSON only with keys: summary, improvements, highlight_adjustments. "
        "summary.status must be one of ok, needs_rerun, unavailable, error. "
        "Read context.candidate_intent and keep it distinct from workflow mode; valid intents are "
        "review_only, suggest_candidates, and prepare_approved_candidates. "
        "Every item must clearly separate a review-only note from an executable candidate. "
        f"{build_prompt_contract_text()} "
        f"{language_instruction}"
    )
