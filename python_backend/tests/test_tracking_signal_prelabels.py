from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from football_tracking.tracking_signal_labels import TRACKING_SIGNAL_LABELS_REPORT_NAME, load_tracking_signal_labels
from football_tracking.tracking_signal_prelabels import (
    build_tracking_signal_prelabels,
    write_tracking_signal_prelabels,
)


class TrackingSignalPrelabelsTests(unittest.TestCase):
    def test_build_tracking_signal_prelabels_maps_review_artifacts_to_fact_labels(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_json(
                output_dir / "ball_audit.json",
                {
                    "review_events": [
                        _event("event:large", "large_jump", 10, 12, evidence={"max_step_px": 220.0}),
                        _event("event:lost", "lost_gap", 20, 25, evidence={"gap_frames": 6}),
                        _event("event:short", "short_tracklet", 30, 31, evidence={"length": 2}),
                        _event("event:ambiguous", "candidate_ambiguity", 40, 40, evidence={"score_delta": 0.02}),
                    ]
                },
            )
            _write_json(
                output_dir / "ai_review_triggers.json",
                {
                    "triggers": [
                        _event(
                            "trigger:dense",
                            "dense_noise_cluster",
                            50,
                            55,
                            reason="dense high-recall noise",
                            evidence={"peak_frames": [51, 54]},
                        )
                    ]
                },
            )
            _write_json(
                output_dir / "camera_motion_audit.json",
                {
                    "review_events": [
                        _event("camera:motion", "camera_motion_spike", 60, 60),
                        _event("camera:accel", "camera_acceleration_spike", 61, 61),
                        _event("camera:zoom", "camera_zoom_jump", 62, 62),
                    ]
                },
            )

            payload = build_tracking_signal_prelabels(output_dir)

        by_source_id = {label["evidence"][0]["source_id"]: label for label in payload["labels"]}
        self.assertEqual("ok", payload["summary"]["status"])
        self.assertEqual(("tracking_dynamics", "large_jump_after_reacquire"), _category_subtype(by_source_id["event:large"]))
        self.assertEqual("not_visible", by_source_id["event:lost"]["match_ball_state"])
        self.assertEqual(("tracking_dynamics", "lost_gap"), _category_subtype(by_source_id["event:lost"]))
        self.assertEqual(("tracking_dynamics", "short_false_tracklet"), _category_subtype(by_source_id["event:short"]))
        self.assertEqual(("tracking_dynamics", "candidate_ambiguity"), _category_subtype(by_source_id["event:ambiguous"]))
        self.assertEqual(("detector_artifact", "high_recall_noise_cluster"), _category_subtype(by_source_id["trigger:dense"]))
        self.assertEqual(("camera_motion", "camera_motion_spike"), _category_subtype(by_source_id["camera:motion"]))
        self.assertEqual(("camera_motion", "camera_acceleration_spike"), _category_subtype(by_source_id["camera:accel"]))
        self.assertEqual(("camera_motion", "camera_zoom_jump"), _category_subtype(by_source_id["camera:zoom"]))

        evidence = by_source_id["trigger:dense"]["evidence"][0]
        self.assertEqual("ai_review_triggers.json", evidence["source_artifact"])
        self.assertEqual(50, evidence["start_frame"])
        self.assertEqual(55, evidence["end_frame"])
        self.assertEqual("dense high-recall noise", evidence["reason"])
        self.assertEqual({"peak_frames": [51, 54]}, evidence["raw_evidence"])
        self.assertNotIn("action_eligibility", by_source_id["trigger:dense"])
        self.assertNotIn("recommended_action", by_source_id["trigger:dense"])

    def test_missing_review_artifacts_returns_stable_empty_labels(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            first = build_tracking_signal_prelabels(output_dir)
            second = build_tracking_signal_prelabels(output_dir)

        self.assertEqual(first["labels"], second["labels"])
        self.assertEqual([], first["labels"])
        self.assertEqual("empty", first["summary"]["status"])
        self.assertEqual([], first["validation_errors"])

    def test_written_prelabels_can_be_loaded_by_tracking_signal_label_loader(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_json(output_dir / "ball_audit.json", {"review_events": [_event("event:large", "large_jump", 10, 12)]})

            written = write_tracking_signal_prelabels(output_dir)
            loaded = load_tracking_signal_labels(output_dir)
            artifact_exists = (output_dir / TRACKING_SIGNAL_LABELS_REPORT_NAME).exists()

        self.assertTrue(artifact_exists)
        self.assertEqual(written["labels"], loaded["labels"])
        self.assertEqual("loaded", loaded["artifact_status"])
        self.assertNotEqual("error", loaded["summary"]["status"])
        self.assertEqual("ok", loaded["summary"]["status"])

    def test_write_prelabels_preserves_existing_ai_or_human_labels(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            existing_payload = {
                "schema_version": "1.0",
                "labels": [
                    {
                        "label_id": "human:keep",
                        "candidate_id": "human-candidate",
                        "match_ball_state": "not_match_ball",
                        "interference_category": "extra_ball",
                        "interference_subtype": "same_pitch_extra_ball",
                        "evidence": [{"source_artifact": "human_review"}],
                        "recommended_action": "reject_noise",
                        "human_note": "coach marked this as a spare ball",
                    }
                ],
            }
            _write_json(output_dir / TRACKING_SIGNAL_LABELS_REPORT_NAME, existing_payload)
            _write_json(output_dir / "ball_audit.json", {"review_events": [_event("event:large", "large_jump", 10, 12)]})

            write_tracking_signal_prelabels(output_dir)
            loaded = load_tracking_signal_labels(output_dir)

        by_label_id = {label["label_id"]: label for label in loaded["labels"]}
        self.assertIn("human:keep", by_label_id)
        self.assertTrue(any(label["interference_subtype"] == "large_jump_after_reacquire" for label in loaded["labels"]))
        self.assertNotIn("action_eligibility", by_label_id["human:keep"])
        self.assertEqual(
            {
                "human_note": "coach marked this as a spare ball",
                "recommended_action": "reject_noise",
            },
            by_label_id["human:keep"]["metadata"],
        )

    def test_duplicate_audit_and_trigger_events_are_deduped_by_source_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            audit_event = {
                "type": "large_jump",
                "start_frame": 1,
                "end_frame": 2,
                "reason": "large jump",
                "evidence": {"max_step_px": 240.0},
            }
            trigger_event = {
                "id": "event:0:large_jump:1-2",
                "type": "large_jump",
                "start_frame": 1,
                "end_frame": 2,
                "reason": "large jump",
                "evidence": {"max_step_px": 240.0},
            }
            _write_json(output_dir / "ball_audit.json", {"review_events": [audit_event]})
            _write_json(output_dir / "ai_review_triggers.json", {"triggers": [trigger_event]})

            payload = build_tracking_signal_prelabels(output_dir)

        matching = [
            label
            for label in payload["labels"]
            if label["evidence"][0]["event_type"] == "large_jump"
            and label["evidence"][0]["start_frame"] == 1
            and label["evidence"][0]["end_frame"] == 2
        ]
        self.assertEqual(1, len(matching))

    def test_invalid_review_artifacts_are_ignored_without_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            (output_dir / "ball_audit.json").write_text("{not-json", encoding="utf-8")
            _write_json(output_dir / "ai_review_triggers.json", {"triggers": {"bad": "shape"}})

            payload = build_tracking_signal_prelabels(output_dir)

        self.assertEqual([], payload["labels"])
        self.assertEqual("empty", payload["summary"]["status"])


def _event(
    event_id: str,
    event_type: str,
    start_frame: int,
    end_frame: int,
    *,
    reason: str | None = None,
    evidence: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "id": event_id,
        "type": event_type,
        "severity": "warn",
        "start_frame": start_frame,
        "end_frame": end_frame,
        "frame_count": end_frame - start_frame + 1,
        "reason": reason or f"{event_type} reason",
        "evidence": evidence or {"detail": event_type},
    }


def _category_subtype(label: dict[str, object]) -> tuple[str, str]:
    return str(label["interference_category"]), str(label["interference_subtype"])


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
