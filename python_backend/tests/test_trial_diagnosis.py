from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from football_tracking.api.schemas import TrialSignalGateV2
from football_tracking.config import load_config
from football_tracking.detector_candidate_contract import (
    assign_candidate_ids,
    candidate_to_contract_record,
)
from football_tracking.metrics import build_metrics_report, stats_from_metrics_report
from football_tracking.trial_diagnosis import (
    TRIAL_SIGNAL_GATE_SCHEMA_VERSION,
    TRIAL_SIGNAL_THRESHOLD_PROFILE,
    TRIAL_TUNING_SCHEMA_VERSION,
    _evidence_status,
    build_trial_diagnosis,
    build_trial_signal_gate_v2,
    collect_trial_stage_counts,
    normalize_production_trial_config_patch,
    production_tuning_values_sha256,
    trial_tuning_schema,
    validate_production_trial_config_patch,
)
from football_tracking.types import Candidate


def _track(**changes: object) -> dict[str, object]:
    result: dict[str, object] = {
        "frame_count": 100,
        "detected": 80,
        "predicted": 10,
        "lost": 10,
        "detected_ratio": 0.8,
        "predicted_ratio": 0.1,
        "lost_ratio": 0.1,
        "longest_lost_streak": 5,
        "false_positive_island_count": 1,
        "max_step_px": 120.0,
    }
    result.update(changes)
    return result


def _stage(**changes: object) -> dict[str, object]:
    result: dict[str, object] = {
        "schema_version": "2.0",
        "coverage_status": "complete",
        "evaluated_frames": {"value": 100, "status": "collected"},
        "detected_frames": {"value": 80, "status": "collected"},
        "predicted_frames": {"value": 10, "status": "collected"},
        "lost_frames": {"value": 10, "status": "collected"},
        "raw_candidates": {"value": 120, "status": "collected"},
        "class_mapped_candidates": {"value": 120, "status": "collected"},
        "filtered_candidates": {"value": 90, "status": "collected"},
        "selected_candidates": {"value": 80, "status": "collected"},
        "tracklets": {"value": 4, "status": "collected"},
        "rejection_reasons": {"too_small": 30},
        "reconciliation": {"status": "reconciled", "reason_codes": []},
    }
    result.update(changes)
    return result


def _debug_stage_evidence(
    class_mapped_count: int,
    *,
    detector_output_count: int | None = None,
    class_rejection_counts: dict[str, int] | None = None,
    frame_exception: bool = False,
    selected_candidate_count: int = 0,
) -> dict[str, object]:
    return {
        "detector_stage_schema_version": "1.0",
        "detector_output_count": (class_mapped_count if detector_output_count is None else detector_output_count),
        "class_mapped_candidate_count": class_mapped_count,
        "class_rejection_counts": class_rejection_counts or {},
        "frame_exception": frame_exception,
        "selected_candidate_count": selected_candidate_count,
    }


def _status_track(*, detected: int, predicted: int, lost: int) -> dict[str, object]:
    frame_count = detected + predicted + lost
    return _track(
        frame_count=frame_count,
        detected=detected,
        predicted=predicted,
        lost=lost,
        detected_ratio=round(detected / frame_count, 4) if frame_count else 0.0,
        predicted_ratio=round(predicted / frame_count, 4) if frame_count else 0.0,
        lost_ratio=round(lost / frame_count, 4) if frame_count else 0.0,
    )


def _runtime_candidate_records(frame_indexes: list[int]) -> list[dict[str, object]]:
    candidates = [
        Candidate(
            frame_index=frame_index,
            x1=float(index * 10),
            y1=10.0,
            x2=float(index * 10 + 4),
            y2=14.0,
            confidence=0.75,
            label="sports ball",
            source="yolo_direct",
        )
        for index, frame_index in enumerate(frame_indexes, start=1)
    ]
    assign_candidate_ids(candidates, hashlib.sha256(b"trial-source").hexdigest())
    return [candidate_to_contract_record(candidate) for candidate in candidates]


def _gate(
    *,
    run_status: str = "completed",
    raw: dict[str, object] | None = None,
    cleaned: dict[str, object] | None = None,
    stage: dict[str, object] | None = None,
    decoder_failure: bool = False,
    audit: dict[str, object] | None = None,
    raw_tracklets: int | None = None,
    acceptance_contract_complete: bool = True,
) -> dict[str, object]:
    audit_summary: dict[str, object] | None = None
    if audit is not None:
        audit_summary = {
            "tracklet_count": 4,
            "suspicious_tracklet_count": 0,
            "review_event_count": 0,
            "lost_gap_count": 0,
            **audit,
        }
    return build_trial_signal_gate_v2(
        run_status=run_status,
        raw_track=raw,
        cleaned_track=raw if cleaned is None else cleaned,
        stage_counts=stage,
        audit_summary=audit_summary,
        raw_tracklet_count=(
            raw_tracklets
            if raw_tracklets is not None
            else audit_summary.get("tracklet_count")
            if isinstance(audit_summary, dict)
            else None
        ),
        follow_cam_summary={
            "camera_motion_audit": {
                "summary": {
                    "max_pan_step_px": 12.0,
                    "max_pan_accel_px": 18.0,
                    "max_zoom_step_ratio": 0.02,
                }
            }
        },
        decoder_failure=decoder_failure,
        evidence={
            "wide_context": "available",
            "tight_crop": "available",
            "follow_cam": "available",
            "follow_cam_action_retention": "complete",
            "scale_strata": "complete",
            "lighting_strata": "complete",
            "attack_transition_windows": "complete",
            "media_integrity": "complete",
            "identity_binding": "complete",
        },
        ai_review_summary={"trigger_count": 0},
        event_summary={"candidate_count": 0},
        acceptance_contract_complete=acceptance_contract_complete,
    )


class TrialSignalGateTests(unittest.TestCase):
    def test_unknown_and_not_collected_fail_closed(self) -> None:
        gate = _gate(raw=None, stage=None, audit=None)

        self.assertEqual(TRIAL_SIGNAL_GATE_SCHEMA_VERSION, gate["schema_version"])
        self.assertEqual("insufficient_evidence", gate["status"])
        self.assertFalse(gate["coverage_complete"])
        self.assertFalse(gate["quality_acceptable"])
        self.assertEqual("insufficient_evidence", gate["failure_classification"]["code"])
        self.assertIn("metrics_not_collected", gate["reason_codes"])
        self.assertIn("stage_counts_not_collected", gate["reason_codes"])

    def test_zero_evaluated_frames_is_insufficient_not_zero_signal(self) -> None:
        raw = _track(
            frame_count=0,
            detected=0,
            predicted=0,
            lost=0,
            detected_ratio=0.0,
            predicted_ratio=0.0,
            lost_ratio=0.0,
        )
        stage = _stage(
            evaluated_frames={"value": 0, "status": "collected"},
            detected_frames={"value": 0, "status": "collected"},
            predicted_frames={"value": 0, "status": "collected"},
            lost_frames={"value": 0, "status": "collected"},
            raw_candidates={"value": 0, "status": "collected"},
            class_mapped_candidates={"value": 0, "status": "collected"},
            filtered_candidates={"value": 0, "status": "collected"},
            selected_candidates={"value": 0, "status": "collected"},
            tracklets={"value": 0, "status": "collected"},
        )
        gate = _gate(raw=raw, stage=stage, audit={"tracklet_count": 0, "suspicious_tracklet_count": 0})

        self.assertEqual("insufficient_evidence", gate["status"])
        self.assertIn("evaluated_frames_zero", gate["reason_codes"])
        self.assertNotIn("zero_candidate", gate["reason_codes"])

    def test_decoder_failure_has_a_stable_classification(self) -> None:
        gate = _gate(
            run_status="failed",
            raw=None,
            stage=None,
            audit=None,
            decoder_failure=True,
        )

        self.assertEqual("insufficient_evidence", gate["status"])
        self.assertEqual("decode_failure", gate["failure_classification"]["code"])
        self.assertIn("decode_failure", gate["reason_codes"])

    def test_zero_candidate_zero_tracklet_and_all_lost_are_all_reported(self) -> None:
        raw = _track(
            detected=0,
            predicted=0,
            lost=100,
            detected_ratio=0.0,
            predicted_ratio=0.0,
            lost_ratio=1.0,
            longest_lost_streak=100,
            false_positive_island_count=0,
            max_step_px=None,
        )
        stage = _stage(
            detected_frames={"value": 0, "status": "collected"},
            predicted_frames={"value": 0, "status": "collected"},
            lost_frames={"value": 100, "status": "collected"},
            raw_candidates={"value": 0, "status": "collected"},
            class_mapped_candidates={"value": 0, "status": "collected"},
            filtered_candidates={"value": 0, "status": "collected"},
            selected_candidates={"value": 0, "status": "collected"},
            tracklets={"value": 0, "status": "collected"},
        )
        gate = _gate(raw=raw, stage=stage, audit={"tracklet_count": 0, "suspicious_tracklet_count": 0})

        self.assertEqual("retune_required", gate["status"])
        self.assertTrue(gate["coverage_complete"])
        self.assertFalse(gate["quality_acceptable"])
        self.assertEqual("no_raw_candidates", gate["failure_classification"]["code"])
        self.assertEqual(
            ["zero_candidate", "zero_tracklet", "all_lost"],
            [reason for reason in gate["reason_codes"] if reason in {"zero_candidate", "zero_tracklet", "all_lost"}],
        )

    def test_candidates_filtered_classification_precedes_tracklet_failure(self) -> None:
        raw = _track(
            detected=0,
            predicted=0,
            lost=100,
            detected_ratio=0.0,
            predicted_ratio=0.0,
            lost_ratio=1.0,
            longest_lost_streak=100,
        )
        stage = _stage(
            detected_frames={"value": 0, "status": "collected"},
            predicted_frames={"value": 0, "status": "collected"},
            lost_frames={"value": 100, "status": "collected"},
            raw_candidates={"value": 40, "status": "collected"},
            class_mapped_candidates={"value": 40, "status": "collected"},
            filtered_candidates={"value": 0, "status": "collected"},
            selected_candidates={"value": 0, "status": "collected"},
            tracklets={"value": 0, "status": "collected"},
        )
        gate = _gate(raw=raw, stage=stage, audit={"tracklet_count": 0, "suspicious_tracklet_count": 0})

        self.assertEqual("all_candidates_filtered", gate["failure_classification"]["code"])

    def test_non_ball_model_outputs_are_class_rejected_not_inconsistent(self) -> None:
        raw = _track(
            detected=0,
            predicted=0,
            lost=100,
            detected_ratio=0.0,
            predicted_ratio=0.0,
            lost_ratio=1.0,
            longest_lost_streak=100,
        )
        stage = _stage(
            detected_frames={"value": 0, "status": "collected"},
            predicted_frames={"value": 0, "status": "collected"},
            lost_frames={"value": 100, "status": "collected"},
            raw_candidates={"value": 40, "status": "collected"},
            class_mapped_candidates={"value": 0, "status": "collected"},
            filtered_candidates={"value": 0, "status": "collected"},
            selected_candidates={"value": 0, "status": "collected"},
            tracklets={"value": 0, "status": "collected"},
            rejection_reasons={"class_not_allowed:person": 40},
        )

        gate = _gate(raw=raw, stage=stage, audit={"tracklet_count": 0, "suspicious_tracklet_count": 0})

        self.assertTrue(gate["coverage_complete"])
        self.assertEqual("retune_required", gate["status"])
        self.assertEqual(
            "all_candidates_class_rejected",
            gate["failure_classification"]["code"],
        )
        self.assertIn("all_candidates_class_rejected", gate["reason_codes"])
        self.assertNotIn("class_mapped_candidate_count_mismatch", gate["reason_codes"])

    def test_partial_noisy_and_unstable_trajectories_fail_closed(self) -> None:
        partial = _track(
            detected=35,
            predicted=10,
            lost=55,
            detected_ratio=0.35,
            predicted_ratio=0.1,
            lost_ratio=0.55,
            longest_lost_streak=20,
        )
        noisy = _track(false_positive_island_count=15)
        unstable = _track(max_step_px=900.0)

        partial_gate = _gate(
            raw=partial,
            stage=_stage(
                detected_frames={"value": 35, "status": "collected"},
                predicted_frames={"value": 10, "status": "collected"},
                lost_frames={"value": 55, "status": "collected"},
                selected_candidates={"value": 35, "status": "collected"},
            ),
            audit={"tracklet_count": 4, "suspicious_tracklet_count": 0},
        )
        noisy_gate = _gate(
            raw=noisy,
            stage=_stage(tracklets={"value": 20, "status": "collected"}),
            audit={"tracklet_count": 20, "suspicious_tracklet_count": 10},
        )
        unstable_gate = _gate(raw=unstable, stage=_stage(), audit={"tracklet_count": 4, "suspicious_tracklet_count": 0})

        self.assertEqual("unstable_tracking", partial_gate["failure_classification"]["code"])
        self.assertIn("partial_signal", partial_gate["reason_codes"])
        self.assertEqual("wrong_or_noisy_candidates", noisy_gate["failure_classification"]["code"])
        self.assertIn("trajectory_noisy", noisy_gate["reason_codes"])
        self.assertEqual("unstable_tracking", unstable_gate["failure_classification"]["code"])
        self.assertIn("trajectory_unstable", unstable_gate["reason_codes"])

    def test_acceptable_trajectory_uses_versioned_threshold_profile(self) -> None:
        gate = _gate(raw=_track(), stage=_stage(), audit={"tracklet_count": 4, "suspicious_tracklet_count": 0})

        self.assertEqual("acceptable", gate["status"])
        self.assertTrue(gate["coverage_complete"])
        self.assertTrue(gate["quality_acceptable"])
        self.assertTrue(gate["operator_confirmation_required"])
        self.assertEqual("acceptable", gate["failure_classification"]["code"])
        self.assertEqual(TRIAL_SIGNAL_THRESHOLD_PROFILE["version"], gate["threshold_profile"]["version"])
        self.assertEqual(
            TRIAL_SIGNAL_THRESHOLD_PROFILE["thresholds"]["maximum_predicted_ratio"],
            gate["threshold_profile"]["thresholds"]["maximum_predicted_ratio"],
        )
        self.assertRegex(gate["threshold_profile"]["sha256"], r"^[0-9a-f]{64}$")
        profile_without_sha = {key: value for key, value in gate["threshold_profile"].items() if key != "sha256"}
        expected_sha = hashlib.sha256(
            json.dumps(
                profile_without_sha,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        self.assertEqual(expected_sha, gate["threshold_profile"]["sha256"])
        self.assertEqual(
            TRIAL_SIGNAL_THRESHOLD_PROFILE["algorithm_version"],
            gate["threshold_profile"]["algorithm_version"],
        )
        self.assertEqual(
            TRIAL_SIGNAL_THRESHOLD_PROFILE["matching_rules"],
            gate["threshold_profile"]["matching_rules"],
        )
        self.assertIn("quality_thresholds_passed", gate["reason_codes"])

    def test_acceptance_contract_is_explicit_and_fails_closed_by_default(self) -> None:
        gate = _gate(
            raw=_track(),
            stage=_stage(),
            audit={"tracklet_count": 4, "suspicious_tracklet_count": 0},
            acceptance_contract_complete=False,
        )

        self.assertEqual("insufficient_evidence", gate["status"])
        self.assertFalse(gate["acceptance_contract_complete"])
        self.assertFalse(gate["quality_acceptable"])
        self.assertIn("acceptance_contract_not_collected", gate["reason_codes"])

    def test_cleaned_trajectory_must_pass_the_same_frozen_thresholds(self) -> None:
        cases = {
            "detected_ratio": _track(
                detected=40,
                predicted=50,
                lost=10,
                detected_ratio=0.4,
                predicted_ratio=0.5,
                lost_ratio=0.1,
            ),
            "predicted_ratio": _track(
                detected=50,
                predicted=40,
                lost=10,
                detected_ratio=0.5,
                predicted_ratio=0.4,
                lost_ratio=0.1,
            ),
            "lost_ratio": _track(
                detected=60,
                predicted=10,
                lost=30,
                detected_ratio=0.6,
                predicted_ratio=0.1,
                lost_ratio=0.3,
            ),
            "longest_lost_streak": _track(longest_lost_streak=31),
            "false_positive_islands": _track(false_positive_island_count=9),
            "max_step": _track(max_step_px=601.0),
        }

        for name, cleaned in cases.items():
            gate = _gate(
                raw=_track(),
                cleaned=cleaned,
                stage=_stage(),
                audit={"tracklet_count": 4, "suspicious_tracklet_count": 0},
            )
            with self.subTest(metric=name):
                self.assertEqual("retune_required", gate["status"])
                self.assertFalse(gate["trajectory_acceptable"])
                self.assertFalse(gate["quality_acceptable"])

    def test_counter_mismatch_is_insufficient_evidence(self) -> None:
        stage = _stage(reconciliation={"status": "mismatch", "reason_codes": ["raw_candidate_count_mismatch"]})
        gate = _gate(raw=_track(), stage=stage, audit={"tracklet_count": 4, "suspicious_tracklet_count": 0})

        self.assertEqual("insufficient_evidence", gate["status"])
        self.assertFalse(gate["coverage_complete"])
        self.assertIn("stage_counter_mismatch", gate["reason_codes"])

    def test_logically_impossible_stage_order_fails_closed_even_with_reconciled_header(self) -> None:
        stage = _stage(
            raw_candidates={"value": 10, "status": "collected"},
            class_mapped_candidates={"value": 5, "status": "collected"},
            filtered_candidates={"value": 4, "status": "collected"},
            selected_candidates={"value": 0, "status": "collected"},
            tracklets={"value": 1, "status": "collected"},
        )

        gate = _gate(
            raw=_track(),
            stage=stage,
            audit={"tracklet_count": 1, "suspicious_tracklet_count": 0},
        )

        self.assertFalse(gate["coverage_complete"])
        self.assertEqual("insufficient_evidence", gate["status"])
        self.assertIn("tracklet_count_exceeds_selected_candidates", gate["reason_codes"])

    def test_raw_tracklet_reconciliation_does_not_compare_against_raw_plus_cleaned_total(self) -> None:
        gate = _gate(
            raw=_track(),
            stage=_stage(tracklets={"value": 1, "status": "collected"}),
            audit={"tracklet_count": 3, "suspicious_tracklet_count": 0},
            raw_tracklets=1,
        )

        self.assertTrue(gate["coverage_complete"])
        self.assertNotIn("tracklet_count_mismatch", gate["reason_codes"])

    def test_stored_stage_header_cannot_override_not_collected_counters(self) -> None:
        for name in (
            "evaluated_frames",
            "raw_candidates",
            "class_mapped_candidates",
            "filtered_candidates",
            "selected_candidates",
            "tracklets",
        ):
            stage = _stage(**{name: {"value": None, "status": "not_collected"}})
            gate = _gate(raw=_track(), stage=stage, audit={"tracklet_count": 4, "suspicious_tracklet_count": 0})
            with self.subTest(counter=name):
                self.assertEqual("insufficient_evidence", gate["status"])
                self.assertFalse(gate["coverage_complete"])
                self.assertFalse(gate["acceptance_metrics_complete"])
                self.assertFalse(gate["quality_acceptable"])
                self.assertIn(f"stage_counter_not_collected:{name}", gate["reason_codes"])

    def test_malformed_present_budget_summaries_are_not_zero(self) -> None:
        base = {
            "run_status": "completed",
            "raw_track": _track(),
            "cleaned_track": _track(),
            "stage_counts": _stage(),
            "audit_summary": {
                "tracklet_count": 4,
                "suspicious_tracklet_count": 0,
                "review_event_count": 0,
                "lost_gap_count": 0,
            },
            "raw_tracklet_count": 4,
            "follow_cam_summary": {
                "camera_motion_audit": {
                    "summary": {
                        "max_pan_step_px": 12.0,
                        "max_pan_accel_px": 18.0,
                        "max_zoom_step_ratio": 0.02,
                    }
                }
            },
            "decoder_failure": False,
            "evidence": {
                "wide_context": "available",
                "tight_crop": "available",
                "follow_cam": "available",
                "follow_cam_action_retention": "complete",
                "scale_strata": "complete",
                "lighting_strata": "complete",
                "attack_transition_windows": "complete",
                "media_integrity": "complete",
                "identity_binding": "complete",
            },
            "ai_review_summary": {"priority": "none"},
            "event_summary": {"frame_count": 100},
            "acceptance_contract_complete": True,
        }

        gate = build_trial_signal_gate_v2(**base)

        self.assertEqual("insufficient_evidence", gate["status"])
        self.assertFalse(gate["acceptance_metrics_complete"])
        self.assertIn("ai_review_trigger_budget_not_collected", gate["reason_codes"])
        self.assertIn("event_candidate_budget_not_collected", gate["reason_codes"])
        self.assertEqual("not_collected", gate["diagnostics"]["ai_review_trigger_count"]["status"])
        self.assertIsNone(gate["diagnostics"]["ai_review_trigger_count"]["value"])
        self.assertEqual("not_collected", gate["diagnostics"]["event_candidate_count"]["status"])
        self.assertEqual("collected", gate["diagnostics"]["follow_cam"]["status"])

    def test_typed_diagnostics_compare_tracks_rejections_budgets_and_camera_motion(self) -> None:
        stage = _stage(
            raw_candidates={"value": 40, "status": "collected"},
            class_mapped_candidates={"value": 20, "status": "collected"},
            filtered_candidates={"value": 12, "status": "collected"},
            selected_candidates={"value": 8, "status": "collected"},
            rejection_reasons={"class_not_allowed:person": 20, "too_small": 8},
        )
        gate = build_trial_signal_gate_v2(
            run_status="completed",
            raw_track=_track(),
            cleaned_track=_track(
                detected=75,
                predicted=15,
                lost=10,
                detected_ratio=0.75,
                predicted_ratio=0.15,
                lost_ratio=0.10,
            ),
            stage_counts=stage,
            audit_summary={
                "tracklet_count": 4,
                "suspicious_tracklet_count": 0,
                "review_event_count": 0,
                "lost_gap_count": 0,
            },
            raw_tracklet_count=4,
            follow_cam_summary={
                "camera_motion_audit": {
                    "summary": {
                        "max_pan_step_px": 12.0,
                        "max_pan_accel_px": 18.0,
                        "max_zoom_step_ratio": 0.02,
                    }
                }
            },
            decoder_failure=False,
            evidence={
                "wide_context": "available",
                "tight_crop": "available",
                "follow_cam": "available",
                "follow_cam_action_retention": "complete",
                "scale_strata": "complete",
                "lighting_strata": "complete",
                "attack_transition_windows": "complete",
                "media_integrity": "complete",
                "identity_binding": "complete",
            },
            ai_review_summary={"trigger_count": 2},
            event_summary={"candidate_count": 3},
            acceptance_contract_complete=True,
        )

        diagnostics = gate["diagnostics"]
        TrialSignalGateV2.model_validate(gate)
        self.assertEqual("collected", diagnostics["raw_track"]["status"])
        self.assertEqual(80, diagnostics["raw_track"]["detected"]["value"])
        self.assertEqual(75, diagnostics["cleaned_track"]["detected"]["value"])
        self.assertEqual(
            {"class_not_allowed:person": 20, "too_small": 8},
            diagnostics["rejection_reasons"]["value"],
        )
        self.assertEqual(2, diagnostics["ai_review_trigger_count"]["value"])
        self.assertEqual(2.0, diagnostics["ai_review_triggers_per_100_frames"]["value"])
        self.assertEqual(3.0, diagnostics["event_candidates_per_100_frames"]["value"])
        self.assertEqual(12.0, diagnostics["follow_cam"]["max_pan_step_px"]["value"])

    def test_typed_diagnostics_do_not_turn_missing_camera_or_budgets_into_zero(self) -> None:
        gate = build_trial_signal_gate_v2(
            run_status="completed",
            raw_track=_track(),
            cleaned_track=_track(),
            stage_counts=_stage(),
            audit_summary={
                "tracklet_count": 4,
                "suspicious_tracklet_count": 0,
                "review_event_count": 0,
                "lost_gap_count": 0,
            },
            raw_tracklet_count=4,
            follow_cam_summary=None,
            decoder_failure=False,
            evidence={},
            ai_review_summary=None,
            event_summary=None,
        )

        diagnostics = gate["diagnostics"]
        for observation in (
            diagnostics["ai_review_trigger_count"],
            diagnostics["ai_review_triggers_per_100_frames"],
            diagnostics["event_candidate_count"],
            diagnostics["event_candidates_per_100_frames"],
            diagnostics["follow_cam"]["max_pan_step_px"],
            diagnostics["follow_cam"]["max_pan_accel_px"],
            diagnostics["follow_cam"]["max_zoom_step_ratio"],
        ):
            self.assertEqual("not_collected", observation["status"])
            self.assertIsNone(observation["value"])

    def test_missing_required_visual_evidence_fails_closed(self) -> None:
        for missing in (
            "wide_context",
            "tight_crop",
            "follow_cam",
            "follow_cam_action_retention",
            "scale_strata",
            "lighting_strata",
            "attack_transition_windows",
            "media_integrity",
            "identity_binding",
        ):
            evidence = {
                "wide_context": "available",
                "tight_crop": "available",
                "follow_cam": "available",
                "follow_cam_action_retention": "complete",
                "scale_strata": "complete",
                "lighting_strata": "complete",
                "attack_transition_windows": "complete",
                "media_integrity": "complete",
                "identity_binding": "complete",
            }
            evidence[missing] = "not_collected"
            gate = build_trial_signal_gate_v2(
                run_status="completed",
                raw_track=_track(),
                cleaned_track=_track(),
                stage_counts=_stage(),
                audit_summary={"tracklet_count": 4, "suspicious_tracklet_count": 0},
                raw_tracklet_count=4,
                follow_cam_summary=None,
                decoder_failure=False,
                evidence=evidence,
                ai_review_summary={"trigger_count": 0},
                event_summary={"candidate_count": 0},
                acceptance_contract_complete=True,
            )

            with self.subTest(missing=missing):
                self.assertEqual("insufficient_evidence", gate["status"])
                self.assertTrue(gate["coverage_complete"])
                self.assertFalse(gate["evidence_available"])
                self.assertFalse(gate["quality_acceptable"])
                self.assertEqual("insufficient_evidence", gate["failure_classification"]["code"])
                self.assertIn(f"evidence_not_collected:{missing}", gate["reason_codes"])

    def test_missing_acceptance_metric_is_never_inferred_as_pass(self) -> None:
        cases: list[tuple[str, dict[str, object]]] = []
        raw_missing = _track()
        raw_missing.pop("longest_lost_streak")
        cases.append(("raw_metric_not_collected:longest_lost_streak", {"raw_track": raw_missing}))
        cleaned_missing = _track()
        cleaned_missing.pop("false_positive_island_count")
        cases.append(("cleaned_metric_not_collected:false_positive_island_count", {"cleaned_track": cleaned_missing}))
        cases.append(
            (
                "audit_metric_not_collected:review_event_count",
                {"audit_summary": {"tracklet_count": 4, "suspicious_tracklet_count": 0, "lost_gap_count": 0}},
            )
        )
        cases.append(("follow_cam_motion_not_collected", {"follow_cam_summary": None}))
        cases.append(("ai_review_trigger_budget_not_collected", {"ai_review_summary": None}))
        cases.append(("event_candidate_budget_not_collected", {"event_summary": None}))

        base = {
            "run_status": "completed",
            "raw_track": _track(),
            "cleaned_track": _track(),
            "stage_counts": _stage(),
            "audit_summary": {
                "tracklet_count": 4,
                "suspicious_tracklet_count": 0,
                "review_event_count": 0,
                "lost_gap_count": 0,
            },
            "raw_tracklet_count": 4,
            "follow_cam_summary": {
                "camera_motion_audit": {
                    "summary": {
                        "max_pan_step_px": 12.0,
                        "max_pan_accel_px": 18.0,
                        "max_zoom_step_ratio": 0.02,
                    }
                }
            },
            "decoder_failure": False,
            "evidence": {
                "wide_context": "available",
                "tight_crop": "available",
                "follow_cam": "available",
                "follow_cam_action_retention": "complete",
                "scale_strata": "complete",
                "lighting_strata": "complete",
                "attack_transition_windows": "complete",
                "media_integrity": "complete",
                "identity_binding": "complete",
            },
            "ai_review_summary": {"trigger_count": 0},
            "event_summary": {"candidate_count": 0},
            "acceptance_contract_complete": True,
        }
        for reason, override in cases:
            gate = build_trial_signal_gate_v2(**{**base, **override})
            with self.subTest(reason=reason):
                self.assertEqual("insufficient_evidence", gate["status"])
                self.assertFalse(gate["quality_acceptable"])
                self.assertIn(reason, gate["reason_codes"])


class TrialStageCounterTests(unittest.TestCase):
    def test_collects_and_reconciles_debug_and_candidate_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            rows = [
                {
                    **_debug_stage_evidence(3, selected_candidate_count=1),
                    "frame": 10,
                    "status": "Detected",
                    "raw_candidate_count": 3,
                    "filtered_candidate_count": 2,
                    "reacquire_candidate_count": 0,
                    "filter_rejection_counts": {"too_small": 1},
                },
                {
                    **_debug_stage_evidence(1),
                    "frame": 11,
                    "status": "Lost",
                    "raw_candidate_count": 1,
                    "filtered_candidate_count": 0,
                    "reacquire_candidate_count": 0,
                    "filter_rejection_counts": {"too_small": 1},
                },
            ]
            (root / "debug.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in rows),
                encoding="utf-8",
            )
            contract = {
                "schema_version": "2.0",
                "summary": {"status": "ok", "frame_count": 2, "candidate_count": 4, "validation_error_count": 0},
                "frames": [{"frame_index": 10}, {"frame_index": 11}],
                "candidates": _runtime_candidate_records([10, 10, 10, 11]),
                "validation_errors": [],
            }

            summary = collect_trial_stage_counts(
                root,
                contract,
                tracklet_count=1,
                raw_track=_status_track(detected=1, predicted=0, lost=1),
            )

        self.assertEqual("complete", summary["coverage_status"])
        self.assertEqual(2, summary["evaluated_frames"]["value"])
        self.assertEqual({"value": 1, "status": "collected"}, summary["detected_frames"])
        self.assertEqual({"value": 0, "status": "collected"}, summary["predicted_frames"])
        self.assertEqual({"value": 1, "status": "collected"}, summary["lost_frames"])
        self.assertEqual(4, summary["raw_candidates"]["value"])
        self.assertEqual(4, summary["class_mapped_candidates"]["value"])
        self.assertEqual(2, summary["filtered_candidates"]["value"])
        self.assertEqual(1, summary["selected_candidates"]["value"])
        self.assertEqual({"too_small": 2}, summary["rejection_reasons"])
        self.assertEqual("reconciled", summary["reconciliation"]["status"])

    def test_debug_status_counts_fail_closed_when_raw_track_counts_disagree(self) -> None:
        rows = [
            {
                **_debug_stage_evidence(1),
                "frame": frame,
                "status": status,
                "raw_candidate_count": 1,
                "filtered_candidate_count": 1,
                "reacquire_candidate_count": 0,
                "selected_candidate_count": int(status == "Detected"),
                "filter_rejection_counts": {},
            }
            for frame, status in enumerate(("Detected", "Predicted", "Lost"), start=10)
        ]
        contract = {
            "schema_version": "2.0",
            "summary": {"status": "ok", "frame_count": 3, "candidate_count": 3, "validation_error_count": 0},
            "frames": [{"frame_index": frame} for frame in range(10, 13)],
            "candidates": _runtime_candidate_records([10, 11, 12]),
            "validation_errors": [],
        }
        cases = {
            "raw_track_detected_count_mismatch": _status_track(detected=0, predicted=1, lost=2),
            "raw_track_predicted_count_mismatch": _status_track(detected=1, predicted=0, lost=2),
            "raw_track_lost_count_mismatch": _status_track(detected=1, predicted=2, lost=0),
            "raw_track_frame_count_mismatch": _status_track(detected=1, predicted=1, lost=2),
        }
        for reason, raw_track in cases.items():
            with tempfile.TemporaryDirectory() as temp_name:
                root = Path(temp_name)
                (root / "debug.jsonl").write_text(
                    "".join(json.dumps(row) + "\n" for row in rows),
                    encoding="utf-8",
                )
                summary = collect_trial_stage_counts(
                    root,
                    contract,
                    tracklet_count=1,
                    raw_track=raw_track,
                )
            gate = _gate(
                raw=raw_track,
                stage=summary,
                audit={"tracklet_count": 1, "suspicious_tracklet_count": 0},
            )
            with self.subTest(reason=reason):
                self.assertEqual("invalid", summary["coverage_status"])
                self.assertIn(reason, summary["reconciliation"]["reason_codes"])
                self.assertEqual("insufficient_evidence", gate["status"])
                self.assertFalse(gate["coverage_complete"])

    def test_counts_model_outputs_before_allowed_label_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            row = {
                **_debug_stage_evidence(
                    0,
                    detector_output_count=2,
                    class_rejection_counts={"person": 2},
                ),
                "frame": 10,
                "status": "Lost",
                "raw_candidate_count": 0,
                "filtered_candidate_count": 0,
                "reacquire_candidate_count": 0,
                "filter_rejection_counts": {},
            }
            (root / "debug.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
            contract = {
                "schema_version": "2.0",
                "summary": {
                    "status": "ok",
                    "frame_count": 1,
                    "candidate_count": 0,
                    "validation_error_count": 0,
                },
                "frames": [{"frame_index": 10}],
                "candidates": [],
                "validation_errors": [],
            }

            summary = collect_trial_stage_counts(
                root,
                contract,
                tracklet_count=0,
                raw_track=_status_track(detected=0, predicted=0, lost=1),
            )

        self.assertEqual("complete", summary["coverage_status"])
        self.assertEqual({"value": 2, "status": "collected"}, summary["raw_candidates"])
        self.assertEqual(
            {"value": 0, "status": "collected"},
            summary["class_mapped_candidates"],
        )
        self.assertEqual(
            {"class_not_allowed:person": 2},
            summary["rejection_reasons"],
        )

    def test_legacy_debug_without_detector_stage_evidence_is_not_collected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            row = {
                "frame": 10,
                "status": "Detected",
                "raw_candidate_count": 1,
                "filtered_candidate_count": 1,
                "reacquire_candidate_count": 0,
                "filter_rejection_counts": {},
            }
            (root / "debug.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
            contract = {
                "schema_version": "2.0",
                "summary": {
                    "status": "ok",
                    "frame_count": 1,
                    "candidate_count": 1,
                    "validation_error_count": 0,
                },
                "frames": [{"frame_index": 10}],
                "candidates": _runtime_candidate_records([10]),
                "validation_errors": [],
            }

            summary = collect_trial_stage_counts(
                root,
                contract,
                tracklet_count=1,
                raw_track=_status_track(detected=1, predicted=0, lost=0),
            )

        self.assertEqual("invalid", summary["coverage_status"])
        self.assertEqual(
            {"value": None, "status": "not_collected"},
            summary["raw_candidates"],
        )
        self.assertEqual(
            {"value": None, "status": "not_collected"},
            summary["class_mapped_candidates"],
        )
        self.assertIn(
            "detector_stage_evidence_not_collected:1",
            summary["reconciliation"]["reason_codes"],
        )

    def test_legacy_debug_without_status_evidence_is_not_collected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            row = {
                **_debug_stage_evidence(1, selected_candidate_count=1),
                "frame": 10,
                "raw_candidate_count": 1,
                "filtered_candidate_count": 1,
                "reacquire_candidate_count": 0,
                "filter_rejection_counts": {},
            }
            (root / "debug.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
            contract = {
                "schema_version": "2.0",
                "summary": {
                    "status": "ok",
                    "frame_count": 1,
                    "candidate_count": 1,
                    "validation_error_count": 0,
                },
                "frames": [{"frame_index": 10}],
                "candidates": _runtime_candidate_records([10]),
                "validation_errors": [],
            }
            raw_track = _status_track(detected=1, predicted=0, lost=0)
            summary = collect_trial_stage_counts(
                root,
                contract,
                tracklet_count=1,
                raw_track=raw_track,
            )

        gate = _gate(
            raw=raw_track,
            stage=summary,
            audit={"tracklet_count": 1, "suspicious_tracklet_count": 0},
        )
        self.assertEqual("invalid", summary["coverage_status"])
        for name in ("detected_frames", "predicted_frames", "lost_frames"):
            self.assertEqual(
                {"value": None, "status": "not_collected"},
                summary[name],
            )
        for name, expected in (
            ("raw_candidates", 1),
            ("class_mapped_candidates", 1),
            ("filtered_candidates", 1),
            ("selected_candidates", 1),
        ):
            self.assertEqual(
                {"value": expected, "status": "collected"},
                summary[name],
            )
        self.assertIn(
            "debug_status_not_collected:1",
            summary["reconciliation"]["reason_codes"],
        )
        self.assertEqual("insufficient_evidence", gate["status"])
        self.assertFalse(gate["coverage_complete"])

    def test_frame_exception_fails_the_signal_gate_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            row = {
                **_debug_stage_evidence(0, frame_exception=True),
                "frame": 10,
                "status": "Lost",
                "raw_candidate_count": 0,
                "filtered_candidate_count": 0,
                "reacquire_candidate_count": 0,
                "filter_rejection_counts": {},
            }
            (root / "debug.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
            contract = {
                "schema_version": "2.0",
                "summary": {
                    "status": "ok",
                    "frame_count": 1,
                    "candidate_count": 0,
                    "validation_error_count": 0,
                },
                "frames": [{"frame_index": 10}],
                "candidates": [],
                "validation_errors": [],
            }
            summary = collect_trial_stage_counts(
                root,
                contract,
                tracklet_count=0,
                raw_track=_status_track(detected=0, predicted=0, lost=1),
            )

        raw = _track(
            frame_count=1,
            detected=0,
            predicted=0,
            lost=1,
            detected_ratio=0.0,
            predicted_ratio=0.0,
            lost_ratio=1.0,
            longest_lost_streak=1,
            false_positive_island_count=0,
            max_step_px=None,
        )
        gate = _gate(
            raw=raw,
            stage=summary,
            audit={"tracklet_count": 0, "suspicious_tracklet_count": 0},
        )
        self.assertEqual("invalid", summary["coverage_status"])
        self.assertIn("debug_frame_exception:1", summary["reconciliation"]["reason_codes"])
        self.assertEqual("insufficient_evidence", gate["status"])
        self.assertFalse(gate["signal_acceptable"])
        self.assertIn("frame_exception", gate["reason_codes"])

    def test_missing_debug_is_not_collected_instead_of_zero(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            summary = collect_trial_stage_counts(
                Path(temp_name),
                None,
                tracklet_count=None,
                raw_track=None,
            )

        self.assertEqual("not_collected", summary["coverage_status"])
        for name in (
            "evaluated_frames",
            "detected_frames",
            "predicted_frames",
            "lost_frames",
            "raw_candidates",
            "class_mapped_candidates",
            "filtered_candidates",
            "selected_candidates",
            "tracklets",
        ):
            self.assertIsNone(summary[name]["value"])
            self.assertEqual("not_collected", summary[name]["status"])

    def test_missing_required_debug_counter_invalidates_coverage(self) -> None:
        cases = {
            "reacquire_candidate_count": {
                **_debug_stage_evidence(1, selected_candidate_count=1),
                "frame": 10,
                "status": "Detected",
                "raw_candidate_count": 1,
                "filtered_candidate_count": 1,
                "filter_rejection_counts": {},
            },
            "filter_rejection_counts": {
                **_debug_stage_evidence(1, selected_candidate_count=1),
                "frame": 10,
                "status": "Detected",
                "raw_candidate_count": 1,
                "filtered_candidate_count": 1,
                "reacquire_candidate_count": 0,
            },
        }
        contract = {
            "schema_version": "2.0",
            "summary": {"status": "ok", "frame_count": 1, "candidate_count": 1, "validation_error_count": 0},
            "frames": [{"frame_index": 10}],
            "candidates": _runtime_candidate_records([10]),
            "validation_errors": [],
        }
        for missing, row in cases.items():
            with tempfile.TemporaryDirectory() as temp_name:
                root = Path(temp_name)
                (root / "debug.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
                summary = collect_trial_stage_counts(
                    root,
                    contract,
                    tracklet_count=1,
                    raw_track=_status_track(detected=1, predicted=0, lost=0),
                )
            with self.subTest(counter=missing):
                self.assertEqual("invalid", summary["coverage_status"])
                self.assertEqual("mismatch", summary["reconciliation"]["status"])
                self.assertTrue(
                    any(missing in reason for reason in summary["reconciliation"]["reason_codes"]),
                    summary["reconciliation"]["reason_codes"],
                )

    def test_candidate_counter_gaps_do_not_erase_independent_status_or_selector_counts(self) -> None:
        complete_row = {
            **_debug_stage_evidence(1, selected_candidate_count=1),
            "frame": 10,
            "status": "Detected",
            "raw_candidate_count": 1,
            "filtered_candidate_count": 1,
            "reacquire_candidate_count": 0,
            "filter_rejection_counts": {},
        }
        contract = {
            "schema_version": "2.0",
            "summary": {
                "status": "ok",
                "frame_count": 1,
                "candidate_count": 1,
                "validation_error_count": 0,
            },
            "frames": [{"frame_index": 10}],
            "candidates": _runtime_candidate_records([10]),
            "validation_errors": [],
        }
        for missing in (
            "raw_candidate_count",
            "filtered_candidate_count",
            "reacquire_candidate_count",
        ):
            row = dict(complete_row)
            row.pop(missing)
            with tempfile.TemporaryDirectory() as temp_name:
                root = Path(temp_name)
                (root / "debug.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
                summary = collect_trial_stage_counts(
                    root,
                    contract,
                    tracklet_count=1,
                    raw_track=_status_track(detected=1, predicted=0, lost=0),
                )
            with self.subTest(missing=missing):
                self.assertEqual("invalid", summary["coverage_status"])
                self.assertEqual({"value": 1, "status": "collected"}, summary["evaluated_frames"])
                self.assertEqual({"value": 1, "status": "collected"}, summary["detected_frames"])
                self.assertEqual({"value": 1, "status": "collected"}, summary["selected_candidates"])
                for name in (
                    "raw_candidates",
                    "class_mapped_candidates",
                    "filtered_candidates",
                ):
                    self.assertEqual(
                        {"value": None, "status": "not_collected"},
                        summary[name],
                    )

    def test_missing_or_inconsistent_selector_count_fails_closed_independently(self) -> None:
        contract = {
            "schema_version": "2.0",
            "summary": {
                "status": "ok",
                "frame_count": 1,
                "candidate_count": 1,
                "validation_error_count": 0,
            },
            "frames": [{"frame_index": 10}],
            "candidates": _runtime_candidate_records([10]),
            "validation_errors": [],
        }
        base_row = {
            **_debug_stage_evidence(1, selected_candidate_count=1),
            "frame": 10,
            "status": "Detected",
            "raw_candidate_count": 1,
            "filtered_candidate_count": 1,
            "reacquire_candidate_count": 0,
            "filter_rejection_counts": {},
        }
        cases = {
            "debug_selected_counter_not_collected:1": {
                key: value for key, value in base_row.items() if key != "selected_candidate_count"
            },
            "selected_detected_count_mismatch": {
                **base_row,
                "selected_candidate_count": 0,
            },
        }
        for reason, row in cases.items():
            with tempfile.TemporaryDirectory() as temp_name:
                root = Path(temp_name)
                (root / "debug.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
                summary = collect_trial_stage_counts(
                    root,
                    contract,
                    tracklet_count=1,
                    raw_track=_status_track(detected=1, predicted=0, lost=0),
                )
            with self.subTest(reason=reason):
                self.assertEqual("invalid", summary["coverage_status"])
                self.assertIn(reason, summary["reconciliation"]["reason_codes"])
                self.assertEqual({"value": 1, "status": "collected"}, summary["detected_frames"])
                if reason.startswith("debug_selected"):
                    self.assertEqual(
                        {"value": None, "status": "not_collected"},
                        summary["selected_candidates"],
                    )
                else:
                    self.assertEqual(
                        {"value": 0, "status": "collected"},
                        summary["selected_candidates"],
                    )


class TrialDiagnosisIntegrationTests(unittest.TestCase):
    def test_full_audit_raw_source_supplies_tracking_stage_tracklets(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            debug_row = {
                **_debug_stage_evidence(1, selected_candidate_count=1),
                "frame": 10,
                "status": "Detected",
                "raw_candidate_count": 1,
                "filtered_candidate_count": 1,
                "reacquire_candidate_count": 0,
                "filter_rejection_counts": {},
            }
            (root / "debug.jsonl").write_text(json.dumps(debug_row) + "\n", encoding="utf-8")
            (root / "tracking_contract.v2.json").write_text(
                json.dumps(
                    {
                        "schema_version": "2.0",
                        "summary": {
                            "status": "ok",
                            "frame_count": 1,
                            "candidate_count": 1,
                            "validation_error_count": 0,
                        },
                        "frames": [{"frame_index": 10}],
                        "candidates": _runtime_candidate_records([10]),
                        "validation_errors": [],
                    }
                ),
                encoding="utf-8",
            )
            (root / "ball_audit.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "summary": {
                            "tracklet_count": 3,
                            "suspicious_tracklet_count": 0,
                            "review_event_count": 0,
                            "lost_gap_count": 0,
                        },
                        "sources": [
                            {"name": "raw", "tracklet_count": 1},
                            {"name": "cleaned", "tracklet_count": 2},
                        ],
                        "tracklets": [],
                        "review_events": [],
                    }
                ),
                encoding="utf-8",
            )
            raw_track = _status_track(detected=1, predicted=0, lost=0)
            diagnosis = build_trial_diagnosis(
                root,
                {
                    "run_id": "production_trial_raw_tracklets",
                    "status": "completed",
                    "modules_enabled": {"postprocess": False, "follow_cam": False},
                },
                metrics_report={
                    "tracks": {"raw": raw_track, "cleaned": None},
                    "ball_audit": {
                        "tracklet_count": 3,
                        "suspicious_tracklet_count": 0,
                        "review_event_count": 0,
                        "lost_gap_count": 0,
                    },
                },
            )

        gate = diagnosis["trial_signal_gate_v2"]
        self.assertEqual(
            {"value": 1, "status": "collected"},
            gate["stage_counts"]["tracklets"],
        )
        self.assertNotIn("tracklet_count_mismatch", gate["reason_codes"])
        self.assertNotIn("raw_audit_tracklet_count_not_collected", gate["reason_codes"])

    def test_modules_enabled_is_authoritative_and_note_conflict_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            diagnosis = build_trial_diagnosis(
                Path(temp_name),
                {
                    "run_id": "conflicting_trial_options",
                    "status": "completed",
                    "modules_enabled": {"postprocess": True, "follow_cam": True},
                    "notes": json.dumps(
                        {
                            "purpose": "production_trial",
                            "enable_postprocess": False,
                            "enable_follow_cam": False,
                        }
                    ),
                },
                metrics_report={},
            )

        gate = diagnosis["trial_signal_gate_v2"]
        self.assertEqual("insufficient_evidence", gate["status"])
        self.assertFalse(gate["coverage_complete"])
        self.assertIn("trial_option_conflict:postprocess", gate["reason_codes"])
        self.assertIn("trial_option_conflict:follow_cam", gate["reason_codes"])
        self.assertEqual("not_collected", gate["evidence"]["follow_cam"])

    def test_ordinary_output_frame_is_not_tight_crop_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            (root / "frames").mkdir()
            (root / "frames" / "frame_000001.jpg").write_bytes(b"not-empty")

            evidence = _evidence_status(root)

        self.assertEqual("not_collected", evidence["tight_crop"])

    def test_malformed_raw_budget_reports_cannot_be_hidden_by_compact_zero_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            (root / "ai_review_triggers.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "decision": {"needs_ai_review": False},
                        "summary": {},
                        "triggers": [],
                    }
                ),
                encoding="utf-8",
            )
            (root / "event_candidates.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "summary": {"frame_count": 100},
                        "candidates": [],
                    }
                ),
                encoding="utf-8",
            )
            (root / "ball_audit.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "summary": {"tracklet_count": 4},
                        "sources": [{"name": "raw", "tracklet_count": 4}],
                    }
                ),
                encoding="utf-8",
            )
            diagnosis = build_trial_diagnosis(
                root,
                {
                    "run_id": "production_trial_malformed_budgets",
                    "status": "completed",
                    "notes": json.dumps(
                        {
                            "purpose": "production_trial",
                            "enable_postprocess": True,
                            "enable_follow_cam": False,
                        }
                    ),
                },
                metrics_report={
                    "tracks": {"raw": _track(), "cleaned": _track()},
                    "ball_audit": {
                        "tracklet_count": 4,
                        "suspicious_tracklet_count": 0,
                        "review_event_count": 0,
                        "lost_gap_count": 0,
                    },
                    "detection_stages": _stage(),
                    # These lossy compact values must not rescue malformed raw reports.
                    "ai_review_triggers": {"trigger_count": 0},
                    "event_candidates": {"candidate_count": 0},
                },
            )

        gate = diagnosis["trial_signal_gate_v2"]
        self.assertTrue(gate["coverage_complete"])
        self.assertFalse(gate["acceptance_metrics_complete"])
        self.assertFalse(gate["quality_acceptable"])
        self.assertIn("ai_review_trigger_budget_not_collected", gate["reason_codes"])
        self.assertIn("event_candidate_budget_not_collected", gate["reason_codes"])

    def test_metrics_report_embeds_v2_gate_only_for_production_trials(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            (root / "ball_track.csv").write_text(
                "Frame,X,Y,Confidence,Status\n0,,,0.0,Lost\n",
                encoding="utf-8",
            )
            (root / "debug.jsonl").write_text(
                json.dumps(
                    {
                        **_debug_stage_evidence(0),
                        "frame": 0,
                        "status": "Lost",
                        "raw_candidate_count": 0,
                        "filtered_candidate_count": 0,
                        "reacquire_candidate_count": 0,
                        "filter_rejection_counts": {},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (root / "ball_audit.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "summary": {
                            "frame_count": 1,
                            "source_count": 1,
                            "tracklet_count": 0,
                            "suspicious_tracklet_count": 0,
                            "review_event_count": 0,
                            "lost_gap_count": 0,
                            "max_step_px": None,
                        },
                        "sources": [{"name": "raw", "tracklet_count": 0}],
                        "tracklets": [],
                        "review_events": [],
                    }
                ),
                encoding="utf-8",
            )
            (root / "tracking_contract.v2.json").write_text(
                json.dumps(
                    {
                        "schema_version": "2.0",
                        "summary": {
                            "status": "ok",
                            "frame_count": 1,
                            "candidate_count": 0,
                            "validation_error_count": 0,
                        },
                        "frames": [{"frame_index": 0}],
                        "candidates": [],
                        "validation_errors": [],
                    }
                ),
                encoding="utf-8",
            )
            run = {
                "run_id": "production_trial_zero",
                "status": "completed",
                "notes": json.dumps(
                    {
                        "purpose": "production_trial",
                        "enable_postprocess": False,
                        "enable_follow_cam": False,
                    }
                ),
            }

            report = build_metrics_report(root, run=run)
            ordinary_report = build_metrics_report(root)

        self.assertEqual("stable", report["quality_gate"]["status"])
        self.assertEqual("retune_required", report["trial_signal_gate_v2"]["status"])
        self.assertEqual("no_raw_candidates", report["trial_signal_gate_v2"]["failure_classification"]["code"])
        self.assertEqual(0, report["detection_stages"]["raw_candidates"]["value"])
        self.assertIn("trial_signal_gate_v2", stats_from_metrics_report(report))
        self.assertNotIn("trial_signal_gate_v2", ordinary_report)

    def test_realistic_all_lost_output_does_not_reinterpret_legacy_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            raw = _track(
                detected=0,
                predicted=0,
                lost=100,
                detected_ratio=0.0,
                predicted_ratio=0.0,
                lost_ratio=1.0,
                longest_lost_streak=100,
                false_positive_island_count=0,
                max_step_px=None,
            )
            metrics = {
                "schema_version": "1.0",
                "tracks": {"raw": raw, "cleaned": raw},
                "ball_audit": {"tracklet_count": 0, "suspicious_tracklet_count": 0},
                "quality_gate": {"status": "stable"},
            }
            (root / "ball_audit.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "summary": {"tracklet_count": 0},
                        "sources": [
                            {"name": "raw", "tracklet_count": 0},
                            {"name": "cleaned", "tracklet_count": 0},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (root / "debug.jsonl").write_text(
                "".join(
                    json.dumps(
                        {
                            **_debug_stage_evidence(0),
                            "frame": frame,
                            "status": "Lost",
                            "raw_candidate_count": 0,
                            "filtered_candidate_count": 0,
                            "reacquire_candidate_count": 0,
                            "filter_rejection_counts": {},
                        }
                    )
                    + "\n"
                    for frame in range(100)
                ),
                encoding="utf-8",
            )
            (root / "tracking_contract.v2.json").write_text(
                json.dumps(
                    {
                        "schema_version": "2.0",
                        "summary": {
                            "status": "ok",
                            "frame_count": 100,
                            "candidate_count": 0,
                            "validation_error_count": 0,
                        },
                        "frames": [{"frame_index": frame} for frame in range(100)],
                        "candidates": [],
                        "validation_errors": [],
                    }
                ),
                encoding="utf-8",
            )

            diagnosis = build_trial_diagnosis(
                root,
                {"run_id": "production_trial_zero", "status": "completed", "error": None},
                metrics_report=metrics,
            )

        self.assertEqual("stable", diagnosis["legacy_quality_gate_status"])
        self.assertEqual("retune_required", diagnosis["trial_signal_gate_v2"]["status"])
        self.assertEqual("no_raw_candidates", diagnosis["trial_signal_gate_v2"]["failure_classification"]["code"])

    def test_tuning_schema_is_versioned_bounded_and_excludes_model_selection(self) -> None:
        schema = trial_tuning_schema()

        self.assertEqual(TRIAL_TUNING_SCHEMA_VERSION, schema["schema_version"])
        paths = {control["path"] for control in schema["controls"]}
        for expected in (
            "detector.allowed_labels",
            "detector.inference_mode",
            "detector.confidence_threshold",
            "sahi.slice_height",
            "filtering.min_confidence",
            "selection.min_accept_score",
            "tracking.match_distance",
            "postprocess.low_confidence_threshold",
        ):
            self.assertIn(expected, paths)
        self.assertNotIn("detector.model_path", paths)
        self.assertEqual(
            [
                {
                    "action_code": "return_to_field_setup",
                    "target_step": "field_setup",
                    "reason_code": "field_geometry_requires_new_calibration",
                    "affected_paths": [
                        "filtering.roi",
                        "scene_bias.ground_zones",
                        "scene_bias.negative_rois",
                    ],
                    "lineage_constraint": ("invalidate_trial_and_downstream_then_create_new_calibration_version"),
                }
            ],
            schema["actions"],
        )
        for control in schema["controls"]:
            self.assertIn(
                control["kind"],
                {"number", "integer", "boolean", "select", "multi_select"},
            )
            if control["kind"] in {"number", "integer"}:
                self.assertLess(control["minimum"], control["maximum"])
                self.assertGreater(control["step"], 0)


class ProductionTrialTuningPatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.controls = trial_tuning_schema()["controls"]
        self.base_values = {
            control["path"]: (
                control["options"][0]
                if control["kind"] == "select"
                else [control["options"][0]]
                if control["kind"] == "multi_select"
                else False
                if control["kind"] == "boolean"
                else int(control["minimum"])
                if control["kind"] == "integer"
                else control["minimum"]
            )
            for control in self.controls
        }
        self.base_config: dict[str, object] = {}
        for path, value in self.base_values.items():
            current = self.base_config
            parts = path.split(".")
            for part in parts[:-1]:
                current = current.setdefault(part, {})  # type: ignore[assignment]
            current[parts[-1]] = value

    def versioned_patch(self) -> dict[str, object]:
        values = deepcopy(self.base_values)
        values["detector.confidence_threshold"] = 0.02
        return {
            "detector": {"confidence_threshold": 0.02},
            "metadata": {
                "production_tuning": {
                    "schema_version": "1.0",
                    "version_id": "tuning-v1",
                    "parent_version_id": None,
                    "created_at": "2026-07-17T12:00:00.000Z",
                    "values_sha256": production_tuning_values_sha256(values),
                    "values": values,
                    "history": [],
                }
            },
        }

    def test_accepts_versioned_sparse_patch_derived_from_resolved_base(self) -> None:
        validate_production_trial_config_patch(
            self.versioned_patch(),
            base_config=self.base_config,
        )

    def test_real_default_config_can_seed_a_complete_versioned_tuning_draft(self) -> None:
        default_config = load_config(Path(__file__).resolve().parents[1] / "config" / "default.yaml")
        values: dict[str, object] = {}
        base_config: dict[str, object] = {}
        for control in self.controls:
            current: object = default_config
            for part in control["path"].split("."):
                current = getattr(current, part)
            values[control["path"]] = current

            nested = base_config
            parts = control["path"].split(".")
            for part in parts[:-1]:
                nested = nested.setdefault(part, {})  # type: ignore[assignment]
            nested[parts[-1]] = current

        validate_production_trial_config_patch(
            {
                "metadata": {
                    "production_tuning": {
                        "schema_version": "1.0",
                        "version_id": "default-seed-v1",
                        "parent_version_id": None,
                        "created_at": "2026-07-17T12:00:00.000Z",
                        "values_sha256": production_tuning_values_sha256(values),
                        "values": values,
                        "history": [],
                    }
                }
            },
            base_config=base_config,
        )

    def test_normalizes_bounded_legacy_tuning_to_complete_version_metadata(self) -> None:
        legacy_patch = {"detector": {"confidence_threshold": 0.02}}
        normalized = normalize_production_trial_config_patch(
            legacy_patch,
            base_config=self.base_config,
            legacy_created_at="2026-07-17T12:00:00.000Z",
        )

        self.assertNotIn("metadata", legacy_patch)
        metadata = normalized["metadata"]["production_tuning"]
        expected_values = deepcopy(self.base_values)
        expected_values["detector.confidence_threshold"] = 0.02
        self.assertEqual(expected_values, metadata["values"])
        self.assertEqual(
            production_tuning_values_sha256(expected_values),
            metadata["values_sha256"],
        )
        self.assertEqual([], metadata["history"])
        self.assertIsNone(metadata["parent_version_id"])
        self.assertTrue(metadata["version_id"].startswith("legacy-"))
        validate_production_trial_config_patch(
            normalized,
            base_config=self.base_config,
        )
        self.assertEqual(
            normalized,
            normalize_production_trial_config_patch(
                normalized,
                base_config=self.base_config,
            ),
        )

    def test_legacy_normalization_prunes_explicit_defaults_but_preserves_system_leaves(self) -> None:
        normalized = normalize_production_trial_config_patch(
            {
                "filtering": {
                    "min_confidence": self.base_values["filtering.min_confidence"],
                    "roi": [10, 20, 600, 340],
                }
            },
            base_config=self.base_config,
            legacy_created_at="2026-07-17T12:00:00.000Z",
        )

        self.assertEqual({"roi": [10, 20, 600, 340]}, normalized["filtering"])
        validate_production_trial_config_patch(
            normalized,
            base_config=self.base_config,
        )
        self.assertEqual(
            normalized,
            normalize_production_trial_config_patch(
                normalized,
                base_config=self.base_config,
            ),
        )

    def test_rejects_invalid_bounded_legacy_tuning(self) -> None:
        for patch in (
            {"detector": {"confidence_threshold": 0.015}},
            {"detector": {"image_size": 641}},
            {"detector": {"allowed_labels": []}},
            {"detector": {"allowed_labels": ["person"]}},
            {"detector": {"allowed_labels": ["ball", "ball"]}},
            {"filtering": {"min_width": 5.0}},
        ):
            with self.subTest(patch=patch):
                with self.assertRaises(ValueError):
                    normalize_production_trial_config_patch(
                        patch,
                        base_config=self.base_config,
                    )

    def test_rejects_model_path_unknown_and_prototype_paths(self) -> None:
        for patch in (
            {"detector": {"model_path": "weights/other.pt"}},
            {"detector": {"confidence": 0.2}},
            {"detector": {"__proto__": {"model_path": "weights/other.pt"}}},
        ):
            with self.subTest(patch=patch):
                with self.assertRaises(ValueError):
                    validate_production_trial_config_patch(
                        patch,
                        base_config=self.base_config,
                    )

    def test_rejects_invalid_range_step_and_relation_values(self) -> None:
        for path, value in (
            ("detector.confidence_threshold", 0.015),
            ("detector.image_size", 641),
            ("filtering.min_width", 5.0),
        ):
            patch = self.versioned_patch()
            metadata = patch["metadata"]["production_tuning"]  # type: ignore[index]
            values = metadata["values"]  # type: ignore[index]
            values[path] = value  # type: ignore[index]
            if path == "filtering.min_width":
                values["filtering.max_width"] = 4.0  # type: ignore[index]
            metadata["values_sha256"] = production_tuning_values_sha256(values)  # type: ignore[index]
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    validate_production_trial_config_patch(
                        patch,
                        base_config=self.base_config,
                    )

    def test_rejects_metadata_leaf_and_base_mismatch(self) -> None:
        patch = self.versioned_patch()
        patch["detector"]["confidence_threshold"] = 0.03  # type: ignore[index]
        with self.assertRaisesRegex(ValueError, "does not match config_patch leaf"):
            validate_production_trial_config_patch(
                patch,
                base_config=self.base_config,
            )

        redundant = self.versioned_patch()
        metadata = redundant["metadata"]["production_tuning"]  # type: ignore[index]
        metadata["values"]["detector.confidence_threshold"] = 0.01  # type: ignore[index]
        metadata["values_sha256"] = production_tuning_values_sha256(metadata["values"])  # type: ignore[index]
        with self.assertRaisesRegex(ValueError, "redundant or inconsistent"):
            validate_production_trial_config_patch(
                redundant,
                base_config=self.base_config,
            )

    def test_rejects_digest_and_parent_history_inconsistency(self) -> None:
        bad_digest = self.versioned_patch()
        bad_digest["metadata"]["production_tuning"]["values_sha256"] = "0" * 64  # type: ignore[index]
        with self.assertRaisesRegex(ValueError, "does not match values"):
            validate_production_trial_config_patch(
                bad_digest,
                base_config=self.base_config,
            )

        bad_parent = self.versioned_patch()
        metadata = bad_parent["metadata"]["production_tuning"]  # type: ignore[index]
        metadata["parent_version_id"] = "missing-history-version"  # type: ignore[index]
        with self.assertRaisesRegex(ValueError, "parent/history linkage"):
            validate_production_trial_config_patch(
                bad_parent,
                base_config=self.base_config,
            )

    def test_accepts_bound_history_and_rejects_tampered_snapshot(self) -> None:
        patch = self.versioned_patch()
        metadata = patch["metadata"]["production_tuning"]  # type: ignore[index]
        snapshot = {
            "version_id": "tuning-v0",
            "created_at": "2026-07-17T11:00:00.000Z",
            "values_sha256": production_tuning_values_sha256(self.base_values),
            "values": deepcopy(self.base_values),
        }
        metadata["parent_version_id"] = "tuning-v0"  # type: ignore[index]
        metadata["history"] = [snapshot]  # type: ignore[index]
        validate_production_trial_config_patch(
            patch,
            base_config=self.base_config,
        )

        snapshot["values_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "does not match values"):
            validate_production_trial_config_patch(
                patch,
                base_config=self.base_config,
            )

    def test_accepts_current_protected_geometry_and_runtime_patch(self) -> None:
        validate_production_trial_config_patch(
            {
                "input_video": "data/match.mp4",
                "filtering": {"roi": [100, 150, 1800, 950]},
                "scene_bias": {
                    "enabled": True,
                    "ground_zones": [
                        {
                            "name": "production_field",
                            "points": [[100, 200], [1800, 150], [1700, 950]],
                        }
                    ],
                    "negative_rois": [],
                },
                "postprocess": {"enabled": True},
                "follow_cam": {"enabled": False},
                "runtime": {"start_frame": 25, "max_frames": 240},
                "metadata": {"production_workflow": {"purpose": "production_trial"}},
            },
            base_config=self.base_config,
        )


if __name__ == "__main__":
    unittest.main()
