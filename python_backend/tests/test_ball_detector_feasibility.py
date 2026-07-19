from __future__ import annotations

import time
import unittest
from math import sqrt
from unittest.mock import patch

import football_tracking.ball_detector_feasibility as feasibility_module
from football_tracking.ball_detector_feasibility import (
    FeasibilityError,
    _score_validated_feasibility,
    build_candidate_universe_authority,
    inherit_temporal_group,
    sample_unseen_temporal_groups,
    temporal_group_for_frame,
    validate_derivative_ancestry,
)

SOURCE_HEIGHT = 400
ATTEMPT_FAMILY_SHA256 = "d" * 64
DEVELOPMENT_PACKAGE_BINDING = {
    "session_id": "development-session",
    "package_sha256": "e" * 64,
    "attempt_family_sha256": ATTEMPT_FAMILY_SHA256,
}


def _positive(frame_index: int, *, scale: str, lighting: str) -> dict[str, object]:
    side = {"far": 2.0, "mid": 5.0, "near": 14.0}[scale]
    half = side / 2.0
    return {
        "frame_index": frame_index,
        "presence": "present",
        "visibility": "visible",
        "training_use": "excluded",
        "annotation_state": "confirmed",
        "bbox_source_px": {
            "left": 20.0 - half,
            "top": 20.0 - half,
            "right": 20.0 + half,
            "bottom": 20.0 + half,
        },
        "point_source_px": {"x": 20.0, "y": 20.0},
        "scale_stratum": scale,
        "lighting_tag": lighting,
        "motion_occlusion_tags": [],
    }


def _absent(frame_index: int) -> dict[str, object]:
    return {
        "frame_index": frame_index,
        "presence": "absent",
        "visibility": "not_applicable",
        "training_use": "excluded",
        "annotation_state": "confirmed",
        "bbox_source_px": None,
        "point_source_px": None,
        "scale_stratum": "not_applicable",
        "lighting_tag": "bright_sun",
        "motion_occlusion_tags": [],
    }


def _candidate(frame_index: int, x: float = 20.0, y: float = 20.0) -> dict[str, object]:
    return {
        "frame_index": frame_index,
        "bbox_source_px": [x - 1, y - 1, x + 1, y + 1],
        "confidence": 0.8,
    }


def _frozen_lighting(
    annotations: list[dict[str, object]],
) -> dict[int, str]:
    return {int(annotation["frame_index"]): str(annotation["lighting_tag"]) for annotation in annotations}


class BallDetectorFeasibilityTests(unittest.TestCase):
    def test_sampler_is_deterministic_non_overlapping_and_excludes_revealed_derivatives(self) -> None:
        source = "a" * 64
        excluded_group = temporal_group_for_frame(source, 20)
        excluded = {excluded_group["group_id"]}
        first = sample_unseen_temporal_groups(
            source_sha256=source,
            candidate_frame_indices=list(range(0, 200)),
            target_count=20,
            excluded_group_ids=excluded,
            reserved_group_ids=set(),
            excluded_groups=[excluded_group],
            seed="frozen-intent",
        )
        second = sample_unseen_temporal_groups(
            source_sha256=source,
            candidate_frame_indices=list(range(0, 200)),
            target_count=20,
            excluded_group_ids=excluded,
            reserved_group_ids=set(),
            excluded_groups=[excluded_group],
            seed="frozen-intent",
        )
        self.assertEqual(first, second)
        self.assertEqual(20, len(first))
        self.assertNotIn(next(iter(excluded)), {item["group_id"] for item in first})
        self.assertTrue(
            all(
                item["end_frame"] < excluded_group["start_frame"] or excluded_group["end_frame"] < item["start_frame"]
                for item in first
            )
        )
        spans = [(item["start_frame"], item["end_frame"]) for item in first]
        for index, span in enumerate(spans):
            for other in spans[index + 1 :]:
                self.assertTrue(span[1] < other[0] or other[1] < span[0])
        # Pre-reveal truth cannot honestly stratify by ball scale.  The sampler
        # therefore guarantees broad deterministic time-block coverage; scale
        # and lighting are enforced later as support gates.
        temporal_blocks = {min(19, (item["frame_index"] * 20) // 200) for item in first}
        self.assertEqual(set(range(20)), temporal_blocks)

    def test_sampler_fails_closed_when_unseen_support_is_short(self) -> None:
        with self.assertRaisesRegex(FeasibilityError, "unseen temporal groups"):
            sample_unseen_temporal_groups(
                source_sha256="a" * 64,
                candidate_frame_indices=list(range(10)),
                target_count=20,
                excluded_group_ids=set(),
                reserved_group_ids=set(),
                seed="short",
            )

    def test_sampler_enforces_pre_reveal_lighting_quotas_and_records_strata(self) -> None:
        sampled = sample_unseen_temporal_groups(
            source_sha256="a" * 64,
            candidate_frame_indices=range(200),
            target_count=20,
            excluded_group_ids=set(),
            reserved_group_ids=set(),
            seed="lighting-frozen-intent",
            lighting_strata=[
                {
                    "stratum": "bright_sun",
                    "quota": 10,
                    "frame_intervals": [{"start_frame": 0, "end_frame": 99}],
                },
                {
                    "stratum": "shadow",
                    "quota": 10,
                    "frame_intervals": [{"start_frame": 100, "end_frame": 199}],
                },
            ],
        )
        self.assertEqual(
            {"bright_sun": 10, "shadow": 10},
            {
                stratum: sum(item["pre_reveal_lighting_stratum"] == stratum for item in sampled)
                for stratum in ("bright_sun", "shadow")
            },
        )
        self.assertTrue(
            all(
                item["end_frame"] <= 99
                if item["pre_reveal_lighting_stratum"] == "bright_sun"
                else item["start_frame"] >= 100
                for item in sampled
            )
        )
        with self.assertRaisesRegex(FeasibilityError, "quotas"):
            sample_unseen_temporal_groups(
                source_sha256="a" * 64,
                candidate_frame_indices=range(200),
                target_count=20,
                excluded_group_ids=set(),
                reserved_group_ids=set(),
                seed="lighting-quota-mismatch",
                lighting_strata=[
                    {
                        "stratum": "bright_sun",
                        "quota": 19,
                        "frame_intervals": [{"start_frame": 0, "end_frame": 199}],
                    }
                ],
            )
        for malformed_quota in (None, True, "20"):
            with (
                self.subTest(malformed_quota=malformed_quota),
                self.assertRaisesRegex(FeasibilityError, "sampling authority"),
            ):
                sample_unseen_temporal_groups(
                    source_sha256="a" * 64,
                    candidate_frame_indices=range(200),
                    target_count=20,
                    excluded_group_ids=set(),
                    reserved_group_ids=set(),
                    seed="lighting-malformed-quota",
                    lighting_strata=[
                        {
                            "stratum": "bright_sun",
                            "quota": malformed_quota,
                            "frame_intervals": [{"start_frame": 0, "end_frame": 199}],
                        }
                    ],
                )

    def test_sampler_excludes_temporal_families_that_cross_lighting_boundaries(self) -> None:
        lighting = [
            {
                "stratum": "bright_sun",
                "quota": 10,
                "frame_intervals": [{"start_frame": 0, "end_frame": 99}],
            },
            {
                "stratum": "shadow",
                "quota": 10,
                "frame_intervals": [{"start_frame": 100, "end_frame": 199}],
            },
        ]
        bright_boundary_candidates = [
            *range(5, 95, 10),
            99,
            *range(105, 200, 10),
        ]
        with self.assertRaisesRegex(FeasibilityError, "bright_sun has only 9"):
            sample_unseen_temporal_groups(
                source_sha256="a" * 64,
                candidate_frame_indices=bright_boundary_candidates,
                target_count=20,
                excluded_group_ids=set(),
                reserved_group_ids=set(),
                seed="bright-boundary-family",
                lighting_strata=lighting,
                candidate_start_frame=0,
                candidate_end_frame=199,
                candidate_frame_count=len(bright_boundary_candidates),
            )

        shadow_boundary_candidates = [
            *range(5, 100, 10),
            100,
            *range(115, 200, 10),
        ]
        with self.assertRaisesRegex(FeasibilityError, "shadow has only 9"):
            sample_unseen_temporal_groups(
                source_sha256="a" * 64,
                candidate_frame_indices=shadow_boundary_candidates,
                target_count=20,
                excluded_group_ids=set(),
                reserved_group_ids=set(),
                seed="shadow-boundary-family",
                lighting_strata=lighting,
                candidate_start_frame=0,
                candidate_end_frame=199,
                candidate_frame_count=len(shadow_boundary_candidates),
            )

    def test_sampler_rejects_id_only_unavailable_authority(self) -> None:
        group = temporal_group_for_frame("a" * 64, 20)
        with self.assertRaisesRegex(FeasibilityError, "complete span-bound registry entries"):
            sample_unseen_temporal_groups(
                source_sha256="a" * 64,
                candidate_frame_indices=list(range(200)),
                target_count=20,
                excluded_group_ids={group["group_id"]},
                reserved_group_ids=set(),
                seed="id-only-is-not-authority",
            )

    def test_sampler_is_deterministic_and_bounded_for_large_source_range(self) -> None:
        source = "a" * 64
        lighting = [
            {
                "stratum": "bright_sun",
                "quota": 10,
                "frame_intervals": [{"start_frame": 0, "end_frame": 99_999}],
            },
            {
                "stratum": "shadow",
                "quota": 10,
                "frame_intervals": [{"start_frame": 100_000, "end_frame": 199_999}],
            },
        ]
        peak_retained_options = 0
        offer_option = feasibility_module._offer_bounded_option

        def record_bound(options: list[tuple[bytes, int]], rank: bytes, frame_index: int) -> None:
            nonlocal peak_retained_options
            offer_option(options, rank, frame_index)
            peak_retained_options = max(peak_retained_options, len(options))

        started = time.perf_counter()
        with patch.object(
            feasibility_module,
            "_offer_bounded_option",
            side_effect=record_bound,
        ):
            first = sample_unseen_temporal_groups(
                source_sha256=source,
                candidate_frame_indices=(frame for frame in range(200_000)),
                target_count=20,
                excluded_group_ids=set(),
                reserved_group_ids=set(),
                seed="large-frozen-intent",
                lighting_strata=lighting,
                candidate_start_frame=0,
                candidate_end_frame=199_999,
                candidate_frame_count=200_000,
            )
        second = sample_unseen_temporal_groups(
            source_sha256=source,
            candidate_frame_indices=range(200_000),
            target_count=20,
            excluded_group_ids=set(),
            reserved_group_ids=set(),
            seed="large-frozen-intent",
            lighting_strata=lighting,
        )
        self.assertEqual(first, second)
        self.assertEqual(20, len(first))
        self.assertLessEqual(peak_retained_options, 16)
        self.assertLess(time.perf_counter() - started, 10.0)

    def test_candidate_universe_authority_binds_intervals_exclusions_profiles_and_count(self) -> None:
        source = "a" * 64
        excluded = temporal_group_for_frame(source, 20)
        authority = build_candidate_universe_authority(
            source_sha256=source,
            start_frame=0,
            end_frame=249_999,
            lighting_strata=[
                {
                    "stratum": "bright_sun",
                    "quota": 20,
                    "frame_intervals": [{"start_frame": 0, "end_frame": 249_999}],
                }
            ],
            excluded_groups=[excluded],
        )
        self.assertEqual(250_000, authority["candidate_frame_count"])
        self.assertEqual([excluded], authority["excluded_temporal_groups"])
        self.assertNotIn("candidate_frame_indices", authority)
        changed = build_candidate_universe_authority(
            source_sha256=source,
            start_frame=0,
            end_frame=249_998,
            lighting_strata=[
                {
                    "stratum": "bright_sun",
                    "quota": 20,
                    "frame_intervals": [{"start_frame": 0, "end_frame": 249_998}],
                }
            ],
            excluded_groups=[excluded],
        )
        self.assertNotEqual(authority, changed)
        for interval in (
            {"start_frame": 1, "end_frame": 249_999},
            {"start_frame": 0, "end_frame": 250_000},
        ):
            with self.subTest(interval=interval), self.assertRaisesRegex(FeasibilityError, "candidate universe"):
                build_candidate_universe_authority(
                    source_sha256=source,
                    start_frame=0,
                    end_frame=249_999,
                    lighting_strata=[
                        {
                            "stratum": "bright_sun",
                            "quota": 20,
                            "frame_intervals": [interval],
                        }
                    ],
                    excluded_groups=[excluded],
                )

    def test_derivative_artifacts_inherit_group_and_cannot_regroup(self) -> None:
        source_group = temporal_group_for_frame("a" * 64, 20)
        inherited = inherit_temporal_group(
            source_group,
            artifact_type="proxy",
            artifact_id="proxy-frame-20",
        )
        validate_derivative_ancestry(source_group, inherited)
        self.assertEqual(source_group["group_id"], inherited["group_id"])
        recomputed = inherit_temporal_group(
            temporal_group_for_frame("a" * 64, 21),
            artifact_type="proxy",
            artifact_id="proxy-frame-20",
        )
        with self.assertRaisesRegex(FeasibilityError, "recomputed or changed"):
            validate_derivative_ancestry(source_group, recomputed)

    def test_top1_top5_are_fixed_budget_and_report_raw_counts_and_intervals(self) -> None:
        annotations: list[dict[str, object]] = []
        candidates: dict[int, list[dict[str, object]]] = {}
        scales = ["near"] * 5 + ["mid"] * 5 + ["far"] * 5
        lights = ["bright_sun"] * 8 + ["shadow"] * 7
        for frame, (scale, light) in enumerate(zip(scales, lights, strict=True)):
            annotations.append(_positive(frame, scale=scale, lighting=light))
            candidates[frame] = [_candidate(frame)] if frame < 9 else [_candidate(frame, 40, 20), _candidate(frame)]
        for frame in range(15, 20):
            annotations.append(_absent(frame))
            candidates[frame] = []

        report = _score_validated_feasibility(
            session_id="session-one",
            source_sha256="a" * 64,
            locked_profile_id="locked-profile",
            locked_profile_sha256="b" * 64,
            metric_profile_id="tiny_ball_feasibility_metric_v1",
            sampling_manifest_sha256="c" * 64,
            annotations=annotations,
            candidates_by_frame=candidates,
            source_height=SOURCE_HEIGHT,
            frozen_lighting_by_frame=_frozen_lighting(annotations),
            attempt_family_sha256=ATTEMPT_FAMILY_SHA256,
            development_package_binding=DEVELOPMENT_PACKAGE_BINDING,
            applicable_scale_strata=["near", "mid", "far"],
            applicable_lighting_strata=["bright_sun", "shadow"],
        )
        self.assertEqual("feasibility_passed", report["status"])
        self.assertEqual({"numerator": 9, "denominator": 15}, report["metrics"]["top1_recall"]["raw"])
        self.assertEqual({"numerator": 15, "denominator": 15}, report["metrics"]["top5_recall"]["raw"])
        self.assertEqual(5, report["metric_profile"]["candidate_budget"])
        self.assertIn("one_sided_95_lower", report["metrics"]["top5_recall"])
        self.assertIn("one_sided_95_upper", report["metrics"]["false_candidates_per_evaluable_frame"])
        self.assertFalse(report["authorizations"]["trial_eligible"])
        self.assertTrue(report["authorizations"]["may_expand_to_100_300_boxes"])
        self.assertEqual(
            {
                "source_height_px": SOURCE_HEIGHT,
                "plausible_diagonal_min_source_px": 1.0,
                "far_diagonal_max_source_px": 5.0,
                "mid_diagonal_max_source_px": 10.0,
                "near_diagonal_max_source_px": 30.0,
                "plausible_diagonal_max_source_px": 30.0,
                "aspect_ratio_min": 0.25,
                "aspect_ratio_max": 4.0,
                "matching_radius_cap_source_px": 80 / 9,
            },
            report["computed_source_px_bounds"],
        )
        self.assertEqual({"near", "mid", "far"}, set(report["strata_metrics"]["scale"]))
        self.assertEqual(
            {
                "bright_sun",
                "shadow",
                "backlight",
                "twilight",
                "artificial_light",
            },
            set(report["strata_metrics"]["lighting"]),
        )
        self.assertEqual(
            {
                "none",
                "ground",
                "airborne",
                "motion_blurred",
                "occluded",
                "reappearance",
                "stationary",
            },
            set(report["strata_metrics"]["motion_occlusion"]),
        )
        far = report["strata_metrics"]["scale"]["far"]
        self.assertEqual(5, far["support"]["localizable_positives"])
        self.assertEqual({"numerator": 5, "denominator": 5}, far["top5_recall"]["raw"])
        self.assertEqual(10, far["candidate_totals"]["scored"])
        self.assertEqual(10, far["candidate_totals"]["raw"])
        self.assertIn("one_sided_95_lower", far["top1_recall"])
        self.assertIn("one_sided_95_upper", far["false_candidates_per_evaluable_frame"])
        self.assertTrue(far["exploratory_small_n"])
        first_frame = report["frames"][0]
        self.assertEqual("near", first_frame["observed_scale_stratum"])
        self.assertEqual("near", first_frame["derived_scale_stratum"])
        self.assertEqual("bright_sun", first_frame["observed_lighting_tag"])
        self.assertEqual("bright_sun", first_frame["frozen_lighting_stratum"])
        self.assertEqual([], first_frame["diagnostic_codes"])

    def test_top1_failure_and_sixth_candidate_cannot_be_rescued_by_top5(self) -> None:
        annotations = [
            _positive(i, scale=("near", "mid", "far")[i % 3], lighting=("bright_sun", "shadow")[i % 2])
            for i in range(15)
        ]
        annotations.extend(_absent(i) for i in range(15, 20))
        wrong = [_candidate(0, 40 + index * 2, 20) for index in range(5)]
        candidates = {
            annotation["frame_index"]: [
                {**candidate, "frame_index": annotation["frame_index"]}
                for candidate in [*wrong, _candidate(int(annotation["frame_index"]))]
            ]
            for annotation in annotations[:15]
        }
        candidates.update({frame: [] for frame in range(15, 20)})
        report = _score_validated_feasibility(
            session_id="session-sixth",
            source_sha256="a" * 64,
            locked_profile_id="locked-profile",
            locked_profile_sha256="b" * 64,
            metric_profile_id="tiny_ball_feasibility_metric_v1",
            sampling_manifest_sha256="c" * 64,
            annotations=annotations,
            candidates_by_frame=candidates,
            source_height=SOURCE_HEIGHT,
            frozen_lighting_by_frame=_frozen_lighting(annotations),
            attempt_family_sha256=ATTEMPT_FAMILY_SHA256,
            development_package_binding=DEVELOPMENT_PACKAGE_BINDING,
            applicable_scale_strata=["near", "mid", "far"],
            applicable_lighting_strata=["bright_sun", "shadow"],
        )
        self.assertEqual("feasibility_failed", report["status"])
        self.assertEqual({"numerator": 0, "denominator": 15}, report["metrics"]["top1_recall"]["raw"])
        self.assertEqual({"numerator": 0, "denominator": 15}, report["metrics"]["top5_recall"]["raw"])

    def test_missing_support_is_insufficient_even_when_point_estimates_pass(self) -> None:
        annotations = [_positive(i, scale="far", lighting="shadow") for i in range(15)]
        candidates = {i: [_candidate(i)] for i in range(15)}
        report = _score_validated_feasibility(
            session_id="session-one",
            source_sha256="a" * 64,
            locked_profile_id="locked-profile",
            locked_profile_sha256="b" * 64,
            metric_profile_id="tiny_ball_feasibility_metric_v1",
            sampling_manifest_sha256="c" * 64,
            annotations=annotations,
            candidates_by_frame=candidates,
            source_height=SOURCE_HEIGHT,
            frozen_lighting_by_frame=_frozen_lighting(annotations),
            attempt_family_sha256=ATTEMPT_FAMILY_SHA256,
            development_package_binding=DEVELOPMENT_PACKAGE_BINDING,
            applicable_scale_strata=["near", "mid", "far"],
            applicable_lighting_strata=["bright_sun", "shadow"],
        )
        self.assertEqual("insufficient_evidence", report["status"])
        self.assertIn("total_frame_support", report["support"]["missing"])
        self.assertIn("confirmed_absent_support", report["support"]["missing"])
        self.assertFalse(report["authorizations"]["may_expand_to_100_300_boxes"])

    def test_frozen_not_applicable_stratum_contradicted_by_truth_cannot_pass(self) -> None:
        annotations = []
        scales = ["near", "mid"] + ["far"] * 13
        for frame, scale in enumerate(scales):
            annotations.append(_positive(frame, scale=scale, lighting="shadow"))
        annotations.extend(_absent(frame) for frame in range(15, 20))
        candidates = {frame: [_candidate(frame)] for frame in range(15)}
        candidates.update({frame: [] for frame in range(15, 20)})
        report = _score_validated_feasibility(
            session_id="session-contradiction",
            source_sha256="a" * 64,
            locked_profile_id="locked-profile",
            locked_profile_sha256="b" * 64,
            metric_profile_id="tiny_ball_feasibility_metric_v1",
            sampling_manifest_sha256="c" * 64,
            annotations=annotations,
            candidates_by_frame=candidates,
            source_height=SOURCE_HEIGHT,
            frozen_lighting_by_frame=_frozen_lighting(annotations),
            attempt_family_sha256=ATTEMPT_FAMILY_SHA256,
            development_package_binding=DEVELOPMENT_PACKAGE_BINDING,
            applicable_scale_strata=["far"],
            applicable_lighting_strata=["shadow"],
        )
        self.assertEqual("insufficient_evidence", report["status"])
        self.assertIn("applicability_contradiction:scale:near", report["support"]["missing"])
        self.assertIn("applicability_contradiction:scale:mid", report["support"]["missing"])

    def test_suggestions_and_unresolved_frames_never_enter_truth_denominators(self) -> None:
        annotation = _positive(1, scale="far", lighting="shadow")
        annotation["annotation_state"] = "suggested"
        with self.assertRaisesRegex(FeasibilityError, "confirmed annotations"):
            _score_validated_feasibility(
                session_id="session-one",
                source_sha256="a" * 64,
                locked_profile_id="locked-profile",
                locked_profile_sha256="b" * 64,
                metric_profile_id="tiny_ball_feasibility_metric_v1",
                sampling_manifest_sha256="c" * 64,
                annotations=[annotation],
                candidates_by_frame={1: [_candidate(1)]},
                source_height=SOURCE_HEIGHT,
                frozen_lighting_by_frame={1: "shadow"},
                attempt_family_sha256=ATTEMPT_FAMILY_SHA256,
                development_package_binding=DEVELOPMENT_PACKAGE_BINDING,
                applicable_scale_strata=["far"],
                applicable_lighting_strata=["shadow"],
            )

    def test_bbox_plausibility_and_scale_contradictions_are_excluded_from_recall(self) -> None:
        base_annotations = [
            _positive(
                frame,
                scale=("near", "mid", "far")[frame % 3],
                lighting=("bright_sun", "shadow")[frame % 2],
            )
            for frame in range(16)
        ]
        base_annotations.extend(_absent(frame) for frame in range(16, 21))
        candidates = {frame: [_candidate(frame)] for frame in range(16)}
        candidates.update({frame: [] for frame in range(16, 21)})

        adversarial_boxes = {
            "bbox_diagonal_below_minimum": {
                "left": 19.9,
                "top": 19.9,
                "right": 20.1,
                "bottom": 20.1,
            },
            "bbox_diagonal_above_maximum": {
                "left": 4.0,
                "top": 4.0,
                "right": 36.0,
                "bottom": 36.0,
            },
            "bbox_aspect_ratio_out_of_bounds": {
                "left": 19.5,
                "top": 15.0,
                "right": 20.5,
                "bottom": 25.0,
            },
        }
        for code, box in adversarial_boxes.items():
            annotations = [dict(item) for item in base_annotations]
            annotations[0] = {
                **annotations[0],
                "bbox_source_px": box,
                "point_source_px": {
                    "x": (box["left"] + box["right"]) / 2,
                    "y": (box["top"] + box["bottom"]) / 2,
                },
            }
            report = _score_validated_feasibility(
                session_id=f"session-{code}",
                source_sha256="a" * 64,
                locked_profile_id="locked-profile",
                locked_profile_sha256="b" * 64,
                metric_profile_id="tiny_ball_feasibility_metric_v1",
                sampling_manifest_sha256="c" * 64,
                annotations=annotations,
                candidates_by_frame=candidates,
                source_height=SOURCE_HEIGHT,
                frozen_lighting_by_frame=_frozen_lighting(annotations),
                attempt_family_sha256=ATTEMPT_FAMILY_SHA256,
                development_package_binding=DEVELOPMENT_PACKAGE_BINDING,
                applicable_scale_strata=["near", "mid", "far"],
                applicable_lighting_strata=["bright_sun", "shadow"],
            )
            with self.subTest(code=code):
                self.assertEqual("insufficient_evidence", report["status"])
                self.assertEqual(15, report["metrics"]["top5_recall"]["raw"]["denominator"])
                self.assertIn(code, report["frames"][0]["diagnostic_codes"])
                self.assertIn("annotation_plausibility_contradiction", report["support"]["missing"])

        annotations = [dict(item) for item in base_annotations]
        annotations[0] = {**annotations[0], "scale_stratum": "far"}
        report = _score_validated_feasibility(
            session_id="session-scale-mismatch",
            source_sha256="a" * 64,
            locked_profile_id="locked-profile",
            locked_profile_sha256="b" * 64,
            metric_profile_id="tiny_ball_feasibility_metric_v1",
            sampling_manifest_sha256="c" * 64,
            annotations=annotations,
            candidates_by_frame=candidates,
            source_height=SOURCE_HEIGHT,
            frozen_lighting_by_frame=_frozen_lighting(annotations),
            attempt_family_sha256=ATTEMPT_FAMILY_SHA256,
            development_package_binding=DEVELOPMENT_PACKAGE_BINDING,
            applicable_scale_strata=["near", "mid", "far"],
            applicable_lighting_strata=["bright_sun", "shadow"],
        )
        self.assertEqual("insufficient_evidence", report["status"])
        self.assertEqual(15, report["metrics"]["top5_recall"]["raw"]["denominator"])
        self.assertIn("scale_stratum_mismatch:far:near", report["frames"][0]["diagnostic_codes"])
        self.assertIn("scale_strata_mismatch", report["support"]["missing"])

    def test_frozen_lighting_mismatch_is_raw_insufficient_and_requires_new_attempt(self) -> None:
        annotations = [
            _positive(
                frame,
                scale=("near", "mid", "far")[frame % 3],
                lighting=("bright_sun", "shadow")[frame % 2],
            )
            for frame in range(15)
        ]
        annotations.extend(_absent(frame) for frame in range(15, 20))
        frozen = _frozen_lighting(annotations)
        frozen[0] = "shadow"
        report = _score_validated_feasibility(
            session_id="session-lighting-mismatch",
            source_sha256="a" * 64,
            locked_profile_id="locked-profile",
            locked_profile_sha256="b" * 64,
            metric_profile_id="tiny_ball_feasibility_metric_v1",
            sampling_manifest_sha256="c" * 64,
            annotations=annotations,
            candidates_by_frame={frame: [_candidate(frame)] for frame in range(15)},
            source_height=SOURCE_HEIGHT,
            frozen_lighting_by_frame=frozen,
            attempt_family_sha256=ATTEMPT_FAMILY_SHA256,
            development_package_binding=DEVELOPMENT_PACKAGE_BINDING,
            applicable_scale_strata=["near", "mid", "far"],
            applicable_lighting_strata=["bright_sun", "shadow"],
        )
        self.assertEqual("insufficient_evidence", report["status"])
        self.assertTrue(report["resolution"]["requires_new_attempt"])
        self.assertEqual(1, report["resolution"]["raw_lighting_mismatch_count"])
        self.assertIn("lighting_strata_mismatch", report["support"]["missing"])
        self.assertIn(
            "lighting_stratum_mismatch:shadow:bright_sun",
            report["frames"][0]["diagnostic_codes"],
        )
        self.assertEqual(14, report["metrics"]["top5_recall"]["raw"]["denominator"])

    def test_matching_radius_uses_source_height_cap(self) -> None:
        annotation = _positive(0, scale="near", lighting="bright_sun")
        annotations = [annotation]
        report = _score_validated_feasibility(
            session_id="session-radius-cap",
            source_sha256="a" * 64,
            locked_profile_id="locked-profile",
            locked_profile_sha256="b" * 64,
            metric_profile_id="tiny_ball_feasibility_metric_v1",
            sampling_manifest_sha256="c" * 64,
            annotations=annotations,
            candidates_by_frame={0: [_candidate(0, 30.0, 20.0)]},
            source_height=SOURCE_HEIGHT,
            frozen_lighting_by_frame={0: "bright_sun"},
            attempt_family_sha256=ATTEMPT_FAMILY_SHA256,
            development_package_binding=DEVELOPMENT_PACKAGE_BINDING,
            applicable_scale_strata=["near"],
            applicable_lighting_strata=["bright_sun"],
        )
        diagnostic = report["frames"][0]["candidate_diagnostics"][0]
        self.assertAlmostEqual(80 / 9, diagnostic["evaluation_radius_source_px"])
        self.assertFalse(diagnostic["matched"])

        far_boundary_side = (SOURCE_HEIGHT / 80) / sqrt(2)
        boundary = _positive(0, scale="far", lighting="bright_sun")
        boundary["bbox_source_px"] = {
            "left": 20 - far_boundary_side / 2,
            "top": 20 - far_boundary_side / 2,
            "right": 20 + far_boundary_side / 2,
            "bottom": 20 + far_boundary_side / 2,
        }
        boundary_report = _score_validated_feasibility(
            session_id="session-boundary",
            source_sha256="a" * 64,
            locked_profile_id="locked-profile",
            locked_profile_sha256="b" * 64,
            metric_profile_id="tiny_ball_feasibility_metric_v1",
            sampling_manifest_sha256="c" * 64,
            annotations=[boundary],
            candidates_by_frame={0: [_candidate(0)]},
            source_height=SOURCE_HEIGHT,
            frozen_lighting_by_frame={0: "bright_sun"},
            attempt_family_sha256=ATTEMPT_FAMILY_SHA256,
            development_package_binding=DEVELOPMENT_PACKAGE_BINDING,
            applicable_scale_strata=["far"],
            applicable_lighting_strata=["bright_sun"],
        )
        self.assertEqual("far", boundary_report["frames"][0]["derived_scale_stratum"])
        self.assertNotIn("scale_strata_mismatch", boundary_report["support"]["missing"])


if __name__ == "__main__":
    unittest.main()
