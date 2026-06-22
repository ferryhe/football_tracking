from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from football_tracking.metrics import (
    FALSE_POSITIVE_ISLAND_MAX_LENGTH,
    build_metrics_report,
    compute_track_metrics,
    stats_from_metrics_report,
    write_run_artifacts,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "benchmark_tracks"


class MetricsTests(unittest.TestCase):
    def test_compute_track_metrics_from_csv_is_stable(self) -> None:
        metrics = compute_track_metrics(FIXTURES_DIR / "ball_track.csv")

        self.assertEqual(10, metrics["frame_count"])
        self.assertEqual({"Detected": 5, "Predicted": 2, "Lost": 3}, metrics["status_counts"])
        self.assertEqual(0.5, metrics["detected_ratio"])
        self.assertEqual(0.2, metrics["predicted_ratio"])
        self.assertEqual(0.3, metrics["lost_ratio"])
        self.assertEqual(4, metrics["detected_segments"])
        self.assertEqual(2, metrics["predicted_segments"])
        self.assertEqual(2, metrics["lost_segments"])
        self.assertEqual(4, metrics["false_positive_island_count"])
        self.assertEqual(3, metrics["reacquire_count"])
        self.assertEqual(2, metrics["longest_lost_streak"])
        self.assertEqual(5.0, metrics["mean_step_px"])
        self.assertEqual(5.0, metrics["max_step_px"])
        self.assertEqual(0.0, metrics["mean_accel_px"])
        self.assertEqual(0.0, metrics["max_accel_px"])
        self.assertEqual(2, FALSE_POSITIVE_ISLAND_MAX_LENGTH)

    def test_acceleration_uses_vector_change_not_step_length_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            csv_path = Path(temp_name) / "ball_track.csv"
            csv_path.write_text(
                "\n".join(
                    [
                        "Frame,X,Y,Confidence,Status",
                        "0,0,0,0.9000,Detected",
                        "1,10,0,0.9000,Detected",
                        "2,0,0,0.9000,Detected",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            metrics = compute_track_metrics(csv_path)

        self.assertEqual(10.0, metrics["mean_step_px"])
        self.assertEqual(20.0, metrics["mean_accel_px"])
        self.assertEqual(20.0, metrics["max_accel_px"])

    def test_write_run_artifacts_creates_manifest_and_metrics_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            (output_dir / "ball_track.csv").write_text(
                (FIXTURES_DIR / "ball_track.csv").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            (output_dir / "cleanup_report.json").write_text(
                json.dumps({"scrubbed_frame_count": 1, "scrubbed_segment_count": 1}),
                encoding="utf-8",
            )
            (output_dir / "follow_cam_report.json").write_text(
                json.dumps({"track_source": "raw", "target_resolution": [1920, 1080]}),
                encoding="utf-8",
            )
            (output_dir / "player_detections.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "frame": 0,
                                "bbox": [0, 0, 20, 50],
                                "confidence": 0.9,
                                "label": "person",
                                "team": "home",
                            }
                        ),
                        json.dumps(
                            {
                                "frame": 1,
                                "bbox": [2, 0, 22, 50],
                                "confidence": 0.8,
                                "label": "person",
                                "team": "home",
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            manifest, report = write_run_artifacts(
                output_dir=output_dir,
                run={
                    "run_id": "run_fixture",
                    "source": "api",
                    "status": "completed",
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "started_at": "2026-01-01T00:00:01+00:00",
                    "completed_at": "2026-01-01T00:00:02+00:00",
                    "config_name": "default.yaml",
                    "config_path": "config/default.yaml",
                    "input_video": "data/input.mp4",
                    "output_dir": str(output_dir),
                    "modules_enabled": {"postprocess": True, "follow_cam": True},
                    "notes": "fixture run",
                },
            )

            self.assertTrue((output_dir / "run_manifest.json").exists())
            self.assertTrue((output_dir / "metrics_report.json").exists())
            self.assertTrue((output_dir / "ball_audit.json").exists())
            self.assertTrue((output_dir / "ai_review_triggers.json").exists())
            self.assertTrue((output_dir / "event_candidates.json").exists())
            self.assertEqual("run_fixture", manifest["run_id"])
            self.assertIn("git_commit", manifest)
            self.assertEqual(10, report["tracks"]["raw"]["frame_count"])
            self.assertEqual(1, report["cleanup"]["scrubbed_frame_count"])
            self.assertEqual("raw", report["follow_cam"]["track_source"])
            self.assertEqual(3, report["ball_audit"]["tracklet_count"])
            self.assertEqual(1, report["ball_audit"]["review_event_count"])
            self.assertIn("ai_review_triggers", report)
            self.assertTrue(report["ai_review_triggers"]["needs_ai_review"])
            self.assertGreaterEqual(report["ai_review_triggers"]["trigger_count"], 1)
            self.assertIn("event_candidates", report)
            self.assertEqual("raw", report["event_candidates"]["source_name"])
            self.assertGreaterEqual(report["event_candidates"]["candidate_count"], 0)
            self.assertTrue((output_dir / "player_tracks.json").exists())
            self.assertTrue((output_dir / "player_tracks.csv").exists())
            self.assertEqual(1, report["player_tracks"]["track_count"])
            self.assertEqual({"home": 1}, report["player_tracks"]["teams"])

    def test_write_run_artifacts_uses_run_fps_for_event_candidate_buffers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            (output_dir / "ball_track.csv").write_text(
                "\n".join(
                    [
                        "Frame,X,Y,Confidence,Status",
                        "100,10,40,0.90,Detected",
                        "101,58,40,0.90,Detected",
                        "102,112,40,0.90,Detected",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            write_run_artifacts(
                output_dir=output_dir,
                run={
                    "run_id": "run_30fps",
                    "source": "api",
                    "status": "completed",
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "input_video": "data/input.mp4",
                    "output_dir": str(output_dir),
                    "fps": 30.0,
                },
            )
            event_candidates = json.loads((output_dir / "event_candidates.json").read_text(encoding="utf-8"))

        candidate = event_candidates["candidates"][0]
        self.assertEqual({"start_frame": 77, "end_frame": 237}, candidate["render_window"])
        self.assertEqual("fps", candidate["buffer_policy"]["fps_source"])
        self.assertEqual(23, candidate["buffer_policy"]["pre_buffer_frames"])
        self.assertEqual(135, candidate["buffer_policy"]["post_buffer_frames"])

    def test_write_run_artifacts_preserves_manifest_when_ball_audit_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            (output_dir / "ball_track.csv").write_text(
                "Frame,X,Y,Confidence,Status\n0,1,2,0.9000,Detected\n",
                encoding="utf-8",
            )

            def fail_after_partial_audit_write(path: Path) -> None:
                (path / "ball_audit.json").write_text("{", encoding="utf-8")
                raise RuntimeError("partial audit write")

            with mock.patch(
                "football_tracking.metrics.write_ball_audit_report",
                side_effect=fail_after_partial_audit_write,
            ):
                manifest, report = write_run_artifacts(
                    output_dir=output_dir,
                    run={
                        "run_id": "run_audit_failure",
                        "source": "api",
                        "status": "completed",
                    },
                )

            self.assertEqual("run_audit_failure", manifest["run_id"])
            self.assertTrue((output_dir / "run_manifest.json").exists())
            self.assertTrue((output_dir / "metrics_report.json").exists())
            self.assertIn("ball_audit_error", report)
            self.assertNotIn("ball_audit", report)

    def test_build_metrics_report_includes_compact_ai_review_trigger_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            (output_dir / "ai_review_triggers.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "generated_at": "2026-01-01T00:00:00+00:00",
                        "decision": {
                            "needs_ai_review": True,
                            "priority": "high",
                            "reason": "high_priority_triggers",
                            "trigger_count": 2,
                            "recommended_review_windows": [
                                {"start_frame": 10, "end_frame": 20, "reason": "large_jump"}
                            ],
                        },
                        "triggers": [],
                        "summary": {
                            "counts_by_type": {"large_jump": 1, "dense_noise_cluster": 1},
                            "counts_by_priority": {"high": 2},
                            "max_trigger_priority": "high",
                        },
                    }
                ),
                encoding="utf-8",
            )

            report = build_metrics_report(output_dir)
            stats = stats_from_metrics_report(report)

        self.assertEqual("high", report["ai_review_triggers"]["priority"])
        self.assertEqual(2, report["ai_review_triggers"]["trigger_count"])
        self.assertEqual(1, report["ai_review_triggers"]["recommended_window_count"])
        self.assertEqual(report["ai_review_triggers"], stats["ai_review_triggers"])

    def test_build_metrics_report_includes_compact_player_tracks_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            (output_dir / "player_tracks.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "generated_at": "2026-01-01T00:00:00+00:00",
                        "source": {
                            "path": "player_detections.jsonl",
                            "status": "loaded",
                            "detection_count": 2,
                            "malformed_line_count": 0,
                        },
                        "summary": {
                            "frame_count": 2,
                            "detection_count": 2,
                            "track_count": 1,
                            "active_track_count": 1,
                            "mean_track_length": 2.0,
                            "longest_track_length": 2,
                            "teams": {"home": 1},
                        },
                        "tracks": [],
                    }
                ),
                encoding="utf-8",
            )

            report = build_metrics_report(output_dir)
            stats = stats_from_metrics_report(report)

        self.assertEqual(1, report["player_tracks"]["track_count"])
        self.assertEqual({"home": 1}, report["player_tracks"]["teams"])
        self.assertEqual(report["player_tracks"], stats["player_tracks"])

    def test_build_metrics_report_includes_compact_event_candidate_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            (output_dir / "event_candidates.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "source": {"name": "cleaned", "path": "ball_track.cleaned.csv", "row_count": 7},
                        "summary": {
                            "frame_count": 7,
                            "detected_frame_count": 7,
                            "candidate_count": 2,
                            "counts_by_type": {"shot_candidate": 1, "goal_candidate": 1},
                            "min_frame": 10,
                            "max_frame": 50,
                        },
                        "candidates": [
                            {"id": "cleaned:shot_candidate:10-14", "score": 0.68},
                            {"id": "cleaned:goal_candidate:40-44", "score": 0.84},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            report = build_metrics_report(output_dir)
            stats = stats_from_metrics_report(report)

        self.assertEqual("cleaned", report["event_candidates"]["source_name"])
        self.assertEqual(2, report["event_candidates"]["candidate_count"])
        self.assertEqual({"shot_candidate": 1, "goal_candidate": 1}, report["event_candidates"]["counts_by_type"])
        self.assertEqual(0.84, report["event_candidates"]["max_score"])
        self.assertEqual(report["event_candidates"], stats["event_candidates"])

    def test_build_metrics_report_includes_compact_review_packet_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            (output_dir / "review_packets.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "generated_at": "2026-01-01T00:00:00+00:00",
                        "summary": {
                            "packet_count": 3,
                            "counts_by_label": {"highlight_worthy": 1, "needs_ai_review": 2},
                            "media_packet_count": 3,
                        },
                        "packets": [],
                    }
                ),
                encoding="utf-8",
            )

            report = build_metrics_report(output_dir)
            stats = stats_from_metrics_report(report)

        self.assertEqual(3, report["review_packets"]["packet_count"])
        self.assertEqual({"highlight_worthy": 1, "needs_ai_review": 2}, report["review_packets"]["counts_by_label"])
        self.assertEqual(report["review_packets"], stats["review_packets"])

    def test_build_metrics_report_flags_unreviewed_long_lost_gap(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            (output_dir / "ball_audit.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "review_events": [
                            {
                                "type": "lost_gap",
                                "start_frame": 2049,
                                "end_frame": 2544,
                                "frame_count": 496,
                                "reason": "Ball track is lost for 496 frames between tracklets.",
                            }
                        ],
                        "tracklets": [],
                    }
                ),
                encoding="utf-8",
            )
            (output_dir / "review_packets.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "summary": {"packet_count": 1, "counts_by_label": {"needs_ai_review": 1}},
                        "packets": [
                            {
                                "source": {"kind": "trigger", "type": "large_jump", "start_frame": 160, "end_frame": 276},
                                "window": {"start_frame": 130, "end_frame": 306},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            report = build_metrics_report(output_dir)
            stats = stats_from_metrics_report(report)

        self.assertEqual("needs_review", report["quality_gate"]["status"])
        self.assertEqual(1, report["quality_gate"]["unreviewed_long_lost_gap_count"])
        self.assertEqual([(2049, 2544)], [tuple(item) for item in report["quality_gate"]["unreviewed_long_lost_gaps"]])
        self.assertEqual(report["quality_gate"], stats["quality_gate"])

    def test_build_metrics_report_accepts_reviewed_long_lost_gap(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            (output_dir / "ball_audit.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "review_events": [
                            {
                                "type": "lost_gap",
                                "start_frame": 2049,
                                "end_frame": 2544,
                                "frame_count": 496,
                            }
                        ],
                        "tracklets": [],
                    }
                ),
                encoding="utf-8",
            )
            (output_dir / "review_packets.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "summary": {"packet_count": 1, "counts_by_label": {"ball_not_visible": 1}},
                        "packets": [
                            {
                                "source": {"kind": "trigger", "type": "lost_gap", "start_frame": 2049, "end_frame": 2544},
                                "window": {"start_frame": 2019, "end_frame": 2574},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            report = build_metrics_report(output_dir)

        self.assertEqual("review_ready", report["quality_gate"]["status"])
        self.assertEqual(0, report["quality_gate"]["unreviewed_long_lost_gap_count"])

    def test_build_metrics_report_ignores_budget_rejection_for_reviewed_long_lost_gap(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            (output_dir / "ball_audit.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "review_events": [
                            {
                                "type": "lost_gap",
                                "start_frame": 100,
                                "end_frame": 300,
                                "frame_count": 201,
                            }
                        ],
                        "tracklets": [],
                    }
                ),
                encoding="utf-8",
            )
            (output_dir / "review_packets.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "summary": {"packet_count": 1, "counts_by_label": {"ball_not_visible": 1}},
                        "packets": [
                            {
                                "source": {"kind": "trigger", "type": "lost_gap", "start_frame": 100, "end_frame": 300},
                                "window": {"start_frame": 70, "end_frame": 330},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            report_dir = output_dir / "high_recall_windows"
            report_dir.mkdir()
            (report_dir / "report.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "windows": [],
                        "rejected_windows": [
                            {
                                "start_frame": 90,
                                "end_frame": 310,
                                "reason": "ball_audit: lost_gap",
                                "priority": "medium",
                                "rejection_reason": "max_total_frames_exceeded",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            report = build_metrics_report(output_dir)

        self.assertEqual("review_ready", report["quality_gate"]["status"])
        self.assertEqual(1, report["quality_gate"]["reviewed_long_lost_gap_count"])
        self.assertEqual(0, report["quality_gate"]["high_recall_lost_gap_budget_rejection_count"])

    def test_build_metrics_report_counts_high_recall_lost_gap_packet_as_reviewed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            (output_dir / "ball_audit.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "review_events": [
                            {
                                "type": "lost_gap",
                                "start_frame": 100,
                                "end_frame": 300,
                                "frame_count": 201,
                            }
                        ],
                        "tracklets": [],
                    }
                ),
                encoding="utf-8",
            )
            (output_dir / "review_packets.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "summary": {"packet_count": 1, "counts_by_label": {"needs_ai_review": 1}},
                        "packets": [
                            {
                                "source": {
                                    "kind": "high_recall_rejection",
                                    "type": "high_recall_rejected",
                                    "start_frame": 90,
                                    "end_frame": 310,
                                    "reason": "High-recall window rejected: max_total_frames_exceeded",
                                    "evidence": {
                                        "window_reason": "ball_audit: lost_gap",
                                        "rejection_reason": "max_total_frames_exceeded",
                                    },
                                },
                                "window": {"start_frame": 60, "end_frame": 340},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            report = build_metrics_report(output_dir)

        self.assertEqual("review_ready", report["quality_gate"]["status"])
        self.assertEqual(1, report["quality_gate"]["reviewed_long_lost_gap_count"])

    def test_build_metrics_report_checks_custom_high_recall_rejection_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            (output_dir / "ball_audit.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "review_events": [
                            {
                                "type": "lost_gap",
                                "start_frame": 100,
                                "end_frame": 300,
                                "frame_count": 201,
                            }
                        ],
                        "tracklets": [],
                    }
                ),
                encoding="utf-8",
            )
            report_dir = output_dir / "second_pass_windows"
            report_dir.mkdir()
            (report_dir / "report.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "windows": [],
                        "rejected_windows": [
                            {
                                "start_frame": 90,
                                "end_frame": 310,
                                "reason": "ball_audit: lost_gap",
                                "priority": "medium",
                                "rejection_reason": "max_total_frames_exceeded",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            report = build_metrics_report(output_dir)

        self.assertEqual("needs_review", report["quality_gate"]["status"])
        self.assertEqual(1, report["quality_gate"]["high_recall_lost_gap_budget_rejection_count"])
        self.assertEqual([[90, 310]], report["quality_gate"]["high_recall_lost_gap_budget_rejections"])

    def test_build_metrics_report_ignores_short_high_recall_lost_gap_budget_rejections(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            (output_dir / "ball_audit.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "review_events": [],
                        "tracklets": [],
                    }
                ),
                encoding="utf-8",
            )
            report_dir = output_dir / "high_recall_windows"
            report_dir.mkdir()
            (report_dir / "report.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "windows": [],
                        "rejected_windows": [
                            {
                                "start_frame": 200,
                                "end_frame": 230,
                                "reason": "ball_audit: lost_gap",
                                "priority": "medium",
                                "rejection_reason": "max_total_frames_exceeded",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            report = build_metrics_report(output_dir)

        self.assertEqual("stable", report["quality_gate"]["status"])
        self.assertEqual(0, report["quality_gate"]["high_recall_lost_gap_budget_rejection_count"])

    def test_build_metrics_report_ignores_high_recall_rejections_without_current_long_lost_gap(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            (output_dir / "ball_audit.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "review_events": [],
                        "tracklets": [],
                    }
                ),
                encoding="utf-8",
            )
            report_dir = output_dir / "high_recall_windows"
            report_dir.mkdir()
            (report_dir / "report.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "windows": [],
                        "rejected_windows": [
                            {
                                "start_frame": 100,
                                "end_frame": 400,
                                "reason": "ai_review: large_jump; lost_gap; dense_noise_cluster",
                                "priority": "high",
                                "rejection_reason": "max_total_frames_exceeded",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            report = build_metrics_report(output_dir)

        self.assertEqual("stable", report["quality_gate"]["status"])
        self.assertEqual(0, report["quality_gate"]["high_recall_lost_gap_budget_rejection_count"])

    def test_build_metrics_report_includes_ai_visual_review_and_accepted_highlights(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            (output_dir / "ai_visual_review.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "generated_at": "2026-01-01T00:00:00+00:00",
                        "summary": {
                            "packet_count": 4,
                            "reviewed_count": 3,
                            "error_count": 1,
                            "counts_by_verdict": {"accept_highlight": 2, "needs_human_review": 1},
                            "accepted_highlight_count": 2,
                            "needs_human_review_count": 1,
                            "reject_noise_count": 0,
                        },
                        "reviews": [],
                        "errors": [{"packet_id": "packet_bad", "error": "bad model output"}],
                    }
                ),
                encoding="utf-8",
            )
            accepted_dir = output_dir / "highlights_ai_accepted"
            accepted_dir.mkdir()
            (accepted_dir / "ai_accepted_highlights_report.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "generated_at": "2026-01-01T00:00:01+00:00",
                        "summary": {
                            "qualified_count": 2,
                            "copied_count": 1,
                            "skipped_count": 1,
                            "error_count": 0,
                        },
                        "copied": [{"packet_id": "packet_keep"}],
                        "skipped": [{"packet_id": "packet_missing"}],
                        "errors": [],
                    }
                ),
                encoding="utf-8",
            )

            report = build_metrics_report(output_dir)
            stats = stats_from_metrics_report(report)

        self.assertEqual(4, report["ai_visual_review"]["packet_count"])
        self.assertEqual(2, report["ai_visual_review"]["accepted_highlight_count"])
        self.assertEqual(1, report["accepted_highlights"]["copied_count"])
        self.assertEqual(1, report["accepted_highlights"]["skipped_count"])
        self.assertEqual(report["ai_visual_review"], stats["ai_visual_review"])
        self.assertEqual(report["accepted_highlights"], stats["accepted_highlights"])

    def test_build_metrics_report_includes_compact_temporal_chunk_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            (output_dir / "temporal_chunks_report.json").write_text(
                json.dumps(
                    {
                        "chunk_count": 5,
                        "frame_count": 5192,
                        "chunks": [
                            {
                                "index": 0,
                                "name": "chunk_0000",
                                "start_frame": 0,
                                "end_frame": 1279,
                                "core_start_frame": 0,
                                "core_end_frame": 1199,
                            },
                            {
                                "index": 1,
                                "name": "chunk_0001",
                                "start_frame": 1120,
                                "end_frame": 2479,
                                "core_start_frame": 1200,
                                "core_end_frame": 2399,
                            },
                        ],
                        "boundary_events": [{"frame": 1199}, {"frame": 2399}],
                        "execution": {
                            "status": "succeeded",
                            "mode": "subprocess",
                            "requested_workers": 4,
                            "effective_workers": 2,
                        },
                        "stitch": {"status": "succeeded"},
                    }
                ),
                encoding="utf-8",
            )

            report = build_metrics_report(output_dir)
            stats = stats_from_metrics_report(report)

        expected = {
            "enabled": True,
            "chunk_count": 5,
            "effective_workers": 2,
            "requested_workers": 4,
            "execution_mode": "subprocess",
            "execution_status": "succeeded",
            "stitch_status": "succeeded",
            "merged_frame_count": 5192,
            "overlap_frames": 80,
            "boundary_review_event_count": 2,
        }
        self.assertEqual(expected, report["temporal_chunks"])
        self.assertEqual(expected, stats["temporal_chunks"])

    def test_build_metrics_report_handles_sparse_temporal_chunk_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            (output_dir / "temporal_chunks_report.json").write_text(
                json.dumps({"execution": {"status": "failed"}, "stitch": {"status": "failed"}}),
                encoding="utf-8",
            )

            report = build_metrics_report(output_dir)

        self.assertEqual(
            {
                "enabled": True,
                "execution_status": "failed",
                "stitch_status": "failed",
            },
            report["temporal_chunks"],
        )

    def test_write_run_artifacts_preserves_manifest_when_ai_review_triggers_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            (output_dir / "ball_track.csv").write_text(
                "Frame,X,Y,Confidence,Status\n0,1,2,0.9000,Detected\n",
                encoding="utf-8",
            )

            with mock.patch(
                "football_tracking.metrics.write_ai_review_trigger_report",
                side_effect=RuntimeError("trigger write failed"),
            ):
                manifest, report = write_run_artifacts(
                    output_dir=output_dir,
                    run={
                        "run_id": "run_trigger_failure",
                        "source": "api",
                        "status": "completed",
                    },
                )

            self.assertEqual("run_trigger_failure", manifest["run_id"])
            self.assertTrue((output_dir / "run_manifest.json").exists())
            self.assertTrue((output_dir / "metrics_report.json").exists())
            self.assertTrue((output_dir / "ball_audit.json").exists())
            self.assertIn("ai_review_triggers_error", report)

    def test_write_run_artifacts_preserves_manifest_when_player_tracks_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            (output_dir / "ball_track.csv").write_text(
                "Frame,X,Y,Confidence,Status\n0,1,2,0.9000,Detected\n",
                encoding="utf-8",
            )

            with mock.patch(
                "football_tracking.metrics.write_player_tracks_artifacts",
                side_effect=RuntimeError("player tracks failed"),
            ):
                manifest, report = write_run_artifacts(
                    output_dir=output_dir,
                    run={
                        "run_id": "run_player_tracks_failure",
                        "source": "api",
                        "status": "completed",
                    },
                )

            self.assertEqual("run_player_tracks_failure", manifest["run_id"])
            self.assertTrue((output_dir / "run_manifest.json").exists())
            self.assertTrue((output_dir / "metrics_report.json").exists())
            self.assertIn("player_tracks_error", report)
            self.assertIn("player track artifacts", report["player_tracks_error"])


if __name__ == "__main__":
    unittest.main()
