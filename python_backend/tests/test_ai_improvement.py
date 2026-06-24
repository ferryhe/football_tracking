from __future__ import annotations

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from football_tracking.ai_contracts import AI_RECOMMENDED_ACTIONS
from football_tracking.ai_improvement import (
    _instructions,
    approve_ai_improvement_actions,
    build_ai_improvement_context,
    build_ai_improvement_report,
    compact_ai_improvement_summary,
    write_ai_improvement_report,
)
from football_tracking.api.ai_provider import OpenAIProviderSettings, OpenAIResponsesClient
from football_tracking.high_recall_windows import build_high_recall_windows
from football_tracking.metrics import build_metrics_report, stats_from_metrics_report
from football_tracking.review_packets import build_review_packet_report


class _FakeImprovementClient:
    def __init__(self, response: object, *, enabled: bool = True) -> None:
        self.response = response
        self.enabled = enabled
        self.calls: list[dict[str, object]] = []
        self.settings = SimpleNamespace(chat_model="gpt-chat-mini", improvement_model="gpt-improve-strong")

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
    def test_improvement_prompt_describes_pr2_contract_and_model_routing(self) -> None:
        instructions = _instructions(language="en")

        self.assertIn("candidate_intent", instructions)
        self.assertIn("review_only", instructions)
        self.assertIn("executable candidate", instructions)
        self.assertIn("bounded, traceable, and comparable", instructions)
        self.assertIn("cover the entire lost gap", instructions)
        self.assertIn("explain uncovered subwindows", instructions)
        self.assertIn("explicit uncovered subranges", instructions)
        self.assertIn("source_packet_id or visual_review_id", instructions)
        self.assertIn("not_visible", instructions)
        self.assertIn("hidden, off-frame, or impossible to identify", instructions)
        self.assertIn("unknown_false_positive", instructions)
        self.assertIn("evidence ids", instructions)
        self.assertIn("tracking_rerun_before_follow_cam", instructions)
        self.assertIn("adjust_follow_cam", instructions)
        self.assertIn("candidate.core_window", instructions)
        self.assertIn("candidate.buffer_policy.min_tail_frames", instructions)
        self.assertIn("do not trim result tail", instructions)
        self.assertIn("stronger model", instructions)
        self.assertIn("dry-run smoke", instructions)

    def test_missing_ball_prompt_requires_uncovered_subranges(self) -> None:
        instructions = _instructions(language="en")

        self.assertIn("full-window coverage", instructions)
        self.assertIn("explicit uncovered subranges", instructions)

    def test_noise_prompt_requires_bounded_false_positive_class(self) -> None:
        instructions = _instructions(language="en")

        self.assertIn("false_positive_class", instructions)
        self.assertIn("bounded start_frame/end_frame", instructions)
        self.assertIn("evidence ids", instructions)

    def test_context_and_report_record_candidate_intent_and_provider_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)

            context = build_ai_improvement_context(output_dir, candidate_intent="prepare_approved_candidates")
            report = build_ai_improvement_report(output_dir, dry_run=True)

        self.assertEqual("prepare_approved_candidates", context["candidate_intent"])
        self.assertEqual("review_only", report["candidate_intent"])
        self.assertTrue(report["provider_dry_run"])
        self.assertEqual("dry-run", report["provider_mode"])
        self.assertFalse(report["can_lead_to_executable_candidates"])

    def test_context_records_required_long_gap_coverage_from_real_review_packets(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_long_lost_gap_review_inputs(output_dir, start=100, end=800, total_frames=900)
            review_packets = build_review_packet_report(output_dir, max_packets=1, include_media=False)
            _write_json(output_dir / "review_packets.json", review_packets)

            context = build_ai_improvement_context(output_dir)

        coverage = context["validation_facts"]["required_window_coverage"][0]
        self.assertEqual({"start_frame": 100, "end_frame": 800, "frame_count": 701}, coverage["required_window"])
        self.assertEqual("partial", coverage["coverage_status"])
        self.assertEqual(100, coverage["covered_start_frame"])
        self.assertEqual(195, coverage["covered_end_frame"])
        self.assertEqual([{"label": "start", "start_frame": 100, "end_frame": 195}], coverage["covered_ranges"])
        self.assertEqual(
            [{"label": "start", "start_frame": 100, "end_frame": 195}],
            coverage["covered_required_window_ranges"],
        )
        self.assertEqual(
            [
                {"label": "middle", "start_frame": 418, "end_frame": 481},
                {"label": "end", "start_frame": 705, "end_frame": 800},
                {"label": "tail", "start_frame": 801, "end_frame": 830},
            ],
            coverage["uncovered_ranges"],
        )
        self.assertEqual(["start"], coverage["covered_labels"])

    def test_context_records_required_long_gap_coverage_when_all_labels_are_present(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_long_lost_gap_review_inputs(output_dir, start=100, end=800, total_frames=900)
            review_packets = build_review_packet_report(output_dir, max_packets=4, include_media=False)
            _write_json(output_dir / "review_packets.json", review_packets)

            context = build_ai_improvement_context(output_dir)

        coverage = context["validation_facts"]["required_window_coverage"][0]
        self.assertEqual("covered", coverage["coverage_status"])
        self.assertEqual(100, coverage["covered_start_frame"])
        self.assertEqual(800, coverage["covered_end_frame"])
        self.assertEqual([], coverage["uncovered_ranges"])
        self.assertEqual(["start", "middle", "end", "tail"], coverage["covered_labels"])
        self.assertEqual(
            [
                {"label": "start", "start_frame": 100, "end_frame": 195},
                {"label": "middle", "start_frame": 418, "end_frame": 481},
                {"label": "end", "start_frame": 705, "end_frame": 800},
            ],
            coverage["covered_required_window_ranges"],
        )

    def test_generic_review_note_is_non_executable_not_silent_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            client = _FakeImprovementClient(
                {
                    "summary": {"status": "needs_rerun", "primary_issue": "tracking"},
                    "improvements": [
                        {
                            "id": "imp_note",
                            "priority": "P2",
                            "area": "tracking",
                            "failure_tags": ["unknown"],
                            "root_cause_module": "unknown",
                            "diagnosis": "The packet should be reviewed by a person before any candidate run.",
                            "recommended_action": "manual_review",
                            "evidence": ["generic review note"],
                            "confidence": 0.42,
                        }
                    ],
                }
            )

            report = build_ai_improvement_report(output_dir, client=client)

        self.assertEqual("needs_rerun", report["summary"]["status"])
        self.assertEqual("review_only", report["improvements"][0]["candidate_intent"])
        self.assertFalse(report["improvements"][0]["executable"])
        self.assertEqual(0, report["summary"]["executable_candidate_count"])

    def test_missing_recommended_action_is_downgraded_to_manual_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            client = _FakeImprovementClient(
                {
                    "summary": {"status": "needs_rerun", "primary_issue": "tracking"},
                    "improvements": [
                        {
                            "id": "imp_missing_action",
                            "priority": "P2",
                            "area": "tracking",
                            "failure_tags": ["ball_lost"],
                            "root_cause_module": "reacquisition",
                            "diagnosis": "The item is otherwise shaped but omitted recommended_action.",
                            "likely_ball_region": {"description": "not visible", "confidence": 0.0},
                            "evidence": [{"source_packet_id": "packet_001"}],
                            "confidence": 0.48,
                        }
                    ],
                }
            )

            report = build_ai_improvement_report(output_dir, client=client)

        improvement = report["improvements"][0]
        self.assertEqual("needs_rerun", report["summary"]["status"])
        self.assertEqual("manual_review", improvement["recommended_action"])
        self.assertFalse(improvement["executable"])
        self.assertTrue(any("missing recommended_action" in warning for warning in report["warnings"]))

    def test_blank_recommended_action_values_are_downgraded_to_manual_review(self) -> None:
        for blank_value in (None, "", "   \t"):
            with self.subTest(blank_value=blank_value):
                with tempfile.TemporaryDirectory() as temp_name:
                    output_dir = Path(temp_name)
                    _write_minimal_artifacts(output_dir)
                    client = _FakeImprovementClient(
                        {
                            "summary": {"status": "needs_rerun", "primary_issue": "tracking"},
                            "improvements": [
                                {
                                    "id": "imp_blank_action",
                                    "priority": "P2",
                                    "area": "tracking",
                                    "failure_tags": ["unknown"],
                                    "root_cause_module": "unknown",
                                    "diagnosis": "The item is otherwise shaped but supplied a blank action.",
                                    "recommended_action": blank_value,
                                    "evidence": ["blank action fixture"],
                                    "confidence": 0.48,
                                }
                            ],
                        }
                    )

                    report = build_ai_improvement_report(output_dir, client=client)

                improvement = report["improvements"][0]
                self.assertEqual("needs_rerun", report["summary"]["status"])
                self.assertEqual("manual_review", improvement["recommended_action"])
                self.assertFalse(improvement["executable"])
                self.assertTrue(any("blank recommended_action" in warning for warning in report["warnings"]))

    def test_non_string_recommended_action_values_are_downgraded_to_manual_review(self) -> None:
        invalid_actions: tuple[tuple[object, str], ...] = (
            ({"action": "targeted_rerun"}, "dict"),
            (["targeted_rerun"], "list"),
            (7, "int"),
            (3.5, "float"),
            (True, "bool"),
        )
        for action_value, expected_type in invalid_actions:
            with self.subTest(action_value=action_value):
                with tempfile.TemporaryDirectory() as temp_name:
                    output_dir = Path(temp_name)
                    _write_minimal_artifacts(output_dir)
                    client = _FakeImprovementClient(
                        {
                            "summary": {"status": "needs_rerun", "primary_issue": "tracking"},
                            "improvements": [
                                {
                                    "id": "imp_nonstring_action",
                                    "priority": "P2",
                                    "area": "tracking",
                                    "failure_tags": ["unknown"],
                                    "root_cause_module": "unknown",
                                    "diagnosis": "The item supplied a non-string action value.",
                                    "recommended_action": action_value,
                                    "evidence": ["non-string action fixture"],
                                    "confidence": 0.48,
                                }
                            ],
                        }
                    )

                    report = build_ai_improvement_report(output_dir, client=client)

                improvement = report["improvements"][0]
                warning_text = "\n".join(report["warnings"])
                self.assertEqual("needs_rerun", report["summary"]["status"])
                self.assertEqual("manual_review", improvement["recommended_action"])
                self.assertFalse(improvement["executable"])
                self.assertEqual(0, report["summary"]["executable_candidate_count"])
                self.assertNotIn("original_recommended_action", improvement)
                self.assertEqual(expected_type, improvement["original_recommended_action_type"])
                self.assertIn("non-string recommended_action", warning_text)
                self.assertIn(expected_type, warning_text)

    def test_non_string_missing_ball_action_with_traceable_packet_stays_manual_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            client = _FakeImprovementClient(
                {
                    "summary": {"status": "needs_rerun", "primary_issue": "tracking"},
                    "improvements": [
                        {
                            "id": "imp_nonstring_locator",
                            "priority": "P0",
                            "area": "tracking",
                            "failure_tags": ["ball_lost"],
                            "root_cause_module": "reacquisition",
                            "diagnosis": "The packet has missing-ball evidence but a non-string action.",
                            "recommended_action": {"action": "localize_ball_roi"},
                            "source_packet_id": "packet_001",
                            "evidence": [{"source_packet_id": "packet_001"}],
                            "confidence": 0.72,
                        }
                    ],
                }
            )

            report = build_ai_improvement_report(output_dir, client=client)

        improvement = report["improvements"][0]
        self.assertEqual("needs_rerun", report["summary"]["status"])
        self.assertEqual("manual_review", improvement["recommended_action"])
        self.assertNotEqual("request_targeted_localization", improvement["recommended_action"])
        self.assertNotIn("requested_action", improvement)
        self.assertNotIn("local_search_roi", improvement)
        self.assertNotIn("likely_ball_region", improvement)
        self.assertNotIn("original_recommended_action", improvement)
        self.assertEqual("dict", improvement["original_recommended_action_type"])
        self.assertFalse(improvement["executable"])
        self.assertEqual(0, report["summary"]["executable_candidate_count"])
        self.assertTrue(any("non-string recommended_action" in warning for warning in report["warnings"]))
        self.assertFalse(any("request_targeted_localization" in warning for warning in report["warnings"]))

    def test_unsupported_string_recommended_action_preserves_original_action(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            client = _FakeImprovementClient(
                {
                    "summary": {"status": "needs_rerun", "primary_issue": "tracking"},
                    "improvements": [
                        {
                            "id": "imp_unsupported_string_action",
                            "priority": "P2",
                            "area": "tracking",
                            "failure_tags": ["unknown"],
                            "root_cause_module": "unknown",
                            "diagnosis": "The model used a non-contract action spelling.",
                            "recommended_action": "targeted rerun",
                            "evidence": ["unsupported string action fixture"],
                            "confidence": 0.48,
                        }
                    ],
                }
            )

            report = build_ai_improvement_report(output_dir, client=client)

        improvement = report["improvements"][0]
        self.assertEqual("needs_rerun", report["summary"]["status"])
        self.assertEqual("manual_review", improvement["recommended_action"])
        self.assertEqual("targeted rerun", improvement["original_recommended_action"])
        self.assertNotIn("original_recommended_action_type", improvement)
        self.assertFalse(improvement["executable"])
        self.assertTrue(any("unsupported recommended_action" in warning for warning in report["warnings"]))

    def test_blank_missing_ball_action_with_traceable_packet_stays_manual_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            client = _FakeImprovementClient(
                {
                    "summary": {"status": "needs_rerun", "primary_issue": "tracking"},
                    "improvements": [
                        {
                            "id": "imp_blank_locator",
                            "priority": "P0",
                            "area": "tracking",
                            "failure_tags": ["ball_lost"],
                            "root_cause_module": "reacquisition",
                            "diagnosis": "The packet has missing-ball evidence but no requested action.",
                            "recommended_action": "  ",
                            "source_packet_id": "packet_001",
                            "evidence": [{"source_packet_id": "packet_001"}],
                            "confidence": 0.72,
                        }
                    ],
                }
            )

            report = build_ai_improvement_report(output_dir, client=client)

        improvement = report["improvements"][0]
        self.assertEqual("needs_rerun", report["summary"]["status"])
        self.assertEqual("manual_review", improvement["recommended_action"])
        self.assertNotEqual("request_targeted_localization", improvement["recommended_action"])
        self.assertNotIn("requested_action", improvement)
        self.assertNotIn("local_search_roi", improvement)
        self.assertNotIn("likely_ball_region", improvement)
        self.assertFalse(improvement["executable"])
        self.assertEqual(0, report["summary"]["executable_candidate_count"])
        self.assertTrue(any("blank recommended_action" in warning for warning in report["warnings"]))

    def test_missing_required_field_other_than_recommended_action_remains_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            client = _FakeImprovementClient(
                {
                    "summary": {"status": "needs_rerun", "primary_issue": "tracking"},
                    "improvements": [
                        {
                            "id": "imp_missing_priority",
                            "area": "tracking",
                            "failure_tags": ["ball_lost"],
                            "root_cause_module": "reacquisition",
                            "diagnosis": "Missing priority must still fail the contract.",
                            "likely_ball_region": {"description": "not visible", "confidence": 0.0},
                            "recommended_action": "manual_review",
                            "evidence": [{"source_packet_id": "packet_001"}],
                            "confidence": 0.48,
                        }
                    ],
                }
            )

            report = build_ai_improvement_report(output_dir, client=client)

        self.assertEqual("error", report["summary"]["status"])
        self.assertIn("missing required fields: priority", report["error"])

    def test_executable_action_missing_candidate_contract_fields_is_review_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            client = _FakeImprovementClient(
                {
                    "summary": {"status": "needs_rerun", "primary_issue": "tracking"},
                    "improvements": [
                        {
                            "id": "imp_sparse_rerun",
                            "priority": "P1",
                            "area": "tracking",
                            "failure_tags": ["ball_lost"],
                            "root_cause_module": "reacquisition",
                            "diagnosis": "The gap needs a bounded rerun but no candidate contract is supplied.",
                            "recommended_action": "targeted_rerun",
                            "rerun_scope": {"start_frame": 10, "end_frame": 20},
                            "source_packet_id": "packet_001",
                            "likely_ball_region": {"description": "not visible", "confidence": 0.0},
                            "evidence": [{"source_packet_id": "packet_001"}],
                            "confidence": 0.66,
                        }
                    ],
                }
            )

            report = build_ai_improvement_report(output_dir, client=client)

        improvement = report["improvements"][0]
        self.assertFalse(improvement["executable"])
        self.assertEqual("review_only", improvement["candidate_intent"])
        self.assertEqual(
            ["problem_type", "candidate_id", "expected_artifact", "comparison_criteria"],
            improvement["candidate_contract"]["missing_fields"],
        )
        self.assertEqual(0, report["summary"]["executable_candidate_count"])

    def test_incomplete_localize_ball_roi_with_traceable_packet_requests_targeted_localization(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            client = _FakeImprovementClient(
                {
                    "summary": {"status": "needs_rerun", "primary_issue": "tracking"},
                    "improvements": [
                        {
                            "id": "imp_needs_locator",
                            "priority": "P0",
                            "area": "tracking",
                            "failure_tags": ["ball_lost"],
                            "root_cause_module": "reacquisition",
                            "diagnosis": "The right corner packet likely needs a visual locator before ROI rerun.",
                            "recommended_action": "localize_ball_roi",
                            "source_packet_id": "packet_001",
                            "evidence": [{"source_packet_id": "packet_001"}],
                            "confidence": 0.76,
                        }
                    ],
                }
            )

            report = build_ai_improvement_report(output_dir, client=client)

        improvement = report["improvements"][0]
        self.assertEqual("needs_rerun", report["summary"]["status"])
        self.assertEqual("request_targeted_localization", improvement["recommended_action"])
        self.assertEqual("localize_ball_roi", improvement["requested_action"])
        self.assertFalse(improvement["executable"])
        self.assertEqual("review_only", improvement["candidate_intent"])
        self.assertEqual(["local_search_roi"], improvement["candidate_contract"]["missing_fields"])
        self.assertIn("missing likely_ball_region/local_search_roi", improvement["downgrade_reason"])
        self.assertEqual(0, report["summary"]["executable_candidate_count"])
        self.assertTrue(
            any("missing-ball suggestion missing likely_ball_region/local_search_roi" in warning for warning in report["warnings"])
        )

    def test_incomplete_localize_ball_roi_without_traceable_provenance_becomes_manual_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            client = _FakeImprovementClient(
                {
                    "summary": {"status": "needs_rerun", "primary_issue": "tracking"},
                    "improvements": [
                        {
                            "id": "imp_unlinked_locator",
                            "priority": "P0",
                            "area": "tracking",
                            "failure_tags": ["ball_lost"],
                            "root_cause_module": "reacquisition",
                            "diagnosis": "No packet or visual evidence is cited.",
                            "recommended_action": "localize_ball_roi",
                            "evidence": ["unlinked impression"],
                            "confidence": 0.76,
                        }
                    ],
                }
            )

            report = build_ai_improvement_report(output_dir, client=client)

        improvement = report["improvements"][0]
        self.assertEqual("needs_rerun", report["summary"]["status"])
        self.assertEqual("manual_review", improvement["recommended_action"])
        self.assertFalse(improvement["executable"])
        self.assertEqual("review_only", improvement["candidate_intent"])
        self.assertIn("missing likely_ball_region/local_search_roi", improvement["downgrade_reason"])
        self.assertNotIn("local_search_roi", improvement)
        self.assertNotIn("likely_ball_region", improvement)
        self.assertEqual(0, report["summary"]["executable_candidate_count"])
        self.assertTrue(
            any("missing-ball suggestion missing likely_ball_region/local_search_roi" in warning for warning in report["warnings"])
        )

    def test_incomplete_missing_ball_suggestion_does_not_block_valid_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            client = _FakeImprovementClient(
                {
                    "summary": {"status": "needs_rerun", "primary_issue": "tracking"},
                    "improvements": [
                        {
                            "id": "imp_unlinked_locator",
                            "priority": "P0",
                            "area": "tracking",
                            "failure_tags": ["ball_lost"],
                            "root_cause_module": "reacquisition",
                            "diagnosis": "No packet or visual evidence is cited.",
                            "recommended_action": "localize_ball_roi",
                            "evidence": ["unlinked impression"],
                            "confidence": 0.76,
                        },
                        _candidate_ready_targeted_rerun(),
                    ],
                }
            )

            report = build_ai_improvement_report(
                output_dir,
                client=client,
                candidate_intent="prepare_approved_candidates",
            )

        self.assertEqual("needs_rerun", report["summary"]["status"])
        self.assertEqual("manual_review", report["improvements"][0]["recommended_action"])
        self.assertFalse(report["improvements"][0]["executable"])
        self.assertTrue(report["improvements"][1]["executable"])
        self.assertEqual("rerun_ball_window", report["improvements"][1]["recommended_action"])
        self.assertEqual(1, report["summary"]["executable_candidate_count"])

    def test_unsupported_traceable_missing_ball_action_warning_matches_final_downgrade(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            client = _FakeImprovementClient(
                {
                    "summary": {"status": "needs_rerun", "primary_issue": "tracking"},
                    "improvements": [
                        {
                            "id": "imp_unknown_locator",
                            "priority": "P0",
                            "area": "tracking",
                            "failure_tags": ["ball_lost"],
                            "root_cause_module": "reacquisition",
                            "diagnosis": "The packet needs a locator, but the model used a non-contract action.",
                            "recommended_action": "find_ball_in_corner",
                            "source_packet_id": "packet_001",
                            "evidence": [{"source_packet_id": "packet_001"}],
                            "confidence": 0.72,
                        }
                    ],
                }
            )

            report = build_ai_improvement_report(output_dir, client=client)

        improvement = report["improvements"][0]
        self.assertEqual("request_targeted_localization", improvement["recommended_action"])
        self.assertEqual("find_ball_in_corner", improvement["original_recommended_action"])
        self.assertFalse(improvement["executable"])
        self.assertTrue(
            any(
                "unsupported recommended_action downgraded to request_targeted_localization"
                in warning
                for warning in report["warnings"]
            )
        )
        self.assertFalse(
            any(
                "unsupported recommended_action downgraded to manual_review" in warning
                for warning in report["warnings"]
            )
        )

    def test_direct_request_targeted_localization_with_traceable_packet_is_review_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            client = _FakeImprovementClient(
                {
                    "summary": {"status": "needs_rerun", "primary_issue": "tracking"},
                    "improvements": [
                        {
                            "id": "imp_direct_locator",
                            "priority": "P0",
                            "area": "tracking",
                            "failure_tags": ["ball_lost"],
                            "root_cause_module": "reacquisition",
                            "diagnosis": "Need a targeted visual localization pass.",
                            "recommended_action": "request_targeted_localization",
                            "source_packet_id": "packet_001",
                            "evidence": [{"source_packet_id": "packet_001"}],
                            "confidence": 0.8,
                        }
                    ],
                }
            )

            report = build_ai_improvement_report(output_dir, client=client)

        improvement = report["improvements"][0]
        self.assertEqual("request_targeted_localization", improvement["recommended_action"])
        self.assertEqual("localize_ball_roi", improvement["requested_action"])
        self.assertFalse(improvement["executable"])
        self.assertEqual(["local_search_roi"], improvement["candidate_contract"]["missing_fields"])

    def test_request_targeted_localization_without_traceable_provenance_becomes_error_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            client = _FakeImprovementClient(
                {
                    "summary": {"status": "needs_rerun", "primary_issue": "tracking"},
                    "improvements": [
                        {
                            "id": "imp_unlinked_direct_locator",
                            "priority": "P0",
                            "area": "tracking",
                            "failure_tags": ["ball_lost"],
                            "root_cause_module": "reacquisition",
                            "diagnosis": "Unlinked request should not self-certify.",
                            "recommended_action": "request_targeted_localization",
                            "likely_ball_region": {"description": "right corner", "confidence": 0.5},
                            "evidence": ["unlinked impression"],
                            "confidence": 0.8,
                        }
                    ],
                }
            )

            report = build_ai_improvement_report(output_dir, client=client)

        self.assertEqual("error", report["summary"]["status"])
        self.assertIn("request_targeted_localization requires", report["error"])

    def test_review_only_candidate_intent_overrides_executable_action(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            client = _FakeImprovementClient(
                {
                    "summary": {"status": "needs_rerun", "primary_issue": "tracking"},
                    "improvements": [_candidate_ready_targeted_rerun()],
                }
            )

            report = build_ai_improvement_report(output_dir, client=client, candidate_intent="review_only")

        self.assertEqual("review_only", report["candidate_intent"])
        self.assertFalse(report["improvements"][0]["executable"])
        self.assertEqual("review_only", report["improvements"][0]["candidate_intent"])

    def test_prepare_approved_candidate_intent_is_preserved_for_executable_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            client = _FakeImprovementClient(
                {
                    "summary": {"status": "needs_rerun", "primary_issue": "tracking"},
                    "improvements": [_candidate_ready_targeted_rerun()],
                }
            )

            report = build_ai_improvement_report(
                output_dir,
                client=client,
                candidate_intent="prepare_approved_candidates",
            )

        improvement = report["improvements"][0]
        self.assertTrue(improvement["executable"])
        self.assertEqual("prepare_approved_candidates", improvement["candidate_intent"])
        self.assertEqual([], improvement["candidate_contract"]["missing_fields"])
        self.assertEqual(1, report["summary"]["executable_candidate_count"])

    def test_clean_visual_localization_can_back_executable_localize_ball_roi(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            local_roi = {
                "coordinate_space": "image",
                "frame": 15,
                "x": 120,
                "y": 40,
                "width": 80,
                "height": 50,
                "confidence": 0.72,
            }
            _write_json(
                output_dir / "ai_visual_localization.json",
                {
                    "requests": [
                        _clean_visual_localization_request(
                            "visual_localization:packet_001",
                            source_packet_id="packet_001",
                            local_search_roi=local_roi,
                        )
                    ]
                },
            )
            client = _FakeImprovementClient(
                {
                    "summary": {"status": "needs_rerun", "primary_issue": "tracking"},
                    "improvements": [
                        {
                            "id": "imp_visual_localized",
                            "priority": "P0",
                            "area": "tracking",
                            "failure_tags": ["ball_lost"],
                            "root_cause_module": "reacquisition",
                            "diagnosis": "Clean visual localization supplied a bounded ROI.",
                            "recommended_action": "localize_ball_roi",
                            "problem_type": "missing_ball",
                            "candidate_id": "candidate_001",
                            "start_frame": 10,
                            "end_frame": 20,
                            "visual_localization_id": "visual_localization:packet_001",
                            "local_search_roi": local_roi,
                            "expected_artifact": {"name": "ball_track.csv"},
                            "comparison_criteria": {"report": "missing_ball_recovery_comparison.json"},
                            "confidence": 0.82,
                        }
                    ],
                }
            )

            report = build_ai_improvement_report(output_dir, client=client)

        improvement = report["improvements"][0]
        self.assertEqual("needs_rerun", report["summary"]["status"])
        self.assertTrue(improvement["executable"])
        self.assertEqual(1, report["summary"]["executable_candidate_count"])

    def test_dirty_visual_localization_media_cannot_back_executable_localize_ball_roi(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            local_roi = {
                "coordinate_space": "image",
                "frame": 15,
                "x": 120,
                "y": 40,
                "width": 80,
                "height": 50,
                "confidence": 0.72,
            }
            dirty_request = _clean_visual_localization_request(
                "visual_localization:packet_001",
                source_packet_id="packet_001",
                local_search_roi=local_roi,
            )
            dirty_request["media_warnings"] = ["crop_sheet_low_information"]
            dirty_request["media_integrity"] = {"status": "warn", "low_information_image_count": 1}
            _write_json(output_dir / "ai_visual_localization.json", {"requests": [dirty_request]})
            client = _FakeImprovementClient(
                {
                    "summary": {"status": "needs_rerun", "primary_issue": "tracking"},
                    "improvements": [
                        {
                            "id": "imp_dirty_visual_localized",
                            "priority": "P0",
                            "area": "tracking",
                            "failure_tags": ["ball_lost"],
                            "root_cause_module": "reacquisition",
                            "diagnosis": "Dirty visual localization must not certify the ROI.",
                            "recommended_action": "localize_ball_roi",
                            "problem_type": "missing_ball",
                            "candidate_id": "candidate_001",
                            "start_frame": 10,
                            "end_frame": 20,
                            "visual_localization_id": "visual_localization:packet_001",
                            "local_search_roi": local_roi,
                            "expected_artifact": {"name": "ball_track.csv"},
                            "comparison_criteria": {"report": "missing_ball_recovery_comparison.json"},
                            "confidence": 0.82,
                        }
                    ],
                }
            )

            report = build_ai_improvement_report(output_dir, client=client)

        self.assertEqual("error", report["summary"]["status"])
        self.assertIn("clean ai_visual_localization evidence", report["error"])

    def test_nested_corrupt_visual_localization_media_cannot_back_executable_localize_ball_roi(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            local_roi = {
                "coordinate_space": "image",
                "frame": 15,
                "x": 120,
                "y": 40,
                "width": 80,
                "height": 50,
                "confidence": 0.72,
            }
            dirty_request = _clean_visual_localization_request(
                "visual_localization:packet_001",
                source_packet_id="packet_001",
                local_search_roi=local_roi,
            )
            dirty_request["media_warnings"] = []
            dirty_request["media_integrity"] = {
                "contact_sheet": {
                    "status": "ok",
                    "likely_corrupt": True,
                    "low_information": False,
                    "gray": False,
                }
            }
            _write_json(output_dir / "ai_visual_localization.json", {"requests": [dirty_request]})
            client = _FakeImprovementClient(
                {
                    "summary": {"status": "needs_rerun", "primary_issue": "tracking"},
                    "improvements": [
                        {
                            "id": "imp_nested_dirty_visual_localized",
                            "priority": "P0",
                            "area": "tracking",
                            "failure_tags": ["ball_lost"],
                            "root_cause_module": "reacquisition",
                            "diagnosis": "Nested corrupt media integrity must block executable ROI.",
                            "recommended_action": "localize_ball_roi",
                            "problem_type": "missing_ball",
                            "candidate_id": "candidate_001",
                            "start_frame": 10,
                            "end_frame": 20,
                            "visual_localization_id": "visual_localization:packet_001",
                            "local_search_roi": local_roi,
                            "expected_artifact": {"name": "ball_track.csv"},
                            "comparison_criteria": {"report": "missing_ball_recovery_comparison.json"},
                            "confidence": 0.82,
                        }
                    ],
                }
            )

            report = build_ai_improvement_report(output_dir, client=client)

        self.assertEqual("error", report["summary"]["status"])
        self.assertIn("clean ai_visual_localization evidence", report["error"])

    def test_status_only_visual_localization_cannot_back_executable_localize_ball_roi(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            local_roi = {
                "coordinate_space": "image",
                "frame": 15,
                "x": 120,
                "y": 40,
                "width": 80,
                "height": 50,
                "confidence": 0.72,
            }
            _write_json(
                output_dir / "ai_visual_localization.json",
                {
                    "requests": [
                        {
                            "visual_localization_id": "visual_localization:packet_001",
                            "source_packet_id": "packet_001",
                            "status": "localized",
                            "media_warnings": [],
                            "media_integrity": {"status": "ok"},
                        }
                    ]
                },
            )
            client = _FakeImprovementClient(
                {
                    "summary": {"status": "needs_rerun", "primary_issue": "tracking"},
                    "improvements": [
                        {
                            "id": "imp_status_only_visual_localized",
                            "priority": "P0",
                            "area": "tracking",
                            "failure_tags": ["ball_lost"],
                            "root_cause_module": "reacquisition",
                            "diagnosis": "A localized status without ROI or ball-visible frame is too weak.",
                            "recommended_action": "localize_ball_roi",
                            "problem_type": "missing_ball",
                            "candidate_id": "candidate_001",
                            "start_frame": 10,
                            "end_frame": 20,
                            "visual_localization_id": "visual_localization:packet_001",
                            "local_search_roi": local_roi,
                            "expected_artifact": {"name": "ball_track.csv"},
                            "comparison_criteria": {"report": "missing_ball_recovery_comparison.json"},
                            "confidence": 0.82,
                        }
                    ],
                }
            )

            report = build_ai_improvement_report(output_dir, client=client)

        self.assertEqual("error", report["summary"]["status"])
        self.assertIn("usable visual evidence", report["error"])

    def test_legacy_targeted_rerun_input_is_canonicalized_for_public_executable_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            client = _FakeImprovementClient(
                {
                    "summary": {"status": "needs_rerun", "primary_issue": "tracking"},
                    "improvements": [_candidate_ready_targeted_rerun()],
                }
            )

            report = build_ai_improvement_report(
                output_dir,
                client=client,
                candidate_intent="prepare_approved_candidates",
            )
            _write_json(output_dir / "ai_improvement_report.json", report)
            artifact = approve_ai_improvement_actions(
                output_dir,
                run_id="run_123",
                improvement_ids=["imp_candidate_ready"],
            )

        improvement = report["improvements"][0]
        self.assertTrue(improvement["executable"])
        self.assertEqual("rerun_ball_window", improvement["recommended_action"])
        self.assertEqual("targeted_rerun", improvement["legacy_recommended_action"])
        self.assertEqual("rerun_ball_window", improvement["candidate_contract"]["approved_action"])
        self.assertEqual("rerun_ball_window", artifact["approved_actions"][0]["approved_action"])
        self.assertEqual("targeted_rerun", artifact["approved_actions"][0]["legacy_approved_action"])

    def test_action_problem_type_mismatch_is_review_only_and_approval_rejects_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            payload = _candidate_ready_targeted_rerun()
            payload["recommended_action"] = "rerun_ball_window"
            payload["problem_type"] = "noise"
            client = _FakeImprovementClient(
                {
                    "summary": {"status": "needs_rerun", "primary_issue": "tracking"},
                    "improvements": [payload],
                }
            )

            report = build_ai_improvement_report(
                output_dir,
                client=client,
                candidate_intent="prepare_approved_candidates",
            )
            _write_json(output_dir / "ai_improvement_report.json", report)

            with self.assertRaisesRegex(ValueError, "problem_type"):
                approve_ai_improvement_actions(output_dir, run_id="run_123", improvement_ids=["imp_candidate_ready"])

        improvement = report["improvements"][0]
        self.assertFalse(improvement["executable"])
        self.assertEqual("review_only", improvement["candidate_intent"])
        self.assertIn("problem_type_mismatch", improvement["candidate_contract"]["missing_fields"])

    def test_candidate_contract_accepts_traceable_evidence_id_without_evidence_array(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            payload = _candidate_ready_targeted_rerun()
            payload["recommended_action"] = "rerun_ball_window"
            payload.pop("evidence", None)
            client = _FakeImprovementClient(
                {
                    "summary": {"status": "needs_rerun", "primary_issue": "tracking"},
                    "improvements": [payload],
                }
            )

            report = build_ai_improvement_report(
                output_dir,
                client=client,
                candidate_intent="prepare_approved_candidates",
            )

        improvement = report["improvements"][0]
        self.assertTrue(improvement["executable"])
        self.assertEqual([], improvement["candidate_contract"]["missing_fields"])

    def test_follow_cam_candidate_contract_requires_camera_motion_event_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            payload = _valid_improvement_for_action("adjust_follow_cam")
            payload.pop("camera_motion_event_id", None)
            payload["problem_type"] = "follow_cam"
            payload["source_packet_id"] = "packet_001"
            payload["expected_artifact"] = {"name": "follow_cam.mp4"}
            payload["comparison_criteria"] = {"report": "follow_cam_candidate_comparison.json"}
            client = _FakeImprovementClient(
                {
                    "summary": {"status": "needs_rerun", "primary_issue": "camera_motion"},
                    "improvements": [payload],
                }
            )

            report = build_ai_improvement_report(
                output_dir,
                client=client,
                candidate_intent="prepare_approved_candidates",
            )

        improvement = report["improvements"][0]
        self.assertFalse(improvement["executable"])
        self.assertIn("camera_motion_event_id", improvement["candidate_contract"]["missing_fields"])

    def test_existing_ai_recommended_actions_remain_accepted(self) -> None:
        for action in sorted(AI_RECOMMENDED_ACTIONS):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as temp_name:
                output_dir = Path(temp_name)
                _write_minimal_artifacts_with_media(output_dir)
                if action in {"adjust_highlight_window", "render_suggested_highlight"}:
                    payload = _valid_improvement_for_action(action, candidate_id="cleaned:shot_candidate:10-20")
                else:
                    payload = _valid_improvement_for_action(action)
                client = _FakeImprovementClient({"summary": {"status": "needs_rerun"}, "improvements": [payload]})

                report = build_ai_improvement_report(output_dir, client=client)

            self.assertNotEqual("error", report["summary"]["status"], report.get("error"))
            expected_action = "rerun_ball_window" if action == "targeted_rerun" else action
            self.assertEqual(expected_action, report["improvements"][0]["recommended_action"])
            if action == "targeted_rerun":
                self.assertEqual("targeted_rerun", report["improvements"][0]["legacy_recommended_action"])

    def test_highlight_action_with_unsupported_clip_action_is_downgraded_to_manual_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            payload = _valid_improvement_for_action(
                "adjust_highlight_window",
                candidate_id="highlight_candidate_001",
            )
            payload.update(
                {
                    "problem_type": "highlight",
                    "clip_action": "keep_window_but_flag_tracking_issue",
                    "expected_artifact": {"name": "highlight.mp4", "role": "candidate"},
                    "comparison_criteria": {"report": "highlight_candidate_comparison.json"},
                }
            )
            client = _FakeImprovementClient({"summary": {"status": "needs_rerun"}, "improvements": [payload]})

            report = build_ai_improvement_report(
                output_dir,
                client=client,
                candidate_intent="prepare_approved_candidates",
            )

        self.assertNotEqual("error", report["summary"]["status"], report.get("error"))
        improvement = report["improvements"][0]
        self.assertEqual("manual_review", improvement["recommended_action"])
        self.assertEqual("adjust_highlight_window", improvement["legacy_recommended_action"])
        self.assertFalse(improvement["executable"])
        self.assertNotIn("clip_action", improvement)
        self.assertEqual("keep_window_but_flag_tracking_issue", improvement["original_clip_action"])
        self.assertTrue(any("unsupported clip_action" in warning for warning in report["warnings"]))
        self.assertEqual(0, report["summary"]["executable_candidate_count"])

    def test_highlight_action_with_non_string_clip_action_is_downgraded_to_manual_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            payload = _valid_improvement_for_action(
                "render_suggested_highlight",
                candidate_id="highlight_candidate_001",
            )
            payload.update(
                {
                    "problem_type": "highlight",
                    "clip_action": {"action": "extend_tail"},
                    "expected_artifact": {"name": "highlight.mp4", "role": "candidate"},
                    "comparison_criteria": {"report": "highlight_candidate_comparison.json"},
                }
            )
            client = _FakeImprovementClient({"summary": {"status": "needs_rerun"}, "improvements": [payload]})

            report = build_ai_improvement_report(
                output_dir,
                client=client,
                candidate_intent="prepare_approved_candidates",
            )

        self.assertNotEqual("error", report["summary"]["status"], report.get("error"))
        improvement = report["improvements"][0]
        self.assertEqual("manual_review", improvement["recommended_action"])
        self.assertEqual("render_suggested_highlight", improvement["legacy_recommended_action"])
        self.assertFalse(improvement["executable"])
        self.assertNotIn("clip_action", improvement)
        self.assertEqual("dict", improvement["original_clip_action_type"])
        self.assertTrue(any("non-string clip_action" in warning for warning in report["warnings"]))
        self.assertEqual(0, report["summary"]["executable_candidate_count"])

    def test_non_highlight_actions_ignore_invalid_clip_action_with_warning(self) -> None:
        cases = (
            ("manual_review", _valid_improvement_for_action("manual_review"), "manual_review"),
            ("targeted_rerun", _candidate_ready_targeted_rerun(), "rerun_ball_window"),
        )
        for label, payload, expected_action in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp_name:
                output_dir = Path(temp_name)
                _write_minimal_artifacts(output_dir)
                payload["clip_action"] = "keep_window_but_flag_tracking_issue"
                client = _FakeImprovementClient({"summary": {"status": "needs_rerun"}, "improvements": [payload]})

                report = build_ai_improvement_report(output_dir, client=client)

            self.assertNotEqual("error", report["summary"]["status"], report.get("error"))
            improvement = report["improvements"][0]
            self.assertEqual(expected_action, improvement["recommended_action"])
            self.assertNotIn("clip_action", improvement)
            self.assertNotIn(improvement["recommended_action"], {"adjust_highlight_window", "render_suggested_highlight"})
            self.assertTrue(any("unsupported clip_action" in warning for warning in report["warnings"]))

    def test_supported_highlight_clip_action_can_remain_executable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            payload = _valid_improvement_for_action(
                "render_suggested_highlight",
                candidate_id="highlight_candidate_001",
            )
            payload.update(
                {
                    "problem_type": "highlight",
                    "expected_artifact": {"name": "highlight.mp4", "role": "candidate"},
                    "comparison_criteria": {"report": "highlight_candidate_comparison.json"},
                }
            )
            client = _FakeImprovementClient({"summary": {"status": "needs_rerun"}, "improvements": [payload]})

            report = build_ai_improvement_report(
                output_dir,
                client=client,
                candidate_intent="prepare_approved_candidates",
            )

        improvement = report["improvements"][0]
        self.assertEqual("render_suggested_highlight", improvement["recommended_action"])
        self.assertNotIn("legacy_recommended_action", improvement)
        self.assertEqual("extend_tail", improvement["clip_action"])
        self.assertTrue(improvement["executable"])
        self.assertEqual(1, report["summary"]["executable_candidate_count"])

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

    def test_write_ai_improvement_report_preserves_tracks_and_does_not_create_apply_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            _write_ball_track(
                output_dir,
                [
                    (10, "Detected", 100, 100),
                    (11, "Lost", "", ""),
                    (12, "Detected", 120, 105),
                ],
            )
            cleaned_path = output_dir / "ball_track.cleaned.csv"
            cleaned_path.write_text(
                "Frame,X,Y,Status\n10,100,100,Detected\n11,110,102,Detected\n12,120,105,Detected\n",
                encoding="utf-8",
            )
            raw_path = output_dir / "ball_track.csv"
            before = {path.name: (_hash_file(path), path.stat().st_mtime_ns) for path in (raw_path, cleaned_path)}
            client = _FakeImprovementClient(
                {
                    "summary": {"status": "needs_rerun", "primary_issue": "tracking"},
                    "improvements": [
                        {
                            "id": "imp_track",
                            "priority": "P0",
                            "area": "tracking",
                            "failure_tags": ["ball_lost"],
                            "root_cause_module": "reacquisition",
                            "diagnosis": "Localized rerun may recover a lost ball.",
                            "recommended_action": "targeted_rerun",
                            "config_patch": {"tracking": {"max_lost_frames": 30}},
                            "rerun_scope": {"start_frame": 8, "end_frame": 18},
                            "source_packet_id": "packet_001",
                            "likely_ball_region": {
                                "description": "right channel near the player cluster",
                                "frame": 11,
                                "confidence": 0.7,
                            },
                            "evidence": [{"source_packet_id": "packet_001"}],
                            "confidence": 0.82,
                        }
                    ],
                    "highlight_adjustments": [
                        {
                            "candidate_id": "cleaned:shot_candidate:10-20",
                            "current_window": {"start_frame": 10, "end_frame": 20},
                            "suggested_window": {"start_frame": 5, "end_frame": 45},
                            "reason": "Preserve the result tail.",
                            "confidence": 0.74,
                        }
                    ],
                }
            )

            write_ai_improvement_report(output_dir, client=client, model="gpt-improve")

            after = {path.name: (_hash_file(path), path.stat().st_mtime_ns) for path in (raw_path, cleaned_path)}
            artifact_exists = {
                "report": (output_dir / "ai_improvement_report.json").exists(),
                "config_patch": (output_dir / "ai_improvement_approved_config_patch.json").exists(),
                "follow_cam_plan": (output_dir / "follow_cam_rerender_plan.json").exists(),
                "highlight_report": (output_dir / "highlight_report.json").exists(),
                "highlight_video": (output_dir / "highlight.mp4").exists(),
            }

        self.assertEqual(before, after)
        self.assertTrue(artifact_exists["report"])
        self.assertFalse(artifact_exists["config_patch"])
        self.assertFalse(artifact_exists["follow_cam_plan"])
        self.assertFalse(artifact_exists["highlight_report"])
        self.assertFalse(artifact_exists["highlight_video"])

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
                            "source_packet_id": "packet_001",
                            "likely_ball_region": {
                                "frame": 15,
                                "description": "right channel near the player cluster",
                                "confidence": 0.7,
                            },
                            "evidence": [
                                {"source_packet_id": "packet_001", "reason": "lost gap overlaps an active play packet"}
                            ],
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
        self.assertEqual("gpt-improve-strong", report["model_selection"]["model"])
        self.assertEqual("improvement_model", report["model_selection"]["source"])
        self.assertFalse(report["model_selection"]["provider_dry_run"])
        self.assertEqual("real", report["model_selection"]["provider_mode"])

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
        self.assertEqual("gpt-explicit", report["model_selection"]["model"])
        self.assertEqual("explicit", report["model_selection"]["source"])
        self.assertFalse(report["model_selection"]["provider_dry_run"])
        self.assertEqual("real", report["model_selection"]["provider_mode"])

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
                            "source_packet_id": "packet_001",
                            "likely_ball_region": {"description": "not visible", "confidence": 0.0},
                            "evidence": [
                                {"source_packet_id": "packet_001", "reason": "packet decision marks ball_not_visible"}
                            ],
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
            _write_minimal_artifacts_with_media(output_dir)
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
                            "candidate_id": "candidate_001",
                            "local_search_roi": {
                                "coordinate_space": "image",
                                "frame": 2088,
                                "x": 4300,
                                "y": 760,
                                "width": 900,
                                "height": 520,
                                "confidence": 0.74,
                            },
                            "evidence": [
                                {
                                    "source_packet_id": "packet_001",
                                    "reason": "visual packet shows a likely ball near the corner arc",
                                }
                            ],
                            "confidence": 0.78,
                        }
                    ],
                }
            )

            report = build_ai_improvement_report(output_dir, client=client)

        self.assertEqual("needs_rerun", report["summary"]["status"])
        self.assertEqual("localize_ball_roi", report["improvements"][0]["recommended_action"])
        self.assertEqual(4300.0, report["improvements"][0]["local_search_roi"]["x"])
        self.assertEqual(
            "ai_visual_review", report["improvements"][0]["evidence_payload"]["local_search_roi_provenance"]["source"]
        )

    def test_localize_ball_roi_with_packet_but_no_usable_visual_evidence_becomes_error_report(self) -> None:
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
                            "diagnosis": "The ROI cites a packet id, but that packet has no usable media.",
                            "recommended_action": "localize_ball_roi",
                            "problem_type": "missing_ball",
                            "candidate_id": "candidate_001",
                            "start_frame": 2049,
                            "end_frame": 2544,
                            "expected_artifact": {"name": "ball_track.cleaned.csv", "role": "candidate"},
                            "comparison_criteria": {"report": "missing_ball_recovery_comparison.json"},
                            "source_packet_id": "packet_001",
                            "local_search_roi": {
                                "coordinate_space": "image",
                                "frame": 2088,
                                "x": 4300,
                                "y": 760,
                                "width": 900,
                                "height": 520,
                                "confidence": 0.74,
                            },
                            "evidence": [{"source_packet_id": "packet_001"}],
                            "confidence": 0.78,
                        }
                    ],
                }
            )

            report = build_ai_improvement_report(output_dir, client=client)

        self.assertEqual("error", report["summary"]["status"])
        self.assertIn("usable visual evidence", report["error"])

    def test_review_only_localize_ball_roi_without_visual_evidence_is_non_executable(self) -> None:
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
                            "diagnosis": "Review-only ROI should not execute.",
                            "recommended_action": "localize_ball_roi",
                            **_candidate_contract_fields(),
                            "source_packet_id": "packet_001",
                            "local_search_roi": {
                                "coordinate_space": "image",
                                "frame": 2088,
                                "x": 4300,
                                "y": 760,
                                "width": 900,
                                "height": 520,
                                "confidence": 0.74,
                            },
                            "evidence": [{"source_packet_id": "packet_001"}],
                            "confidence": 0.78,
                        }
                    ],
                }
            )

            report = build_ai_improvement_report(output_dir, client=client, candidate_intent="review_only")

        self.assertEqual("needs_rerun", report["summary"]["status"])
        self.assertFalse(report["improvements"][0]["executable"])
        self.assertEqual("review_only", report["improvements"][0]["candidate_intent"])

    def test_invalid_visual_review_roi_does_not_satisfy_executable_localize(self) -> None:
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
                            "local_search_roi": {
                                "coordinate_space": "image",
                                "frame": 15,
                                "x": 620.0,
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
                    "summary": {"status": "needs_rerun"},
                    "improvements": [
                        {
                            "id": "imp_001",
                            "priority": "P0",
                            "area": "tracking",
                            "failure_tags": ["ball_lost"],
                            "root_cause_module": "reacquisition",
                            "diagnosis": "Visual review ROI is out of frame.",
                            "recommended_action": "localize_ball_roi",
                            **_candidate_contract_fields(),
                            "source_packet_id": "packet_001",
                            "local_search_roi": {
                                "coordinate_space": "image",
                                "frame": 15,
                                "x": 620,
                                "y": 40,
                                "width": 80,
                                "height": 50,
                                "confidence": 0.72,
                            },
                            "evidence": [{"source_packet_id": "packet_001"}],
                            "confidence": 0.78,
                        }
                    ],
                }
            )

            report = build_ai_improvement_report(output_dir, client=client)

        self.assertEqual("error", report["summary"]["status"])
        self.assertIn("usable visual evidence", report["error"])

    def test_visual_review_roi_without_frame_dimensions_is_not_usable_evidence(self) -> None:
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
                            "local_search_roi": {
                                "coordinate_space": "image",
                                "frame": 15,
                                "x": 120.0,
                                "y": 40.0,
                                "width": 80.0,
                                "height": 50.0,
                                "confidence": 0.72,
                            },
                            "provenance": {"source": "ai_visual_review", "model": "vision-test"},
                        }
                    ]
                },
            )
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
                            "diagnosis": "Visual review ROI has no frame dimensions.",
                            "recommended_action": "localize_ball_roi",
                            **_candidate_contract_fields(),
                            "source_packet_id": "packet_001",
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
                            "confidence": 0.78,
                        }
                    ],
                }
            )

            report = build_ai_improvement_report(output_dir, client=client)

        self.assertEqual("error", report["summary"]["status"])
        self.assertIn("usable visual evidence", report["error"])

    def test_external_visual_review_media_path_is_not_usable_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name, tempfile.TemporaryDirectory() as external_temp:
            output_dir = Path(temp_name)
            external_image = Path(external_temp) / "outside.png"
            external_image.write_bytes(_tiny_png_bytes())
            _write_minimal_artifacts(output_dir)
            _write_json(
                output_dir / "ai_visual_review.json",
                {
                    "reviews": [
                        {
                            "visual_review_id": "visual_review:packet_001",
                            "packet_id": "packet_001",
                            "source_packet_id": "packet_001",
                            "match_ball_visible": "yes",
                            "wide": str(external_image),
                            "provenance": {"source": "ai_visual_review", "model": "vision-test"},
                        }
                    ]
                },
            )
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
                            "diagnosis": "Visual review media points outside output_dir.",
                            "recommended_action": "localize_ball_roi",
                            **_candidate_contract_fields(),
                            "source_packet_id": "packet_001",
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
                            "confidence": 0.78,
                        }
                    ],
                }
            )

            report = build_ai_improvement_report(output_dir, client=client)

        self.assertEqual("error", report["summary"]["status"])
        self.assertIn("usable visual evidence", report["error"])

    def test_parent_relative_visual_review_media_path_is_not_usable_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name) / "run"
            output_dir.mkdir()
            external_image = output_dir.parent / "outside.png"
            external_image.write_bytes(_tiny_png_bytes())
            _write_minimal_artifacts(output_dir)
            _write_json(
                output_dir / "ai_visual_review.json",
                {
                    "reviews": [
                        {
                            "visual_review_id": "visual_review:packet_001",
                            "packet_id": "packet_001",
                            "source_packet_id": "packet_001",
                            "match_ball_visible": "yes",
                            "wide": "../outside.png",
                            "provenance": {"source": "ai_visual_review", "model": "vision-test"},
                        }
                    ]
                },
            )
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
                            "diagnosis": "Visual review media uses parent traversal.",
                            "recommended_action": "localize_ball_roi",
                            **_candidate_contract_fields(),
                            "source_packet_id": "packet_001",
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
                            "confidence": 0.78,
                        }
                    ],
                }
            )

            report = build_ai_improvement_report(output_dir, client=client)

        self.assertEqual("error", report["summary"]["status"])
        self.assertIn("usable visual evidence", report["error"])

    def test_non_webp_riff_media_is_not_usable_visual_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            media_dir = output_dir / "review_packets" / "packet_001"
            media_dir.mkdir(parents=True, exist_ok=True)
            riff_path = media_dir / "not_webp.riff"
            riff_path.write_bytes(b"RIFF\x04\x00\x00\x00WAVE")
            _write_minimal_artifacts(output_dir)
            _write_json(
                output_dir / "ai_visual_review.json",
                {
                    "reviews": [
                        {
                            "visual_review_id": "visual_review:packet_001",
                            "packet_id": "packet_001",
                            "source_packet_id": "packet_001",
                            "match_ball_visible": "yes",
                            "wide": "review_packets/packet_001/not_webp.riff",
                            "provenance": {"source": "ai_visual_review", "model": "vision-test"},
                        }
                    ]
                },
            )
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
                            "diagnosis": "Visual review media is RIFF but not WEBP.",
                            "recommended_action": "localize_ball_roi",
                            **_candidate_contract_fields(),
                            "source_packet_id": "packet_001",
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
                            "confidence": 0.78,
                        }
                    ],
                }
            )

            report = build_ai_improvement_report(output_dir, client=client)

        self.assertEqual("error", report["summary"]["status"])
        self.assertIn("usable visual evidence", report["error"])

    def test_localize_ball_roi_without_candidate_id_is_non_executable_contract_gap(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts_with_media(output_dir)
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
                            "problem_type": "missing_ball",
                            "source_packet_id": "packet_001",
                            "start_frame": 2049,
                            "end_frame": 2544,
                            "expected_artifact": {"name": "ball_track.cleaned.csv", "role": "candidate"},
                            "comparison_criteria": {"report": "missing_ball_recovery_comparison.json"},
                            "local_search_roi": {
                                "coordinate_space": "image",
                                "frame": 2088,
                                "x": 4300,
                                "y": 760,
                                "width": 900,
                                "height": 520,
                                "confidence": 0.74,
                            },
                            "evidence": [{"source_packet_id": "packet_001"}],
                            "confidence": 0.78,
                        }
                    ],
                }
            )

            report = build_ai_improvement_report(output_dir, client=client)

        self.assertEqual("needs_rerun", report["summary"]["status"])
        improvement = report["improvements"][0]
        self.assertFalse(improvement["executable"])
        self.assertEqual("review_only", improvement["candidate_intent"])
        self.assertEqual(["candidate_id"], improvement["candidate_contract"]["missing_fields"])

    def test_localize_ball_roi_without_packet_or_visual_provenance_becomes_error_report(self) -> None:
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
                            "diagnosis": "Ball is likely in the right corner, but no packet evidence is cited.",
                            "recommended_action": "localize_ball_roi",
                            "candidate_id": "candidate_001",
                            "local_search_roi": {
                                "coordinate_space": "image",
                                "frame": 2088,
                                "x": 4300,
                                "y": 760,
                                "width": 900,
                                "height": 520,
                                "confidence": 0.74,
                            },
                            "evidence": ["unlinked visual impression"],
                            "confidence": 0.78,
                        }
                    ],
                }
            )

            report = build_ai_improvement_report(output_dir, client=client)

        self.assertEqual("error", report["summary"]["status"])
        self.assertIn("source_packet_id or visual_review_id", report["error"])

    def test_missing_ball_likely_region_without_packet_or_visual_provenance_becomes_error_report(self) -> None:
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
                            "diagnosis": "Ball is likely in the lower-right corner, but no packet evidence is cited.",
                            "recommended_action": "targeted_rerun",
                            "rerun_scope": {"start_frame": 10, "end_frame": 20},
                            "likely_ball_region": {
                                "description": "lower-right corner near the touchline",
                                "frame": 15,
                                "confidence": 0.74,
                            },
                            "evidence": ["unlinked visual impression"],
                            "confidence": 0.78,
                        }
                    ],
                }
            )

            report = build_ai_improvement_report(output_dir, client=client)

        self.assertEqual("error", report["summary"]["status"])
        self.assertIn("likely_ball_region requires source_packet_id or visual_review_id", report["error"])

    def test_not_visible_without_packet_or_visual_evidence_becomes_error_report(self) -> None:
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
                            "diagnosis": "The model guesses that the ball is not visible.",
                            "recommended_action": "targeted_rerun",
                            "rerun_scope": {"start_frame": 10, "end_frame": 20},
                            "likely_ball_region": {"description": "not_visible", "confidence": 0.0},
                            "evidence": ["not visible guess without packet or visual citation"],
                            "confidence": 0.52,
                        }
                    ],
                }
            )

            report = build_ai_improvement_report(output_dir, client=client)

        self.assertEqual("error", report["summary"]["status"])
        self.assertIn("not_visible", report["error"])

    def test_not_visible_cannot_self_certify_with_unrelated_packet_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            _write_json(
                output_dir / "review_packets.json",
                {
                    "summary": {"packet_count": 1},
                    "packets": [
                        {
                            "packet_id": "packet_001",
                            "source": {"kind": "trigger", "type": "lost_gap", "start_frame": 10, "end_frame": 20},
                            "window": {"start_frame": 10, "end_frame": 20},
                            "decision": {"label": "manual_review"},
                            "visual_evidence": ["ball appears in the packet crop"],
                        }
                    ],
                },
            )
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
                            "diagnosis": "The model claims the ball is not visible, but the cited packet does not.",
                            "recommended_action": "targeted_rerun",
                            "rerun_scope": {"start_frame": 10, "end_frame": 20},
                            "source_packet_id": "packet_001",
                            "likely_ball_region": {"description": "not_visible", "confidence": 0.0},
                            "evidence": [{"source_packet_id": "packet_001", "reason": "model says not visible"}],
                            "confidence": 0.52,
                        }
                    ],
                }
            )

            report = build_ai_improvement_report(output_dir, client=client)

        self.assertEqual("error", report["summary"]["status"])
        self.assertIn("not_visible", report["error"])

    def test_localize_ball_roi_rejects_unknown_packet_provenance(self) -> None:
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
                            "diagnosis": "Model cites a packet id that is not in review_packets.json.",
                            "recommended_action": "localize_ball_roi",
                            "candidate_id": "candidate_001",
                            "source_packet_id": "packet_does_not_exist",
                            "local_search_roi": {
                                "coordinate_space": "image",
                                "frame": 2088,
                                "x": 4300,
                                "y": 760,
                                "width": 900,
                                "height": 520,
                                "confidence": 0.74,
                            },
                            "evidence": [{"source_packet_id": "packet_does_not_exist"}],
                            "confidence": 0.78,
                        }
                    ],
                }
            )

            report = build_ai_improvement_report(output_dir, client=client)

        self.assertEqual("error", report["summary"]["status"])
        self.assertIn("matches review_packets.json or ai_visual_review.json", report["error"])

    def test_roi_provenance_uses_full_on_disk_packet_index_not_truncated_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            packet_005_dir = output_dir / "review_packets" / "packet_005"
            packet_005_dir.mkdir(parents=True, exist_ok=True)
            packet_005_contact = packet_005_dir / "contact_sheet.png"
            packet_005_crop = packet_005_dir / "crop_sheet.png"
            packet_005_contact.write_bytes(_tiny_png_bytes())
            packet_005_crop.write_bytes(_tiny_png_bytes())
            _write_json(
                output_dir / "review_packets.json",
                {
                    "summary": {"packet_count": 5},
                    "packets": [
                        {
                            "packet_id": f"packet_{index:03d}",
                            "source": {
                                "kind": "trigger",
                                "type": "lost_gap",
                                "start_frame": index * 10,
                                "end_frame": index * 10 + 2,
                            },
                            "window": {"start_frame": index * 10, "end_frame": index * 10 + 2},
                            **(
                                {
                                    "media": {
                                        "contact_sheet": str(packet_005_contact),
                                        "crop_sheet": str(packet_005_crop),
                                    },
                                    "media_warnings": [],
                                }
                                if index == 5
                                else {}
                            ),
                        }
                        for index in range(1, 6)
                    ],
                },
            )
            _write_json(
                output_dir / "ai_visual_review.json",
                {
                    "reviews": [
                        {
                            "visual_review_id": "visual_review:packet_005",
                            "packet_id": "packet_005",
                            "source_packet_id": "packet_005",
                            "visible": True,
                            "local_search_roi": {
                                "coordinate_space": "image",
                                "frame": 50,
                                "x": 4300.0,
                                "y": 760.0,
                                "width": 900.0,
                                "height": 520.0,
                                "confidence": 0.74,
                            },
                            "frame_dimensions": {"width": 5760, "height": 1440},
                            "provenance": {"source": "ai_visual_review", "model": "vision-test"},
                        }
                    ]
                },
            )
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
                            "diagnosis": "Packet id exists in review_packets.json but is outside the prompt context limit.",
                            "recommended_action": "localize_ball_roi",
                            "candidate_id": "candidate_001",
                            "source_packet_id": "packet_005",
                            "local_search_roi": {
                                "coordinate_space": "image",
                                "frame": 50,
                                "x": 4300,
                                "y": 760,
                                "width": 900,
                                "height": 520,
                                "confidence": 0.74,
                            },
                            "confidence": 0.78,
                        }
                    ],
                }
            )

            report = build_ai_improvement_report(output_dir, client=client, max_items=3)
            prompt_payload = json.loads(str(client.calls[0]["prompt"]))

        self.assertEqual("needs_rerun", report["summary"]["status"])
        self.assertEqual("packet_005", report["improvements"][0]["source_packet_id"])
        prompt_packets = prompt_payload["context"]["artifacts"]["review_packets"]["packets"]
        self.assertEqual(["packet_001", "packet_002", "packet_003"], [packet["packet_id"] for packet in prompt_packets])
        self.assertNotIn("traceable_provenance", prompt_payload["context"])

    def test_targeted_rerun_with_local_search_roi_requires_traceable_provenance(self) -> None:
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
                            "diagnosis": "A targeted rerun carries an ROI without packet evidence.",
                            "recommended_action": "targeted_rerun",
                            "rerun_scope": {"start_frame": 2060, "end_frame": 2110},
                            "local_search_roi": {
                                "coordinate_space": "image",
                                "frame": 2088,
                                "x": 4300,
                                "y": 760,
                                "width": 900,
                                "height": 520,
                                "confidence": 0.74,
                            },
                            "evidence": ["unlinked visual impression"],
                            "confidence": 0.78,
                        }
                    ],
                }
            )

            report = build_ai_improvement_report(output_dir, client=client)

        self.assertEqual("error", report["summary"]["status"])
        self.assertIn("local_search_roi requires traceable packet or visual review provenance", report["error"])

    def test_partial_long_gap_suggestion_without_uncovered_subwindow_explanation_becomes_error_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            _write_json(
                output_dir / "ball_audit.json",
                {
                    "summary": {"review_event_count": 1, "lost_gap_count": 1},
                    "review_events": [
                        {
                            "type": "lost_gap",
                            "severity": "fail",
                            "start_frame": 100,
                            "end_frame": 260,
                            "frame_count": 161,
                            "reason": "Ball track is lost for a long sequence.",
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
                            "packet_id": "packet_long_gap",
                            "source": {"kind": "trigger", "type": "lost_gap", "start_frame": 100, "end_frame": 260},
                            "window": {"start_frame": 100, "end_frame": 260},
                        }
                    ],
                },
            )
            client = _FakeImprovementClient(
                {
                    "summary": {"status": "needs_rerun"},
                    "improvements": [
                        {
                            "id": "imp_partial",
                            "priority": "P0",
                            "area": "tracking",
                            "failure_tags": ["ball_lost"],
                            "root_cause_module": "reacquisition",
                            "diagnosis": "Only the first part of the long gap has a proposed recovery window.",
                            "recommended_action": "targeted_rerun",
                            "rerun_scope": {"start_frame": 100, "end_frame": 150},
                            "source_packet_id": "packet_long_gap",
                            "likely_ball_region": {
                                "description": "right channel near the player cluster",
                                "frame": 125,
                                "confidence": 0.7,
                            },
                            "evidence": [{"source_packet_id": "packet_long_gap"}],
                            "confidence": 0.82,
                        }
                    ],
                }
            )

            report = build_ai_improvement_report(output_dir, client=client)

        self.assertEqual("error", report["summary"]["status"])
        self.assertIn("cover the entire lost gap", report["error"])

    def test_long_gap_validation_uses_full_artifact_not_truncated_prompt_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            events = [
                {
                    "type": "large_jump",
                    "severity": "warn",
                    "start_frame": index * 10,
                    "end_frame": index * 10 + 1,
                    "frame_count": 2,
                }
                for index in range(20)
            ]
            events.append(
                {
                    "type": "lost_gap",
                    "severity": "fail",
                    "start_frame": 1000,
                    "end_frame": 1180,
                    "frame_count": 181,
                    "reason": "Long gap appears after the prompt item limit.",
                }
            )
            _write_json(
                output_dir / "ball_audit.json",
                {"summary": {"review_event_count": len(events)}, "review_events": events},
            )
            _write_json(
                output_dir / "review_packets.json",
                {
                    "summary": {"packet_count": 1},
                    "packets": [
                        {
                            "packet_id": "packet_late_gap",
                            "source": {"kind": "trigger", "type": "lost_gap", "start_frame": 1000, "end_frame": 1180},
                            "window": {"start_frame": 1000, "end_frame": 1180},
                        }
                    ],
                },
            )
            client = _FakeImprovementClient(
                {
                    "summary": {"status": "needs_rerun"},
                    "improvements": [
                        {
                            "id": "imp_partial_late",
                            "priority": "P0",
                            "area": "tracking",
                            "failure_tags": ["ball_lost"],
                            "root_cause_module": "reacquisition",
                            "diagnosis": "Only covers the first part of a long gap outside the prompt limit.",
                            "recommended_action": "targeted_rerun",
                            "rerun_scope": {"start_frame": 1000, "end_frame": 1040},
                            "source_packet_id": "packet_late_gap",
                            "likely_ball_region": {"description": "right channel", "frame": 1020, "confidence": 0.7},
                            "evidence": [{"source_packet_id": "packet_late_gap"}],
                            "confidence": 0.82,
                        }
                    ],
                }
            )

            report = build_ai_improvement_report(output_dir, client=client, max_items=20)

        self.assertEqual("error", report["summary"]["status"])
        self.assertIn("cover the entire lost gap", report["error"])

    def test_unrelated_uncovered_subwindow_explanation_does_not_cover_long_gap_tail(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            _write_json(
                output_dir / "ball_audit.json",
                {
                    "summary": {"review_event_count": 1, "lost_gap_count": 1},
                    "review_events": [
                        {
                            "type": "lost_gap",
                            "severity": "fail",
                            "start_frame": 100,
                            "end_frame": 260,
                            "frame_count": 161,
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
                            "packet_id": "packet_long_gap",
                            "source": {"kind": "trigger", "type": "lost_gap", "start_frame": 100, "end_frame": 260},
                            "window": {"start_frame": 100, "end_frame": 260},
                        }
                    ],
                },
            )
            client = _FakeImprovementClient(
                {
                    "summary": {"status": "needs_rerun"},
                    "improvements": [
                        {
                            "id": "imp_partial",
                            "priority": "P0",
                            "area": "tracking",
                            "failure_tags": ["ball_lost"],
                            "root_cause_module": "reacquisition",
                            "diagnosis": "Explains an unrelated window, not the uncovered long-gap tail.",
                            "recommended_action": "targeted_rerun",
                            "rerun_scope": {"start_frame": 100, "end_frame": 150},
                            "source_packet_id": "packet_long_gap",
                            "likely_ball_region": {"description": "right channel", "frame": 125, "confidence": 0.7},
                            "uncovered_subwindows": [
                                {"start_frame": 10, "end_frame": 20, "reason": "unrelated packet"}
                            ],
                            "evidence": [{"source_packet_id": "packet_long_gap"}],
                            "confidence": 0.82,
                        }
                    ],
                }
            )

            report = build_ai_improvement_report(output_dir, client=client)

        self.assertEqual("error", report["summary"]["status"])
        self.assertIn("explain uncovered subwindows", report["error"])

    def test_uncovered_subwindow_explanation_requires_standalone_frame_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            _write_json(
                output_dir / "ball_audit.json",
                {
                    "summary": {"review_event_count": 1, "lost_gap_count": 1},
                    "review_events": [
                        {
                            "type": "lost_gap",
                            "severity": "fail",
                            "start_frame": 10,
                            "end_frame": 150,
                            "frame_count": 141,
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
                            "packet_id": "packet_gap",
                            "source": {"kind": "trigger", "type": "lost_gap", "start_frame": 10, "end_frame": 150},
                            "window": {"start_frame": 10, "end_frame": 150},
                        }
                    ],
                },
            )
            client = _FakeImprovementClient(
                {
                    "summary": {"status": "needs_rerun"},
                    "improvements": [
                        {
                            "id": "imp_partial",
                            "priority": "P0",
                            "area": "tracking",
                            "failure_tags": ["ball_lost"],
                            "root_cause_module": "reacquisition",
                            "diagnosis": "The explanation cites lookalike frame numbers, not the uncovered range.",
                            "recommended_action": "targeted_rerun",
                            "rerun_scope": {"start_frame": 10, "end_frame": 20},
                            "source_packet_id": "packet_gap",
                            "likely_ball_region": {"description": "right channel", "frame": 15, "confidence": 0.7},
                            "coverage_explanation": "Frames 121-150 are not actionable in a different packet.",
                            "evidence": [{"source_packet_id": "packet_gap"}],
                            "confidence": 0.82,
                        }
                    ],
                }
            )

            report = build_ai_improvement_report(output_dir, client=client)

        self.assertEqual("error", report["summary"]["status"])
        self.assertIn("explain uncovered subwindows", report["error"])

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

    def test_unknown_noise_class_is_normalized_with_warning(self) -> None:
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
                            "failure_tags": ["unknown"],
                            "root_cause_module": "selection",
                            "start_frame": 120,
                            "end_frame": 142,
                            "diagnosis": "The dense-noise packet cites a class outside the shared vocabulary.",
                            "recommended_action": "noise_filter_adjustment",
                            "false_positive_class": "spectator_hat",
                            "config_patch": {"selection": {"min_accept_score": 0.62}},
                            "evidence": ["dense-noise packet has repeated off-ball detections"],
                            "confidence": 0.73,
                        }
                    ],
                }
            )

            report = build_ai_improvement_report(output_dir, client=client)

        improvement = report["improvements"][0]
        self.assertEqual("needs_rerun", report["summary"]["status"])
        self.assertEqual("unknown", improvement["false_positive_class"])
        self.assertTrue(any("false_positive_class normalized to unknown" in warning for warning in report["warnings"]))

    def test_known_extra_ball_noise_class_is_preserved(self) -> None:
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
                            "failure_tags": ["unknown"],
                            "root_cause_module": "selection",
                            "start_frame": 120,
                            "end_frame": 142,
                            "diagnosis": "Packet shows a spare ball outside the active play.",
                            "recommended_action": "noise_filter_adjustment",
                            "false_positive_class": "extra_ball",
                            "config_patch": {"selection": {"min_accept_score": 0.62}},
                            "evidence": ["dense-noise packet shows non-match ball"],
                            "confidence": 0.73,
                        }
                    ],
                }
            )

            report = build_ai_improvement_report(output_dir, client=client)

        improvement = report["improvements"][0]
        self.assertEqual("extra_ball", improvement["false_positive_class"])
        self.assertFalse(any("false_positive_class normalized to unknown" in warning for warning in report["warnings"]))

    def test_unknown_false_positive_noise_class_is_preserved(self) -> None:
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
                            "failure_tags": ["unknown"],
                            "root_cause_module": "selection",
                            "start_frame": 120,
                            "end_frame": 142,
                            "diagnosis": "Packet has a false positive, but the specific class is uncertain.",
                            "recommended_action": "noise_filter_adjustment",
                            "false_positive_class": "unknown_false_positive",
                            "config_patch": {"selection": {"min_accept_score": 0.62}},
                            "evidence": ["dense-noise packet shows an unknown off-ball detection"],
                            "confidence": 0.73,
                        }
                    ],
                }
            )

            report = build_ai_improvement_report(output_dir, client=client)

        improvement = report["improvements"][0]
        self.assertEqual("unknown_false_positive", improvement["false_positive_class"])
        self.assertFalse(any("false_positive_class normalized to unknown" in warning for warning in report["warnings"]))

    def test_false_positive_class_in_failure_tags_is_rehomed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            client = _FakeImprovementClient(
                {
                    "summary": {"status": "needs_rerun"},
                    "improvements": [
                        {
                            "id": "imp_noise",
                            "priority": "P1",
                            "area": "tracking",
                            "failure_tags": ["extra_ball"],
                            "root_cause_module": "selection",
                            "diagnosis": "A second ball-like object appears near the advertising board.",
                            "recommended_action": "noise_filter_adjustment",
                            "start_frame": 10,
                            "end_frame": 16,
                            "config_patch": {"selection": {"min_accept_score": 0.62}},
                            "evidence": ["bounded false positive window"],
                            "confidence": 0.63,
                        }
                    ],
                }
            )

            report = build_ai_improvement_report(output_dir, client=client)

        self.assertEqual("needs_rerun", report["summary"]["status"])
        improvement = report["improvements"][0]
        self.assertEqual(["unknown"], improvement["failure_tags"])
        self.assertEqual("extra_ball", improvement["false_positive_class"])

    def test_camera_actions_are_validated_and_strip_invalid_follow_cam_patch_names(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            _write_camera_motion_event(output_dir, frame=40, severity="warn", max_step_px=100.0)
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

    def test_camera_adjust_follow_cam_is_rejected_when_event_overlaps_lost_track_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            _write_camera_motion_event(output_dir, frame=40, severity="fail", max_step_px=150.0)
            _write_ball_track(
                output_dir,
                [
                    (38, "Detected", 100, 100),
                    (39, "Detected", 110, 100),
                    (40, "Lost", "", ""),
                    (41, "Predicted", 130, 100),
                    (42, "Detected", 140, 100),
                ],
            )
            client = _FakeImprovementClient(
                {
                    "summary": {"status": "needs_rerun", "primary_issue": "camera_motion"},
                    "improvements": [
                        {
                            "id": "imp_camera",
                            "priority": "P0",
                            "area": "camera_motion",
                            "failure_tags": ["camera_catchup_spike"],
                            "root_cause_module": "follow_cam",
                            "diagnosis": "Incorrectly treats a tracking-loss camera spike as follow-cam tuning.",
                            "recommended_action": "adjust_follow_cam",
                            "config_patch": {"follow_cam": {"pan_smoothing": 0.82}},
                            "camera_motion_event_id": "cam_event_001",
                            "confidence": 0.7,
                        }
                    ],
                }
            )

            report = build_ai_improvement_report(output_dir, client=client)

        self.assertEqual("error", report["summary"]["status"])
        self.assertIn("tracking_rerun_before_follow_cam", report["error"])

    def test_camera_adjust_follow_cam_with_unknown_event_id_still_checks_overlapping_track_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            _write_camera_motion_event(output_dir, frame=40, severity="fail", max_step_px=150.0)
            _write_ball_track(
                output_dir,
                [
                    (39, "Detected", 110, 100),
                    (40, "Lost", "", ""),
                    (41, "Predicted", 130, 100),
                ],
            )
            client = _FakeImprovementClient(
                {
                    "summary": {"status": "needs_rerun", "primary_issue": "camera_motion"},
                    "improvements": [
                        {
                            "id": "imp_camera",
                            "priority": "P0",
                            "area": "camera_motion",
                            "failure_tags": ["camera_catchup_spike"],
                            "root_cause_module": "follow_cam",
                            "start_frame": 40,
                            "end_frame": 40,
                            "diagnosis": "Uses a typo event id while overlapping a tracking-loss camera spike.",
                            "recommended_action": "adjust_follow_cam",
                            "config_patch": {"follow_cam": {"pan_smoothing": 0.82}},
                            "camera_motion_event_id": "cam_event_typo",
                            "confidence": 0.7,
                        }
                    ],
                }
            )

            report = build_ai_improvement_report(output_dir, client=client)

        self.assertEqual("error", report["summary"]["status"])
        self.assertIn("tracking_rerun_before_follow_cam", report["error"])

    def test_camera_adjust_follow_cam_with_unknown_event_id_and_no_window_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            _write_camera_motion_event(output_dir, frame=40, severity="fail", max_step_px=150.0)
            client = _FakeImprovementClient(
                {
                    "summary": {"status": "needs_rerun", "primary_issue": "camera_motion"},
                    "improvements": [
                        {
                            "id": "imp_camera",
                            "priority": "P0",
                            "area": "camera_motion",
                            "failure_tags": ["camera_catchup_spike"],
                            "root_cause_module": "follow_cam",
                            "diagnosis": "Uses a typo event id without a fallback frame window.",
                            "recommended_action": "adjust_follow_cam",
                            "config_patch": {"follow_cam": {"pan_smoothing": 0.82}},
                            "camera_motion_event_id": "cam_event_typo",
                            "confidence": 0.7,
                        }
                    ],
                }
            )

            report = build_ai_improvement_report(output_dir, client=client)

        self.assertEqual("error", report["summary"]["status"])
        self.assertIn("unknown camera_motion_event_id", report["error"])

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

    def test_unknown_ai_action_becomes_review_only(self) -> None:
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

        self.assertEqual("needs_rerun", report["summary"]["status"])
        improvement = report["improvements"][0]
        self.assertEqual("manual_review", improvement["recommended_action"])
        self.assertEqual("targeted rerun", improvement["original_recommended_action"])
        self.assertFalse(improvement["executable"])
        self.assertEqual("review_only", improvement["candidate_intent"])
        self.assertTrue(any("unsupported recommended_action" in warning for warning in report["warnings"]))

    def test_unbounded_full_video_spatial_split_is_not_executable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            client = _FakeImprovementClient(
                {
                    "summary": {"status": "needs_rerun"},
                    "improvements": [
                        {
                            "id": "imp_full_video_split",
                            "priority": "P1",
                            "area": "detection",
                            "failure_tags": ["unknown"],
                            "root_cause_module": "detection",
                            "diagnosis": "Broad full-video spatial SAHI split request.",
                            "recommended_action": "spatial_sahi_split_full_video",
                            "evidence": ["broad model suggestion without bounded provenance"],
                            "confidence": 0.55,
                        }
                    ],
                }
            )

            report = build_ai_improvement_report(output_dir, client=client)

        improvement = report["improvements"][0]
        self.assertEqual("manual_review", improvement["recommended_action"])
        self.assertEqual("spatial_sahi_split_full_video", improvement["original_recommended_action"])
        self.assertFalse(improvement["executable"])
        self.assertEqual("review_only", improvement["candidate_intent"])

    def test_small_or_unavailable_model_mode_is_non_mutating(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            client = _FakeImprovementClient({"summary": {"status": "ok"}, "improvements": []})
            client.settings = SimpleNamespace(chat_model="gpt-chat-mini", improvement_model=None)

            report = build_ai_improvement_report(output_dir, client=client)

        self.assertEqual("unavailable", report["summary"]["status"])
        self.assertEqual("strong_model_unavailable", report["model_selection"]["source"])
        self.assertFalse(report["can_lead_to_executable_candidates"])
        self.assertEqual([], client.calls)

    def test_2079_local_evidence_cannot_close_2049_2544_without_full_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            _write_json(
                output_dir / "ball_audit.json",
                {
                    "summary": {"review_event_count": 1, "lost_gap_count": 1},
                    "review_events": [
                        {
                            "type": "lost_gap",
                            "severity": "fail",
                            "start_frame": 2049,
                            "end_frame": 2544,
                            "frame_count": 496,
                            "reason": "Ball track is lost through the long window.",
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
                            "packet_id": "packet_2079",
                            "source": {"kind": "trigger", "type": "lost_gap"},
                            "window": {"start_frame": 2049, "end_frame": 2544},
                        }
                    ],
                },
            )
            client = _FakeImprovementClient(
                {
                    "summary": {"status": "needs_rerun"},
                    "improvements": [
                        {
                            "id": "imp_2079",
                            "priority": "P0",
                            "area": "tracking",
                            "failure_tags": ["ball_lost"],
                            "root_cause_module": "reacquisition",
                            "diagnosis": "Only frame 2079 has local evidence.",
                            "recommended_action": "targeted_rerun",
                            "candidate_id": "candidate_2079",
                            "problem_type": "missing_ball",
                            "source_packet_id": "packet_2079",
                            "rerun_scope": {"start_frame": 2079, "end_frame": 2079},
                            "likely_ball_region": {
                                "frame": 2079,
                                "description": "visible near midfield",
                                "confidence": 0.8,
                            },
                            "expected_artifact": {"name": "ball_track.csv"},
                            "comparison_criteria": {"report": "missing_ball_recovery_comparison.json"},
                            "evidence": [{"source_packet_id": "packet_2079"}],
                            "confidence": 0.72,
                        }
                    ],
                }
            )

            report = build_ai_improvement_report(output_dir, client=client)

        self.assertEqual("error", report["summary"]["status"])
        self.assertIn("2049-2544", report["error"])

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
                            "source_packet_id": "packet_001",
                            "likely_ball_region": {"description": "not visible", "confidence": 0.0},
                            "evidence": [
                                {"source_packet_id": "packet_001", "reason": "packet decision marks ball_not_visible"}
                            ],
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
                    ],
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
        self.assertEqual("rerun_ball_window", report["improvements"][0]["recommended_action"])
        self.assertEqual("targeted_rerun", report["improvements"][0]["legacy_recommended_action"])
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

    def test_visual_review_out_of_frame_roi_is_not_merged(self) -> None:
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
                                "x": 620.0,
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
                            "likely_ball_region": {"description": "not visible", "confidence": 0.0},
                            "evidence": [{"source_packet_id": "packet_001"}],
                            "confidence": 0.6,
                        }
                    ],
                }
            )

            report = build_ai_improvement_report(output_dir, client=client)

        self.assertEqual("needs_rerun", report["summary"]["status"])
        self.assertNotIn("local_search_roi", report["improvements"][0])
        self.assertTrue(
            any("ignored invalid ai_visual_review local_search_roi" in warning for warning in report["warnings"])
        )

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
                            "candidate_id": "candidate_001",
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

    def test_approving_targeted_rerun_writes_normalized_rerun_ball_window_action(self) -> None:
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
                            "candidate_id": "candidate_001",
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
                            "evidence": [{"source_packet_id": "packet_001"}],
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
        self.assertEqual("rerun_ball_window", action["approved_action"])
        self.assertEqual("targeted_rerun", action["legacy_approved_action"])
        self.assertEqual("missing_ball", action["problem_type"])
        self.assertEqual([{"source_packet_id": "packet_001"}], action["evidence"])
        self.assertEqual("candidate_001", action["candidate_id"])
        self.assertEqual("operator-a", action["approved_by"])
        self.assertEqual("packet_001", action["source_packet_id"])
        self.assertEqual("visual_review:packet_001", action["visual_review_id"])
        self.assertEqual({"start_frame": 10, "end_frame": 20}, action["rerun_scope"])
        self.assertEqual("gpt-improve", action["provenance"]["model"])

    def test_approving_canonical_rerun_ball_window_action(self) -> None:
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
                            "recommended_action": "rerun_ball_window",
                            "problem_type": "missing_ball",
                            "candidate_id": "candidate_001",
                            "rerun_scope": {"start_frame": 10, "end_frame": 20},
                            "likely_ball_region": {"description": "not visible", "confidence": 0.0},
                            "source_packet_id": "packet_001",
                            "expected_artifact": {"name": "ball_track.csv"},
                            "comparison_criteria": {"report": "missing_ball_recovery_comparison.json"},
                            "evidence": [{"source_packet_id": "packet_001"}],
                            "confidence": 0.82,
                        }
                    ],
                },
            )

            artifact = approve_ai_improvement_actions(output_dir, run_id="run_123", improvement_ids=["imp_001"])

        action = artifact["approved_actions"][0]
        self.assertEqual("rerun_ball_window", action["approved_action"])
        self.assertNotIn("legacy_approved_action", action)
        self.assertEqual("missing_ball", action["problem_type"])
        self.assertEqual("candidate_001", action["candidate_id"])
        self.assertEqual({"name": "ball_track.csv"}, action["expected_artifact"])
        self.assertEqual({"report": "missing_ball_recovery_comparison.json"}, action["comparison_criteria"])

    def test_approval_rejects_localize_ball_roi_without_traceable_provenance(self) -> None:
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
                            "diagnosis": "Manually edited report removed source ids.",
                            "recommended_action": "localize_ball_roi",
                            "candidate_id": "candidate_001",
                            "match_ball_confirmed": True,
                            "start_frame": 10,
                            "end_frame": 20,
                            "local_search_roi": {
                                "coordinate_space": "image",
                                "frame": 15,
                                "x": 120,
                                "y": 40,
                                "width": 80,
                                "height": 50,
                                "confidence": 0.72,
                            },
                            "evidence_payload": {"local_search_roi_provenance": {"source": "ai_visual_review"}},
                            "confidence": 0.82,
                        }
                    ],
                },
            )

            with self.assertRaisesRegex(ValueError, "traceable packet or visual review provenance"):
                approve_ai_improvement_actions(
                    output_dir,
                    run_id="run_123",
                    improvement_ids=["imp_001"],
                    approved_by="operator-a",
                )

    def test_approval_accepts_localize_ball_roi_with_evidence_list_provenance(self) -> None:
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
                            "diagnosis": "Evidence list carries the packet id.",
                            "recommended_action": "localize_ball_roi",
                            "candidate_id": "candidate_001",
                            "match_ball_confirmed": True,
                            "start_frame": 10,
                            "end_frame": 20,
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
            rerun_report = build_high_recall_windows(
                output_dir,
                approved_actions_path=output_dir / "ai_improvement_approved_actions.json",
                approved_only=True,
                total_frames=100,
            )

        action = artifact["approved_actions"][0]
        self.assertEqual("localize_ball_roi", action["approved_action"])
        self.assertEqual("packet_001", action["source_packet_id"])
        self.assertEqual(10, action["start_frame"])
        self.assertEqual(20, action["end_frame"])
        self.assertIs(True, action["match_ball_confirmed"])
        self.assertEqual(120.0, action["local_search_roi"]["x"])
        self.assertEqual(1, rerun_report["summary"]["selected_window_count"])
        self.assertEqual("packet_001", rerun_report["windows"][0]["source_packet_id"])

    def test_approval_accepts_localize_ball_roi_with_visual_localization_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            _write_json(
                output_dir / "ai_visual_localization.json",
                {
                    "requests": [
                        _clean_visual_localization_request(
                            "visual_localization:packet_001",
                            source_packet_id="packet_001",
                            local_search_roi={
                                "coordinate_space": "image",
                                "frame": 15,
                                "x": 120,
                                "y": 40,
                                "width": 80,
                                "height": 50,
                                "confidence": 0.72,
                            },
                        )
                    ]
                },
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
                            "id": "imp_visual_localized",
                            "priority": "P0",
                            "area": "tracking",
                            "failure_tags": ["ball_lost"],
                            "root_cause_module": "reacquisition",
                            "diagnosis": "Visual localization artifact carries the ROI provenance.",
                            "recommended_action": "localize_ball_roi",
                            "candidate_id": "candidate_001",
                            "problem_type": "missing_ball",
                            "match_ball_confirmed": True,
                            "start_frame": 10,
                            "end_frame": 20,
                            "visual_localization_id": "visual_localization:packet_001",
                            "local_search_roi": {
                                "coordinate_space": "image",
                                "frame": 15,
                                "x": 120,
                                "y": 40,
                                "width": 80,
                                "height": 50,
                                "confidence": 0.72,
                            },
                            "expected_artifact": {"name": "ball_track.csv"},
                            "comparison_criteria": {"report": "missing_ball_recovery_comparison.json"},
                            "confidence": 0.82,
                        }
                    ],
                },
            )

            artifact = approve_ai_improvement_actions(
                output_dir,
                run_id="run_123",
                improvement_ids=["imp_visual_localized"],
                approved_by="operator-a",
            )
            rerun_report = build_high_recall_windows(
                output_dir,
                approved_actions_path=output_dir / "ai_improvement_approved_actions.json",
                approved_only=True,
                total_frames=100,
            )

        action = artifact["approved_actions"][0]
        self.assertEqual("visual_localization:packet_001", action["visual_localization_id"])
        self.assertEqual("visual_localization:packet_001", rerun_report["windows"][0]["visual_localization_id"])

    def test_approval_rejects_request_targeted_localization(self) -> None:
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
                            "id": "imp_direct_locator",
                            "priority": "P0",
                            "area": "tracking",
                            "failure_tags": ["ball_lost"],
                            "root_cause_module": "reacquisition",
                            "diagnosis": "This is a review-only locator request.",
                            "recommended_action": "request_targeted_localization",
                            "source_packet_id": "packet_001",
                            "confidence": 0.82,
                        }
                    ],
                },
            )

            with self.assertRaisesRegex(ValueError, "review-only"):
                approve_ai_improvement_actions(output_dir, run_id="run_123", improvement_ids=["imp_direct_locator"])

    def test_approval_rejects_localize_ball_roi_without_frame_bounds(self) -> None:
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
                            "diagnosis": "ROI without a bounded frame window is not executable.",
                            "recommended_action": "localize_ball_roi",
                            "candidate_id": "candidate_001",
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
                            "confidence": 0.82,
                        }
                    ],
                },
            )

            with self.assertRaisesRegex(ValueError, "localize_ball_roi requires frame bounds"):
                approve_ai_improvement_actions(
                    output_dir,
                    run_id="run_123",
                    improvement_ids=["imp_001"],
                    approved_by="operator-a",
                )

    def test_approval_rejects_unsafe_missing_ball_candidate_id_before_writing_artifact(self) -> None:
        unsafe_ids = ["../escape", "candidate/path", "C:tmp", ".", "..", "CON", "candidate.", "candidate "]
        for candidate_id in unsafe_ids:
            with self.subTest(candidate_id=candidate_id):
                with tempfile.TemporaryDirectory() as temp_name:
                    output_dir = Path(temp_name)
                    _write_minimal_artifacts(output_dir)
                    approved_path = output_dir / "ai_improvement_approved_actions.json"
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
                                    "diagnosis": "Unsafe candidate id should not be written.",
                                    "recommended_action": "targeted_rerun",
                                    "candidate_id": candidate_id,
                                    "rerun_scope": {"start_frame": 10, "end_frame": 20},
                                    "source_packet_id": "packet_001",
                                    "confidence": 0.82,
                                }
                            ],
                        },
                    )

                    with self.assertRaisesRegex(ValueError, "candidate_id"):
                        approve_ai_improvement_actions(
                            output_dir,
                            run_id="run_123",
                            improvement_ids=["imp_001"],
                            approved_by="operator-a",
                        )

                    self.assertFalse(approved_path.exists())

    def test_approval_rejects_unsafe_localize_candidate_id_before_writing_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            approved_path = output_dir / "ai_improvement_approved_actions.json"
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
                            "diagnosis": "Unsafe candidate id should not be written.",
                            "recommended_action": "localize_ball_roi",
                            "candidate_id": "candidate/path",
                            "start_frame": 10,
                            "end_frame": 20,
                            "source_packet_id": "packet_001",
                            "local_search_roi": {
                                "coordinate_space": "image",
                                "frame": 15,
                                "x": 120,
                                "y": 40,
                                "width": 80,
                                "height": 50,
                                "confidence": 0.72,
                            },
                            "confidence": 0.82,
                        }
                    ],
                },
            )

            with self.assertRaisesRegex(ValueError, "candidate_id"):
                approve_ai_improvement_actions(
                    output_dir,
                    run_id="run_123",
                    improvement_ids=["imp_001"],
                    approved_by="operator-a",
                )

            self.assertFalse(approved_path.exists())

    def test_approval_config_patch_strips_invalid_paths_and_writes_derived_patch(self) -> None:
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
                            "id": "imp_filter",
                            "priority": "P1",
                            "area": "tracking",
                            "failure_tags": ["foot_confusion"],
                            "root_cause_module": "selection",
                            "start_frame": 40,
                            "end_frame": 52,
                            "diagnosis": "Noise filter can tighten.",
                            "recommended_action": "noise_filter_adjustment",
                            "candidate_id": "noise_candidate_001",
                            "source_packet_id": "packet_001",
                            "false_positive_class": "foot_confusion",
                            "evidence": [{"source_packet_id": "packet_001"}],
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
            patch_artifact = json.loads(
                (output_dir / "ai_improvement_approved_config_patch.json").read_text(encoding="utf-8")
            )

        self.assertEqual({"selection": {"min_accept_score": 0.6}}, artifact["approved_actions"][0]["config_patch"])
        self.assertEqual("noise_candidate_001", artifact["approved_actions"][0]["candidate_id"])
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
                            "candidate_id": "follow_cam_candidate_001",
                            "camera_motion_event_id": "cam_event_001",
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
                            "candidate_id": "follow_cam_candidate_001",
                            "camera_motion_event_id": "cam_event_001",
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
                            "candidate_id": "follow_cam_candidate_adjust",
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
                            "candidate_id": "follow_cam_candidate_track",
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
                            "id": "imp_noise",
                            "priority": "P1",
                            "area": "tracking",
                            "failure_tags": ["foot_confusion"],
                            "root_cause_module": "selection",
                            "start_frame": 40,
                            "end_frame": 52,
                            "diagnosis": "Noise filter can tighten.",
                            "recommended_action": "noise_filter_adjustment",
                            "candidate_id": "noise_candidate_001",
                            "source_packet_id": "packet_001",
                            "evidence": [{"source_packet_id": "packet_001"}],
                            "false_positive_class": "foot_confusion",
                            "config_patch": {"selection": {"min_accept_score": 0.55}},
                            "confidence": 0.7,
                        }
                    ],
                },
            )
            approve_ai_improvement_actions(output_dir, run_id="run_123", improvement_ids=["imp_noise"])
            self.assertTrue((output_dir / "ai_improvement_approved_config_patch.json").exists())
            written = json.loads((output_dir / "ai_improvement_approved_actions.json").read_text(encoding="utf-8"))
            action = written["approved_actions"][0]
            self.assertEqual({"name": "ball_track.cleaned.csv", "role": "candidate"}, action["expected_artifact"])
            self.assertEqual({"report": "noise_candidate_comparison.json"}, action["comparison_criteria"])
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
            _write_minimal_artifacts(output_dir)
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
                            "candidate_id": "noise_candidate_001",
                            "source_packet_id": "packet_001",
                            "evidence": [{"source_packet_id": "packet_001"}],
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
                            "event_candidate_id": "cleaned:shot_candidate:10-20",
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
                            "candidate_id": "highlight_candidate_001",
                            "event_candidate_id": "cleaned:shot_candidate:10-20",
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

    def test_highlight_tail_can_clamp_at_source_video_end(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            _write_json(
                output_dir / "event_candidates.json",
                {
                    "summary": {"candidate_count": 1, "total_source_frames": 51},
                    "candidates": [
                        {
                            "id": "cleaned:shot_candidate:10-20",
                            "type": "shot_candidate",
                            "start_frame": 10,
                            "end_frame": 20,
                            "core_window": {"start_frame": 10, "end_frame": 20},
                            "render_window": {"start_frame": 0, "end_frame": 50},
                            "buffer_policy": {
                                "fps": 20.0,
                                "post_buffer_frames": 90,
                                "min_post_event_frames": 90,
                                "min_tail_frames": 90,
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
                            "id": "imp_source_end",
                            "priority": "P1",
                            "area": "highlights",
                            "failure_tags": ["post_roll_too_short"],
                            "root_cause_module": "event_scoring",
                            "diagnosis": "Extend to the final source frame; no more tail exists.",
                            "recommended_action": "render_suggested_highlight",
                            "candidate_id": "highlight_candidate_001",
                            "event_candidate_id": "cleaned:shot_candidate:10-20",
                            "suggested_window": {"start_frame": 0, "end_frame": 50},
                            "clip_action": "extend_tail",
                            "confidence": 0.8,
                        }
                    ],
                    "highlight_adjustments": [],
                }
            )

            report = build_ai_improvement_report(output_dir, client=client)

        self.assertEqual("needs_rerun", report["summary"]["status"])
        self.assertEqual({"start_frame": 0, "end_frame": 50}, report["improvements"][0]["suggested_window"])

    def test_highlight_suggestion_cannot_extend_past_source_video_end(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            _write_json(
                output_dir / "event_candidates.json",
                {
                    "summary": {"candidate_count": 1, "total_source_frames": 51},
                    "candidates": [
                        {
                            "id": "cleaned:shot_candidate:10-20",
                            "type": "shot_candidate",
                            "core_window": {"start_frame": 10, "end_frame": 20},
                            "render_window": {"start_frame": 0, "end_frame": 50},
                            "buffer_policy": {"min_tail_frames": 20},
                        }
                    ],
                },
            )
            client = _FakeImprovementClient(
                {
                    "summary": {"status": "needs_rerun"},
                    "improvements": [
                        {
                            "id": "imp_beyond_source",
                            "priority": "P1",
                            "area": "highlights",
                            "failure_tags": ["post_roll_too_short"],
                            "root_cause_module": "event_scoring",
                            "diagnosis": "Suggests frames beyond the source video.",
                            "recommended_action": "render_suggested_highlight",
                            "candidate_id": "highlight_candidate_001",
                            "event_candidate_id": "cleaned:shot_candidate:10-20",
                            "suggested_window": {"start_frame": 0, "end_frame": 999},
                            "clip_action": "extend_tail",
                            "confidence": 0.8,
                        }
                    ],
                    "highlight_adjustments": [],
                }
            )

            report = build_ai_improvement_report(output_dir, client=client)

        self.assertEqual("error", report["summary"]["status"])
        self.assertIn("source video end", report["error"])

    def test_approving_highlight_action_preserves_event_candidate_id(self) -> None:
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
                            "id": "imp_highlight",
                            "priority": "P1",
                            "area": "highlights",
                            "failure_tags": ["post_roll_too_short"],
                            "root_cause_module": "event_scoring",
                            "diagnosis": "Extend result tail.",
                            "recommended_action": "render_suggested_highlight",
                            "problem_type": "highlight",
                            "candidate_id": "highlight_candidate_001",
                            "event_candidate_id": "cleaned:shot_candidate:10-20",
                            "suggested_window": {"start_frame": 0, "end_frame": 45},
                            "clip_action": "extend_tail",
                            "expected_artifact": {"name": "highlight.mp4"},
                            "comparison_criteria": {"report": "highlight_candidate_comparison.json"},
                            "confidence": 0.8,
                        }
                    ],
                },
            )

            artifact = approve_ai_improvement_actions(output_dir, run_id="run_123", improvement_ids=["imp_highlight"])

        action = artifact["approved_actions"][0]
        self.assertEqual("highlight_candidate_001", action["candidate_id"])
        self.assertEqual("cleaned:shot_candidate:10-20", action["event_candidate_id"])

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
                            "candidate_id": "highlight_candidate_001",
                            "event_candidate_id": "cleaned:shot_candidate:10-20",
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

    def test_real_improvement_without_strong_model_records_unavailable_without_chat_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            client = _FakeImprovementClient(RuntimeError("chat fallback must not be called"), enabled=True)
            client.settings = SimpleNamespace(chat_model="gpt-chat-mini", improvement_model=None)

            report = write_ai_improvement_report(output_dir, client=client)

        self.assertEqual([], client.calls)
        self.assertIsNone(report["model"])
        self.assertEqual("strong_model_unavailable", report["model_selection"]["source"])
        self.assertEqual("unavailable", report["summary"]["status"])

    def test_real_improvement_without_settings_requires_explicit_model(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_minimal_artifacts(output_dir)
            client = _FakeImprovementClient(RuntimeError("model=None fallback must not be called"), enabled=True)
            del client.settings

            report = write_ai_improvement_report(output_dir, client=client)

        self.assertEqual([], client.calls)
        self.assertIsNone(report["model"])
        self.assertEqual("strong_model_unavailable", report["model_selection"]["source"])
        self.assertEqual("unavailable", report["summary"]["status"])

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
        self.assertIsNone(report["model"])
        self.assertEqual("strong_model_unavailable", report["model_selection"]["source"])

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
                    "executable_candidate_count": 0,
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
                    exit_code = main(
                        [
                            str(output_dir),
                            "--dry-run",
                            "--max-items",
                            "7",
                            "--model",
                            "gpt-cli",
                            "--candidate-intent",
                            "prepare_approved_candidates",
                        ]
                    )

            printed = json.loads(stdout.getvalue())

        self.assertEqual(0, exit_code)
        self.assertEqual(7, calls[0]["max_items"])
        self.assertEqual(True, calls[0]["dry_run"])
        self.assertEqual("gpt-cli", calls[0]["model"])
        self.assertEqual("prepare_approved_candidates", calls[0]["candidate_intent"])
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
    _write_camera_motion_event(output_dir, frame=40, severity="warn", max_step_px=110.0)


def _write_long_lost_gap_review_inputs(output_dir: Path, *, start: int, end: int, total_frames: int) -> None:
    rows = ["Frame,X,Y,Confidence,Status"]
    for frame in range(total_frames):
        if start <= frame <= end:
            rows.append(f"{frame},,,0.00,Lost")
        else:
            rows.append(f"{frame},{100 + frame * 0.1:.1f},100,0.90,Detected")
    (output_dir / "ball_track.cleaned.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    _write_json(
        output_dir / "ball_audit.json",
        {
            "summary": {"review_event_count": 1, "lost_gap_count": 1},
            "review_events": [
                {
                    "type": "lost_gap",
                    "severity": "fail",
                    "start_frame": start,
                    "end_frame": end,
                    "frame_count": end - start + 1,
                    "reason": "Ball track is lost between tracklets.",
                }
            ],
        },
    )
    _write_json(
        output_dir / "ai_review_triggers.json",
        {
            "schema_version": "1.0",
            "decision": {"needs_ai_review": True},
            "triggers": [
                {
                    "id": f"event:1:lost_gap:{start}-{end}",
                    "type": "lost_gap",
                    "priority": "medium",
                    "source": "cleaned",
                    "start_frame": start,
                    "end_frame": end,
                    "frame_count": end - start + 1,
                    "reason": f"Ball track is lost for {end - start + 1} frames between tracklets.",
                    "evidence": {"lost_frame_count": end - start + 1},
                }
            ],
        },
    )


def _write_minimal_artifacts_with_media(output_dir: Path) -> None:
    _write_minimal_artifacts(output_dir)
    packet_dir = output_dir / "review_packets" / "packet_001"
    packet_dir.mkdir(parents=True, exist_ok=True)
    contact_sheet = packet_dir / "contact_sheet.png"
    crop_sheet = packet_dir / "crop_sheet.png"
    contact_sheet.write_bytes(_tiny_png_bytes())
    crop_sheet.write_bytes(_tiny_png_bytes())
    _write_json(
        output_dir / "review_packets.json",
        {
            "summary": {"packet_count": 1, "media_packet_count": 1},
            "packets": [
                {
                    "packet_id": "packet_001",
                    "source": {"kind": "trigger", "type": "lost_gap", "start_frame": 10, "end_frame": 20},
                    "window": {"start_frame": 0, "end_frame": 35},
                    "decision": {"label": "ball_not_visible"},
                    "media": {
                        "contact_sheet": str(contact_sheet),
                        "crop_sheet": str(crop_sheet),
                    },
                    "media_warnings": [],
                }
            ],
        },
    )
    _write_json(
        output_dir / "ai_visual_review.json",
        {
            "reviews": [
                {
                    "visual_review_id": "visual_review:packet_001",
                    "packet_id": "packet_001",
                    "source_packet_id": "packet_001",
                    "visible": True,
                    "local_search_roi": {
                        "coordinate_space": "image",
                        "frame": 15,
                        "x": 10.0,
                        "y": 20.0,
                        "width": 30.0,
                        "height": 40.0,
                        "confidence": 0.7,
                    },
                    "frame_dimensions": {"width": 640, "height": 360},
                    "provenance": {"source": "ai_visual_review", "model": "vision-test"},
                }
            ]
        },
    )


def _clean_visual_localization_request(
    visual_localization_id: str,
    *,
    source_packet_id: str,
    local_search_roi: dict[str, object],
) -> dict[str, object]:
    frame = int(local_search_roi["frame"])
    return {
        "visual_localization_id": visual_localization_id,
        "source_packet_id": source_packet_id,
        "status": "localized",
        "media_warnings": [],
        "media_integrity": {
            "status": "ok",
            "image_count": 2,
            "low_information_image_count": 0,
            "likely_corrupt_image_count": 0,
        },
        "local_search_roi": dict(local_search_roi),
        "roi_status": "accepted",
        "frames": [
            {
                "frame": frame,
                "status": "localized",
                "ball_visible": True,
                "confidence": local_search_roi.get("confidence", 0.7),
                "local_search_roi": dict(local_search_roi),
            }
        ],
        "coverage": {
            "covered_subwindows": [{"start_frame": frame, "end_frame": frame, "status": "localized"}],
            "uncovered_subwindows": [],
        },
    }


def _candidate_ready_targeted_rerun() -> dict[str, object]:
    return {
        "id": "imp_candidate_ready",
        "priority": "P1",
        "area": "tracking",
        "failure_tags": ["ball_lost"],
        "root_cause_module": "reacquisition",
        "diagnosis": "Candidate-ready bounded rerun.",
        "recommended_action": "targeted_rerun",
        "problem_type": "missing_ball",
        "candidate_id": "candidate_missing_ball_001",
        "rerun_scope": {"start_frame": 10, "end_frame": 20},
        "source_packet_id": "packet_001",
        "likely_ball_region": {"description": "not visible", "confidence": 0.0},
        "expected_artifact": {"name": "ball_track.cleaned.csv", "role": "candidate"},
        "comparison_criteria": {
            "report": "missing_ball_recovery_comparison.json",
            "minimum_recovered_frames": 24,
        },
        "evidence": [{"source_packet_id": "packet_001"}],
        "confidence": 0.66,
    }


def _candidate_contract_fields() -> dict[str, object]:
    return {
        "problem_type": "missing_ball",
        "candidate_id": "candidate_001",
        "start_frame": 2049,
        "end_frame": 2544,
        "expected_artifact": {"name": "ball_track.cleaned.csv", "role": "candidate"},
        "comparison_criteria": {"report": "missing_ball_recovery_comparison.json"},
    }


def _valid_improvement_for_action(action: str, *, candidate_id: str = "candidate_001") -> dict[str, object]:
    base: dict[str, object] = {
        "id": f"imp_{action}",
        "priority": "P1",
        "area": "tracking",
        "failure_tags": ["unknown"],
        "root_cause_module": "unknown",
        "diagnosis": f"Valid fixture for {action}.",
        "recommended_action": action,
        "evidence": ["contract compatibility fixture"],
        "confidence": 0.6,
    }
    if action == "targeted_rerun":
        base.update(
            {
                "failure_tags": ["ball_lost"],
                "root_cause_module": "reacquisition",
                "rerun_scope": {"start_frame": 0, "end_frame": 35},
                "source_packet_id": "packet_001",
                "likely_ball_region": {"description": "not visible", "confidence": 0.0},
                "evidence": [{"source_packet_id": "packet_001"}],
            }
        )
    elif action == "localize_ball_roi":
        base.update(
            {
                "failure_tags": ["ball_lost"],
                "root_cause_module": "reacquisition",
                "candidate_id": candidate_id,
                "source_packet_id": "packet_001",
                "local_search_roi": {
                    "coordinate_space": "image",
                    "frame": 15,
                    "x": 10,
                    "y": 20,
                    "width": 30,
                    "height": 40,
                    "confidence": 0.7,
                },
                "evidence": [{"source_packet_id": "packet_001"}],
            }
        )
    elif action == "request_targeted_localization":
        base.update(
            {
                "failure_tags": ["ball_lost"],
                "root_cause_module": "reacquisition",
                "source_packet_id": "packet_001",
                "evidence": [{"source_packet_id": "packet_001"}],
            }
        )
    elif action in {"noise_filter_adjustment", "tighten_noise_filter", "reject_noise"}:
        base.update(
            {
                "failure_tags": ["unknown"],
                "root_cause_module": "selection",
                "candidate_id": candidate_id,
                "source_packet_id": "packet_001",
                "false_positive_class": "extra_ball",
                "start_frame": 10,
                "end_frame": 12,
                "evidence": [{"source_packet_id": "packet_001"}],
            }
        )
        if action == "noise_filter_adjustment":
            base["config_patch"] = {"selection": {"min_accept_score": 0.62}}
    elif action == "rerun_ball_window":
        base.update(
            {
                "failure_tags": ["ball_lost"],
                "root_cause_module": "reacquisition",
                "problem_type": "missing_ball",
                "candidate_id": candidate_id,
                "rerun_scope": {"start_frame": 0, "end_frame": 35},
                "source_packet_id": "packet_001",
                "likely_ball_region": {"description": "not visible", "confidence": 0.0},
                "expected_artifact": {"name": "ball_track.csv"},
                "comparison_criteria": {"report": "missing_ball_recovery_comparison.json"},
                "evidence": [{"source_packet_id": "packet_001"}],
            }
        )
    elif action == "mark_ball_not_visible":
        base.update(
            {
                "failure_tags": ["ball_lost"],
                "root_cause_module": "reacquisition",
                "problem_type": "missing_ball",
                "candidate_id": candidate_id,
                "start_frame": 10,
                "end_frame": 20,
                "source_packet_id": "packet_001",
                "likely_ball_region": {"description": "not visible", "confidence": 0.0},
                "expected_artifact": {"name": "missing_ball_resolution.json"},
                "comparison_criteria": {"resolution": "not_visible"},
                "evidence": [{"source_packet_id": "packet_001"}],
            }
        )
    elif action in {"adjust_highlight_window", "render_suggested_highlight"}:
        base.update(
            {
                "area": "highlights",
                "failure_tags": ["post_roll_too_short"],
                "root_cause_module": "event_scoring",
                "candidate_id": candidate_id,
                "event_candidate_id": "cleaned:shot_candidate:10-20",
                "suggested_window": {"start_frame": 0, "end_frame": 45},
                "clip_action": "extend_tail",
            }
        )
    elif action == "adjust_follow_cam":
        base.update(
            {
                "area": "camera_motion",
                "failure_tags": ["camera_catchup_spike"],
                "root_cause_module": "follow_cam",
                "candidate_id": candidate_id,
                "camera_motion_event_id": "cam_event_001",
                "config_patch": {"follow_cam": {"glide_pan_smoothing": 0.18}},
            }
        )
    elif action == "tracking_rerun_before_follow_cam":
        base.update(
            {
                "area": "camera_motion",
                "failure_tags": ["camera_catchup_spike"],
                "root_cause_module": "follow_cam",
                "candidate_id": candidate_id,
                "camera_motion_event_id": "cam_event_001",
                "rerun_scope": {"start_frame": 10, "end_frame": 30},
            }
        )
    elif action == "human_review_camera_motion":
        base.update(
            {
                "area": "camera_motion",
                "failure_tags": ["camera_catchup_spike"],
                "root_cause_module": "follow_cam",
                "camera_motion_event_id": "cam_event_001",
                "start_frame": 10,
                "end_frame": 10,
            }
        )
    return base


def _tiny_png_bytes() -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
        b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
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


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
