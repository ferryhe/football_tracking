from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from football_tracking.tracking_benchmark import (
    BENCHMARK_REPORT_NAME,
    build_benchmark_report,
    load_benchmark_report,
    write_benchmark_report,
)


class TrackingBenchmarkTests(unittest.TestCase):
    def test_computes_candidate_selective_and_broadcast_coverage_metrics(self) -> None:
        report = build_benchmark_report(
            candidate_evaluations=[
                {
                    "candidate_id": "a",
                    "truth": "match_ball",
                    "truth_origin": "human_confirmed",
                    "decision": "accept",
                    "confidence": 0.99,
                },
                {
                    "candidate_id": "b",
                    "truth": "noise",
                    "truth_origin": "human_confirmed",
                    "decision": "accept",
                    "confidence": 0.95,
                },
                {
                    "candidate_id": "c",
                    "truth": "match_ball",
                    "truth_origin": "human_confirmed",
                    "decision": "reject",
                    "confidence": 0.9,
                },
                {
                    "candidate_id": "d",
                    "truth": "noise",
                    "truth_origin": "ai_confirmed",
                    "decision": "reject",
                    "confidence": 0.8,
                },
                {
                    "candidate_id": "e",
                    "truth": "match_ball",
                    "truth_origin": "ai_confirmed",
                    "decision": "abstain",
                    "confidence": 0.5,
                },
            ],
            frame_evaluations=[
                {
                    "frame_index": 0,
                    "ball_visible": True,
                    "ball_in_frame": True,
                    "key_action": True,
                    "action_in_frame": True,
                },
                {
                    "frame_index": 1,
                    "ball_visible": True,
                    "ball_in_frame": False,
                    "key_action": True,
                    "action_in_frame": False,
                },
                {
                    "frame_index": 2,
                    "ball_visible": False,
                    "ball_in_frame": False,
                    "key_action": False,
                    "action_in_frame": False,
                },
            ],
        )

        metrics = report["metrics"]
        self.assertEqual(
            {"value": 0.5, "numerator": 1, "denominator": 2, "available": True},
            metrics["auto_accepted_candidate_precision"],
        )
        self.assertEqual(
            {"value": 1 / 3, "numerator": 1, "denominator": 3, "available": True},
            metrics["true_ball_false_reject_rate"],
        )
        self.assertEqual(
            {"value": 0.8, "numerator": 4, "denominator": 5, "available": True}, metrics["selective_coverage"]
        )
        self.assertEqual({"value": 0.5, "numerator": 2, "denominator": 4, "available": True}, metrics["selective_risk"])
        self.assertEqual(
            {"value": 0.5, "numerator": 1, "denominator": 2, "available": True}, metrics["visible_ball_in_frame"]
        )
        self.assertEqual(
            {"value": 0.5, "numerator": 1, "denominator": 2, "available": True}, metrics["action_coverage"]
        )
        self.assertEqual(5, len(report["risk_coverage_curve"]))
        self.assertEqual(0.99, report["risk_coverage_curve"][0]["threshold"])
        self.assertEqual(0.2, report["risk_coverage_curve"][0]["coverage"])
        self.assertEqual(0.0, report["risk_coverage_curve"][0]["risk"])

    def test_zero_denominators_are_explicitly_unavailable(self) -> None:
        report = build_benchmark_report(candidate_evaluations=[], frame_evaluations=[])

        for metric in report["metrics"].values():
            self.assertEqual({"value": None, "numerator": 0, "denominator": 0, "available": False}, metric)
        self.assertEqual([], report["risk_coverage_curve"])
        self.assertEqual("empty", report["summary"]["status"])

    def test_prelabels_are_excluded_from_ground_truth_metrics(self) -> None:
        report = build_benchmark_report(
            candidate_evaluations=[
                {
                    "candidate_id": "prelabel-only",
                    "truth": "match_ball",
                    "truth_origin": "prelabel",
                    "decision": "reject",
                    "confidence": 0.99,
                },
                {
                    "candidate_id": "human",
                    "truth": "match_ball",
                    "truth_origin": "human_confirmed",
                    "decision": "accept",
                    "confidence": 0.9,
                },
            ]
        )

        self.assertEqual(1, report["summary"]["excluded_prelabel_count"])
        self.assertEqual(1.0, report["metrics"]["auto_accepted_candidate_precision"]["value"])
        self.assertEqual(0.0, report["metrics"]["true_ball_false_reject_rate"]["value"])

    def test_missing_truth_origin_is_invalid_instead_of_assumed_confirmed(self) -> None:
        report = build_benchmark_report(
            candidate_evaluations=[
                {
                    "candidate_id": "ambiguous-origin",
                    "truth": "match_ball",
                    "decision": "accept",
                    "confidence": 0.9,
                }
            ]
        )

        self.assertEqual("invalid", report["summary"]["status"])
        self.assertEqual([], report["candidate_evaluations"])
        self.assertIn("truth_origin", report["validation_errors"][0])

    def test_duplicate_candidate_and_frame_ids_are_rejected_without_inflating_metrics(self) -> None:
        report = build_benchmark_report(
            candidate_evaluations=[
                {
                    "candidate_id": "duplicate",
                    "truth": "match_ball",
                    "truth_origin": "human_confirmed",
                    "decision": "accept",
                    "confidence": 0.9,
                },
                {
                    "candidate_id": "duplicate",
                    "truth": "noise",
                    "truth_origin": "human_confirmed",
                    "decision": "accept",
                    "confidence": 0.8,
                },
            ],
            frame_evaluations=[
                {
                    "frame_index": 1,
                    "ball_visible": True,
                    "ball_in_frame": True,
                    "key_action": True,
                    "action_in_frame": True,
                },
                {
                    "frame_index": 1,
                    "ball_visible": True,
                    "ball_in_frame": False,
                    "key_action": True,
                    "action_in_frame": False,
                },
            ],
        )

        self.assertEqual("invalid", report["summary"]["status"])
        self.assertEqual(1, len(report["candidate_evaluations"]))
        self.assertEqual(1, len(report["frame_evaluations"]))
        self.assertEqual(1, report["metrics"]["auto_accepted_candidate_precision"]["denominator"])
        self.assertEqual(1, report["metrics"]["visible_ball_in_frame"]["denominator"])
        self.assertTrue(any("duplicate candidate_id" in error for error in report["validation_errors"]))
        self.assertTrue(any("duplicate frame_index" in error for error in report["validation_errors"]))

    def test_risk_coverage_curve_groups_equal_thresholds_and_preserves_abstain_thresholds(self) -> None:
        report = build_benchmark_report(
            candidate_evaluations=[
                {
                    "candidate_id": "a",
                    "truth": "match_ball",
                    "truth_origin": "human_confirmed",
                    "decision": "accept",
                    "confidence": 0.9,
                },
                {
                    "candidate_id": "b",
                    "truth": "noise",
                    "truth_origin": "human_confirmed",
                    "decision": "reject",
                    "confidence": 0.9,
                },
                {
                    "candidate_id": "c",
                    "truth": "match_ball",
                    "truth_origin": "human_confirmed",
                    "decision": "abstain",
                    "confidence": 0.7,
                },
            ]
        )

        self.assertEqual([0.9, 0.7], [point["threshold"] for point in report["risk_coverage_curve"]])
        self.assertEqual([2, 2], [point["selected_count"] for point in report["risk_coverage_curve"]])
        self.assertEqual([2 / 3, 2 / 3], [point["coverage"] for point in report["risk_coverage_curve"]])

    def test_write_and_load_report_validate_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            written = write_benchmark_report(output_dir, candidate_evaluations=[], frame_evaluations=[])
            raw = (output_dir / BENCHMARK_REPORT_NAME).read_text(encoding="utf-8")
            loaded = load_benchmark_report(output_dir)
            invalid_path = output_dir / "invalid.json"
            invalid_path.write_text(json.dumps({"schema_version": "wrong", "metrics": []}), encoding="utf-8")
            invalid = load_benchmark_report(invalid_path)

        self.assertTrue(raw.endswith("\n"))
        self.assertEqual(written, json.loads(raw))
        self.assertEqual("loaded", loaded["artifact_status"])
        self.assertEqual(written["metrics"], loaded["metrics"])
        self.assertEqual("invalid", invalid["artifact_status"])
        self.assertEqual("invalid", invalid["summary"]["status"])

    def test_correct_version_skeletal_or_malformed_envelope_is_invalid(self) -> None:
        malformed_payloads = [
            {"schema_version": "1.0"},
            {
                "schema_version": "1.0",
                "generated_at": 123,
                "summary": [],
                "metrics": [],
                "risk_coverage_curve": {},
                "candidate_evaluations": [],
                "frame_evaluations": [],
                "validation_errors": {},
            },
        ]
        with tempfile.TemporaryDirectory() as temp_name:
            results = []
            for index, payload in enumerate(malformed_payloads):
                path = Path(temp_name) / f"malformed-{index}.json"
                path.write_text(json.dumps(payload), encoding="utf-8")
                results.append(load_benchmark_report(path))

        for result in results:
            self.assertEqual("invalid", result["artifact_status"])
            self.assertEqual("invalid", result["summary"]["status"])
            self.assertGreater(len(result["validation_errors"]), 0)

    def test_load_preserves_validation_errors_from_written_invalid_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            written = write_benchmark_report(
                output_dir,
                candidate_evaluations=[
                    {
                        "candidate_id": "bad",
                        "truth": "match_ball",
                        "truth_origin": "human_confirmed",
                        "decision": "guess",
                        "confidence": 0.9,
                    }
                ],
            )
            loaded = load_benchmark_report(output_dir)

        self.assertEqual("invalid", written["summary"]["status"])
        self.assertEqual("invalid", loaded["artifact_status"])
        self.assertEqual(written["validation_errors"], loaded["validation_errors"])


if __name__ == "__main__":
    unittest.main()
