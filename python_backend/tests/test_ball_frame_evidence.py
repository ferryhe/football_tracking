from __future__ import annotations

import unittest
from copy import deepcopy
from unittest.mock import patch

from football_tracking.ball_detector_annotations import annotation_etag
from football_tracking.ball_detector_feasibility import (
    inherit_temporal_group,
    temporal_group_for_frame,
)
from football_tracking.ball_frame_evidence import (
    BallFrameEvidenceError,
    _attempt_family_authority,
    build_detector_probe_result_manifest_authority,
    build_frame_evidence_row,
    build_nullable_proxy_binding,
    build_source_frame_timing_binding,
    validate_detector_probe_candidate_accounting,
    validate_frame_evidence_row,
    validate_nullable_proxy_binding,
    validate_source_frame_timing_binding,
    verify_frame_evidence_package,
)
from football_tracking.detector_development_common import canonical_sha256
from football_tracking.review_proxy_mapping import build_review_proxy_manifest

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64
SHA_F = "f" * 64
SHA_0 = "0" * 64
SHA_1 = "1" * 64
SHA_2 = "2" * 64


def _timing(
    frame_index: int,
    frame_sha256: str,
    *,
    pos_msec: float | None = None,
    cross_decode_verification: dict | None = None,
) -> dict:
    observed_pos_msec = frame_index / 25.0 * 1000.0 if pos_msec is None else pos_msec
    return build_source_frame_timing_binding(
        source_sha256=SHA_A,
        runtime_environment_sha256=SHA_2,
        source_frame_jpeg_sha256=frame_sha256,
        frame_index=frame_index,
        decoded_frame_position=frame_index,
        fps=25.0,
        effective_decode_mode="preroll_verified",
        decoder_reported_pos_msec=observed_pos_msec,
        decoder_timing_observation_method=("opencv_cap_prop_pos_msec_after_verified_frame_read"),
        position_verification="opencv_next_frame_index_with_0.25_tolerance",
        true_presentation_timestamp={
            "status": "not_collected",
            "value_seconds": None,
            "method": None,
        },
        cross_decode_verification=cross_decode_verification,
    )


def _probe(
    frame_index: int,
    *,
    job_id: str,
    report_sha256: str,
    result_manifest_sha256: str,
) -> dict:
    return {
        "probe_job_id": job_id,
        "probe_report_sha256": report_sha256,
        "probe_result_manifest_sha256": result_manifest_sha256,
        "artifact_id": f"source-frame-{frame_index:09d}",
    }


def _annotation(frame_index: int) -> dict:
    return {
        "frame_index": frame_index,
        "point_source_px": None,
        "bbox_source_px": None,
        "presence": "absent",
        "visibility": "not_applicable",
        "training_use": "background",
        "annotation_state": "confirmed",
        "scale_stratum": "not_applicable",
        "lighting_tag": "bright_sun",
        "motion_occlusion_tags": [],
        "provenance": "manual_human_annotation",
    }


def _revision(frame_index: int) -> dict:
    effective = {key: value for key, value in _annotation(frame_index).items() if key != "frame_index"}
    mutation_id = f"mutation-{frame_index}"
    request = {
        "mutation_id": mutation_id,
        "expected_revision": 0,
        "operation": "set",
        "undo_revision": None,
        "annotation": effective,
        "suggestion_kind": None,
        "suggestion_id": None,
        "accepted_suggestion_job_id": None,
        "accepted_suggestion_sha256": None,
        "dismissed_suggestion_kind": None,
        "dismissed_suggestion_id": None,
        "dismissed_suggestion_job_id": None,
        "dismissed_suggestion_sha256": None,
    }
    return {
        "schema_version": "1.0",
        "artifact_type": "ball_annotation_revision",
        "revision_id": f"revision-{canonical_sha256({'session_id': 'annotation-session-1', 'frame_index': frame_index, 'revision': 1})[:24]}",
        "session_id": "annotation-session-1",
        "frame_index": frame_index,
        "revision": 1,
        "operation": "set",
        "mutation_id": mutation_id,
        "mutation_sha256": canonical_sha256(
            {
                "session_id": "annotation-session-1",
                "frame_index": frame_index,
                "request": request,
            }
        ),
        "expected_revision": 0,
        "supersedes_revision": None,
        "undo_revision": None,
        "accepted_suggestion_kind": None,
        "accepted_suggestion_id": None,
        "accepted_suggestion_job_id": None,
        "accepted_suggestion_sha256": None,
        "dismissed_suggestion_kind": None,
        "dismissed_suggestion_id": None,
        "dismissed_suggestion_job_id": None,
        "dismissed_suggestion_sha256": None,
        "previous_effective_annotation": None,
        "effective_annotation": effective,
        "operator_id": "operator-one",
        "annotation_etag": annotation_etag("annotation-session-1", frame_index, 1, effective),
        "created_at": "2026-07-18T00:00:00+00:00",
    }


def _primary_row(*, report_sha256: str = SHA_B) -> dict:
    frame_index = 10
    return build_frame_evidence_row(
        frame_role="primary",
        source={"sha256": SHA_A, "width": 5120, "height": 1440},
        frame_index=frame_index,
        source_frame_jpeg_sha256=SHA_D,
        source_frame_jpeg_size_bytes=1234,
        temporal_group=temporal_group_for_frame(SHA_A, frame_index),
        probe_evidence=_probe(
            frame_index,
            job_id="probe-primary",
            report_sha256=report_sha256,
            result_manifest_sha256=SHA_C,
        ),
        timing_binding=_timing(frame_index, SHA_D),
        proxy_binding=None,
        effective_annotation=_annotation(frame_index),
        revision_chain=[_revision(frame_index)],
        propagation_evidence=None,
    )


def _propagation_report(*, neighbor_report_sha256: str = SHA_1, neighbor_result_sha256: str = SHA_2) -> dict:
    tracker_core = {
        "profile_id": "tiny-ball-tracker-v1",
        "version": "1.0",
    }
    tracker_sha256 = canonical_sha256(tracker_core)
    seed_binding = {
        "frame_index": 10,
        "annotation_revision": 1,
    }
    target_frame_indices = [11]
    intent = {
        "session_id": "annotation-session-1",
        "mutation_id": "propagate-one",
        "seed_frame_index": 10,
        "radius_frames": 1,
        "expected_seed_revision": 1,
        "seed_binding": seed_binding,
        "target_frame_indices": target_frame_indices,
    }
    report = {
        "schema_version": "1.0",
        "artifact_type": "ball_propagation_report",
        "job_id": "propagation-job-1",
        "session_id": "annotation-session-1",
        "intent_sha256": canonical_sha256(intent),
        "mutation_id": "propagate-one",
        "seed_frame_index": 10,
        "expected_seed_revision": 1,
        "radius_frames": 1,
        "seed_binding": seed_binding,
        "seed_binding_sha256": canonical_sha256(seed_binding),
        "target_frame_indices": target_frame_indices,
        "tracker_profile": {
            **tracker_core,
            "profile_sha256": tracker_sha256,
        },
        "tracker_profile_sha256": tracker_sha256,
        "neighbor_probe_job_id": "probe-neighbor",
        "neighbor_probe_report_sha256": neighbor_report_sha256,
        "neighbor_probe_result_manifest_sha256": neighbor_result_sha256,
        "frame_results": [
            {
                "frame_index": 11,
                "status": "success",
                "pending_human_confirmation": False,
                "human_decision": {
                    "decision": "dismissed_manual_annotation",
                    "revision_id": _revision(11)["revision_id"],
                    "revision": 1,
                    "operator_id": "operator-one",
                    "decided_at": "2026-07-18T00:00:00+00:00",
                },
            }
        ],
        "suggestions": [{"suggestion_id": "propagation-suggestion-1"}],
        "summary": {
            "succeeded_frame_count": 1,
            "human_validated_frame_count": 0,
            "human_dismissed_frame_count": 1,
            "pending_human_confirmation_count": 0,
            "pending_human_confirmation": False,
        },
        "decision_counts": {"confirmed": 0, "dismissed": 1, "pending": 0},
        "created_at": "2026-07-18T00:00:00+00:00",
        "updated_at": "2026-07-18T00:00:01+00:00",
    }
    report["report_sha256"] = canonical_sha256(report)
    return report


def _supplemental_row(*, neighbor_report_sha256: str = SHA_1, neighbor_result_sha256: str = SHA_2) -> dict:
    frame_index = 11
    propagation_report = _propagation_report(
        neighbor_report_sha256=neighbor_report_sha256,
        neighbor_result_sha256=neighbor_result_sha256,
    )
    source_group = temporal_group_for_frame(SHA_A, 10)
    temporal_group = inherit_temporal_group(
        source_group,
        artifact_type="propagation",
        artifact_id=f"source-frame-{frame_index:09d}",
    )
    proxy_mapping = {
        "source_frame_index": frame_index,
        "source_timing_status": "observed",
        "source_decoder_pos_msec": 440.0,
        "proxy_frame_index": frame_index,
        "proxy_timing_basis": "verified_cfr_frame_index_time_v1",
        "proxy_cfr_time_msec": 490.0,
        "source_frame_sha256": SHA_0,
        "proxy_frame_sha256": SHA_1,
        "media_integrity": {
            "status": "ok",
            "gray": False,
            "low_information": False,
            "likely_corrupt": False,
        },
    }
    proxy = build_nullable_proxy_binding(
        {
            "proxy": {
                "sha256": SHA_E,
                "size_bytes": 98765,
                "width": 2560,
                "height": 720,
            },
            "map_sha256": canonical_sha256([proxy_mapping]),
            "source_frame": {
                "frame_index": frame_index,
                "decoder_reported_pos_msec": 440.0,
                "sha256": SHA_0,
            },
            "proxy_frame": {
                "frame_index": frame_index,
                "timing_basis": "verified_cfr_frame_index_time_v1",
                "cfr_time_msec": 490.0,
                "sha256": SHA_1,
            },
            "map_time_tolerance_msec": 1.0,
            "declared_offset_msec": 50.0,
        }
    )
    return build_frame_evidence_row(
        frame_role="supplemental",
        source={"sha256": SHA_A, "width": 5120, "height": 1440},
        frame_index=frame_index,
        source_frame_jpeg_sha256=SHA_0,
        source_frame_jpeg_size_bytes=1357,
        temporal_group=temporal_group,
        probe_evidence=_probe(
            frame_index,
            job_id="probe-neighbor",
            report_sha256=neighbor_report_sha256,
            result_manifest_sha256=neighbor_result_sha256,
        ),
        timing_binding=_timing(frame_index, SHA_0, pos_msec=440.0),
        proxy_binding=proxy,
        effective_annotation=_annotation(frame_index),
        revision_chain=[_revision(frame_index)],
        propagation_evidence={
            "propagation_job_id": "propagation-job-1",
            "neighbor_probe_job_id": "probe-neighbor",
            "neighbor_probe_report_sha256": neighbor_report_sha256,
            "neighbor_probe_result_manifest_sha256": neighbor_result_sha256,
            "neighbor_artifact_id": f"source-frame-{frame_index:09d}",
            "propagation_intent_sha256": propagation_report["intent_sha256"],
            "seed_binding_sha256": propagation_report["seed_binding_sha256"],
            "tracker_profile_sha256": propagation_report["tracker_profile_sha256"],
            "propagation_report_sha256": propagation_report["report_sha256"],
            "propagation_frame_result_sha256": canonical_sha256(propagation_report["frame_results"][0]),
            "suggestion_id": "propagation-suggestion-1",
            "suggestion_sha256": SHA_F,
        },
    )


def _package() -> dict:
    primary = _primary_row()
    proxy_fixture = _supplemental_row()
    group = temporal_group_for_frame(SHA_A, 10)
    sampling_manifest = {
        "schema_version": "1.0",
        "artifact_type": "ball_annotation_sampling_manifest",
        "source_sha256": SHA_A,
        "frame_indices": [10],
        "groups": [{**group, "frame_index": 10}],
    }
    sampling_manifest["manifest_sha256"] = canonical_sha256(sampling_manifest)
    review_proxy_manifest = build_review_proxy_manifest(
        source={
            "sha256": SHA_A,
            "file_identity_sha256": SHA_B,
            "size_bytes": 123456,
            "width": 5120,
            "height": 1440,
            "fps": 25.0,
            "frame_count": 200,
            "codec": "h264",
        },
        proxy={
            **proxy_fixture["proxy_binding"]["proxy"],
            "fps": 25.0,
            "frame_count": 200,
            "codec": "h264",
        },
        mappings=[
            {
                "source_frame_index": 11,
                "source_timing_status": "observed",
                "source_decoder_pos_msec": 440.0,
                "proxy_frame_index": 11,
                "proxy_timing_basis": "verified_cfr_frame_index_time_v1",
                "proxy_cfr_time_msec": 490.0,
                "source_frame_sha256": SHA_0,
                "proxy_frame_sha256": SHA_1,
                "media_integrity": {
                    "status": "ok",
                    "gray": False,
                    "low_information": False,
                    "likely_corrupt": False,
                },
            }
        ],
        expected_frame_indices=[11],
        decoder_fingerprint_sha256=SHA_D,
        requested_decode_mode="preroll",
        effective_decode_mode="preroll_verified",
        map_time_tolerance_msec=1.0,
        declared_offset_msec=50.0,
    )
    probe_report = {
        "schema_version": "1.0",
        "artifact_type": "detector_probe_report",
        "job_id": "probe-neighbor",
        "request_sha256": SHA_D,
        "source": {"file_identity_sha256": SHA_B},
        "lineage": {
            "frozen_profiles_sha256": SHA_E,
            "execution_bundle_sha256": SHA_D,
            "runtime_environment_sha256": SHA_2,
        },
        "review_proxy_manifest": deepcopy(review_proxy_manifest),
        "artifacts": [],
    }
    probe_report["report_sha256"] = canonical_sha256(probe_report)
    probe_result_manifest, probe_result_manifest_sha256 = build_detector_probe_result_manifest_authority(probe_report)
    probe_report_sha256 = probe_report["report_sha256"]
    supplemental = _supplemental_row(
        neighbor_report_sha256=probe_report_sha256,
        neighbor_result_sha256=probe_result_manifest_sha256,
    )
    propagation_reports = [
        _propagation_report(
            neighbor_report_sha256=probe_report_sha256,
            neighbor_result_sha256=probe_result_manifest_sha256,
        )
    ]
    frame_media = [
        {
            "frame_index": row["frame_index"],
            "relative_path": f"frames/{row['frame_index']:09d}.jpg",
            "sha256": row["source_frame_jpeg"]["sha256"],
            "size_bytes": row["source_frame_jpeg"]["size_bytes"],
            "media_type": "image/jpeg",
            "width": 5120,
            "height": 1440,
        }
        for row in (primary, supplemental)
    ]
    package = {
        "schema_version": "1.0",
        "artifact_type": "ball_annotation_package",
        "session_id": "annotation-session-1",
        "operator_id": "operator-one",
        "data_role": "development",
        "source": {
            "sha256": SHA_A,
            "width": 5120,
            "height": 1440,
            "frame_count": 200,
            "fps": 25.0,
        },
        "lineage": {
            "parent_trial_id": "production-trial-one",
            "development_probe_job_ids": ["probe-primary", "probe-neighbor"],
            "runtime_environment_sha256": SHA_2,
            "development_probe_report_sha256s": {
                "probe-primary": SHA_B,
                "probe-neighbor": probe_report_sha256,
            },
            "development_probe_result_manifest_sha256s": {
                "probe-primary": SHA_C,
                "probe-neighbor": probe_result_manifest_sha256,
            },
            "development_probe_execution_bundle_sha256s": {
                "probe-primary": SHA_1,
                "probe-neighbor": SHA_D,
            },
            "development_probe_frozen_profiles_sha256s": {
                "probe-primary": SHA_0,
                "probe-neighbor": SHA_E,
            },
            "decode": {"fps": 25.0},
        },
        "frame_review_proxy_authority": {
            "probe_job_id": "probe-neighbor",
            "probe_report_sha256": probe_report_sha256,
            "probe_result_manifest_sha256": probe_result_manifest_sha256,
            "probe_report": probe_report,
            "probe_result_manifest": probe_result_manifest,
            "review_proxy_manifest": deepcopy(review_proxy_manifest),
        },
        "locked_profile": {"profile_id": "locked-profile"},
        "control_profile_id": "control-profile",
        "control_profile": {"profile_id": "control-profile"},
        "sampling_profile_id": "tiny_ball_temporal_groups_v1",
        "metric_profile_id": "tiny_ball_feasibility_metric_v1",
        "metric_profile_sha256": SHA_E,
        "development_package_binding": None,
        "check_probe_authority": None,
        "sampling_manifest": sampling_manifest,
        "effective_annotations": [_annotation(10), _annotation(11)],
        "revision_chain": [_revision(10), _revision(11)],
        "supplemental_frame_indices": [11],
        "frame_evidence": [primary, supplemental],
        "frame_evidence_sha256": canonical_sha256([primary, supplemental]),
        "frame_media": frame_media,
        "frame_media_sha256": canonical_sha256(frame_media),
        "detector_candidate_evidence": [],
        "detector_candidate_evidence_sha256": canonical_sha256([]),
        "propagation_reports": propagation_reports,
        "propagation_reports_sha256": canonical_sha256(propagation_reports),
    }
    package["attempt_family_sha256"] = canonical_sha256(_attempt_family_authority(package))
    package["may_seed_dataset_expansion"] = False
    package["dataset_expansion_eligibility"] = {
        "eligible": False,
        "reasons": [
            "no_localizable_positive_seed",
        ],
        "validation_evidence": {
            "all_frames_human_confirmed": True,
            "all_primary_roles_complete": True,
            "all_supplemental_roles_complete": True,
            "exact_frame_media_sha256": package["frame_media_sha256"],
            "frame_evidence_sha256": package["frame_evidence_sha256"],
            "revision_chain_sha256": canonical_sha256(package["revision_chain"]),
            "pending_propagation_suggestion_count": 0,
            "pending_detector_candidate_count": 0,
            "pending_suggestion_decision_count": 0,
            "localizable_positive_seed_count": 0,
        },
    }
    package["package_sha256"] = canonical_sha256(package)
    return package


def _reseal_package(package: dict) -> None:
    package["frame_evidence_sha256"] = canonical_sha256(package["frame_evidence"])
    package["package_sha256"] = canonical_sha256(
        {key: value for key, value in package.items() if key != "package_sha256"}
    )


class DetectorCandidateAccountingTests(unittest.TestCase):
    def test_accepts_duplicate_suppression_and_top_k_accounting(self) -> None:
        raw_candidates = [{"bbox_source_px": [index, 1, index + 1, 2], "confidence": 0.5} for index in range(5)]
        for candidate_count, reasons, expected in (
            (
                3,
                {"duplicate_suppressed_iou": 2},
                raw_candidates[:1],
            ),
            (
                7,
                {"top_k_limit": 2},
                raw_candidates,
            ),
            (
                9,
                {"duplicate_suppressed_iou": 2, "top_k_limit": 2},
                raw_candidates,
            ),
        ):
            with self.subTest(
                candidate_count=candidate_count,
                reasons=reasons,
            ):
                profile_result = {
                    "status": "completed",
                    "top_k": 5,
                    "candidate_count": candidate_count,
                    "filter_reasons": reasons,
                    "raw_candidates": expected,
                    "display_candidate": expected[0],
                }
                self.assertEqual(
                    expected,
                    validate_detector_probe_candidate_accounting(profile_result),
                )

    def test_rejects_incoherent_duplicate_and_top_k_accounting(self) -> None:
        candidate = {"bbox_source_px": [1, 1, 2, 2], "confidence": 0.5}
        for candidate_count, reasons, raw_candidates in (
            (1, {"duplicate_suppressed_iou": 1}, [candidate]),
            (7, {"top_k_limit": 1}, [candidate] * 5),
        ):
            with self.subTest(
                candidate_count=candidate_count,
                reasons=reasons,
            ):
                with self.assertRaises(BallFrameEvidenceError):
                    validate_detector_probe_candidate_accounting(
                        {
                            "status": "completed",
                            "top_k": 5,
                            "candidate_count": candidate_count,
                            "filter_reasons": reasons,
                            "raw_candidates": raw_candidates,
                            "display_candidate": raw_candidates[0],
                        }
                    )


class SourceFrameTimingBindingTests(unittest.TestCase):
    def test_binds_raw_decoder_position_time_and_keeps_index_time_display_only(self) -> None:
        binding = _timing(11, SHA_0, pos_msec=550.0)

        self.assertEqual(
            "verified_decoder_pos_msec_after_frame_position_v1",
            binding["timing_profile_id"],
        )
        self.assertEqual(550.0, binding["decoder_reported_pos_msec"])
        self.assertEqual(0.55, binding["decoder_time_seconds"])
        self.assertEqual(11 / 25.0, binding["display_time_seconds"])
        self.assertEqual(11, binding["decoded_frame_position"])
        self.assertEqual(
            "frame_index_divided_by_fps_for_display_only_not_source_pts",
            binding["display_time_derivation"],
        )
        self.assertEqual(
            {
                "status": "not_collected",
                "value_seconds": None,
                "method": None,
            },
            binding["true_presentation_timestamp"],
        )
        self.assertIsNone(binding["cross_decode_verification"])
        self.assertEqual(binding, validate_source_frame_timing_binding(binding))

    def test_allows_finite_negative_startup_decoder_position_time(self) -> None:
        binding = _timing(0, SHA_D, pos_msec=-50.0)

        self.assertEqual(-50.0, binding["decoder_reported_pos_msec"])
        self.assertEqual(-0.05, binding["decoder_time_seconds"])
        self.assertEqual(binding, validate_source_frame_timing_binding(binding))

    def test_optional_cross_decode_evidence_binds_same_position_time_and_jpeg(self) -> None:
        cross = {
            "method": "decoder_pos_msec_and_frame_digest_agreement_v1",
            "tolerance_msec": 0.5,
            "observations": [
                {
                    "effective_decode_mode": "direct_verified",
                    "decoded_frame_position": 11,
                    "decoder_reported_pos_msec": 550.0,
                    "source_frame_jpeg_sha256": SHA_0,
                },
                {
                    "effective_decode_mode": "preroll_verified",
                    "decoded_frame_position": 11,
                    "decoder_reported_pos_msec": 550.0,
                    "source_frame_jpeg_sha256": SHA_0,
                },
            ],
        }

        binding = _timing(
            11,
            SHA_0,
            pos_msec=550.0,
            cross_decode_verification=cross,
        )

        self.assertEqual(
            "decoder_pos_msec_and_frame_digest_agreement_v1",
            binding["cross_decode_verification"]["method"],
        )
        self.assertEqual(binding, validate_source_frame_timing_binding(binding))

    def test_rejects_resealed_missing_or_noncanonical_decoder_timing(self) -> None:
        for field, value in (
            ("decoder_reported_pos_msec", float("nan")),
            ("decoder_time_seconds", 9.5),
            ("decoded_frame_position", 12),
            ("decoder_timing_observation_method", "derived_from_fps"),
            ("display_time_derivation", "source_pts"),
        ):
            with self.subTest(field=field):
                binding = _timing(11, SHA_0, pos_msec=550.0)
                binding[field] = value
                if field != "decoder_reported_pos_msec":
                    binding["timing_binding_sha256"] = canonical_sha256(
                        {key: item for key, item in binding.items() if key != "timing_binding_sha256"}
                    )
                with self.assertRaises(BallFrameEvidenceError):
                    validate_source_frame_timing_binding(binding)

    def test_true_presentation_timestamp_cannot_be_claimed_without_verified_contract(self) -> None:
        forged_values = (
            {
                "status": "verified",
                "value_seconds": 0.44,
                "method": "container_pts_v1",
            },
            {
                "status": "not_collected",
                "value_seconds": 0.44,
                "method": None,
            },
            {
                "status": "not_collected",
                "value_seconds": None,
                "method": "opencv_pos_msec",
            },
        )
        for true_pts in forged_values:
            with self.subTest(true_pts=true_pts):
                binding = _timing(11, SHA_0, pos_msec=550.0)
                binding["true_presentation_timestamp"] = true_pts
                binding["timing_binding_sha256"] = canonical_sha256(
                    {key: item for key, item in binding.items() if key != "timing_binding_sha256"}
                )
                with self.assertRaises(BallFrameEvidenceError):
                    validate_source_frame_timing_binding(binding)

    def test_true_presentation_timestamp_is_part_of_timing_digest(self) -> None:
        binding = _timing(11, SHA_0, pos_msec=550.0)
        binding["true_presentation_timestamp"] = {
            "status": "not_collected",
            "value_seconds": None,
            "method": "forged_method",
        }

        with self.assertRaises(BallFrameEvidenceError):
            validate_source_frame_timing_binding(binding)

    def test_declared_cross_decode_disagreement_fails(self) -> None:
        cross = {
            "method": "decoder_pos_msec_and_frame_digest_agreement_v1",
            "tolerance_msec": 0.5,
            "observations": [
                {
                    "effective_decode_mode": "direct_verified",
                    "decoded_frame_position": 11,
                    "decoder_reported_pos_msec": 550.0,
                    "source_frame_jpeg_sha256": SHA_0,
                },
                {
                    "effective_decode_mode": "preroll_verified",
                    "decoded_frame_position": 11,
                    "decoder_reported_pos_msec": 600.0,
                    "source_frame_jpeg_sha256": SHA_D,
                },
            ],
        }

        with self.assertRaises(BallFrameEvidenceError):
            _timing(
                11,
                SHA_0,
                pos_msec=550.0,
                cross_decode_verification=cross,
            )


class ProxyFrameBindingTests(unittest.TestCase):
    def test_none_is_a_valid_explicit_no_proxy_binding(self) -> None:
        self.assertIsNone(build_nullable_proxy_binding(None))
        self.assertIsNone(validate_nullable_proxy_binding(None))

    def test_uncollected_source_time_and_verified_proxy_cfr_stay_separate(self) -> None:
        timing = build_source_frame_timing_binding(
            source_sha256=SHA_A,
            runtime_environment_sha256=SHA_2,
            source_frame_jpeg_sha256=SHA_D,
            frame_index=10,
            decoded_frame_position=10,
            fps=25.0,
            effective_decode_mode="preroll_verified",
            decoder_reported_pos_msec=None,
            decoder_timing_observation_method=None,
            position_verification="verified_review_proxy_frame_index_mapping_v1",
            true_presentation_timestamp={
                "status": "not_collected",
                "value_seconds": None,
                "method": None,
            },
            timing_status="not_collected",
        )
        proxy = build_nullable_proxy_binding(
            {
                "proxy": {
                    "sha256": SHA_E,
                    "size_bytes": 98765,
                    "width": 2560,
                    "height": 720,
                },
                "map_sha256": SHA_F,
                "source_frame": {
                    "frame_index": 10,
                    "timing_status": "not_collected",
                    "decoder_reported_pos_msec": None,
                    "sha256": SHA_D,
                },
                "proxy_frame": {
                    "frame_index": 10,
                    "timing_basis": "verified_cfr_frame_index_time_v1",
                    "cfr_time_msec": 400.0,
                    "sha256": SHA_1,
                },
                "map_time_tolerance_msec": 1.0,
                "declared_offset_msec": 0.0,
            }
        )

        self.assertEqual("not_collected", timing["timing_status"])
        self.assertIsNone(timing["decoder_reported_pos_msec"])
        self.assertEqual(
            "verified_cfr_frame_index_time_v1",
            proxy["proxy_frame"]["timing_basis"],
        )
        self.assertEqual(400.0, proxy["proxy_frame"]["cfr_time_msec"])
        self.assertNotIn("decoder_reported_pos_msec", proxy["proxy_frame"])
        self.assertEqual(timing, validate_source_frame_timing_binding(timing))
        self.assertEqual(proxy, validate_nullable_proxy_binding(proxy))
        with self.assertRaisesRegex(BallFrameEvidenceError, "requires verified proxy CFR evidence"):
            build_frame_evidence_row(
                frame_role="primary",
                source={"sha256": SHA_A, "width": 5120, "height": 1440},
                frame_index=10,
                source_frame_jpeg_sha256=SHA_D,
                source_frame_jpeg_size_bytes=1234,
                temporal_group=temporal_group_for_frame(SHA_A, 10),
                probe_evidence=_probe(
                    10,
                    job_id="probe-primary",
                    report_sha256=SHA_B,
                    result_manifest_sha256=SHA_C,
                ),
                timing_binding=timing,
                proxy_binding=None,
                effective_annotation=_annotation(10),
                revision_chain=[_revision(10)],
                propagation_evidence=None,
            )

    def test_binds_proxy_media_map_and_both_exact_frames(self) -> None:
        binding = _supplemental_row()["proxy_binding"]

        self.assertEqual(SHA_E, binding["proxy"]["sha256"])
        self.assertEqual(
            _package()["frame_review_proxy_authority"]["review_proxy_manifest"]["mapping_sha256"],
            binding["map_sha256"],
        )
        self.assertEqual(11, binding["source_frame"]["frame_index"])
        self.assertEqual(440.0, binding["source_frame"]["decoder_reported_pos_msec"])
        self.assertEqual(50.0, binding["time_mapping"]["observed_offset_msec"])
        self.assertEqual(SHA_1, binding["proxy_frame"]["sha256"])
        self.assertEqual(binding, validate_nullable_proxy_binding(binding))

    def test_proxy_map_allows_finite_negative_source_time(self) -> None:
        binding = build_nullable_proxy_binding(
            {
                "proxy": {
                    "sha256": SHA_E,
                    "size_bytes": 98765,
                    "width": 2560,
                    "height": 720,
                },
                "map_sha256": SHA_F,
                "source_frame": {
                    "frame_index": 0,
                    "decoder_reported_pos_msec": -50.0,
                    "sha256": SHA_0,
                },
                "proxy_frame": {
                    "frame_index": 0,
                    "timing_basis": "verified_cfr_frame_index_time_v1",
                    "cfr_time_msec": 0.0,
                    "sha256": SHA_1,
                },
                "map_time_tolerance_msec": 1.0,
                "declared_offset_msec": 50.0,
            }
        )

        self.assertEqual(-50.0, binding["source_frame"]["decoder_reported_pos_msec"])
        self.assertEqual(binding, validate_nullable_proxy_binding(binding))

    def test_row_rejects_proxy_bound_to_a_different_source_frame(self) -> None:
        row = _supplemental_row()
        proxy = deepcopy(row["proxy_binding"])
        proxy["source_frame"]["sha256"] = SHA_D
        proxy["binding_sha256"] = canonical_sha256(
            {key: value for key, value in proxy.items() if key != "binding_sha256"}
        )
        row["proxy_binding"] = proxy
        row["frame_evidence_sha256"] = canonical_sha256(
            {key: value for key, value in row.items() if key != "frame_evidence_sha256"}
        )

        with self.assertRaises(BallFrameEvidenceError):
            validate_frame_evidence_row(row)


class FrameEvidencePackageTests(unittest.TestCase):
    def setUp(self) -> None:
        # This legacy synthetic package exercises frame/proxy/propagation
        # invariants without carrying complete detector job records. Authority
        # closure is covered by the service and real-fixture suites.
        self._authority_patch = patch(
            "football_tracking.ball_frame_evidence._sealed_detector_probe_authorities",
            return_value={},
        )
        self._candidate_patch = patch(
            "football_tracking.ball_frame_evidence._sealed_detector_candidate_evidence",
            return_value=0,
        )
        self._request_authority_patch = patch(
            "football_tracking.ball_frame_evidence._sealed_session_request_authority",
            return_value={},
        )
        self._profile_selection_patch = patch(
            "football_tracking.ball_frame_evidence._verify_sealed_profile_selection",
            return_value=None,
        )
        self._authority_patch.start()
        self._candidate_patch.start()
        self._request_authority_patch.start()
        self._profile_selection_patch.start()

    def tearDown(self) -> None:
        self._profile_selection_patch.stop()
        self._request_authority_patch.stop()
        self._candidate_patch.stop()
        self._authority_patch.stop()

    def test_verifies_one_sealed_row_per_effective_primary_and_supplemental_frame(self) -> None:
        package = _package()

        rows = verify_frame_evidence_package(package)

        self.assertEqual([10, 11], [row["frame_index"] for row in rows])
        self.assertEqual("primary", rows[0]["frame_role"])
        self.assertEqual("supplemental", rows[1]["frame_role"])

    def test_rejects_arbitrary_review_proxy_manifest_digest_after_outer_reseal(
        self,
    ) -> None:
        package = _package()
        package["frame_review_proxy_authority"]["review_proxy_manifest"]["manifest_sha256"] = SHA_F
        _reseal_package(package)

        with self.assertRaisesRegex(BallFrameEvidenceError, "proxy manifest authority is not canonical"):
            verify_frame_evidence_package(package)

    def test_rejects_cross_probe_proxy_authority_after_outer_reseal(self) -> None:
        package = _package()
        authority = package["frame_review_proxy_authority"]
        authority["probe_job_id"] = "probe-primary"
        authority["probe_report_sha256"] = SHA_B
        authority["probe_result_manifest_sha256"] = SHA_C
        _reseal_package(package)

        with self.assertRaisesRegex(BallFrameEvidenceError, "changed from its child report/result manifest"):
            verify_frame_evidence_package(package)

    def test_rejects_proxy_authority_digest_not_bound_to_probe_lineage(self) -> None:
        package = _package()
        package["lineage"]["development_probe_report_sha256s"]["probe-neighbor"] = SHA_D
        package["attempt_family_sha256"] = canonical_sha256(_attempt_family_authority(package))
        _reseal_package(package)

        with self.assertRaisesRegex(BallFrameEvidenceError, "changed from development lineage"):
            verify_frame_evidence_package(package)

    def test_rejects_coherent_proxy_manifest_and_row_swap_with_old_child_result(
        self,
    ) -> None:
        package = _package()
        authority = package["frame_review_proxy_authority"]
        old_manifest = authority["review_proxy_manifest"]
        mapping = deepcopy(old_manifest["mappings"][0])
        mapping["proxy_frame_sha256"] = SHA_F
        swapped_manifest = build_review_proxy_manifest(
            source=old_manifest["source"],
            proxy={**old_manifest["proxy"], "sha256": SHA_F},
            mappings=[mapping],
            expected_frame_indices=old_manifest["expected_frame_indices"],
            decoder_fingerprint_sha256=old_manifest["decoder_fingerprint_sha256"],
            requested_decode_mode=old_manifest["requested_decode_mode"],
            effective_decode_mode=old_manifest["effective_decode_mode"],
            map_time_tolerance_msec=old_manifest["map_time_tolerance_msec"],
            declared_offset_msec=old_manifest["declared_offset_msec"],
        )
        authority["review_proxy_manifest"] = swapped_manifest
        row = package["frame_evidence"][1]
        proxy = row["proxy_binding"]
        proxy["proxy"]["sha256"] = SHA_F
        proxy["map_sha256"] = swapped_manifest["mapping_sha256"]
        proxy["proxy_frame"]["sha256"] = SHA_F
        proxy["binding_sha256"] = canonical_sha256(
            {key: value for key, value in proxy.items() if key != "binding_sha256"}
        )
        row["frame_evidence_sha256"] = canonical_sha256(
            {key: value for key, value in row.items() if key != "frame_evidence_sha256"}
        )
        package["dataset_expansion_eligibility"]["validation_evidence"]["frame_evidence_sha256"] = canonical_sha256(
            package["frame_evidence"]
        )
        _reseal_package(package)

        with self.assertRaisesRegex(
            BallFrameEvidenceError,
            "frame proxy manifest changed from its child probe report",
        ):
            verify_frame_evidence_package(package)

    def test_check_sampling_group_keeps_pre_reveal_lighting_outside_row_ancestry(self) -> None:
        package = _package()
        package["data_role"] = "check"
        package["development_package_binding"] = {
            "session_id": "development-session-one",
            "package_sha256": SHA_F,
            "attempt_family_sha256": package["attempt_family_sha256"],
        }
        package["dataset_expansion_eligibility"]["reasons"] = ["check_role_is_evaluation_only"]
        package["effective_annotations"] = [package["effective_annotations"][0]]
        package["revision_chain"] = [package["revision_chain"][0]]
        package["supplemental_frame_indices"] = []
        package["frame_evidence"] = [package["frame_evidence"][0]]
        package["frame_review_proxy_authority"] = None
        package["frame_media"] = [package["frame_media"][0]]
        package["frame_media_sha256"] = canonical_sha256(package["frame_media"])
        package["propagation_reports"] = []
        package["propagation_reports_sha256"] = canonical_sha256([])
        package["check_probe_authority"] = {
            "job_id": "probe-primary",
            "report_sha256": SHA_B,
            "result_manifest_sha256": SHA_C,
            "runtime_environment_sha256": SHA_2,
        }
        group = package["sampling_manifest"]["groups"][0]
        group["pre_reveal_lighting_stratum"] = "bright_sun"
        annotation = package["effective_annotations"][0]
        annotation["training_use"] = "excluded"
        revision = package["revision_chain"][0]
        revision["effective_annotation"]["training_use"] = "excluded"
        revision["mutation_sha256"] = canonical_sha256(
            {
                "session_id": package["session_id"],
                "frame_index": annotation["frame_index"],
                "request": {
                    "mutation_id": revision["mutation_id"],
                    "expected_revision": 0,
                    "operation": "set",
                    "undo_revision": None,
                    "annotation": revision["effective_annotation"],
                    "suggestion_kind": None,
                    "suggestion_id": None,
                    "accepted_suggestion_job_id": None,
                    "accepted_suggestion_sha256": None,
                    "dismissed_suggestion_kind": None,
                    "dismissed_suggestion_id": None,
                    "dismissed_suggestion_job_id": None,
                    "dismissed_suggestion_sha256": None,
                },
            }
        )
        validation = package["dataset_expansion_eligibility"]["validation_evidence"]
        validation["pending_propagation_suggestion_count"] = 0
        validation["pending_suggestion_decision_count"] = 0
        validation["exact_frame_media_sha256"] = package["frame_media_sha256"]
        validation["frame_evidence_sha256"] = canonical_sha256(package["frame_evidence"])
        validation["revision_chain_sha256"] = canonical_sha256(package["revision_chain"])
        revision["annotation_etag"] = annotation_etag(
            package["session_id"],
            annotation["frame_index"],
            1,
            revision["effective_annotation"],
        )
        row = package["frame_evidence"][0]
        row["effective_annotation_sha256"] = canonical_sha256(annotation)
        row["revision_chain_sha256"] = canonical_sha256([revision])
        row["frame_evidence_sha256"] = canonical_sha256(
            {key: value for key, value in row.items() if key != "frame_evidence_sha256"}
        )
        validation["frame_evidence_sha256"] = canonical_sha256(package["frame_evidence"])
        validation["revision_chain_sha256"] = canonical_sha256(package["revision_chain"])
        package["sampling_manifest"]["manifest_sha256"] = canonical_sha256(
            {key: value for key, value in package["sampling_manifest"].items() if key != "manifest_sha256"}
        )
        _reseal_package(package)

        rows = verify_frame_evidence_package(package)

        self.assertNotIn("pre_reveal_lighting_stratum", rows[0]["temporal_group"])

    def test_missing_supplemental_evidence_fails_even_after_package_is_resealed(self) -> None:
        package = _package()
        package["frame_evidence"] = [package["frame_evidence"][0]]
        _reseal_package(package)

        with self.assertRaises(BallFrameEvidenceError):
            verify_frame_evidence_package(package)

    def test_primary_and_supplemental_lists_cannot_overlap(self) -> None:
        package = _package()
        package["supplemental_frame_indices"] = [10, 11]
        _reseal_package(package)

        with self.assertRaises(BallFrameEvidenceError):
            verify_frame_evidence_package(package)

    def test_resealed_probe_report_tamper_fails_against_package_lineage(self) -> None:
        package = _package()
        package["frame_evidence"][0] = _primary_row(report_sha256=SHA_D)
        _reseal_package(package)

        with self.assertRaises(BallFrameEvidenceError):
            verify_frame_evidence_package(package)

    def test_missing_supplemental_propagation_or_suggestion_lineage_fails(self) -> None:
        for field in ("neighbor_probe_report_sha256", "suggestion_sha256"):
            with self.subTest(field=field):
                package = _package()
                row = package["frame_evidence"][1]
                propagation = row["propagation_evidence"]
                propagation.pop(field)
                propagation["binding_sha256"] = canonical_sha256(
                    {key: value for key, value in propagation.items() if key != "binding_sha256"}
                )
                row["frame_evidence_sha256"] = canonical_sha256(
                    {key: value for key, value in row.items() if key != "frame_evidence_sha256"}
                )
                _reseal_package(package)

                with self.assertRaises(BallFrameEvidenceError):
                    verify_frame_evidence_package(package)

    def test_failed_propagation_allows_null_suggestion_but_rejects_partial_pair(self) -> None:
        row = _supplemental_row()
        core = {
            key: deepcopy(row["propagation_evidence"][key])
            for key in (
                "propagation_job_id",
                "neighbor_probe_job_id",
                "neighbor_probe_report_sha256",
                "neighbor_probe_result_manifest_sha256",
                "neighbor_artifact_id",
                "propagation_frame_result_sha256",
                "propagation_intent_sha256",
                "seed_binding_sha256",
                "tracker_profile_sha256",
                "propagation_report_sha256",
                "suggestion_id",
                "suggestion_sha256",
            )
        }
        core["suggestion_id"] = None
        core["suggestion_sha256"] = None
        failed = build_frame_evidence_row(
            frame_role="supplemental",
            source={"sha256": SHA_A, "width": 5120, "height": 1440},
            frame_index=11,
            source_frame_jpeg_sha256=SHA_0,
            source_frame_jpeg_size_bytes=1357,
            temporal_group=row["temporal_group"],
            probe_evidence=_probe(
                11,
                job_id="probe-neighbor",
                report_sha256=SHA_1,
                result_manifest_sha256=SHA_2,
            ),
            timing_binding=_timing(11, SHA_0, pos_msec=440.0),
            proxy_binding=row["proxy_binding"],
            effective_annotation=_annotation(11),
            revision_chain=[_revision(11)],
            propagation_evidence=core,
        )
        self.assertIsNone(failed["propagation_evidence"]["suggestion_id"])
        self.assertIsNone(failed["propagation_evidence"]["suggestion_sha256"])

        core["suggestion_sha256"] = SHA_F
        with self.assertRaises(BallFrameEvidenceError):
            build_frame_evidence_row(
                frame_role="supplemental",
                source={"sha256": SHA_A, "width": 5120, "height": 1440},
                frame_index=11,
                source_frame_jpeg_sha256=SHA_0,
                source_frame_jpeg_size_bytes=1357,
                temporal_group=row["temporal_group"],
                probe_evidence=_probe(
                    11,
                    job_id="probe-neighbor",
                    report_sha256=SHA_1,
                    result_manifest_sha256=SHA_2,
                ),
                timing_binding=_timing(11, SHA_0, pos_msec=440.0),
                proxy_binding=row["proxy_binding"],
                effective_annotation=_annotation(11),
                revision_chain=[_revision(11)],
                propagation_evidence=core,
            )

    def test_resealed_annotation_or_revision_tamper_fails_row_binding(self) -> None:
        for collection, index in (("effective_annotations", 0), ("revision_chain", 0)):
            with self.subTest(collection=collection):
                package = _package()
                package[collection][index]["tampered"] = True
                _reseal_package(package)

                with self.assertRaises(BallFrameEvidenceError):
                    verify_frame_evidence_package(package)

    def test_all_zero_decoder_time_sentinel_fails_across_distinct_frames(self) -> None:
        package = _package()
        row = package["frame_evidence"][1]
        row["timing_binding"] = _timing(11, SHA_0, pos_msec=400.0)
        row["proxy_binding"] = None
        row["frame_evidence_sha256"] = canonical_sha256(
            {key: value for key, value in row.items() if key != "frame_evidence_sha256"}
        )
        _reseal_package(package)

        with self.assertRaises(BallFrameEvidenceError):
            verify_frame_evidence_package(package)

    def test_every_effective_annotation_requires_a_revision_row(self) -> None:
        package = _package()
        package["revision_chain"] = [package["revision_chain"][0]]
        supplemental = package["frame_evidence"][1]
        supplemental["revision_chain_sha256"] = canonical_sha256([])
        supplemental["frame_evidence_sha256"] = canonical_sha256(
            {key: value for key, value in supplemental.items() if key != "frame_evidence_sha256"}
        )
        _reseal_package(package)

        with self.assertRaises(BallFrameEvidenceError):
            verify_frame_evidence_package(package)

    def test_revision_gap_or_supersession_tamper_fails_after_full_reseal(self) -> None:
        for revision_number, supersedes_revision in ((3, 1), (2, None)):
            with self.subTest(
                revision_number=revision_number,
                supersedes_revision=supersedes_revision,
            ):
                package = _package()
                new_revision = {
                    "frame_index": 10,
                    "revision": revision_number,
                    "supersedes_revision": supersedes_revision,
                    "effective_annotation": {
                        key: value for key, value in _annotation(10).items() if key != "frame_index"
                    },
                }
                package["revision_chain"].append(new_revision)
                frame_revisions = [package["revision_chain"][0], new_revision]
                primary = package["frame_evidence"][0]
                primary["effective_revision"] = revision_number
                primary["revision_chain_sha256"] = canonical_sha256(frame_revisions)
                primary["frame_evidence_sha256"] = canonical_sha256(
                    {key: value for key, value in primary.items() if key != "frame_evidence_sha256"}
                )
                _reseal_package(package)

                with self.assertRaises(BallFrameEvidenceError):
                    verify_frame_evidence_package(package)

    def test_revision_chain_must_end_at_effective_annotation_and_revision(self) -> None:
        package = _package()
        primary = package["frame_evidence"][0]
        primary["effective_revision"] = 2
        primary["frame_evidence_sha256"] = canonical_sha256(
            {key: value for key, value in primary.items() if key != "frame_evidence_sha256"}
        )
        _reseal_package(package)

        with self.assertRaises(BallFrameEvidenceError):
            verify_frame_evidence_package(package)

    def test_duplicate_or_revision_only_frame_without_evidence_fails(self) -> None:
        duplicate = _package()
        duplicate["frame_evidence"].append(deepcopy(duplicate["frame_evidence"][0]))
        _reseal_package(duplicate)
        with self.assertRaises(BallFrameEvidenceError):
            verify_frame_evidence_package(duplicate)

        revision_only = _package()
        revision_only["revision_chain"].append(_revision(12))
        _reseal_package(revision_only)
        with self.assertRaises(BallFrameEvidenceError):
            verify_frame_evidence_package(revision_only)


if __name__ == "__main__":
    unittest.main()
