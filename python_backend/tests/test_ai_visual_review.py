from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from football_tracking.ai_visual_review import (
    AI_VISUAL_REVIEW_RESPONSE_SCHEMA,
    OpenAIVisualReviewClient,
    build_ai_visual_review_report,
)


def _valid_review(
    *,
    verdict: str = "accept_highlight",
    highlight_publishable: bool = True,
    recommended_action: str = "keep_highlight",
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "verdict": verdict,
        "confidence": 0.82,
        "reason": "Ball is visible across multiple frames and the marker stays on it.",
        "match_ball_visible": "yes",
        "marker_alignment": "good",
        "highlight_publishable": highlight_publishable,
        "recommended_action": recommended_action,
        "visual_evidence": ["marker remains close to a visible ball"],
        "failure_tags": [],
        "root_cause_module": "unknown",
        "suggested_fixes": [],
        "likely_ball_region": None,
        "local_search_roi": None,
        "best_subclip": None,
        "tuning_direction": "none",
    }
    if extra is not None:
        payload.update(extra)
    return payload


def _legacy_review_only() -> dict[str, object]:
    return {
        "verdict": "needs_human_review",
        "confidence": 0.71,
        "reason": "Legacy response shape without v2 diagnostics.",
        "match_ball_visible": "unclear",
        "marker_alignment": "unclear",
        "highlight_publishable": False,
        "recommended_action": "send_to_human",
        "visual_evidence": ["legacy review evidence"],
    }


class _FakeVisualReviewClient:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def review_packet(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if not isinstance(response, dict):
            raise AssertionError("Fake response must be a dict or exception.")
        return response


class _ExplodingClient:
    def review_packet(self, **kwargs: object) -> dict[str, object]:
        raise AssertionError("dry-run must not call the visual review client")


class _CapturingResponsesClient:
    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    def create_json_vision_response(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        return self.response


class AiVisualReviewTests(unittest.TestCase):
    def test_fake_client_accepts_three_highlights_and_sends_two_to_human_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_review_packets(
                output_dir,
                [
                    ("packet_001", "highlight_worthy"),
                    ("packet_002", "highlight_worthy"),
                    ("packet_003", "highlight_worthy"),
                    ("packet_004", "needs_ai_review"),
                    ("packet_005", "manual_review"),
                ],
            )
            client = _FakeVisualReviewClient(
                [
                    _valid_review(),
                    _valid_review(),
                    _valid_review(),
                    _valid_review(verdict="needs_human_review", highlight_publishable=False, recommended_action="send_to_human"),
                    _valid_review(verdict="needs_human_review", highlight_publishable=False, recommended_action="send_to_human"),
                ]
            )

            report = build_ai_visual_review_report(output_dir, client=client, model="vision-test")

        self.assertEqual(5, report["summary"]["packet_count"])
        self.assertEqual(5, report["summary"]["reviewed_count"])
        self.assertEqual(0, report["summary"]["error_count"])
        self.assertEqual({"accept_highlight": 3, "needs_human_review": 2}, report["summary"]["counts_by_verdict"])
        self.assertEqual(3, report["summary"]["accepted_highlight_count"])
        self.assertEqual(2, report["summary"]["needs_human_review_count"])
        self.assertEqual(0, report["summary"]["reject_noise_count"])
        self.assertEqual(5, len(client.calls))
        first_call = client.calls[0]
        self.assertEqual("packet_001", first_call["metadata"]["packet_id"])
        self.assertEqual("vision-test", first_call["model"])
        self.assertTrue(str(first_call["contact_sheet_data_url"]).startswith("data:image/jpeg;base64,"))
        self.assertTrue(str(first_call["crop_sheet_data_url"]).startswith("data:image/jpeg;base64,"))

    def test_visual_model_routing_prefers_request_then_visual_then_improvement_model(self) -> None:
        cases = [
            ("explicit request", "gpt-request", "gpt-visual", "gpt-improve", "gpt-request", "explicit"),
            ("visual setting", None, "gpt-visual", "gpt-improve", "gpt-visual", "visual_review_model"),
            ("improvement fallback", None, None, "gpt-improve", "gpt-improve", "improvement_model"),
        ]
        for label, requested_model, visual_model, improvement_model, expected_model, expected_source in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp_name:
                output_dir = Path(temp_name)
                _write_review_packets(output_dir, [("packet_001", "needs_ai_review")])
                client = _FakeVisualReviewClient([_valid_review()])
                client.settings = SimpleNamespace(
                    chat_model="gpt-chat-mini",
                    visual_review_model=visual_model,
                    improvement_model=improvement_model,
                )

                report = build_ai_visual_review_report(output_dir, client=client, model=requested_model)

            self.assertEqual(expected_model, client.calls[0]["model"])
            self.assertEqual(expected_model, report["model"])
            self.assertEqual(
                {
                    "model": expected_model,
                    "source": expected_source,
                    "provider_dry_run": False,
                    "provider_mode": "real",
                },
                report["model_selection"],
            )
            self.assertEqual("visual_localization", report["candidate_intent"])

    def test_default_visual_review_client_uses_nested_provider_settings_for_model_routing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_review_packets(output_dir, [("packet_001", "needs_ai_review")])
            responses_client = _CapturingResponsesClient(_valid_review())
            responses_client.settings = SimpleNamespace(
                chat_model="gpt-chat-mini",
                visual_review_model="gpt-visual",
                improvement_model="gpt-improve",
            )
            client = OpenAIVisualReviewClient(responses_client)

            report = build_ai_visual_review_report(output_dir, client=client)

        self.assertEqual("gpt-visual", responses_client.calls[0]["model"])
        self.assertEqual("gpt-visual", report["model"])
        self.assertEqual("visual_review_model", report["model_selection"]["source"])

    def test_real_visual_review_without_strong_model_records_unavailable_without_chat_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_review_packets(output_dir, [("packet_001", "needs_ai_review")])
            client = _ExplodingClient()
            client.settings = SimpleNamespace(chat_model="gpt-chat-mini", visual_review_model=None, improvement_model=None)

            report = build_ai_visual_review_report(output_dir, client=client)

        self.assertEqual([], getattr(client, "calls", []))
        self.assertIsNone(report["model"])
        self.assertEqual("strong_model_unavailable", report["model_selection"]["source"])
        self.assertEqual("unavailable", report["summary"]["status"])
        self.assertEqual("strong_visual_model_unavailable", report["errors"][0]["error_type"])

    def test_client_supplied_visual_review_error_is_not_labeled_as_missing_strong_model(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_review_packets(output_dir, [("packet_001", "needs_ai_review")])
            client = _FakeVisualReviewClient([RuntimeError("provider timeout")])

            report = build_ai_visual_review_report(output_dir, client=client)

        self.assertIsNone(report["model"])
        self.assertEqual("client_supplied", report["model_selection"]["source"])
        self.assertEqual("error", report["summary"]["status"])
        self.assertEqual("RuntimeError", report["errors"][0]["error_type"])

    def test_only_label_filter_is_applied_before_max_packets(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_review_packets(
                output_dir,
                [
                    ("packet_001", "reject_noise"),
                    ("packet_002", "highlight_worthy"),
                    ("packet_003", "highlight_worthy"),
                    ("packet_004", "needs_ai_review"),
                ],
            )
            client = _FakeVisualReviewClient([_valid_review(verdict="needs_human_review", highlight_publishable=False, recommended_action="send_to_human")])

            report = build_ai_visual_review_report(
                output_dir,
                client=client,
                only_labels=["highlight_worthy"],
                max_packets=1,
            )

        self.assertEqual(1, report["summary"]["packet_count"])
        self.assertEqual("packet_002", report["reviews"][0]["packet_id"])
        self.assertEqual("packet_002", client.calls[0]["metadata"]["packet_id"])

    def test_dry_run_marks_every_packet_for_human_review_without_calling_client(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_review_packets(output_dir, [("packet_001", "highlight_worthy"), ("packet_002", "needs_ai_review")])

            report = build_ai_visual_review_report(output_dir, client=_ExplodingClient(), dry_run=True)

        self.assertEqual(2, report["summary"]["packet_count"])
        self.assertEqual(2, report["summary"]["reviewed_count"])
        self.assertEqual(0, report["summary"]["accepted_highlight_count"])
        self.assertEqual(2, report["summary"]["needs_human_review_count"])
        for item in report["reviews"]:
            self.assertEqual(f"visual_review:{item['packet_id']}", item["visual_review_id"])
            self.assertEqual("needs_human_review", item["review"]["verdict"])
            self.assertIn("dry-run", item["review"]["reason"])
            self.assertEqual(["unknown"], item["review"]["failure_tags"])
            self.assertEqual("unknown", item["review"]["root_cause_module"])
            self.assertEqual("none", item["review"]["tuning_direction"])
            self.assertEqual(item["packet_id"], item["review"]["source_packet_id"])
            self.assertEqual(item["visual_review_id"], item["review"]["visual_review_id"])
            self.assertEqual("ai_visual_review", item["review"]["provenance"]["source"])

    def test_bad_model_output_and_client_errors_are_recorded_without_accepting_packets(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_review_packets(
                output_dir,
                [
                    ("packet_001", "highlight_worthy"),
                    ("packet_002", "highlight_worthy"),
                    ("packet_003", "highlight_worthy"),
                    ("packet_004", "highlight_worthy"),
                ],
            )
            client = _FakeVisualReviewClient(
                [
                    _valid_review(verdict="publish_now"),
                    {"verdict": "accept_highlight", "confidence": 0.5},
                    _valid_review() | {"confidence": 1.5},
                    RuntimeError("provider timeout"),
                ]
            )

            report = build_ai_visual_review_report(output_dir, client=client)

        self.assertEqual(4, report["summary"]["packet_count"])
        self.assertEqual(0, report["summary"]["reviewed_count"])
        self.assertEqual(4, report["summary"]["error_count"])
        self.assertEqual(0, report["summary"]["accepted_highlight_count"])
        self.assertEqual([], [item for item in report["reviews"] if item.get("review")])
        self.assertEqual(4, len(report["errors"]))

    def test_media_paths_can_be_relative_to_repo_root(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory(dir=repo_root) as temp_name:
            output_dir = Path(temp_name)
            _write_review_packets(output_dir, [("packet_001", "highlight_worthy")], media_path_mode="repo")
            client = _FakeVisualReviewClient([_valid_review()])

            report = build_ai_visual_review_report(output_dir, client=client)

        self.assertEqual(1, report["summary"]["reviewed_count"])
        self.assertEqual(1, len(client.calls))
        self.assertTrue(str(client.calls[0]["contact_sheet_data_url"]).startswith("data:image/jpeg;base64,"))

    def test_media_paths_can_be_relative_to_output_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_review_packets(output_dir, [("packet_001", "highlight_worthy")], media_path_mode="output")
            client = _FakeVisualReviewClient([_valid_review()])

            report = build_ai_visual_review_report(output_dir, client=client)

        self.assertEqual(1, report["summary"]["reviewed_count"])
        self.assertEqual(1, len(client.calls))

    def test_media_paths_can_be_relative_to_python_backend_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            python_backend_root = Path(temp_name) / "python_backend"
            output_dir = python_backend_root / "outputs" / "run_001"
            _write_review_packets(
                output_dir,
                [("packet_001", "highlight_worthy")],
                media_path_mode="python_backend",
                python_backend_root=python_backend_root,
            )
            client = _FakeVisualReviewClient([_valid_review()])

            with patch("football_tracking.ai_visual_review._python_backend_root", return_value=python_backend_root):
                report = build_ai_visual_review_report(output_dir, client=client)

        self.assertEqual(1, report["summary"]["reviewed_count"])
        self.assertEqual(1, len(client.calls))
        self.assertTrue(str(client.calls[0]["contact_sheet_data_url"]).startswith("data:image/jpeg;base64,"))

    def test_provider_errors_are_redacted_before_being_written(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_review_packets(output_dir, [("packet_001", "highlight_worthy")])
            client = _FakeVisualReviewClient(
                [
                    RuntimeError(
                        "provider echoed Bearer sk-secret-token and "
                        "data:image/jpeg;base64,abcdef123456"
                    )
                ]
            )

            report = build_ai_visual_review_report(output_dir, client=client)

        error = report["errors"][0]["error"]
        self.assertNotIn("sk-secret-token", error)
        self.assertNotIn("abcdef123456", error)
        self.assertIn("<redacted", error)

    def test_optional_diagnostic_fields_are_validated_and_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_review_packets(output_dir, [("packet_001", "needs_ai_review")])
            client = _FakeVisualReviewClient(
                [
                    _valid_review(
                        verdict="needs_human_review",
                        highlight_publishable=False,
                        recommended_action="send_to_human",
                        extra={
                            "failure_tags": ["ball_lost", "camera_catchup_spike"],
                            "root_cause_module": "reacquisition",
                            "suggested_fixes": ["loosen ball recovery near the reacquire frame"],
                            "likely_ball_region": {
                                "frame": 14,
                                "description": "small white ball near the right touch line",
                                "confidence": 0.74,
                            },
                            "local_search_roi": {
                                "coordinate_space": "image",
                                "frame": 14,
                                "x": 320,
                                "y": 140,
                                "width": 96,
                                "height": 72,
                                "confidence": 0.81,
                            },
                            "best_subclip": {
                                "start_frame": 12,
                                "end_frame": 18,
                                "reason": "ball is visible before the marker drifts",
                            },
                            "tuning_direction": "retrack_segment",
                        },
                    )
                ]
            )

            report = build_ai_visual_review_report(output_dir, client=client)

        review = report["reviews"][0]["review"]
        self.assertEqual(["ball_lost", "camera_catchup_spike"], review["failure_tags"])
        self.assertEqual("reacquisition", review["root_cause_module"])
        self.assertEqual("retrack_segment", review["tuning_direction"])
        self.assertEqual(14, review["likely_ball_region"]["frame"])
        self.assertEqual("image", review["local_search_roi"]["coordinate_space"])
        self.assertEqual(12, review["best_subclip"]["start_frame"])
        self.assertEqual("packet_001", review["source_packet_id"])
        self.assertEqual("visual_review:packet_001", review["visual_review_id"])
        self.assertEqual(
            {
                "source": "ai_visual_review",
                "source_packet_id": "packet_001",
                "visual_review_id": "visual_review:packet_001",
            },
            review["provenance"],
        )

    def test_legacy_model_response_is_backfilled_with_v2_diagnostic_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_review_packets(output_dir, [("packet_001", "needs_ai_review")])
            client = _FakeVisualReviewClient([_legacy_review_only()])

            report = build_ai_visual_review_report(output_dir, client=client)

        review = report["reviews"][0]["review"]
        self.assertEqual([], review["failure_tags"])
        self.assertEqual("unknown", review["root_cause_module"])
        self.assertEqual([], review["suggested_fixes"])
        self.assertIsNone(review["likely_ball_region"])
        self.assertIsNone(review["local_search_roi"])
        self.assertIsNone(review["best_subclip"])
        self.assertEqual("none", review["tuning_direction"])
        self.assertEqual("packet_001", review["source_packet_id"])

    def test_invalid_optional_diagnostic_fields_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_review_packets(
                output_dir,
                [
                    ("packet_001", "needs_ai_review"),
                    ("packet_002", "needs_ai_review"),
                    ("packet_003", "needs_ai_review"),
                    ("packet_004", "needs_ai_review"),
                    ("packet_005", "needs_ai_review"),
                    ("packet_006", "needs_ai_review"),
                ],
            )
            client = _FakeVisualReviewClient(
                [
                    _valid_review(extra={"failure_tags": ["missing_ball"]}),
                    _valid_review(extra={"root_cause_module": "tracking"}),
                    _valid_review(extra={"tuning_direction": "speed_up"}),
                    _valid_review(
                        extra={
                            "local_search_roi": {
                                "coordinate_space": "field",
                                "frame": 1,
                                "x": 0,
                                "y": 0,
                                "width": 20,
                                "height": 20,
                                "confidence": 0.5,
                            }
                        }
                    ),
                    _valid_review(extra={"best_subclip": {"start_frame": 30, "end_frame": 20, "reason": "backwards"}}),
                    _valid_review(
                        extra={
                            "local_search_roi": {
                                "coordinate_space": "image",
                                "frame": 62,
                                "x": 620,
                                "y": 10,
                                "width": 40,
                                "height": 20,
                                "confidence": 0.5,
                            }
                        }
                    ),
                ]
            )

            report = build_ai_visual_review_report(output_dir, client=client)

        self.assertEqual(6, report["summary"]["error_count"])
        errors = " ".join(error["error"] for error in report["errors"])
        self.assertIn("failure_tags", errors)
        self.assertIn("root_cause_module", errors)
        self.assertIn("tuning_direction", errors)
        self.assertIn("local_search_roi", errors)
        self.assertIn("best_subclip", errors)
        self.assertIn("frame dimensions", errors)

    def test_openai_visual_review_schema_and_prompt_request_localization_from_packet_media(self) -> None:
        legacy_required = [
            "verdict",
            "confidence",
            "reason",
            "match_ball_visible",
            "marker_alignment",
            "highlight_publishable",
            "recommended_action",
            "visual_evidence",
        ]
        self.assertEqual(legacy_required, AI_VISUAL_REVIEW_RESPONSE_SCHEMA["required"][: len(legacy_required)])
        self.assertEqual(
            set(AI_VISUAL_REVIEW_RESPONSE_SCHEMA["properties"]),
            set(AI_VISUAL_REVIEW_RESPONSE_SCHEMA["required"]),
        )

        response_client = _CapturingResponsesClient(
            _valid_review(
                verdict="needs_human_review",
                highlight_publishable=False,
                recommended_action="send_to_human",
                extra={
                    "failure_tags": ["ball_lost"],
                    "root_cause_module": "reacquisition",
                    "suggested_fixes": ["retrack the missing-ball segment"],
                    "likely_ball_region": {"frame": 12, "description": "not visible", "confidence": 0.0},
                    "local_search_roi": None,
                    "best_subclip": None,
                    "tuning_direction": "retrack_segment",
                },
            )
        )
        client = OpenAIVisualReviewClient(response_client)

        client.review_packet(
            packet={},
            metadata={
                "packet_id": "packet_001",
                "packet_purpose": "diagnose_missing_ball",
                "suspected_failure_tags": ["ball_lost"],
                "root_cause_candidates": ["reacquisition"],
                "frame_dimensions": {"width": 640, "height": 360},
                "decision": {"label": "ball_not_visible"},
            },
            contact_sheet_data_url="data:image/jpeg;base64,contact",
            crop_sheet_data_url="data:image/jpeg;base64,crop",
            model="vision-test",
        )

        call = response_client.calls[0]
        image_labels = [image["label"] for image in call["images"]]
        self.assertEqual(["contact_sheet", "crop_sheet"], image_labels)
        self.assertIn("failure_tags", call["json_schema"]["properties"])
        self.assertIn("local_search_roi", call["json_schema"]["properties"])
        self.assertEqual(
            set(call["json_schema"]["properties"]),
            set(call["json_schema"]["required"]),
        )
        prompt_text = f"{call['instructions']} {call['prompt']}"
        self.assertIn("local_search_roi", prompt_text)
        self.assertIn("only when the ball is visible", prompt_text)
        self.assertIn("not visible", prompt_text)

    def test_run_ai_visual_review_cli_dry_run_writes_report_and_refreshes_metrics(self) -> None:
        from scripts.run_ai_visual_review import main

        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_review_packets(output_dir, [("packet_001", "highlight_worthy")])
            (output_dir / "metrics_report.json").write_text(json.dumps({"schema_version": "1.0"}), encoding="utf-8")

            with patch(
                "football_tracking.ai_visual_review._build_default_client",
                side_effect=AssertionError("dry-run must not create the OpenAI client"),
            ):
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    exit_code = main([str(output_dir), "--dry-run", "--max-packets", "1"])

            ai_report = json.loads((output_dir / "ai_visual_review.json").read_text(encoding="utf-8"))
            metrics_report = json.loads((output_dir / "metrics_report.json").read_text(encoding="utf-8"))
            printed_summary = json.loads(stdout.getvalue())

        self.assertEqual(0, exit_code)
        self.assertEqual(1, ai_report["summary"]["packet_count"])
        self.assertEqual(1, metrics_report["ai_visual_review"]["packet_count"])
        self.assertEqual(ai_report["summary"], printed_summary["ai_visual_review"])
        self.assertFalse((output_dir / "highlights_ai_accepted").exists())

    def test_cli_custom_accepted_dir_summary_wins_over_old_default_metrics(self) -> None:
        from scripts.run_ai_visual_review import main

        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_review_packets(output_dir, [("packet_001", "highlight_worthy")])
            (output_dir / "metrics_report.json").write_text(json.dumps({"schema_version": "1.0"}), encoding="utf-8")
            default_dir = output_dir / "highlights_ai_accepted"
            default_dir.mkdir()
            (default_dir / "ai_accepted_highlights_report.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "summary": {
                            "qualified_count": 99,
                            "copied_count": 99,
                            "planned_count": 0,
                            "skipped_count": 0,
                            "error_count": 0,
                        },
                    }
                ),
                encoding="utf-8",
            )

            def fake_write_ai(*args: object, **kwargs: object) -> dict[str, object]:
                return {"summary": {"packet_count": 1, "reviewed_count": 1, "error_count": 0}}

            def fake_write_accepted(*args: object, **kwargs: object) -> dict[str, object]:
                return {
                    "summary": {
                        "qualified_count": 1,
                        "copied_count": 1,
                        "planned_count": 0,
                        "skipped_count": 0,
                        "error_count": 0,
                    }
                }

            with (
                patch("football_tracking.ai_visual_review.write_ai_visual_review_report", side_effect=fake_write_ai),
                patch(
                    "football_tracking.accepted_highlights.write_accepted_highlights_report",
                    side_effect=fake_write_accepted,
                ),
            ):
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    exit_code = main([str(output_dir), "--accepted-dir-name", "custom_accepted"])

            metrics_report = json.loads((output_dir / "metrics_report.json").read_text(encoding="utf-8"))
            printed_summary = json.loads(stdout.getvalue())

        self.assertEqual(0, exit_code)
        self.assertEqual(1, metrics_report["accepted_highlights"]["copied_count"])
        self.assertEqual(1, printed_summary["accepted_highlights"]["copied_count"])

    def test_cli_rejects_current_directory_as_accepted_dir_name(self) -> None:
        from scripts.run_ai_visual_review import main

        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_review_packets(output_dir, [("packet_001", "highlight_worthy")])
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as context:
                    main([str(output_dir), "--dry-run", "--accepted-dir-name", "."])

        self.assertNotEqual(0, context.exception.code)


def _write_review_packets(
    output_dir: Path,
    packets: list[tuple[str, str]],
    *,
    media_path_mode: str = "absolute",
    python_backend_root: Path | None = None,
) -> None:
    packet_payloads: list[dict[str, object]] = []
    for index, (packet_id, label) in enumerate(packets, start=1):
        packet_dir = output_dir / "review_packets" / packet_id
        packet_dir.mkdir(parents=True, exist_ok=True)
        contact_sheet = packet_dir / "contact_sheet.jpg"
        crop_sheet = packet_dir / "crop_sheet.jpg"
        contact_sheet.write_bytes(b"fake-contact-jpeg")
        crop_sheet.write_bytes(b"fake-crop-jpeg")
        contact_media_path = contact_sheet
        crop_media_path = crop_sheet
        if media_path_mode == "repo":
            repo_root = Path(__file__).resolve().parents[2]
            contact_media_path = contact_sheet.resolve().relative_to(repo_root.resolve())
            crop_media_path = crop_sheet.resolve().relative_to(repo_root.resolve())
        elif media_path_mode == "python_backend":
            backend_root = python_backend_root or Path(__file__).resolve().parents[1]
            contact_media_path = contact_sheet.resolve().relative_to(backend_root.resolve())
            crop_media_path = crop_sheet.resolve().relative_to(backend_root.resolve())
        elif media_path_mode == "output":
            contact_media_path = contact_sheet.relative_to(output_dir)
            crop_media_path = crop_sheet.relative_to(output_dir)
        packet_payloads.append(
            {
                "packet_id": packet_id,
                "source": {"kind": "fixture", "index": index},
                "window": {"start_frame": index * 10, "end_frame": index * 10 + 4, "frame_count": 5},
                "track_summary": {"detected_ratio": 0.8, "lost_ratio": 0.0},
                "decision": {"label": label, "confidence": 0.7, "reason": f"fixture {label}"},
                "frame_dimensions": {"width": 640, "height": 360},
                "media": {"contact_sheet": str(contact_media_path), "crop_sheet": str(crop_media_path)},
                "media_warnings": [],
            }
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "review_packets.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "generated_at": "2026-01-01T00:00:00+00:00",
                "summary": {
                    "packet_count": len(packet_payloads),
                    "counts_by_label": {},
                    "media_packet_count": len(packet_payloads),
                },
                "packets": packet_payloads,
            }
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    unittest.main()
