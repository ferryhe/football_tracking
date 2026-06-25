from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from football_tracking.tracking_signal_labels import (
    TRACKING_SIGNAL_LABELS_REPORT_NAME,
    action_eligibility,
    build_tracking_signal_labels,
    load_tracking_signal_labels,
    normalize_tracking_signal_label,
    write_tracking_signal_labels,
)


class TrackingSignalLabelsTests(unittest.TestCase):
    def test_load_missing_artifact_returns_stable_empty_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            first = load_tracking_signal_labels(Path(temp_name))
            second = load_tracking_signal_labels(Path(temp_name))

        self.assertEqual(first, second)
        self.assertEqual("1.0", first["schema_version"])
        self.assertIsNone(first["generated_at"])
        self.assertEqual("missing", first["artifact_status"])
        self.assertEqual([], first["labels"])
        self.assertEqual([], first["validation_errors"])
        self.assertEqual(
            {
                "status": "empty",
                "label_count": 0,
                "validation_error_count": 0,
                "counts_by_match_ball_state": {},
                "counts_by_interference_category": {},
            },
            first["summary"],
        )

    def test_write_and_load_valid_labels_with_four_layers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            payload = write_tracking_signal_labels(
                output_dir,
                labels=[
                    {
                        "label_id": "label-001",
                        "candidate_id": "candidate-001",
                        "match_ball_state": "confirmed_match_ball",
                        "interference_category": "none",
                        "interference_subtype": "none",
                        "evidence": [{"type": "frame", "frame": 101, "note": "ball visible inside ROI"}],
                    }
                ],
            )
            raw = (output_dir / TRACKING_SIGNAL_LABELS_REPORT_NAME).read_text(encoding="utf-8")
            loaded = load_tracking_signal_labels(output_dir)

        self.assertTrue(raw.endswith("\n"))
        self.assertEqual(payload, json.loads(raw))
        self.assertEqual("noise_interference_labels.json", TRACKING_SIGNAL_LABELS_REPORT_NAME)
        self.assertEqual("loaded", loaded["artifact_status"])
        self.assertEqual("ok", loaded["summary"]["status"])
        self.assertEqual(payload["labels"], loaded["labels"])
        self.assertEqual([], loaded["validation_errors"])
        self.assertIn("generated_at", loaded)
        self.assertEqual(
            {
                "label_id": "label-001",
                "candidate_id": "candidate-001",
                "match_ball_state": "confirmed_match_ball",
                "interference_category": "none",
                "interference_subtype": "none",
                "evidence": [{"type": "frame", "frame": 101, "note": "ball visible inside ROI"}],
            },
            loaded["labels"][0],
        )

    def test_invalid_ai_enums_degrade_without_raising_and_record_validation_errors(self) -> None:
        label = normalize_tracking_signal_label(
            {
                "label_id": "label-invalid",
                "candidate_id": "candidate-invalid",
                "match_ball_state": "definitely_the_ball",
                "interference_category": "ghost_shadow",
                "interference_subtype": "warp_zone",
                "evidence": "AI said so",
            }
        )

        self.assertEqual("unknown", label["match_ball_state"])
        self.assertEqual("unknown", label["interference_category"])
        self.assertEqual("unknown", label["interference_subtype"])
        self.assertEqual([], label["evidence"])
        self.assertEqual(4, len(label["validation_errors"]))

    def test_unknown_and_ambiguous_labels_fail_closed_for_all_actions(self) -> None:
        for state in ("unknown", "ambiguous"):
            with self.subTest(state=state):
                label = normalize_tracking_signal_label(
                    {
                        "label_id": f"label-{state}",
                        "match_ball_state": state,
                        "interference_category": "none",
                        "interference_subtype": "none",
                        "evidence": [{"type": "frame"}],
                    }
                )

                self.assertFalse(action_eligibility(label, "localize_ball_roi")["executable"])
                self.assertFalse(action_eligibility(label, "reject_noise")["executable"])
                self.assertEqual("review_only", action_eligibility(label, "localize_ball_roi")["mode"])

    def test_explicit_blockers_are_review_only(self) -> None:
        blockers = (
            ("roi_empty_turf", "none"),
            ("empty_turf_roi", "none"),
            ("candidate_elsewhere", "none"),
            ("coordinate_mapping_suspect", "none"),
        )
        for subtype, category in blockers:
            with self.subTest(subtype=subtype):
                label = normalize_tracking_signal_label(
                    {
                        "match_ball_state": "confirmed_match_ball",
                        "interference_category": category,
                        "interference_subtype": subtype,
                        "evidence": [{"type": "frame"}],
                    }
                )

                eligibility = action_eligibility(label, "localize_ball_roi")

                self.assertFalse(eligibility["executable"])
                self.assertEqual("review_only", eligibility["mode"])
                self.assertIn(subtype, eligibility["blocking_reasons"])

    def test_validation_errors_are_action_blockers(self) -> None:
        label = normalize_tracking_signal_label(
            {
                "match_ball_state": "confirmed_match_ball",
                "interference_category": "none",
                "interference_subtype": "none",
                "evidence": [{"type": "frame"}],
                "validation_errors": ["upstream parser was uncertain"],
            }
        )

        eligibility = action_eligibility(label, "localize_ball_roi")

        self.assertFalse(eligibility["executable"])
        self.assertIn("validation_errors", eligibility["blocking_reasons"])

    def test_localize_ball_roi_positive_requires_probable_or_confirmed_match_ball(self) -> None:
        for state in ("confirmed_match_ball", "probable_match_ball"):
            with self.subTest(state=state):
                label = normalize_tracking_signal_label(
                    {
                        "match_ball_state": state,
                        "interference_category": "none",
                        "interference_subtype": "none",
                        "evidence": [{"type": "frame"}],
                    }
                )

                eligibility = action_eligibility(label, "localize_ball_roi")

                self.assertTrue(eligibility["executable"])
                self.assertEqual("execute", eligibility["mode"])

    def test_reject_noise_positive_requires_not_match_ball(self) -> None:
        not_match_ball = normalize_tracking_signal_label(
            {
                "match_ball_state": "not_match_ball",
                "interference_category": "none",
                "interference_subtype": "none",
                "evidence": [{"type": "frame"}],
            }
        )
        clear_interference = normalize_tracking_signal_label(
            {
                "match_ball_state": "not_match_ball",
                "interference_category": "player_body",
                "interference_subtype": "foot",
                "evidence": [{"type": "frame"}],
            }
        )

        self.assertTrue(action_eligibility(not_match_ball, "reject_noise")["executable"])
        self.assertTrue(action_eligibility(clear_interference, "reject_noise")["executable"])

    def test_labels_describe_facts_and_action_eligibility_is_derived(self) -> None:
        label = normalize_tracking_signal_label(
            {
                "match_ball_state": "confirmed_match_ball",
                "interference_category": "none",
                "interference_subtype": "none",
                "evidence": [{"type": "frame"}],
                "recommended_action": "reject_noise",
            }
        )

        self.assertNotIn("approved_action", label)
        self.assertNotIn("action_eligibility", label)
        self.assertFalse(action_eligibility(label, "reject_noise")["executable"])
        self.assertTrue(action_eligibility(label, "localize_ball_roi")["executable"])

    def test_reviewed_enum_contract_uses_human_tracking_labels(self) -> None:
        label = normalize_tracking_signal_label(
            {
                "match_ball_state": "occluded",
                "interference_category": "media_roi",
                "interference_subtype": "candidate_elsewhere",
                "evidence": [{"type": "roi_distance", "distance_px": 120}],
            }
        )

        self.assertEqual("occluded", label["match_ball_state"])
        self.assertEqual("media_roi", label["interference_category"])
        self.assertEqual("candidate_elsewhere", label["interference_subtype"])
        self.assertFalse(action_eligibility(label, "localize_ball_roi")["executable"])

    def test_legacy_confirmed_and_probable_aliases_normalize_to_reviewed_contract(self) -> None:
        confirmed = normalize_tracking_signal_label(
            {
                "match_ball_state": "confirmed",
                "interference_category": "none",
                "interference_subtype": "none",
                "evidence": [],
            }
        )
        probable = normalize_tracking_signal_label(
            {
                "match_ball_state": "probable",
                "interference_category": "none",
                "interference_subtype": "none",
                "evidence": [],
            }
        )

        self.assertEqual("confirmed_match_ball", confirmed["match_ball_state"])
        self.assertEqual("probable_match_ball", probable["match_ball_state"])
        self.assertEqual([], confirmed.get("validation_errors", []))

    def test_probable_match_ball_with_body_interference_is_not_safe_to_reject(self) -> None:
        label = normalize_tracking_signal_label(
            {
                "match_ball_state": "probable_match_ball",
                "interference_category": "player_body",
                "interference_subtype": "foot",
                "evidence": [{"type": "frame"}],
            }
        )

        eligibility = action_eligibility(label, "reject_noise")

        self.assertFalse(eligibility["executable"])
        self.assertIn("match_ball_not_rejectable", eligibility["blocking_reasons"])

    def test_build_payload_summarizes_validation_errors_from_degraded_labels(self) -> None:
        payload = build_tracking_signal_labels(
            Path("out"),
            labels=[
                {
                    "match_ball_state": "teleported",
                    "interference_category": "none",
                    "interference_subtype": "none",
                    "evidence": [{"type": "frame"}],
                }
            ],
        )

        self.assertEqual("warn", payload["summary"]["status"])
        self.assertEqual(1, payload["summary"]["validation_error_count"])
        self.assertEqual(payload["labels"][0]["validation_errors"], payload["validation_errors"])

    def test_deprecated_or_ambiguous_values_warn_when_downgraded(self) -> None:
        payload = build_tracking_signal_labels(
            Path("out"),
            labels=[
                {
                    "match_ball_state": "confirmed_match_ball",
                    "interference_category": "ambiguous",
                    "interference_subtype": "other",
                    "evidence": [{"type": "frame"}],
                }
            ],
        )

        label = payload["labels"][0]
        self.assertEqual("unknown", label["interference_category"])
        self.assertEqual("unknown", label["interference_subtype"])
        self.assertEqual("warn", payload["summary"]["status"])
        self.assertEqual(2, payload["summary"]["validation_error_count"])

    def test_reject_noise_clear_interference_without_not_match_ball_fails_closed(self) -> None:
        label = normalize_tracking_signal_label(
            {
                "match_ball_state": "occluded",
                "interference_category": "player_body",
                "interference_subtype": "foot",
                "evidence": [{"type": "frame"}],
            }
        )

        eligibility = action_eligibility(label, "reject_noise")

        self.assertFalse(eligibility["executable"])
        self.assertIn("match_ball_not_rejectable", eligibility["blocking_reasons"])

    def test_not_visible_tracking_dynamics_is_not_noise_rejectable(self) -> None:
        label = normalize_tracking_signal_label(
            {
                "match_ball_state": "not_visible",
                "interference_category": "tracking_dynamics",
                "interference_subtype": "lost_gap",
                "evidence": [{"type": "audit_event"}],
            }
        )

        eligibility = action_eligibility(label, "reject_noise")

        self.assertFalse(eligibility["executable"])
        self.assertIn("match_ball_not_rejectable", eligibility["blocking_reasons"])

    def test_fail_closed_subtypes_block_reject_noise_too(self) -> None:
        for subtype in ("roi_empty_turf", "empty_turf_roi", "candidate_elsewhere", "coordinate_mapping_suspect"):
            with self.subTest(subtype=subtype):
                label = normalize_tracking_signal_label(
                    {
                        "match_ball_state": "not_match_ball",
                        "interference_category": "media_roi",
                        "interference_subtype": subtype,
                        "evidence": [{"type": "frame"}],
                    }
                )

                eligibility = action_eligibility(label, "reject_noise")

                self.assertFalse(eligibility["executable"])
                self.assertEqual("review_only", eligibility["mode"])
                self.assertIn(subtype, eligibility["blocking_reasons"])

    def test_load_malformed_labels_artifact_warns_instead_of_silently_dropping(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            (output_dir / TRACKING_SIGNAL_LABELS_REPORT_NAME).write_text(
                json.dumps({"labels": [{"match_ball_state": "confirmed_match_ball"}, "bad-label"]}),
                encoding="utf-8",
            )

            payload = load_tracking_signal_labels(output_dir)

        self.assertEqual("loaded", payload["artifact_status"])
        self.assertEqual("warn", payload["summary"]["status"])
        self.assertTrue(any("labels[1]" in error for error in payload["validation_errors"]))

    def test_load_non_list_labels_artifact_warns(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            (output_dir / TRACKING_SIGNAL_LABELS_REPORT_NAME).write_text(
                json.dumps({"labels": {"match_ball_state": "confirmed_match_ball"}}),
                encoding="utf-8",
            )

            payload = load_tracking_signal_labels(output_dir)

        self.assertEqual("warn", payload["summary"]["status"])
        self.assertTrue(any("labels must be a list" in error for error in payload["validation_errors"]))

    def test_non_json_evidence_values_warn_and_are_dropped_before_write(self) -> None:
        payload = build_tracking_signal_labels(
            Path("out"),
            labels=[
                {
                    "match_ball_state": "confirmed_match_ball",
                    "interference_category": "none",
                    "interference_subtype": "none",
                    "evidence": [{"type": "frame", "bad": {"not-json"}}],
                }
            ],
        )

        self.assertEqual("warn", payload["summary"]["status"])
        self.assertEqual([{"type": "frame"}], payload["labels"][0]["evidence"])
        self.assertTrue(any("evidence" in error for error in payload["validation_errors"]))

    def test_report_name_rejects_windows_reserved_or_unsafe_names(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            for report_name in ("CON.json", "bad:name.json", "nested/report.json"):
                with self.subTest(report_name=report_name):
                    with self.assertRaises(ValueError):
                        write_tracking_signal_labels(Path(temp_name), report_name=report_name)


if __name__ == "__main__":
    unittest.main()
