from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from football_tracking.ball_audit import write_ball_audit_report
from football_tracking.noise_candidate_comparison import (
    build_noise_candidate_comparison,
    execute_noise_cleanup_candidate,
)


class NoiseCandidateComparisonTests(unittest.TestCase):
    def test_candidate_passes_when_short_false_positive_islands_are_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            baseline = output_dir / "baseline.csv"
            candidate = output_dir / "candidate.csv"
            true_run = [(10, 24)]
            noise_runs = [(30, 31), (40, 40), (50, 51), (70, 70), (80, 80)]
            _write_track(baseline, detected_ranges=[*true_run, *noise_runs], frame_count=100)
            _write_track(candidate, detected_ranges=true_run, frame_count=100)

            report = build_noise_candidate_comparison(
                baseline,
                candidate,
                candidate_id="noise-pass",
                approval=_approval("noise-pass"),
                target_window={"start_frame": 0, "end_frame": 99},
            )

        self.assertEqual("noise", report["problem_type"])
        self.assertEqual("pass", report["summary"]["status"])
        self.assertEqual(5, report["metrics"]["baseline_false_positive_islands"])
        self.assertEqual(0, report["metrics"]["candidate_false_positive_islands"])
        self.assertEqual(
            noise_runs,
            [
                (item["start_frame"], item["end_frame"])
                for item in report["metrics"]["removed_false_positive_island_ranges"]
            ],
        )

    def test_candidate_warns_for_small_useful_decrease_without_coverage_loss(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            baseline = output_dir / "baseline.csv"
            candidate = output_dir / "candidate.csv"
            true_run = [(10, 24)]
            noise_runs = [(40 + index * 5, 40 + index * 5) for index in range(10)]
            _write_track(baseline, detected_ranges=[*true_run, *noise_runs], frame_count=120)
            _write_track(candidate, detected_ranges=[*true_run, *noise_runs[1:]], frame_count=120)

            report = build_noise_candidate_comparison(
                baseline,
                candidate,
                candidate_id="noise-warn",
                approval=_approval("noise-warn"),
                target_window={"start_frame": 0, "end_frame": 119},
            )

        self.assertEqual("warn", report["summary"]["status"])
        reduction = next(check for check in report["checks"] if check["name"] == "false_positive_island_reduction")
        self.assertEqual("warn", reduction["status"])
        self.assertEqual(1, reduction["absolute_decrease"])

    def test_candidate_fails_when_single_island_decrease_is_below_warn_ratio(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            baseline = output_dir / "baseline.csv"
            candidate = output_dir / "candidate.csv"
            true_run = [(0, 9)]
            noise_runs = [(20 + index * 3, 20 + index * 3) for index in range(100)]
            _write_track(baseline, detected_ranges=[*true_run, *noise_runs], frame_count=340)
            _write_track(candidate, detected_ranges=[*true_run, *noise_runs[1:]], frame_count=340)

            report = build_noise_candidate_comparison(
                baseline,
                candidate,
                candidate_id="noise-too-small",
                approval=_approval("noise-too-small"),
                target_window={"start_frame": 0, "end_frame": 339},
            )

        self.assertEqual("fail", report["summary"]["status"])
        reduction = next(check for check in report["checks"] if check["name"] == "false_positive_island_reduction")
        self.assertEqual("fail", reduction["status"])
        self.assertEqual(1, reduction["absolute_decrease"])
        self.assertIn("below the 5 percent warn threshold", reduction["reason"])

    def test_candidate_fails_when_sustained_valid_ball_coverage_drops(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            baseline = output_dir / "baseline.csv"
            candidate = output_dir / "candidate.csv"
            _write_track(
                baseline,
                detected_ranges=[(10, 39), (50, 50), (60, 60), (70, 70)],
                frame_count=100,
            )
            _write_track(candidate, detected_ranges=[(10, 12)], frame_count=100)

            report = build_noise_candidate_comparison(
                baseline,
                candidate,
                candidate_id="noise-coverage-drop",
                approval=_approval("noise-coverage-drop"),
                target_window={"start_frame": 0, "end_frame": 99},
            )

        self.assertEqual("fail", report["summary"]["status"])
        coverage = next(check for check in report["checks"] if check["name"] == "sustained_detected_coverage_preserved")
        self.assertEqual("fail", coverage["status"])

    def test_candidate_fails_when_lost_frame_increase_exceeds_tolerance(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            baseline = output_dir / "baseline.csv"
            candidate = output_dir / "candidate.csv"
            true_run = [(10, 34)]
            noise_runs = [(50 + index * 2, 50 + index * 2) for index in range(16)]
            _write_track(baseline, detected_ranges=[*true_run, *noise_runs], frame_count=100)
            _write_track(candidate, detected_ranges=true_run, frame_count=100)

            report = build_noise_candidate_comparison(
                baseline,
                candidate,
                candidate_id="noise-lost-increase",
                approval=_approval("noise-lost-increase"),
                target_window={"start_frame": 0, "end_frame": 99},
            )

        self.assertEqual("fail", report["summary"]["status"])
        lost = next(check for check in report["checks"] if check["name"] == "lost_frame_budget_preserved")
        self.assertEqual("fail", lost["status"])
        self.assertEqual(16, lost["lost_frame_increase"])

    def test_unbounded_full_video_sahi_provenance_fails_comparison(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            baseline = output_dir / "baseline.csv"
            candidate = output_dir / "candidate.csv"
            _write_track(baseline, detected_ranges=[(10, 24), (40, 40), (50, 50)], frame_count=80)
            _write_track(candidate, detected_ranges=[(10, 24)], frame_count=80)

            report = build_noise_candidate_comparison(
                baseline,
                candidate,
                candidate_id="noise-sahi",
                approval=_approval("noise-sahi"),
                target_window={"start_frame": 0, "end_frame": 79},
                strategy_provenance={"strategy": "full_video_sahi", "full_video_sahi": True},
            )

        self.assertEqual("fail", report["summary"]["status"])
        provenance = next(check for check in report["checks"] if check["name"] == "bounded_strategy_provenance")
        self.assertEqual("fail", provenance["status"])
        self.assertIn("unbounded", provenance["reason"])

    def test_full_video_sahi_flag_fails_even_with_bounded_strategy_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            baseline = output_dir / "baseline.csv"
            candidate = output_dir / "candidate.csv"
            _write_track(baseline, detected_ranges=[(10, 24), (40, 40), (50, 50)], frame_count=80)
            _write_track(candidate, detected_ranges=[(10, 24)], frame_count=80)

            report = build_noise_candidate_comparison(
                baseline,
                candidate,
                candidate_id="noise-sahi",
                approval=_approval("noise-sahi"),
                target_window={"start_frame": 0, "end_frame": 79},
                strategy_provenance={
                    "strategy": "bounded_full_video_sahi",
                    "full_video_sahi": True,
                    "start_frame": 0,
                    "end_frame": 79,
                },
            )

        self.assertEqual("fail", report["summary"]["status"])
        provenance = next(check for check in report["checks"] if check["name"] == "bounded_strategy_provenance")
        self.assertEqual("fail", provenance["status"])
        self.assertIn("unbounded", provenance["reason"])

    def test_full_video_spatial_split_flag_fails_even_with_bounded_strategy_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            baseline = output_dir / "baseline.csv"
            candidate = output_dir / "candidate.csv"
            _write_track(baseline, detected_ranges=[(10, 24), (40, 40), (50, 50)], frame_count=80)
            _write_track(candidate, detected_ranges=[(10, 24)], frame_count=80)

            report = build_noise_candidate_comparison(
                baseline,
                candidate,
                candidate_id="noise-spatial",
                approval=_approval("noise-spatial"),
                target_window={"start_frame": 0, "end_frame": 79},
                strategy_provenance={
                    "strategy": "bounded_full_video_spatial_split",
                    "full_video_spatial_split": True,
                    "start_frame": 0,
                    "end_frame": 79,
                },
            )

        self.assertEqual("fail", report["summary"]["status"])
        provenance = next(check for check in report["checks"] if check["name"] == "bounded_strategy_provenance")
        self.assertEqual("fail", provenance["status"])
        self.assertIn("unbounded", provenance["reason"])

    def test_full_video_strategy_name_fails_even_without_boolean_flags(self) -> None:
        unsafe_strategies = ("bounded_full_video_sahi", "bounded_full_video_spatial_split")
        for strategy in unsafe_strategies:
            with self.subTest(strategy=strategy), tempfile.TemporaryDirectory() as temp_name:
                output_dir = Path(temp_name)
                baseline = output_dir / "baseline.csv"
                candidate = output_dir / "candidate.csv"
                _write_track(baseline, detected_ranges=[(10, 24), (40, 40), (50, 50)], frame_count=80)
                _write_track(candidate, detected_ranges=[(10, 24)], frame_count=80)

                report = build_noise_candidate_comparison(
                    baseline,
                    candidate,
                    candidate_id="noise-full-video-name",
                    approval=_approval("noise-full-video-name"),
                    target_window={"start_frame": 0, "end_frame": 79},
                    strategy_provenance={
                        "strategy": strategy,
                        "start_frame": 0,
                        "end_frame": 79,
                    },
                )

                self.assertEqual("fail", report["summary"]["status"])
                provenance = next(check for check in report["checks"] if check["name"] == "bounded_strategy_provenance")
                self.assertEqual("fail", provenance["status"])
                self.assertIn("unbounded", provenance["reason"])

    def test_execute_cleanup_writes_isolated_candidate_artifacts_and_preserves_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_track(
                output_dir / "ball_track.cleaned.csv",
                detected_ranges=[(10, 24), (30, 31), (40, 40), (50, 51)],
                frame_count=80,
            )
            (output_dir / "ball_track.csv").write_text(
                (output_dir / "ball_track.cleaned.csv").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            write_ball_audit_report(output_dir)
            _write_review_packet(output_dir, packet_id="packet_noise", start=0, end=79)
            before_hash = _sha256(output_dir / "ball_track.cleaned.csv")

            report = execute_noise_cleanup_candidate(
                output_dir,
                _approval("noise-exec", approval_id="noise_1", start=0, end=79),
            )

            candidate_dir = output_dir / "ai_candidates" / "noise" / "noise-exec"
            after_hash = _sha256(output_dir / "ball_track.cleaned.csv")
            candidate_cleaned_exists = (candidate_dir / "ball_track.cleaned.csv").exists()
            candidate_audit_exists = (candidate_dir / "ball_audit.json").exists()
            candidate_comparison_exists = (candidate_dir / "noise_candidate_comparison.json").exists()
            registry_exists = (output_dir / "ai_candidate_registry.json").exists()
            manifest = json.loads((candidate_dir / "candidate_manifest.json").read_text(encoding="utf-8"))
            cleanup_report = json.loads((candidate_dir / "cleanup_report.json").read_text(encoding="utf-8"))

        self.assertEqual(before_hash, after_hash)
        self.assertEqual("pass", report["comparison_status"])
        self.assertTrue(candidate_cleaned_exists)
        self.assertTrue(candidate_audit_exists)
        self.assertTrue(candidate_comparison_exists)
        self.assertEqual("noise-exec", manifest["candidate_id"])
        self.assertEqual("noise_1", manifest["approval_id"])
        self.assertGreater(cleanup_report["summary"]["removed_frame_count"], 0)
        self.assertTrue(registry_exists)

    def test_execute_cleanup_requires_explicit_candidate_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_track(
                output_dir / "ball_track.cleaned.csv",
                detected_ranges=[(10, 24), (30, 31), (40, 40), (50, 51)],
                frame_count=80,
            )
            (output_dir / "ball_track.csv").write_text(
                (output_dir / "ball_track.cleaned.csv").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            write_ball_audit_report(output_dir)
            _write_review_packet(output_dir, packet_id="packet_noise", start=0, end=79)
            approval = _approval("noise-exec", approval_id="noise_1", start=0, end=79)
            approval.pop("candidate_id")

            with self.assertRaisesRegex(ValueError, "candidate_id"):
                execute_noise_cleanup_candidate(output_dir, approval)

            candidate_parent_exists = (output_dir / "ai_candidates" / "noise").exists()

        self.assertFalse(candidate_parent_exists)

    def test_execute_cleanup_requires_traceable_packet_or_visual_evidence_before_writing_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_track(
                output_dir / "ball_track.cleaned.csv",
                detected_ranges=[(10, 24), (30, 31), (40, 40), (50, 51)],
                frame_count=80,
            )
            (output_dir / "ball_track.csv").write_text(
                (output_dir / "ball_track.cleaned.csv").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            write_ball_audit_report(output_dir)

            with self.assertRaisesRegex(ValueError, "traceable packet or visual evidence"):
                execute_noise_cleanup_candidate(
                    output_dir,
                    _approval("noise-exec", approval_id="noise_1", start=0, end=79),
                )

            candidate_dir_exists = (output_dir / "ai_candidates" / "noise" / "noise-exec").exists()

        self.assertFalse(candidate_dir_exists)

    def test_execute_cleanup_refuses_unbounded_full_video_sahi_before_writing_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_track(
                output_dir / "ball_track.cleaned.csv",
                detected_ranges=[(10, 24), (30, 31), (40, 40), (50, 51)],
                frame_count=80,
            )
            (output_dir / "ball_track.csv").write_text(
                (output_dir / "ball_track.cleaned.csv").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            write_ball_audit_report(output_dir)
            _write_review_packet(output_dir, packet_id="packet_noise", start=0, end=79)
            approval = _approval("noise-exec", approval_id="noise_1", start=0, end=79)
            approval["strategy_provenance"] = {"strategy": "full_video_sahi", "full_video_sahi": True}

            with self.assertRaisesRegex(ValueError, "unbounded full-video spatial/SAHI"):
                execute_noise_cleanup_candidate(output_dir, approval)

            candidate_dir_exists = (output_dir / "ai_candidates" / "noise" / "noise-exec").exists()

        self.assertFalse(candidate_dir_exists)

    def test_execute_cleanup_refuses_full_video_sahi_flag_with_bounded_strategy_name_before_writing_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_track(
                output_dir / "ball_track.cleaned.csv",
                detected_ranges=[(10, 24), (30, 31), (40, 40), (50, 51)],
                frame_count=80,
            )
            (output_dir / "ball_track.csv").write_text(
                (output_dir / "ball_track.cleaned.csv").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            write_ball_audit_report(output_dir)
            _write_review_packet(output_dir, packet_id="packet_noise", start=0, end=79)
            approval = _approval("noise-exec", approval_id="noise_1", start=0, end=79)
            approval["strategy_provenance"] = {
                "strategy": "bounded_full_video_sahi",
                "full_video_sahi": True,
                "start_frame": 0,
                "end_frame": 79,
            }

            with self.assertRaisesRegex(ValueError, "unbounded full-video spatial/SAHI"):
                execute_noise_cleanup_candidate(output_dir, approval)

            candidate_dir_exists = (output_dir / "ai_candidates" / "noise" / "noise-exec").exists()

        self.assertFalse(candidate_dir_exists)

    def test_execute_cleanup_refuses_full_video_spatial_flag_with_bounded_strategy_name_before_writing_candidate(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_track(
                output_dir / "ball_track.cleaned.csv",
                detected_ranges=[(10, 24), (30, 31), (40, 40), (50, 51)],
                frame_count=80,
            )
            (output_dir / "ball_track.csv").write_text(
                (output_dir / "ball_track.cleaned.csv").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            write_ball_audit_report(output_dir)
            _write_review_packet(output_dir, packet_id="packet_noise", start=0, end=79)
            approval = _approval("noise-exec", approval_id="noise_1", start=0, end=79)
            approval["strategy_provenance"] = {
                "strategy": "bounded_full_video_spatial_split",
                "full_video_spatial_split": True,
                "start_frame": 0,
                "end_frame": 79,
            }

            with self.assertRaisesRegex(ValueError, "unbounded full-video spatial/SAHI"):
                execute_noise_cleanup_candidate(output_dir, approval)

            candidate_dir_exists = (output_dir / "ai_candidates" / "noise" / "noise-exec").exists()

        self.assertFalse(candidate_dir_exists)

    def test_execute_cleanup_refuses_full_video_strategy_name_without_flag_before_writing_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_track(
                output_dir / "ball_track.cleaned.csv",
                detected_ranges=[(10, 24), (30, 31), (40, 40), (50, 51)],
                frame_count=80,
            )
            (output_dir / "ball_track.csv").write_text(
                (output_dir / "ball_track.cleaned.csv").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            write_ball_audit_report(output_dir)
            _write_review_packet(output_dir, packet_id="packet_noise", start=0, end=79)
            approval = _approval("noise-exec", approval_id="noise_1", start=0, end=79)
            approval["strategy_provenance"] = {
                "strategy": "bounded_full_video_sahi",
                "start_frame": 0,
                "end_frame": 79,
            }

            with self.assertRaisesRegex(ValueError, "unbounded full-video spatial/SAHI"):
                execute_noise_cleanup_candidate(output_dir, approval)

            candidate_dir_exists = (output_dir / "ai_candidates" / "noise" / "noise-exec").exists()

        self.assertFalse(candidate_dir_exists)

    def test_execute_cleanup_does_not_overwrite_corrupt_parent_registry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_track(
                output_dir / "ball_track.cleaned.csv",
                detected_ranges=[(10, 24), (30, 31), (40, 40), (50, 51)],
                frame_count=80,
            )
            (output_dir / "ball_track.csv").write_text(
                (output_dir / "ball_track.cleaned.csv").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            write_ball_audit_report(output_dir)
            _write_review_packet(output_dir, packet_id="packet_noise", start=0, end=79)
            registry_path = output_dir / "ai_candidate_registry.json"
            registry_path.write_text("{", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "Cannot update ai_candidate_registry"):
                execute_noise_cleanup_candidate(
                    output_dir,
                    _approval("noise-exec", approval_id="noise_1", start=0, end=79),
                )

            registry_text = registry_path.read_text(encoding="utf-8")
            candidate_dir_exists = (output_dir / "ai_candidates" / "noise" / "noise-exec").exists()

        self.assertEqual("{", registry_text)
        self.assertFalse(candidate_dir_exists)


def _approval(candidate_id: str, *, approval_id: str = "approval_noise", start: int = 0, end: int = 99) -> dict[str, object]:
    return {
        "approval_id": approval_id,
        "candidate_id": candidate_id,
        "problem_type": "noise",
        "approved_action": "noise_filter_adjustment",
        "start_frame": start,
        "end_frame": end,
        "source_packet_id": "packet_noise",
        "false_positive_class": "shoe_confusion",
        "config_patch": {"selection": {"min_accept_score": 0.62}},
    }


def _write_track(path: Path, *, detected_ranges: list[tuple[int, int]], frame_count: int) -> None:
    rows = ["Frame,X,Y,Confidence,Status"]
    for frame in range(frame_count):
        detected = any(start <= frame <= end for start, end in detected_ranges)
        if detected:
            rows.append(f"{frame},{100 + frame},{200 + frame},0.90,Detected")
        else:
            rows.append(f"{frame},,,0.00,Lost")
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def _write_review_packet(output_dir: Path, *, packet_id: str, start: int, end: int) -> None:
    (output_dir / "review_packets.json").write_text(
        json.dumps(
            {
                "packets": [
                    {
                        "packet_id": packet_id,
                        "window": {"start_frame": start, "end_frame": end},
                        "decision": {"label": "reject_noise"},
                    }
                ]
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    unittest.main()
