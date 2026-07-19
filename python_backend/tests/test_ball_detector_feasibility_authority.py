from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from typing import Any

from football_tracking.ball_detector_annotations import annotation_etag
from football_tracking.ball_detector_feasibility import (
    METRIC_PROFILE_SHA256,
    FeasibilityError,
    _validate_sampling_manifest,
    build_candidate_universe_authority,
    build_feasibility_report,
    temporal_group_for_frame,
)
from football_tracking.detector_development_common import atomic_write_json, canonical_sha256

SESSION_ID = "annotation-sealed-check"
LOCKED_PROFILE_ID = "official-coco-yolo11s-sahi"
CONTROL_PROFILE_ID = "current-coco-yolov8n-direct"


def _binding(profile_id: str, marker: str) -> dict[str, str]:
    return {
        "profile_id": profile_id,
        "profile_sha256": marker * 64,
        "model_id": f"model-{marker}",
        "model_version": "1.0",
        "model_descriptor_sha256": ("c" if marker == "b" else "f") * 64,
        "weights_sha256": ("d" if marker == "b" else "a") * 64,
    }


def _frozen_profile(binding: dict[str, str]) -> dict[str, Any]:
    return {
        **{
            key: binding[key]
            for key in ("profile_id", "profile_sha256", "model_id", "model_version", "model_descriptor_sha256")
        },
        "model_descriptor": {
            "weights": {"sha256": binding["weights_sha256"], "size_bytes": 7},
        },
    }


def _applicability() -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {"scale": [], "lighting": []}
    for dimension, names in (
        ("scale", ("near", "mid", "far")),
        ("lighting", ("bright_sun", "shadow", "backlight", "twilight", "artificial_light")),
    ):
        for name in names:
            status = "applicable" if name in {"near", "mid", "far", "bright_sun", "shadow"} else "not_applicable"
            note = f"pre-reveal source review: {name} is {status}"
            authority: dict[str, Any] = {
                "dimension": dimension,
                "stratum": name,
                "status": status,
                "note": note,
            }
            row: dict[str, Any] = {
                "stratum": name,
                "status": status,
            }
            if dimension == "lighting":
                if name == "bright_sun":
                    quota = 10
                    intervals = [{"start_frame": 0, "end_frame": 47}]
                elif name == "shadow":
                    quota = 10
                    intervals = [{"start_frame": 48, "end_frame": 99}]
                else:
                    quota = 0
                    intervals = []
                row.update({"quota": quota, "frame_intervals": intervals})
                authority.update({"quota": quota, "frame_intervals": intervals})
            result[dimension].append(
                {
                    **row,
                    "evidence": {
                        "declared_before_reveal": True,
                        "note": note,
                        "evidence_sha256": canonical_sha256(authority),
                    },
                }
            )
    return result


def _annotation(frame_index: int) -> dict[str, Any]:
    if frame_index >= 75:
        return {
            "frame_index": frame_index,
            "point_source_px": None,
            "bbox_source_px": None,
            "presence": "absent",
            "visibility": "not_applicable",
            "training_use": "excluded",
            "annotation_state": "confirmed",
            "scale_stratum": "not_applicable",
            "lighting_tag": "shadow",
            "motion_occlusion_tags": [],
            "provenance": "manual_human_annotation",
        }
    positive_index = frame_index // 5
    scale = ("near", "mid", "far")[positive_index % 3]
    side = {"far": 2.0, "mid": 5.0, "near": 14.0}[scale]
    half = side / 2.0
    return {
        "frame_index": frame_index,
        "point_source_px": {"x": 20.0, "y": 20.0},
        "bbox_source_px": {
            "left": 20.0 - half,
            "top": 20.0 - half,
            "right": 20.0 + half,
            "bottom": 20.0 + half,
        },
        "presence": "present",
        "visibility": "visible",
        "training_use": "excluded",
        "annotation_state": "confirmed",
        "scale_stratum": scale,
        "lighting_tag": "bright_sun" if frame_index < 50 else "shadow",
        "motion_occlusion_tags": [],
        "provenance": "manual_human_annotation",
    }


def _sealed_evidence() -> tuple[dict[str, Any], dict[str, Any]]:
    source = {
        "source_id": "source-one",
        "sha256": "1" * 64,
        "file_identity_sha256": "2" * 64,
        "width": 64,
        "height": 400,
        "frame_count": 100,
        "fps": 20.0,
        "tracking_contract_sha256": "3" * 64,
    }
    locked = _binding(LOCKED_PROFILE_ID, "e")
    control = _binding(CONTROL_PROFILE_ID, "b")
    applicability = _applicability()
    frame_indices = list(range(0, 100, 5))
    groups = [
        {
            **temporal_group_for_frame(source["sha256"], frame),
            "frame_index": frame,
            "pre_reveal_lighting_stratum": ("bright_sun" if frame < 50 else "shadow"),
        }
        for frame in frame_indices
    ]
    candidate_universe_authority = build_candidate_universe_authority(
        source_sha256=source["sha256"],
        start_frame=0,
        end_frame=source["frame_count"] - 1,
        lighting_strata=[
            {
                "stratum": row["stratum"],
                "quota": row["quota"],
                "frame_intervals": row["frame_intervals"],
            }
            for row in applicability["lighting"]
            if row["quota"] > 0
        ],
        excluded_groups=[],
    )
    scale_by_name = {row["stratum"]: row for row in applicability["scale"]}
    lighting_by_name = {row["stratum"]: row for row in applicability["lighting"]}
    selection_authority = {
        "schema_version": "1.0",
        "artifact_type": "ball_annotation_sampling_selection_authority",
        "attempt_family_sha256": "a" * 64,
        "development_package_sha256": "d" * 64,
        "source_sha256": source["sha256"],
        "locked_profile_id": locked["profile_id"],
        "locked_profile_sha256": locked["profile_sha256"],
        "sampling_profile_id": "tiny_ball_temporal_groups_v1",
        "metric_profile_id": "tiny_ball_feasibility_metric_v1",
        "metric_profile_sha256": METRIC_PROFILE_SHA256,
        "target_frame_count": 20,
        "scale_applicability": [
            {
                "stratum": stratum,
                "status": scale_by_name[stratum]["status"],
            }
            for stratum in ("near", "mid", "far")
        ],
        "lighting_applicability": [
            {
                "stratum": stratum,
                "status": lighting_by_name[stratum]["status"],
                "quota": lighting_by_name[stratum]["quota"],
                "frame_intervals": lighting_by_name[stratum]["frame_intervals"],
            }
            for stratum in (
                "bright_sun",
                "shadow",
                "backlight",
                "twilight",
                "artificial_light",
            )
        ],
    }
    manifest: dict[str, Any] = {
        "schema_version": "1.0",
        "artifact_type": "ball_annotation_sampling_manifest",
        "profile_id": "tiny_ball_temporal_groups_v1",
        "selection_profile_id": "tiny_ball_temporal_block_hash_v1",
        "scale_stratification_mode": "post_reveal_support_gate_only",
        "lighting_stratification_mode": "predeclared_frame_intervals_and_quota_v1",
        "selection_seed_sha256": canonical_sha256(selection_authority),
        "selection_authority": selection_authority,
        "candidate_universe_sha256": canonical_sha256(candidate_universe_authority),
        "candidate_universe_authority": candidate_universe_authority,
        "candidate_universe_start_frame": 0,
        "candidate_universe_end_frame": source["frame_count"] - 1,
        "metric_profile_id": "tiny_ball_feasibility_metric_v1",
        "metric_profile_sha256": METRIC_PROFILE_SHA256,
        "data_role": "check",
        "locked_before_probe": True,
        "source_sha256": source["sha256"],
        "locked_profile_id": locked["profile_id"],
        "locked_profile_sha256": locked["profile_sha256"],
        "target_frame_count": 20,
        "frame_indices": frame_indices,
        "groups": groups,
        "excluded_development_groups": [],
        "strata_applicability": applicability,
    }
    manifest["manifest_sha256"] = canonical_sha256(manifest)
    effective = [_annotation(frame) for frame in frame_indices]
    revisions = []
    for row in effective:
        frame = row["frame_index"]
        annotation = {key: value for key, value in row.items() if key != "frame_index"}
        revisions.append(
            {
                "schema_version": "1.0",
                "artifact_type": "ball_annotation_revision",
                "revision_id": f"revision-{frame}",
                "session_id": SESSION_ID,
                "frame_index": frame,
                "revision": 1,
                "operation": "set",
                "mutation_id": f"mutation-{frame}",
                "mutation_sha256": canonical_sha256(annotation),
                "expected_revision": 0,
                "supersedes_revision": None,
                "undo_revision": None,
                "previous_effective_annotation": None,
                "effective_annotation": annotation,
                "operator_id": "operator-one",
                "annotation_etag": annotation_etag(SESSION_ID, frame, 1, annotation),
                "created_at": "2026-07-18T00:00:00+00:00",
            }
        )
    frozen_profiles = [_frozen_profile(control), _frozen_profile(locked)]
    frozen_profiles_sha = canonical_sha256(frozen_profiles)
    check_authority = {
        "job_id": "probe-check-one",
        "request_sha256": "4" * 64,
        "intent_sha256": "5" * 64,
        "result_manifest_sha256": "6" * 64,
        "report_sha256": "0" * 64,
        "parent_trial_id": "production_trial_one",
        "runtime_environment_sha256": "7" * 64,
        "execution_bundle_sha256": "8" * 64,
        "frozen_profiles_sha256": frozen_profiles_sha,
        "locked_profile": locked,
        "control_profile": control,
    }
    package: dict[str, Any] = {
        "schema_version": "1.0",
        "artifact_type": "ball_annotation_package",
        "session_id": SESSION_ID,
        "data_role": "check",
        "attempt_family_sha256": "a" * 64,
        "development_package_binding": {
            "session_id": "annotation-development",
            "package_sha256": "d" * 64,
            "attempt_family_sha256": "a" * 64,
        },
        "source": source,
        "operator_id": "operator-one",
        "locked_profile": locked,
        "sampling_manifest": manifest,
        "check_probe_job_id": "probe-check-one",
        "check_probe_authority": check_authority,
        "effective_annotations": effective,
        "revision_chain": revisions,
        "dataset_expansion_eligibility": {
            "eligible": False,
            "reasons": ["check_role_is_evaluation_only"],
            "validation_evidence": {
                "all_frames_human_confirmed": True,
                "all_primary_roles_complete": True,
                "all_supplemental_roles_complete": True,
                "exact_frame_media_sha256": "9" * 64,
                "frame_evidence_sha256": "0" * 64,
                "revision_chain_sha256": canonical_sha256(revisions),
                "pending_detector_candidate_count": 0,
                "pending_propagation_suggestion_count": 0,
                "pending_suggestion_decision_count": 0,
                "localizable_positive_seed_count": 15,
            },
        },
        "created_at": "2026-07-18T00:00:01+00:00",
        "training_eligible": False,
        "qualification_eligible": False,
        "pr4a_pr4b_truth_compatible": False,
    }
    package["package_sha256"] = canonical_sha256(package)
    frames = []
    for frame in frame_indices:
        candidates = (
            []
            if frame >= 75
            else [
                {
                    "frame_index": frame,
                    "bbox_source_px": [
                        _annotation(frame)["bbox_source_px"][key] for key in ("left", "top", "right", "bottom")
                    ],
                    "confidence": 0.9,
                    "class_name": "ball",
                    "coordinate_reason": "sahi_tile_offset_applied",
                }
            ]
        )
        frames.append(
            {
                "frame_index": frame,
                "profile_results": [
                    {
                        "profile_id": CONTROL_PROFILE_ID,
                        "profile_sha256": control["profile_sha256"],
                        "status": "completed",
                        "top_k": 5,
                        "candidate_count": 0,
                        "raw_candidates": [],
                        "filter_reasons": {},
                        "display_candidate": None,
                    },
                    {
                        "profile_id": LOCKED_PROFILE_ID,
                        "profile_sha256": locked["profile_sha256"],
                        "status": "completed",
                        "top_k": 5,
                        "candidate_count": len(candidates),
                        "raw_candidates": candidates,
                        "filter_reasons": {},
                        "display_candidate": candidates[0] if candidates else None,
                    },
                ],
            }
        )
    report: dict[str, Any] = {
        "schema_version": "1.0",
        "artifact_type": "detector_probe_report",
        "source": source,
        "lineage": {
            "parent_trial_id": "production_trial_one",
            "runtime_environment_sha256": "7" * 64,
            "execution_bundle_sha256": "8" * 64,
            "frozen_profiles_sha256": frozen_profiles_sha,
        },
        "frozen_profiles": frozen_profiles,
        "top_k": 5,
        "decode": {
            "width": 64,
            "height": 400,
            "frame_count": 100,
            "fps": 20.0,
            "requested_decode_mode": "preroll",
            "effective_decode_mode": "preroll_verified",
            "verified_frame_indices": frame_indices,
        },
        "frames": frames,
    }
    report["report_sha256"] = canonical_sha256(report)
    package["check_probe_authority"]["report_sha256"] = report["report_sha256"]
    package["package_sha256"] = canonical_sha256(
        {key: value for key, value in package.items() if key != "package_sha256"}
    )
    job = {
        "job_id": "probe-check-one",
        "status": "ready",
        "request_sha256": "4" * 64,
        "intent_sha256": "5" * 64,
        "result_manifest_sha256": "6" * 64,
        "frozen_request": {
            "parent_trial_id": "production_trial_one",
            "profile_ids": sorted([LOCKED_PROFILE_ID, CONTROL_PROFILE_ID]),
            "frame_indices": frame_indices,
            "top_k": 5,
            "annotation_sampling_manifest_sha256": manifest["manifest_sha256"],
        },
        "report": report,
    }
    return package, job


class FrozenFeasibilityAuthorityTests(unittest.TestCase):
    def test_sampling_selection_authority_rejects_tampering_and_free_text_fields(self) -> None:
        package, _ = _sealed_evidence()
        manifest = package["sampling_manifest"]
        for label, mutate in (
            (
                "development-package",
                lambda authority: authority.update({"development_package_sha256": "e" * 64}),
            ),
            (
                "free-text",
                lambda authority: authority.update({"operator_id": "forged"}),
            ),
        ):
            with self.subTest(label=label):
                changed = deepcopy(manifest)
                mutate(changed["selection_authority"])
                changed["selection_seed_sha256"] = canonical_sha256(changed["selection_authority"])
                with self.assertRaisesRegex(FeasibilityError, "sampling selection authority"):
                    _validate_sampling_manifest(
                        changed,
                        attempt_family_sha256="a" * 64,
                        development_package_sha256="d" * 64,
                        source_sha256=package["source"]["sha256"],
                        source_frame_count=package["source"]["frame_count"],
                        locked_profile_id=package["locked_profile"]["profile_id"],
                        locked_profile_sha256=package["locked_profile"]["profile_sha256"],
                    )

    def _score(self, package: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            path = root / "package.json"
            atomic_write_json(path, package, trusted_root=root)
            sampling_lock: dict[str, Any] = {
                "schema_version": "1.0",
                "artifact_type": "ball_annotation_sampling_lock",
                "session_id": package["session_id"],
                "sampling_manifest_sha256": _sealed_evidence()[0]["sampling_manifest"]["manifest_sha256"],
                "source_sha256": package["source"]["sha256"],
                "locked_profile_id": package["locked_profile"]["profile_id"],
                "locked_profile_sha256": package["locked_profile"]["profile_sha256"],
                "locked_before_probe": True,
                "created_at": "2026-07-18T00:00:00+00:00",
            }
            sampling_lock["lock_sha256"] = canonical_sha256(sampling_lock)
            return build_feasibility_report(
                path,
                trusted_root=root,
                get_probe=lambda job_id: (
                    deepcopy(job) if job_id == job["job_id"] else (_ for _ in ()).throw(KeyError(job_id))
                ),
                get_sampling_lock=lambda session_id: (
                    deepcopy(sampling_lock)
                    if session_id == package["session_id"]
                    else (_ for _ in ()).throw(KeyError(session_id))
                ),
            )

    def test_server_owned_sealed_package_scores_and_binds_exact_probe(self) -> None:
        package, job = _sealed_evidence()
        report = self._score(package, job)
        self.assertEqual("feasibility_passed", report["status"])
        self.assertEqual(package["package_sha256"], report["sealed_evidence"]["annotation_package_sha256"])
        self.assertEqual(job["report"]["report_sha256"], report["sealed_evidence"]["check_probe_report_sha256"])
        self.assertIn("center_distance_source_px", report["frames"][0]["candidate_diagnostics"][0])
        self.assertIn("iou", report["frames"][0]["candidate_diagnostics"][0])
        self.assertEqual(package["attempt_family_sha256"], report["attempt_family_sha256"])
        self.assertEqual(
            package["development_package_binding"],
            report["development_package_binding"],
        )

    def test_sealed_truth_contradictions_are_raw_and_require_a_new_attempt(self) -> None:
        for code, mutation in (
            (
                "lighting_strata_mismatch",
                {"lighting_tag": "shadow"},
            ),
            (
                "annotation_plausibility_contradiction",
                {
                    "bbox_source_px": {
                        "left": 4.0,
                        "top": 4.0,
                        "right": 36.0,
                        "bottom": 36.0,
                    },
                    "point_source_px": {"x": 20.0, "y": 20.0},
                },
            ),
        ):
            package, job = _sealed_evidence()
            package["effective_annotations"][0].update(mutation)
            revision = package["revision_chain"][0]
            revision["effective_annotation"].update(mutation)
            revision["annotation_etag"] = annotation_etag(
                SESSION_ID,
                revision["frame_index"],
                revision["revision"],
                revision["effective_annotation"],
            )
            package["package_sha256"] = canonical_sha256(
                {key: value for key, value in package.items() if key != "package_sha256"}
            )
            report = self._score(package, job)
            with self.subTest(code=code):
                self.assertEqual("insufficient_evidence", report["status"])
                self.assertTrue(report["resolution"]["requires_new_attempt"])
                self.assertIn(code, report["resolution"]["reason_codes"])
                self.assertEqual(14, report["metrics"]["top5_recall"]["raw"]["denominator"])

    def test_candidate_count_preserves_pre_dedup_t2_accounting(self) -> None:
        package, job = _sealed_evidence()
        result = job["report"]["frames"][0]["profile_results"][1]
        result["raw_candidates"] = [
            {
                "frame_index": 0,
                "bbox_source_px": [
                    13.0 + rank,
                    13.0,
                    27.0 + rank,
                    27.0,
                ],
                "confidence": 0.9 - rank * 0.1,
                "class_name": "ball",
                "coordinate_reason": "sahi_tile_offset_applied",
            }
            for rank in range(5)
        ]
        result["candidate_count"] = 7
        result["filter_reasons"] = {
            "duplicate_suppressed_iou": 1,
            "top_k_limit": 1,
        }
        result["display_candidate"] = result["raw_candidates"][0]
        job["report"]["report_sha256"] = canonical_sha256(
            {key: value for key, value in job["report"].items() if key != "report_sha256"}
        )
        package["check_probe_authority"]["report_sha256"] = job["report"]["report_sha256"]
        package["package_sha256"] = canonical_sha256(
            {key: value for key, value in package.items() if key != "package_sha256"}
        )
        report = self._score(package, job)
        frame = report["frames"][0]
        self.assertEqual(5, frame["scored_candidate_count"])
        self.assertEqual(7, frame["raw_candidate_count"])
        self.assertEqual(21, report["metrics"]["raw_candidates_per_evaluable_frame"]["raw"]["numerator"])

    def test_fake_digest_check_training_label_and_candidate_provenance_fail_closed(self) -> None:
        package, job = _sealed_evidence()
        package["sampling_manifest"]["manifest_sha256"] = "f" * 64
        package["package_sha256"] = canonical_sha256(
            {key: value for key, value in package.items() if key != "package_sha256"}
        )
        with self.assertRaisesRegex(FeasibilityError, "manifest digest"):
            self._score(package, job)

        package, job = _sealed_evidence()
        package["effective_annotations"][0]["training_use"] = "positive"
        revision = package["revision_chain"][0]
        revision["effective_annotation"]["training_use"] = "positive"
        revision["annotation_etag"] = annotation_etag(
            SESSION_ID, revision["frame_index"], 1, revision["effective_annotation"]
        )
        package["package_sha256"] = canonical_sha256(
            {key: value for key, value in package.items() if key != "package_sha256"}
        )
        with self.assertRaisesRegex(FeasibilityError, "truth contract"):
            self._score(package, job)

        for field, value, message in (
            ("frame_index", 999999, "frame provenance"),
            ("confidence", -999.0, "rank/confidence"),
        ):
            package, job = _sealed_evidence()
            candidate = job["report"]["frames"][0]["profile_results"][1]["raw_candidates"][0]
            candidate[field] = value
            job["report"]["report_sha256"] = canonical_sha256(
                {key: value for key, value in job["report"].items() if key != "report_sha256"}
            )
            package["check_probe_authority"]["report_sha256"] = job["report"]["report_sha256"]
            package["package_sha256"] = canonical_sha256(
                {key: value for key, value in package.items() if key != "package_sha256"}
            )
            with self.subTest(field=field), self.assertRaisesRegex(FeasibilityError, message):
                self._score(package, job)

    def test_applicability_cannot_be_empty_subset_or_post_reveal_tampered(self) -> None:
        package, job = _sealed_evidence()
        package["sampling_manifest"]["strata_applicability"]["scale"] = []
        package["sampling_manifest"]["manifest_sha256"] = canonical_sha256(
            {key: value for key, value in package["sampling_manifest"].items() if key != "manifest_sha256"}
        )
        package["package_sha256"] = canonical_sha256(
            {key: value for key, value in package.items() if key != "package_sha256"}
        )
        with self.assertRaisesRegex(FeasibilityError, "pre-reveal sampling lock"):
            self._score(package, job)

        package, job = _sealed_evidence()
        row = package["sampling_manifest"]["strata_applicability"]["lighting"][0]
        row["status"] = "not_applicable"
        package["sampling_manifest"]["manifest_sha256"] = canonical_sha256(
            {key: value for key, value in package["sampling_manifest"].items() if key != "manifest_sha256"}
        )
        package["package_sha256"] = canonical_sha256(
            {key: value for key, value in package.items() if key != "package_sha256"}
        )
        with self.assertRaisesRegex(FeasibilityError, "pre-reveal sampling lock"):
            self._score(package, job)

        package, job = _sealed_evidence()
        row = package["sampling_manifest"]["strata_applicability"]["lighting"][0]
        row["status"] = "not_applicable"
        note = row["evidence"]["note"]
        row["evidence"]["evidence_sha256"] = canonical_sha256(
            {"dimension": "lighting", "stratum": row["stratum"], "status": row["status"], "note": note}
        )
        package["sampling_manifest"]["manifest_sha256"] = canonical_sha256(
            {key: value for key, value in package["sampling_manifest"].items() if key != "manifest_sha256"}
        )
        package["package_sha256"] = canonical_sha256(
            {key: value for key, value in package.items() if key != "package_sha256"}
        )
        with self.assertRaisesRegex(FeasibilityError, "pre-reveal sampling lock"):
            self._score(package, job)

    def test_changed_weights_or_control_binding_cannot_reuse_same_ids(self) -> None:
        package, job = _sealed_evidence()
        locked = next(row for row in job["report"]["frozen_profiles"] if row["profile_id"] == LOCKED_PROFILE_ID)
        locked["model_descriptor"]["weights"]["sha256"] = "9" * 64
        job["report"]["report_sha256"] = canonical_sha256(
            {key: value for key, value in job["report"].items() if key != "report_sha256"}
        )
        package["check_probe_authority"]["report_sha256"] = job["report"]["report_sha256"]
        package["package_sha256"] = canonical_sha256(
            {key: value for key, value in package.items() if key != "package_sha256"}
        )
        with self.assertRaisesRegex(FeasibilityError, "weights binding changed"):
            self._score(package, job)


if __name__ == "__main__":
    unittest.main()
