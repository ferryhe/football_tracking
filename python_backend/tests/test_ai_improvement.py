from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from football_tracking.ai_improvement import (
    _instructions,
    approve_ai_improvement_actions,
    build_ai_improvement_context,
    build_ai_improvement_report,
    compact_ai_improvement_summary,
    write_ai_improvement_report,
)
from football_tracking.api.ai_provider import OpenAIProviderSettings, OpenAIResponsesClient
from football_tracking.metrics import build_metrics_report, stats_from_metrics_report


class _FakeImprovementClient:
    def __init__(self, response: object, *, enabled: bool = True) -> None:
        self.response = response
        self.enabled = enabled
        self.calls: list[dict[str, object]] = []

    def is_enabled(self) -> bool:
        return self.enabled

    def create_json_response(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        if isinstance(self.response, Exception):
            raise self.response
        if not isinstance(self.response, dict):
            raise AssertionError("Fake response must be a dict or exception.")
        return self.response


class AiImprovementTests(unittest.TestCase):
    def test_improvement_prompt_protects_highlight_core_window_and_tail(self) -> None:
        instructions = _instructions(language="en")

        self.assertIn("candidate.core_window", instructions)
        self.assertIn("candidate.buffer_policy.min_tail_frames", instructions)
        self.assertIn("do not trim result tail", instructions)

    def test_write_ai_improvement_report_handles_missing_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)

            report = write_ai_improvement_report(output_dir, dry_run=True)
            written = json.loads((output_dir / "ai_improvement_report.json").read_text(encoding="utf-8"))

        self.assertEqual("unavailable", report["summary"]["status"])
        self.assertEqual([], report["improvements"])
        self.assertEqual([], report["highlight_adjustments"])
        self.assertEqual("missing", report["artifact_status"]["ball_audit"])
        self.assertEqual(report["summary"], written["summary"])
        self.assertEqual(report["artifact_status"], written["artifact_status"])
        self.assertTrue(any("missing" in warning for warning in report["warnings"]))

    def test_build_context_skips_corrupt_json_limits_items_and_strips_data_urls(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            (output_dir / "ball_audit.json").write_text("{", encoding="utf-8")
            _write_json(
                output_dir / "ai_review_triggers.json",
                {
                    "triggers": [
                        {"id": "trigger_1", "type": "lost_gap", "start_frame": 1, "end_frame": 3},
                        {"id": "trigger_2", "type": "large_jump", "start_frame": 4, "end_frame": 5},
                        {"id": "trigger_3", "type": "postprocess_action", "start_frame": 6, "end_frame": 6},
                    ]
                },
            )
            _write_json(
                output_dir / "review_packets.json",
                {
                    "packets": [
                        {"packet_id": "packet_1", "media": {"contact_sheet": "data:image/jpeg;base64,abc"}},
                        {"packet_id": "packet_2"},
                        {"packet_id": "packet_3"},
                    ]
                },
            )

            context = build_ai_improvement_context(output_dir, max_items=2)

        self.assertEqual("corrupt", context["artifact_status"]["ball_audit"])
        self.assertEqual(2, len(context["artifacts"]["ai_review_triggers"]["triggers"]))
        self.assertEqual(2, len(context["artifacts"]["review_packets"]["packets"]))
        self.assertNotIn("data:image", json.dumps(context, ensure_ascii=False))
        self.assertTrue(any("ball_audit.json" in warning and "corrupt" in warning for warning in context["warnings"]))

    def test_context_enriches_camera_motion_events_with_nearby_track_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_camera_motion_event(output_dir, frame=50, severity="fail", max_step_px=155.0)
            _write_ball_track(
                output_dir,
                [
                    (38, "Detected", 100, 100),
                    (49, "Predicted", 120, 100),
                    (50, "Lost", "", ""),
                    (51, "Detected", 130, 100),
                    (62, "Detected", 140, 100),
                ],
            )

            context = build_ai_improvement_context(output_dir)

        event = context["artifacts"]["camera_motion_audit"]["review_events"][0]
        track_window = event["nearby_ball_track"]
        self.assertEqual({"start_frame": 38, "end_frame": 62}, track_window["window"])
        self.assertEqual({"Detected": 3, "Predicted": 1, "Lost": 1}, track_window["status_counts"])
        self.assertEqual("tracking_issue", track_window["classification"])
        self.assertTrue(track_window["has_tracking_issue"])

    def test_dry_run_camera_event_overlapping_lost_track_recommends_tracking_rerun_first(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_camera_motion_event(output_dir, frame=40, severity="fail", max_step_px=150.0)
            _write_ball_track(
                output_dir,
                [
                    (28, "Detected", 100, 100),
                    (39, "Detected", 120, 100),
                    (40, "Lost", "", ""),
                    (41, "Predicted", 130, 100),
                    (52, "Detected", 140, 100),
                ],
            )

            report = build_ai_improvement_report(output_dir, dry_run=True)

        improvement = report["improvements"][0]
        self.assertEqual("tracking_rerun_before_follow_cam", improvement["recommended_action"])
        self.assertEqual("follow_cam", improvement["root_cause_module"])
        self.assertEqual("cam_event_001", improvement["camera_motion_event_id"])
        self.assertEqual(
            {"Detected": 3, "Predicted": 1, "Lost": 1},
            improvement["evidence_payload"]["nearby_ball_track"]["status_counts"],
        )

    def test_dry_run_stable_detected_camera_spike_recommends_adjust_follow_cam(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_camera_motion_event(output_dir, frame=40, severity="warn", max_step_px=100.0)
            _write_ball_track(
                output_dir,
                [
                    (28, "Detected", 100, 100),
                    (36, "Detected", 102, 100),
                    (40, "Detected", 105, 100),
                    (44, "Detected", 108, 100),
                    (52, "Detected", 110, 100),
                ],
            )

            report = build_ai_improvement_report(output_dir, dry_run=True)

        improvement = report["improvements"][0]
        self.assertEqual("adjust_follow_cam", improvement["recommended_action"])
        self.assertEqual({"follow_cam": {"glide_pan_smoothing": 0.18}}, improvement["config_patch"])
        self.assertFalse(improvement["follow_cam_rerender_plan"]["requires_tracking_rerun"])

    def test_dry_run_stable_high_speed_camera_event_is_evidence_not_auto_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_camera_motion_event(output_dir, frame=40, severity="warn", max_step_px=105.0)
            _write_ball_track(
                output_dir,
                [
                    (28, "Detected", 100, 100),
                    (34, "Detected", 210, 100),
                    (40, "Detected", 330, 100),
                    (46, "Detected", 455, 100),
                    (52, "Detected", 570, 100),
                ],
            )

            report = build_ai_improvement_report(output_dir, dry_run=True)
            context = build_ai_improvement_context(output_dir)

        self.assertEqual("ok", report["summary"]["status"])
        self.assertEqual([], report["improvements"])
        event = context["artifacts"]["camera_motion_audit"]["review_events"][0]
        self.assertEqual("acceptable_fast_play", event["nearby_ball_track"]["classification"])

    def test_dry_run_stable_detected_large_track_jump_requires_human_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_camera_motion_event(output_dir, frame=40, severity="fail", max_step_px=150.0)
            _write_ball_track(
                output_dir,
                [
                    (28, "Detected", 100, 100),
                    (34, "Detected", 120, 100),
                    (40, "Detected", 310, 100),
                    (46, "Detected", 330, 100),
                    (52, "Detected", 350, 100),
                ],
            )

            report = build_ai_improvement_report(output_dir, dry_run=True)
            context = build_ai_improvement_context(output_dir)

        improvement = report["improvements"][0]
        self.assertEqual("human_review_camera_motion", improvement["recommended_action"])
        self.assertEqual("track_jump_review", improvement["evidence_payload"]["nearby_ball_track"]["classification"])
        event = context["artifacts"]["camera_motion_audit"]["review_events"][0]
        self.assertEqual("track_jump_review", event["nearby_ball_track"]["classification"])

    def test_dry_run_camera_event_without_track_context_requires_human_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_camera_motion_event(output_dir, frame=40, severity="warn", max_step_px=120.0)

            report = build_ai_improvement_report(output_dir, dry_run=True)

        improvement = report["improvements"][0]
        self.assertEqual("human_review_camera_motion", improvement["recommended_action"])
        self.assertEqual("no_track_context", improvement["evidence_payload"]["nearby_ball_track"]["classification"])
        self.assertEqual(0, improvement["evidence_payload"]["nearby_ball_track"]["frame_count"])

    def test_camera_track_status_is_normalized_and_unknown_status_is_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_camera_motion_event(output_dir, frame=40, severity="warn", max_step_px=115.0)
            (output_dir / "ball_track.csv").write_text(
                "Frame,X,Y,Status\n"
                "28,100,100, detected \n"
                "36,102,100,DETECTED\n"
                "40,104,100,\n"
                "44,106,100,maybe\n"
                "52,108,100,Detected\n",
                encoding="utf-8",
            )

            context = build_ai_improvement_context(output_dir)
            report = build_ai_improvement_report(output_dir, dry_run=True)

        event = context["artifacts"]["camera_motion_audit"]["review_events"][0]
        self.assertEqual({"Detected": 3, "unknown": 2}, event["nearby_ball_track"]["status_counts"])
        self.assertEqual("ambiguous_status", event["nearby_ball_track"]["classification"])
        improvement = report["improvements"][0]
        self.assertEqual("human_review_camera_motion", improvement["recommended_action"])
        self.assertEqual("ambiguous_status", improvement["evidence_payload"]["nearby_ball_track"]["classification"])

    def test_context_rejects_excessive_max_items(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            with self.assertRaisesRegex(ValueError, "at most 100"):
                build_ai_improvement_context(Path(temp_name), max_items=101)

    def test_fake_client_success_is_validated_sanitized_and_compacted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            client = _FakeImprovementClient(
                {
                    "summary": {"status": "needs_rerun", "primary_issue": "tracking"},
                    "improvements": [
                        {
                            "id": "imp_001",
                            "priority": "P0",
                            "area": "tracking",
                            "failure_tags": ["ball_lost"],
                            "root_cause_module": "reacquisition",
                            "start_frame": 10,
                            "end_frame": 20,
                            "diagnosis": "Ball is likely lost after a crowded touch.",
                            "recommended_action": "targeted_rerun",
                            "config_patch": {
                                "tracking": {"max_lost_frames": 32},
                                "detector": {"confidence_threshold": 0.01},
                            },
                            "rerun_scope": {"start_frame": 0, "end_frame": 40},
                            "likely_ball_region": {
                                "frame": 15,
                                "description": "right channel near the player cluster",
                                "confidence": 0.7,
                            },
                            "evidence": ["lost gap overlaps an active play packet"],
                            "confidence": 0.82,
                        }
                    ],
                    "highlight_adjustments": [
                        {
                            "candidate_id": "cleaned:shot_candidate:10-20",
                            "current_window": {"start_frame": 10, "end_frame": 20},
                            "suggested_window": {"start_frame": 5, "end_frame": 45},
                            "reason": "Post-shot result is truncated.",
                            "confidence": 0.74,
                        }
                    ],
                }
            )

            report = write_ai_improvement_report(output_dir, client=client, model="gpt-improve", max_items=1)
            compact = compact_ai_improvement_summary(report)
            written_exists = (output_dir / "ai_improvement_report.json").exists()

        self.assertEqual(1, len(client.calls))
        self.assertEqual("gpt-improve", client.calls[0]["model"])
        self.assertEqual("gpt-improve", report["model"])
        self.assertEqual("needs_rerun", report["summary"]["status"])
        self.assertEqual(1, report["summary"]["improvement_count"])
        self.assertEqual(1, report["summary"]["targeted_rerun_count"])
        self.assertEqual(1, report["summary"]["config_patch_count"])
        self.assertEqual(1, report["summary"]["highlight_adjustment_count"])
        self.assertEqual({"tracking": {"max_lost_frames": 32}}, report["improvements"][0]["config_patch"])
        self.assertTrue(any("detector.confidence_threshold" in warning for warning in report["warnings"]))
        self.assertEqual(report["summary"], compact)
        self.assertTrue(written_exists)

    def test_improvement_model_setting_is_used_when_model_is_not_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            client = _FakeImprovementClient(
                {
                    "summary": {"status": "ok"},
                    "improvements": [],
                    "highlight_adjustments": [],
                }
            )
            client.settings = SimpleNamespace(chat_model="gpt-review-mini", improvement_model="gpt-improve-strong")

            report = write_ai_improvement_report(output_dir, client=client)

        self.assertEqual("gpt-improve-strong", client.calls[0]["model"])
        self.assertEqual("gpt-improve-strong", report["model"])
        self.assertEqual({"model": "gpt-improve-strong", "source": "improvement_model"}, report["model_selection"])

    def test_explicit_model_overrides_improvement_model_setting(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            client = _FakeImprovementClient(
                {
                    "summary": {"status": "ok"},
                    "improvements": [],
                    "highlight_adjustments": [],
                }
            )
            client.settings = SimpleNamespace(chat_model="gpt-review-mini", improvement_model="gpt-improve-strong")

            report = write_ai_improvement_report(output_dir, client=client, model="gpt-explicit")

        self.assertEqual("gpt-explicit", client.calls[0]["model"])
        self.assertEqual("gpt-explicit", report["model"])
        self.assertEqual({"model": "gpt-explicit", "source": "explicit"}, report["model_selection"])

    def test_provider_failure_writes_redacted_error_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            client = _FakeImprovementClient(
                RuntimeError(
                    "provider echoed plain-secret Bearer sk-secret-token and data:image/jpeg;base64,abcdef123456"
                )
            )
            client.settings = SimpleNamespace(api_key="plain-secret")

            report = write_ai_improvement_report(output_dir, client=client, model="gpt-improve")
            written = json.loads((output_dir / "ai_improvement_report.json").read_text(encoding="utf-8"))

        self.assertEqual("error", report["summary"]["status"])
        self.assertEqual(report["summary"], written["summary"])
        self.assertNotIn("plain-secret", report["error"])
        self.assertNotIn("sk-secret-token", report["error"])
        self.assertNotIn("abcdef123456", report["error"])
        self.assertIn("<redacted", report["error"])

    def test_invalid_targeted_rerun_response_becomes_error_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            client = _FakeImprovementClient(
                {
                    "summary": {"status": "needs_rerun"},
                    "improvements": [
                        {
                            "id": "imp_001",
                            "priority": "P1",
                            "area": "tracking",
                            "failure_tags": ["ball_lost"],
                            "root_cause_module": "reacquisition",
                            "recommended_action": "targeted_rerun",
                            "confidence": 0.5,
                            "evidence": ["missing rerun scope fixture"],
                        }
                    ],
                }
            )

            report = build_ai_improvement_report(output_dir, client=client)

        self.assertEqual("error", report["summary"]["status"])
        self.assertIn("rerun_scope", report["error"])

    def test_status_ok_with_actions_is_normalized_to_needs_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            client = _FakeImprovementClient(
                {
                    "summary": {"status": "ok", "primary_issue": "tracking"},
                    "improvements": [
                        {
                            "id": "imp_001",
                            "priority": "P1",
                            "area": "tracking",
                            "failure_tags": ["ball_lost"],
                            "root_cause_module": "reacquisition",
                            "diagnosis": "Ball went missing.",
                            "recommended_action": "targeted_rerun",
                            "rerun_scope": {"start_frame": 10, "end_frame": 30},
                            "likely_ball_region": {"description": "not visible", "confidence": 0.0},
                            "confidence": 0.6,
                        }
                    ],
                }
            )

            report = build_ai_improvement_report(output_dir, client=client)

        self.assertEqual("needs_rerun", report["summary"]["status"])
        self.assertTrue(any("normalized from ok" in warning for warning in report["warnings"]))

    def test_negative_frame_windows_become_error_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            client = _FakeImprovementClient(
                {
                    "summary": {"status": "needs_rerun"},
                    "improvements": [
                        {
                            "id": "imp_001",
                            "priority": "P1",
                            "area": "tracking",
                            "failure_tags": ["ball_lost"],
                            "root_cause_module": "reacquisition",
                            "diagnosis": "Bad negative window.",
                            "recommended_action": "targeted_rerun",
                            "rerun_scope": {"start_frame": -1, "end_frame": 30},
                            "likely_ball_region": {"description": "not visible", "confidence": 0.0},
                            "confidence": 0.6,
                        }
                    ],
                }
            )

            report = build_ai_improvement_report(output_dir, client=client)

        self.assertEqual("error", report["summary"]["status"])
        self.assertIn("non-negative", report["error"])

    def test_fractional_frame_window_becomes_error_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            client = _FakeImprovementClient(
                {
                    "summary": {"status": "needs_rerun"},
                    "improvements": [
                        {
                            "id": "imp_001",
                            "priority": "P1",
                            "area": "tracking",
                            "failure_tags": ["ball_lost"],
                            "root_cause_module": "reacquisition",
                            "diagnosis": "Bad fractional frame.",
                            "recommended_action": "targeted_rerun",
                            "rerun_scope": {"start_frame": 10.9, "end_frame": 30},
                            "likely_ball_region": {"description": "not visible", "confidence": 0.0},
                            "confidence": 0.6,
                        }
                    ],
                }
            )

            report = build_ai_improvement_report(output_dir, client=client)

        self.assertEqual("error", report["summary"]["status"])
        self.assertIn("integer", report["error"])

    def test_invalid_local_search_roi_becomes_error_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            client = _FakeImprovementClient(
                {
                    "summary": {"status": "needs_rerun"},
                    "improvements": [
                        {
                            "id": "imp_001",
                            "priority": "P1",
                            "area": "tracking",
                            "failure_tags": ["ball_lost"],
                            "root_cause_module": "reacquisition",
                            "diagnosis": "Bad ROI.",
                            "recommended_action": "targeted_rerun",
                            "rerun_scope": {"start_frame": 10, "end_frame": 30},
                            "local_search_roi": {
                                "coordinate_space": "normalized",
                                "frame": 20,
                                "x": 0,
                                "y": 0,
                                "width": 100,
                                "height": 100,
                                "confidence": 0.8,
                            },
                            "confidence": 0.6,
                        }
                    ],
                }
            )

            report = build_ai_improvement_report(output_dir, client=client)

        self.assertEqual("error", report["summary"]["status"])
        self.assertIn("coordinate_space", report["error"])

    def test_localize_ball_roi_action_requires_and_preserves_roi(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            client = _FakeImprovementClient(
                {
                    "summary": {"status": "needs_rerun"},
                    "improvements": [
                        {
                            "id": "imp_001",
                            "priority": "P0",
                            "area": "tracking",
                            "failure_tags": ["ball_lost"],
                            "root_cause_module": "reacquisition",
                            "diagnosis": "Ball is likely in the right corner packet crop.",
                            "recommended_action": "localize_ball_roi",
                            "local_search_roi": {
                                "coordinate_space": "image",
                                "frame": 2088,
                                "x": 4300,
                                "y": 760,
                                "width": 900,
                                "height": 520,
                                "confidence": 0.74,
                            },
                            "evidence": ["visual packet shows a likely ball near the corner arc"],
                            "confidence": 0.78,
                        }
                    ],
                }
            )

            report = build_ai_improvement_report(output_dir, client=client)

        self.assertEqual("needs_rerun", report["summary"]["status"])
        self.assertEqual("localize_ball_roi", report["improvements"][0]["recommended_action"])
        self.assertEqual(4300.0, report["improvements"][0]["local_search_roi"]["x"])

    def test_noise_filter_adjustment_requires_actionable_scope_and_patch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            client = _FakeImprovementClient(
                {
                    "summary": {"status": "needs_rerun", "primary_issue": "noise"},
                    "improvements": [
                        {
                            "id": "imp_noise",
                            "priority": "P1",
                            "area": "tracking",
                            "failure_tags": ["shoe_confusion"],
                            "root_cause_module": "selection",
                            "start_frame": 120,
                            "end_frame": 142,
                            "diagnosis": "Detector candidates are repeatedly accepting shoes near the touchline.",
                            "recommended_action": "noise_filter_adjustment",
                            "false_positive_class": "shoe_confusion",
                            "config_patch": {"selection": {"min_accept_score": 0.62}},
                            "evidence": ["dense-noise packet has three accepted shoe candidates"],
                            "confidence": 0.73,
                        }
                    ],
                }
            )

            report = build_ai_improvement_report(output_dir, client=client)

        improvement = report["improvements"][0]
        self.assertEqual("needs_rerun", report["summary"]["status"])
        self.assertEqual("noise_filter_adjustment", improvement["recommended_action"])
        self.assertEqual("shoe_confusion", improvement["false_positive_class"])
        self.assertEqual({"selection": {"min_accept_score": 0.62}}, improvement["config_patch"])

    def test_camera_actions_are_validated_and_strip_invalid_follow_cam_patch_names(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            client = _FakeImprovementClient(
                {
                    "summary": {"status": "needs_rerun", "primary_issue": "camera_motion"},
                    "improvements": [
                        {
                            "id": "imp_camera",
                            "priority": "P1",
                            "area": "camera_motion",
                            "failure_tags": ["camera_catchup_spike"],
                            "root_cause_module": "follow_cam",
                            "start_frame": 40,
                            "end_frame": 40,
                            "diagnosis": "Stable detected tracking with a follow-cam catch-up spike.",
                            "recommended_action": "adjust_follow_cam",
                            "camera_motion_event_id": "cam_event_001",
                            "config_patch": {
                                "follow_cam": {
                                    "glide_pan_smoothing": 0.2,
                                    "max_zoom_out_per_frame": 24.0,
                                    "dead_zone_ratio_x": 0.9,
                                    "max_pan_per_frame_x": -1,
                                    "unknown_knob": 3,
                                },
                                "detector": {"confidence_threshold": 0.01},
                            },
                            "follow_cam_rerender_plan": {"reason": "rerender after smoothing change"},
                            "confidence": 0.73,
                        },
                        {
                            "id": "imp_review",
                            "priority": "P2",
                            "area": "camera_motion",
                            "failure_tags": ["camera_catchup_spike"],
                            "root_cause_module": "follow_cam",
                            "start_frame": 44,
                            "end_frame": 46,
                            "diagnosis": "Ambiguous camera motion source.",
                            "recommended_action": "human_review_camera_motion",
                            "camera_motion_event_id": "cam_event_002",
                            "evidence": ["ambiguous camera spike"],
                            "confidence": 0.51,
                        },
                    ],
                }
            )

            report = build_ai_improvement_report(output_dir, client=client)

        self.assertEqual("adjust_follow_cam", report["improvements"][0]["recommended_action"])
        self.assertEqual(
            {"follow_cam": {"glide_pan_smoothing": 0.2, "max_zoom_out_per_frame": 24.0}},
            report["improvements"][0]["config_patch"],
        )
        self.assertEqual("human_review_camera_motion", report["improvements"][1]["recommended_action"])
        self.assertTrue(any("follow_cam.dead_zone_ratio_x" in warning for warning in report["warnings"]))
        self.assertTrue(any("follow_cam.max_pan_per_frame_x" in warning for warning in report["warnings"]))
        self.assertTrue(any("follow_cam.unknown_knob" in warning for warning in report["warnings"]))
        self.assertTrue(any("detector.confidence_threshold" in warning for warning in report["warnings"]))

    def test_noise_filter_adjustment_without_false_positive_class_becomes_error_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            client = _FakeImprovementClient(
                {
                    "summary": {"status": "needs_rerun", "primary_issue": "noise"},
                    "improvements": [
                        {
                            "id": "imp_noise",
                            "priority": "P1",
                            "area": "tracking",
                            "failure_tags": ["shoe_confusion"],
                            "root_cause_module": "selection",
                            "start_frame": 120,
                            "end_frame": 142,
                            "diagnosis": "Missing actionable false-positive class.",
                            "recommended_action": "noise_filter_adjustment",
                            "config_patch": {"selection": {"min_accept_score": 0.62}},
                            "evidence": ["dense-noise packet"],
                            "confidence": 0.73,
                        }
                    ],
                }
            )

            report = build_ai_improvement_report(output_dir, client=client)

        self.assertEqual("error", report["summary"]["status"])
        self.assertIn("false_positive_class", report["error"])

    def test_unknown_recommended_action_becomes_error_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            client = _FakeImprovementClient(
                {
                    "summary": {"status": "needs_rerun"},
                    "improvements": [
                        {
                            "id": "imp_001",
                            "priority": "P1",
                            "area": "tracking",
                            "failure_tags": ["ball_lost"],
                            "root_cause_module": "reacquisition",
                            "diagnosis": "Unsupported action spelling.",
                            "recommended_action": "targeted rerun",
                            "likely_ball_region": {"description": "not visible", "confidence": 0.0},
                            "confidence": 0.6,
                        }
                    ],
                }
            )

            report = build_ai_improvement_report(output_dir, client=client)

        self.assertEqual("error", report["summary"]["status"])
        self.assertIn("recommended_action", report["error"])

    def test_flat_dotted_config_patch_keys_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            client = _FakeImprovementClient(
                {
                    "summary": {"status": "needs_rerun"},
                    "improvements": [
                        {
                            "id": "imp_001",
                            "priority": "P1",
                            "area": "tracking",
                            "failure_tags": ["ball_lost"],
                            "root_cause_module": "reacquisition",
                            "diagnosis": "Flat patch key should not be accepted.",
                            "recommended_action": "targeted_rerun",
                            "config_patch": {"tracking.max_lost_frames": 32},
                            "rerun_scope": {"start_frame": 10, "end_frame": 30},
                            "likely_ball_region": {"description": "not visible", "confidence": 0.0},
                            "confidence": 0.6,
                        }
                    ],
                }
            )

            report = build_ai_improvement_report(output_dir, client=client)

        self.assertEqual({}, report["improvements"][0]["config_patch"])
        self.assertTrue(any("tracking.max_lost_frames" in warning for warning in report["warnings"]))

    def test_provider_prompt_redacts_local_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            _write_json(
                output_dir / "review_packets.json",
                {
                    "warnings": [f"failed to write media under {output_dir.resolve()}"],
                    "packets": [
                        {
                            "packet_id": "packet_001",
                            "media": {
                                "contact_sheet": str((output_dir / "contact_sheet.jpg").resolve()),
                                "crop_sheet": str((output_dir / "crop_sheet.jpg").resolve()),
                                "clip": str((output_dir / "packet_001.mp4").resolve()),
                            },
                        }
                    ]
                },
            )
            client = _FakeImprovementClient({"summary": {"status": "ok"}, "improvements": []})

            build_ai_improvement_report(output_dir, client=client)
            prompt = str(client.calls[0]["prompt"])

        self.assertNotIn(str(output_dir.resolve()), prompt)
        self.assertIn("<redacted-path>", prompt)

    def test_dry_run_uses_deterministic_suggestions_without_calling_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            client = _FakeImprovementClient(RuntimeError("must not call provider"))

            report = build_ai_improvement_report(output_dir, client=client, dry_run=True)

        self.assertEqual([], client.calls)
        self.assertEqual("needs_rerun", report["summary"]["status"])
        self.assertEqual("targeted_rerun", report["improvements"][0]["recommended_action"])
        self.assertIn("rerun_scope", report["improvements"][0])
        self.assertEqual("not visible", report["improvements"][0]["likely_ball_region"]["description"])
        self.assertNotIn("local_search_roi", report["improvements"][0])

    def test_visual_review_roi_merges_by_source_packet_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            _write_json(
                output_dir / "ai_visual_review.json",
                {
                    "reviews": [
                        {
                            "visual_review_id": "visual_review:packet_001",
                            "packet_id": "packet_001",
                            "source_packet_id": "packet_001",
                            "visible": True,
                            "likely_ball_region": {
                                "frame": 15,
                                "description": "right touchline",
                                "confidence": 0.72,
                            },
                            "local_search_roi": {
                                "coordinate_space": "image",
                                "frame": 15,
                                "x": 120.0,
                                "y": 40.0,
                                "width": 80.0,
                                "height": 50.0,
                                "confidence": 0.72,
                            },
                            "frame_dimensions": {"width": 640, "height": 360},
                            "provenance": {"source": "ai_visual_review", "model": "vision-test"},
                        }
                    ]
                },
            )
            client = _FakeImprovementClient(
                {
                    "summary": {"status": "needs_rerun", "primary_issue": "tracking"},
                    "improvements": [
                        {
                            "id": "imp_001",
                            "priority": "P1",
                            "area": "tracking",
                            "failure_tags": ["ball_lost"],
                            "root_cause_module": "reacquisition",
                            "diagnosis": "Ball lost in packet.",
                            "recommended_action": "targeted_rerun",
                            "rerun_scope": {"start_frame": 10, "end_frame": 20},
                            "local_search_roi": {
                                "coordinate_space": "image",
                                "frame": 15,
                                "x": 120,
                                "y": 40,
                                "width": 80,
                                "height": 50,
                                "confidence": 0.72,
                            },
                            "likely_ball_region": {"description": "not visible", "confidence": 0.0},
                            "evidence": [{"source_packet_id": "packet_001"}],
                            "confidence": 0.6,
                        }
                    ],
                }
            )

            report = build_ai_improvement_report(output_dir, client=client)

        improvement = report["improvements"][0]
        self.assertEqual("visual_review:packet_001", improvement["evidence_payload"]["visual_review_id"])
        self.assertEqual({"width": 640, "height": 360}, improvement["evidence_payload"]["frame_dimensions"])
        self.assertEqual("ai_visual_review", improvement["evidence_payload"]["local_search_roi_provenance"]["source"])
        self.assertEqual(120.0, improvement["local_search_roi"]["x"])
        self.assertEqual("right touchline", improvement["likely_ball_region"]["description"])

    def test_visual_review_not_visible_does_not_create_fake_roi(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            _write_json(
                output_dir / "ai_visual_review.json",
                {
                    "reviews": [
                        {
                            "visual_review_id": "visual_review:packet_001",
                            "packet_id": "packet_001",
                            "source_packet_id": "packet_001",
                            "visible": False,
                            "likely_ball_region": {"description": "not visible", "confidence": 0.0},
                        }
                    ]
                },
            )
            client = _FakeImprovementClient(
                {
                    "summary": {"status": "needs_rerun", "primary_issue": "tracking"},
                    "improvements": [
                        {
                            "id": "imp_001",
                            "priority": "P1",
                            "area": "tracking",
                            "failure_tags": ["ball_lost"],
                            "root_cause_module": "reacquisition",
                            "diagnosis": "Ball lost in packet.",
                            "recommended_action": "targeted_rerun",
                            "rerun_scope": {"start_frame": 10, "end_frame": 20},
                            "likely_ball_region": {"description": "not visible", "confidence": 0.0},
                            "evidence": [{"source_packet_id": "packet_001"}],
                            "confidence": 0.6,
                        }
                    ],
                }
            )

            report = build_ai_improvement_report(output_dir, client=client)

        self.assertNotIn("local_search_roi", report["improvements"][0])
        self.assertEqual("not visible", report["improvements"][0]["likely_ball_region"]["description"])

    def test_visual_review_not_visible_downgrades_localize_ball_roi_action(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            _write_json(
                output_dir / "ai_visual_review.json",
                {
                    "reviews": [
                        {
                            "visual_review_id": "visual_review:packet_001",
                            "packet_id": "packet_001",
                            "source_packet_id": "packet_001",
                            "visible": False,
                            "likely_ball_region": {"description": "not visible", "confidence": 0.0},
                        }
                    ]
                },
            )
            client = _FakeImprovementClient(
                {
                    "summary": {"status": "needs_rerun", "primary_issue": "tracking"},
                    "improvements": [
                        {
                            "id": "imp_001",
                            "priority": "P1",
                            "area": "tracking",
                            "failure_tags": ["ball_lost"],
                            "root_cause_module": "reacquisition",
                            "diagnosis": "Model thinks the ball can be localized.",
                            "recommended_action": "localize_ball_roi",
                            "local_search_roi": {
                                "coordinate_space": "image",
                                "frame": 15,
                                "x": 120,
                                "y": 40,
                                "width": 80,
                                "height": 50,
                                "confidence": 0.72,
                            },
                            "evidence": [{"source_packet_id": "packet_001"}],
                            "confidence": 0.6,
                        }
                    ],
                }
            )

            report = build_ai_improvement_report(output_dir, client=client)

        improvement = report["improvements"][0]
        self.assertEqual("manual_review", improvement["recommended_action"])
        self.assertNotIn("local_search_roi", improvement)
        self.assertEqual("not visible", improvement["likely_ball_region"]["description"])
        self.assertTrue(any("normalized from localize_ball_roi" in warning for warning in report["warnings"]))

    def test_approving_targeted_rerun_writes_approved_actions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            _write_json(
                output_dir / "ai_improvement_report.json",
                {
                    "schema_version": "1.0",
                    "generated_at": "2026-06-22T00:00:00+00:00",
                    "model": "gpt-improve",
                    "summary": {"status": "needs_rerun"},
                    "improvements": [
                        {
                            "id": "imp_001",
                            "priority": "P0",
                            "area": "tracking",
                            "failure_tags": ["ball_lost"],
                            "root_cause_module": "reacquisition",
                            "diagnosis": "Recover localized ball.",
                            "recommended_action": "targeted_rerun",
                            "rerun_scope": {"start_frame": 10, "end_frame": 20},
                            "local_search_roi": {
                                "coordinate_space": "image",
                                "frame": 15,
                                "x": 120,
                                "y": 40,
                                "width": 80,
                                "height": 50,
                                "confidence": 0.72,
                            },
                            "evidence_payload": {
                                "source_packet_id": "packet_001",
                                "visual_review_id": "visual_review:packet_001",
                            },
                            "confidence": 0.82,
                        }
                    ],
                },
            )

            artifact = approve_ai_improvement_actions(
                output_dir,
                run_id="run_123",
                improvement_ids=["imp_001"],
                approved_by="operator-a",
            )
            written = json.loads((output_dir / "ai_improvement_approved_actions.json").read_text(encoding="utf-8"))

        action = artifact["approved_actions"][0]
        self.assertEqual("run_123", artifact["run_id"])
        self.assertEqual(artifact, written)
        self.assertEqual("approval_001", action["approval_id"])
        self.assertEqual("targeted_rerun", action["approved_action"])
        self.assertEqual("operator-a", action["approved_by"])
        self.assertEqual("packet_001", action["source_packet_id"])
        self.assertEqual("visual_review:packet_001", action["visual_review_id"])
        self.assertEqual({"start_frame": 10, "end_frame": 20}, action["rerun_scope"])
        self.assertEqual("gpt-improve", action["provenance"]["model"])

    def test_approval_config_patch_strips_invalid_paths_and_writes_derived_patch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_json(
                output_dir / "ai_improvement_report.json",
                {
                    "schema_version": "1.0",
                    "generated_at": "2026-06-22T00:00:00+00:00",
                    "model": "gpt-improve",
                    "summary": {"status": "needs_rerun"},
                    "improvements": [
                        {
                            "id": "imp_filter",
                            "priority": "P1",
                            "area": "tracking",
                            "failure_tags": ["foot_confusion"],
                            "root_cause_module": "selection",
                            "start_frame": 40,
                            "end_frame": 52,
                            "diagnosis": "Noise filter can tighten.",
                            "recommended_action": "noise_filter_adjustment",
                            "false_positive_class": "foot_confusion",
                            "config_patch": {"selection": {"min_accept_score": 0.55}},
                            "confidence": 0.7,
                        }
                    ],
                },
            )

            artifact = approve_ai_improvement_actions(
                output_dir,
                run_id="run_123",
                improvement_ids=["imp_filter"],
                config_patch_overrides={
                    "imp_filter": {
                        "selection": {"min_accept_score": 0.6},
                        "detector": {"confidence_threshold": 0.01},
                        "tracking.max_lost_frames": 20,
                    }
                },
            )
            patch_artifact = json.loads((output_dir / "ai_improvement_approved_config_patch.json").read_text(encoding="utf-8"))

        self.assertEqual({"selection": {"min_accept_score": 0.6}}, artifact["approved_actions"][0]["config_patch"])
        self.assertEqual("foot_confusion", artifact["approved_actions"][0]["false_positive_class"])
        self.assertEqual(40, artifact["approved_actions"][0]["start_frame"])
        self.assertEqual(52, artifact["approved_actions"][0]["end_frame"])
        self.assertEqual({"selection": {"min_accept_score": 0.6}}, patch_artifact["merged_config_patch"])
        self.assertTrue(any("detector.confidence_threshold" in warning for warning in artifact["warnings"]))
        self.assertTrue(any("tracking.max_lost_frames" in warning for warning in artifact["warnings"]))

    def test_approving_adjust_follow_cam_writes_follow_cam_rerender_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_json(
                output_dir / "ai_improvement_report.json",
                {
                    "schema_version": "1.0",
                    "generated_at": "2026-06-22T00:00:00+00:00",
                    "model": "gpt-improve",
                    "summary": {"status": "needs_rerun"},
                    "improvements": [
                        {
                            "id": "imp_camera",
                            "priority": "P1",
                            "area": "camera_motion",
                            "failure_tags": ["camera_catchup_spike"],
                            "root_cause_module": "follow_cam",
                            "start_frame": 40,
                            "end_frame": 40,
                            "diagnosis": "Stable tracking but follow-cam jumped.",
                            "recommended_action": "adjust_follow_cam",
                            "config_patch": {
                                "follow_cam": {"glide_pan_smoothing": 0.2},
                                "detector": {"confidence_threshold": 0.01},
                            },
                            "confidence": 0.74,
                        }
                    ],
                },
            )

            artifact = approve_ai_improvement_actions(
                output_dir,
                run_id="run_123",
                improvement_ids=["imp_camera"],
                approved_by="operator-a",
            )
            plan = json.loads((output_dir / "follow_cam_rerender_plan.json").read_text(encoding="utf-8"))

        action = artifact["approved_actions"][0]
        self.assertEqual("adjust_follow_cam", action["approved_action"])
        self.assertEqual("approval_001", plan["approval_id"])
        self.assertEqual("imp_camera", plan["improvement_id"])
        self.assertEqual("ai_improvement_approved_action", plan["source"])
        self.assertEqual("ai_improvement_approved_actions.json", plan["source_approved_actions"])
        self.assertEqual({"follow_cam": {"glide_pan_smoothing": 0.2}}, plan["recommended_config_patch"])
        self.assertFalse(plan["requires_tracking_rerun"])
        self.assertIn("Stable tracking", plan["reason"])
        self.assertTrue(any("detector.confidence_threshold" in warning for warning in artifact["warnings"]))

    def test_approving_tracking_rerun_before_follow_cam_writes_rerun_required_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_json(
                output_dir / "ai_improvement_report.json",
                {
                    "schema_version": "1.0",
                    "generated_at": "2026-06-22T00:00:00+00:00",
                    "model": "gpt-improve",
                    "summary": {"status": "needs_rerun"},
                    "improvements": [
                        {
                            "id": "imp_camera_track",
                            "priority": "P0",
                            "area": "camera_motion",
                            "failure_tags": ["camera_catchup_spike", "ball_lost"],
                            "root_cause_module": "follow_cam",
                            "start_frame": 40,
                            "end_frame": 42,
                            "diagnosis": "Camera jump is track-driven.",
                            "recommended_action": "tracking_rerun_before_follow_cam",
                            "rerun_scope": {"start_frame": 28, "end_frame": 54},
                            "confidence": 0.8,
                        }
                    ],
                },
            )

            approve_ai_improvement_actions(output_dir, run_id="run_123", improvement_ids=["imp_camera_track"])
            plan = json.loads((output_dir / "follow_cam_rerender_plan.json").read_text(encoding="utf-8"))

        self.assertTrue(plan["requires_tracking_rerun"])
        self.assertEqual("ai_improvement_approved_action", plan["source"])
        self.assertEqual({"start_frame": 28, "end_frame": 54}, plan["tracking_rerun_scope"])
        self.assertEqual({}, plan["recommended_config_patch"])
        self.assertIn("Track rerun is required", plan["reason"])

    def test_approving_mixed_camera_actions_prioritizes_tracking_rerun_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_json(
                output_dir / "ai_improvement_report.json",
                {
                    "schema_version": "1.0",
                    "generated_at": "2026-06-22T00:00:00+00:00",
                    "model": "gpt-improve",
                    "summary": {"status": "needs_rerun"},
                    "improvements": [
                        {
                            "id": "imp_camera_adjust",
                            "priority": "P1",
                            "area": "camera_motion",
                            "failure_tags": ["camera_catchup_spike"],
                            "root_cause_module": "follow_cam",
                            "start_frame": 40,
                            "end_frame": 40,
                            "diagnosis": "Stable tracking but follow-cam jumped.",
                            "recommended_action": "adjust_follow_cam",
                            "config_patch": {"follow_cam": {"glide_pan_smoothing": 0.2}},
                            "camera_motion_event_id": "cam_event_001",
                            "confidence": 0.7,
                        },
                        {
                            "id": "imp_camera_track",
                            "priority": "P0",
                            "area": "camera_motion",
                            "failure_tags": ["camera_catchup_spike", "ball_lost"],
                            "root_cause_module": "follow_cam",
                            "start_frame": 44,
                            "end_frame": 46,
                            "diagnosis": "Camera jump is track-driven.",
                            "recommended_action": "tracking_rerun_before_follow_cam",
                            "rerun_scope": {"start_frame": 32, "end_frame": 58},
                            "camera_motion_event_id": "cam_event_002",
                            "confidence": 0.8,
                        },
                    ],
                },
            )

            approve_ai_improvement_actions(
                output_dir,
                run_id="run_123",
                improvement_ids=["imp_camera_adjust", "imp_camera_track"],
            )
            plan = json.loads((output_dir / "follow_cam_rerender_plan.json").read_text(encoding="utf-8"))

        self.assertEqual("tracking_rerun_before_follow_cam", plan["approved_action"])
        self.assertTrue(plan["requires_tracking_rerun"])
        self.assertEqual(2, plan["approved_camera_action_count"])
        self.assertEqual("imp_camera_track", plan["improvement_id"])
        self.assertEqual({"start_frame": 32, "end_frame": 58}, plan["tracking_rerun_scope"])
        self.assertEqual({}, plan["recommended_config_patch"])

    def test_approval_without_config_patch_clears_stale_derived_patch_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_json(
                output_dir / "ai_improvement_report.json",
                {
                    "schema_version": "1.0",
                    "generated_at": "2026-06-22T00:00:00+00:00",
                    "model": "gpt-improve",
                    "summary": {"status": "needs_rerun"},
                    "improvements": [
                        {
                            "id": "imp_noise",
                            "priority": "P1",
                            "area": "tracking",
                            "failure_tags": ["foot_confusion"],
                            "root_cause_module": "selection",
                            "start_frame": 40,
                            "end_frame": 52,
                            "diagnosis": "Noise filter can tighten.",
                            "recommended_action": "noise_filter_adjustment",
                            "false_positive_class": "foot_confusion",
                            "config_patch": {"selection": {"min_accept_score": 0.55}},
                            "confidence": 0.7,
                        }
                    ],
                },
            )
            approve_ai_improvement_actions(output_dir, run_id="run_123", improvement_ids=["imp_noise"])
            self.assertTrue((output_dir / "ai_improvement_approved_config_patch.json").exists())
            _write_json(
                output_dir / "ai_improvement_report.json",
                {
                    "schema_version": "1.0",
                    "generated_at": "2026-06-22T00:00:01+00:00",
                    "model": "gpt-improve",
                    "summary": {"status": "needs_rerun"},
                    "improvements": [
                        {
                            "id": "imp_manual",
                            "priority": "P2",
                            "area": "tracking",
                            "failure_tags": ["unknown"],
                            "root_cause_module": "unknown",
                            "diagnosis": "Manual inspection only.",
                            "recommended_action": "manual_review",
                            "confidence": 0.5,
                        }
                    ],
                },
            )

            approve_ai_improvement_actions(output_dir, run_id="run_123", improvement_ids=["imp_manual"])

        self.assertFalse((output_dir / "ai_improvement_approved_config_patch.json").exists())

    def test_approval_rejects_noise_action_without_bounded_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            stale_patch_path = output_dir / "ai_improvement_approved_config_patch.json"
            _write_json(
                stale_patch_path,
                {"schema_version": "1.0", "merged_config_patch": {"selection": {"min_accept_score": 0.7}}},
            )
            _write_json(
                output_dir / "ai_improvement_report.json",
                {
                    "schema_version": "1.0",
                    "generated_at": "2026-06-22T00:00:00+00:00",
                    "model": "gpt-improve",
                    "summary": {"status": "needs_rerun"},
                    "improvements": [
                        {
                            "id": "imp_noise",
                            "priority": "P1",
                            "area": "tracking",
                            "failure_tags": ["foot_confusion"],
                            "root_cause_module": "selection",
                            "diagnosis": "Malformed historical report.",
                            "recommended_action": "noise_filter_adjustment",
                            "false_positive_class": "foot_confusion",
                            "config_patch": {"selection": {"min_accept_score": 0.55}},
                            "confidence": 0.7,
                        }
                    ],
                },
            )

            with self.assertRaisesRegex(ValueError, "start_frame and end_frame"):
                approve_ai_improvement_actions(output_dir, run_id="run_123", improvement_ids=["imp_noise"])

            self.assertTrue(stale_patch_path.exists())

    def test_highlight_action_report_requires_approvable_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            client = _FakeImprovementClient(
                {
                    "summary": {"status": "needs_rerun"},
                    "improvements": [
                        {
                            "id": "imp_highlight",
                            "priority": "P1",
                            "area": "highlights",
                            "failure_tags": ["post_roll_too_short"],
                            "root_cause_module": "event_scoring",
                            "diagnosis": "Missing suggested window.",
                            "recommended_action": "render_suggested_highlight",
                            "candidate_id": "candidate_001",
                            "confidence": 0.7,
                        }
                    ],
                }
            )

            report = build_ai_improvement_report(output_dir, client=client)

        self.assertEqual("error", report["summary"]["status"])
        self.assertIn("suggested_window", report["error"])

    def test_ai_trim_tail_cannot_remove_result_tail(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            _write_json(
                output_dir / "event_candidates.json",
                {
                    "summary": {"candidate_count": 1},
                    "candidates": [
                        {
                            "id": "cleaned:shot_candidate:10-20",
                            "type": "shot_candidate",
                            "start_frame": 10,
                            "end_frame": 20,
                            "core_window": {"start_frame": 10, "end_frame": 20},
                            "render_window": {"start_frame": 0, "end_frame": 110},
                            "buffer_policy": {
                                "fps": 20.0,
                                "pre_buffer_seconds": 0.75,
                                "post_buffer_seconds": 4.5,
                                "pre_buffer_frames": 15,
                                "post_buffer_frames": 90,
                                "min_post_event_frames": 90,
                            },
                        }
                    ],
                },
            )
            client = _FakeImprovementClient(
                {
                    "summary": {"status": "needs_rerun"},
                    "improvements": [
                        {
                            "id": "imp_trim_tail",
                            "priority": "P1",
                            "area": "highlights",
                            "failure_tags": ["post_roll_too_short"],
                            "root_cause_module": "event_scoring",
                            "diagnosis": "Trim the tail too aggressively.",
                            "recommended_action": "render_suggested_highlight",
                            "candidate_id": "cleaned:shot_candidate:10-20",
                            "suggested_window": {"start_frame": 10, "end_frame": 50},
                            "clip_action": "trim_tail",
                            "confidence": 0.8,
                        }
                    ],
                    "highlight_adjustments": [],
                }
            )

            report = build_ai_improvement_report(output_dir, client=client)

        self.assertEqual("error", report["summary"]["status"])
        self.assertIn("minimum post-event tail", report["error"])

    def test_approving_highlight_action_fails_fast_when_event_candidates_missing_or_corrupt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_json(
                output_dir / "ai_improvement_report.json",
                {
                    "schema_version": "1.0",
                    "generated_at": "2026-06-22T00:00:00+00:00",
                    "model": "gpt-improve",
                    "summary": {"status": "needs_rerun"},
                    "improvements": [
                        {
                            "id": "imp_highlight",
                            "priority": "P1",
                            "area": "highlights",
                            "failure_tags": ["post_roll_too_short"],
                            "root_cause_module": "event_scoring",
                            "diagnosis": "Extend result tail.",
                            "recommended_action": "render_suggested_highlight",
                            "candidate_id": "cleaned:shot_candidate:10-20",
                            "suggested_window": {"start_frame": 0, "end_frame": 40},
                            "clip_action": "extend_tail",
                            "confidence": 0.8,
                        }
                    ],
                },
            )

            with self.assertRaisesRegex(ValueError, "event_candidates.json"):
                approve_ai_improvement_actions(output_dir, run_id="run_123", improvement_ids=["imp_highlight"])

            (output_dir / "event_candidates.json").write_text("{", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "event_candidates.json corrupt"):
                approve_ai_improvement_actions(output_dir, run_id="run_123", improvement_ids=["imp_highlight"])

    def test_non_highlight_approval_tolerates_missing_event_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_json(
                output_dir / "ai_improvement_report.json",
                {
                    "schema_version": "1.0",
                    "generated_at": "2026-06-22T00:00:00+00:00",
                    "model": "gpt-improve",
                    "summary": {"status": "needs_rerun"},
                    "improvements": [
                        {
                            "id": "imp_manual",
                            "priority": "P2",
                            "area": "tracking",
                            "failure_tags": ["unknown"],
                            "root_cause_module": "unknown",
                            "diagnosis": "Manual inspection only.",
                            "recommended_action": "manual_review",
                            "confidence": 0.5,
                        }
                    ],
                },
            )

            artifact = approve_ai_improvement_actions(output_dir, run_id="run_123", improvement_ids=["imp_manual"])

        self.assertEqual("manual_review", artifact["approved_actions"][0]["approved_action"])

    def test_disabled_provider_returns_unavailable_without_crashing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            client = _FakeImprovementClient(RuntimeError("must not call disabled provider"), enabled=False)

            report = write_ai_improvement_report(output_dir, client=client)
            written_exists = (output_dir / "ai_improvement_report.json").exists()

        self.assertEqual([], client.calls)
        self.assertEqual("unavailable", report["summary"]["status"])
        self.assertTrue(written_exists)

    def test_default_provider_without_api_key_returns_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            disabled_client = OpenAIResponsesClient(
                OpenAIProviderSettings(api_key="", base_url="https://example.invalid", chat_model="gpt-test")
            )

            with patch("football_tracking.ai_improvement._build_default_client", return_value=disabled_client):
                report = write_ai_improvement_report(output_dir)

        self.assertEqual("unavailable", report["summary"]["status"])
        self.assertEqual("gpt-test", report["model"])

    def test_cli_passes_max_items_writes_report_and_prints_summary(self) -> None:
        from scripts.run_ai_improvement import main

        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            expected_report = {
                "summary": {
                    "status": "unavailable",
                    "primary_issue": None,
                    "improvement_count": 0,
                    "targeted_rerun_count": 0,
                    "config_patch_count": 0,
                    "highlight_adjustment_count": 0,
                },
                "improvements": [],
                "highlight_adjustments": [],
            }
            calls: list[dict[str, object]] = []

            def fake_write(path: Path, **kwargs: object) -> dict[str, object]:
                calls.append({"path": path, **kwargs})
                (path / "ai_improvement_report.json").write_text(json.dumps(expected_report), encoding="utf-8")
                return expected_report

            with patch("football_tracking.ai_improvement.write_ai_improvement_report", side_effect=fake_write):
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    exit_code = main([str(output_dir), "--dry-run", "--max-items", "7", "--model", "gpt-cli"])

            printed = json.loads(stdout.getvalue())

        self.assertEqual(0, exit_code)
        self.assertEqual(7, calls[0]["max_items"])
        self.assertEqual(True, calls[0]["dry_run"])
        self.assertEqual("gpt-cli", calls[0]["model"])
        self.assertEqual(expected_report["summary"], printed["ai_improvement"])

    def test_cli_rejects_excessive_max_items(self) -> None:
        from scripts.run_ai_improvement import main

        with tempfile.TemporaryDirectory() as temp_name:
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
                main([temp_name, "--max-items", "101"])

        self.assertEqual(2, raised.exception.code)
        self.assertIn("at most 100", stderr.getvalue())

    def test_metrics_report_includes_compact_ai_improvement_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_json(
                output_dir / "ai_improvement_report.json",
                {
                    "schema_version": "1.0",
                    "generated_at": "2026-06-22T00:00:00+00:00",
                    "summary": {
                        "status": "needs_rerun",
                        "primary_issue": "tracking",
                        "improvement_count": 2,
                        "targeted_rerun_count": 1,
                        "config_patch_count": 1,
                        "highlight_adjustment_count": 1,
                        "camera_improvement_count": 2,
                        "camera_severity_counts": {"fail": 1, "warn": 1},
                        "camera_action_counts": {"adjust_follow_cam": 1, "tracking_rerun_before_follow_cam": 1},
                    },
                },
            )

            metrics = build_metrics_report(output_dir)
            stats = stats_from_metrics_report(metrics)

        self.assertEqual("needs_rerun", metrics["ai_improvement"]["status"])
        self.assertEqual(2, metrics["ai_improvement"]["camera_improvement_count"])
        self.assertEqual({"fail": 1, "warn": 1}, metrics["ai_improvement"]["camera_severity_counts"])
        self.assertEqual(1, metrics["ai_improvement"]["camera_action_counts"]["adjust_follow_cam"])
        self.assertEqual(metrics["ai_improvement"], stats["ai_improvement"])


def _write_minimal_artifacts(output_dir: Path) -> None:
    _write_json(
        output_dir / "ball_audit.json",
        {
            "summary": {"review_event_count": 1, "lost_gap_count": 1},
            "review_events": [
                {
                    "type": "lost_gap",
                    "severity": "fail",
                    "start_frame": 10,
                    "end_frame": 20,
                    "frame_count": 11,
                    "reason": "Ball track is lost between tracklets.",
                }
            ],
        },
    )
    _write_json(
        output_dir / "review_packets.json",
        {
            "summary": {"packet_count": 1},
            "packets": [
                {
                    "packet_id": "packet_001",
                    "source": {"kind": "trigger", "type": "lost_gap", "start_frame": 10, "end_frame": 20},
                    "window": {"start_frame": 0, "end_frame": 35},
                    "decision": {"label": "ball_not_visible"},
                }
            ],
        },
    )
    _write_json(
        output_dir / "event_candidates.json",
        {
            "summary": {"candidate_count": 1},
            "candidates": [
                {
                    "id": "cleaned:shot_candidate:10-20",
                    "type": "shot_candidate",
                    "start_frame": 10,
                    "end_frame": 20,
                    "core_window": {"start_frame": 10, "end_frame": 20},
                    "render_window": {"start_frame": 10, "end_frame": 20},
                    "buffer_policy": {
                        "fps": 20.0,
                        "fps_source": "test",
                        "pre_buffer_seconds": 0.75,
                        "post_buffer_seconds": 1.0,
                        "pre_buffer_frames": 15,
                        "post_buffer_frames": 20,
                        "min_post_event_frames": 20,
                        "min_tail_frames": 20,
                    },
                }
            ],
        },
    )


def _write_camera_motion_event(output_dir: Path, *, frame: int, severity: str, max_step_px: float) -> None:
    _write_json(
        output_dir / "camera_motion_audit.json",
        {
            "schema_version": "1.0",
            "summary": {"status": severity, "review_event_count": 1},
            "review_events": [
                {
                    "type": "camera_motion_spike",
                    "severity": severity,
                    "start_frame": frame,
                    "end_frame": frame,
                    "frame_count": 1,
                    "reason": "Output-space camera pan step exceeds the review threshold.",
                    "evidence": {"max_step_px": max_step_px},
                }
            ],
        },
    )


def _write_ball_track(output_dir: Path, rows: list[tuple[int, str, object, object]]) -> None:
    lines = ["Frame,X,Y,Status"]
    for frame, status, x, y in rows:
        lines.append(f"{frame},{x},{y},{status}")
    (output_dir / "ball_track.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
