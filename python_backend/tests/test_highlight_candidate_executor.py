from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from football_tracking.ai_candidate_lifecycle import build_ai_candidate_lifecycle
from football_tracking.ai_candidate_registry import load_candidate_registry
from football_tracking.final_artifact_manifest import build_final_artifact_manifest
from football_tracking.highlight_candidate_executor import execute_highlight_candidate, highlight_candidate_output_dir


class HighlightCandidateExecutorTests(unittest.TestCase):
    def test_render_suggested_highlight_creates_candidate_artifacts_and_registry_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            output_dir = root / "baseline"
            input_video = root / "input.mp4"
            self.write_video(input_video, frame_count=20, fps=10.0)
            self.write_event_candidates(output_dir, source_frames=20)
            approval = {
                "approval_id": "highlight_1",
                "candidate_id": "highlight-candidate-1",
                "event_candidate_id": "event-1",
                "approved_action": "render_suggested_highlight",
                "suggested_window": {"start_frame": 3, "end_frame": 14},
                "clip_action": "publish",
            }

            report = execute_highlight_candidate(output_dir, approval, input_video=input_video)

            candidate_dir = output_dir / "ai_candidates" / "highlight" / "highlight-candidate-1"
            self.assertEqual("pass", report["comparison_status"])
            self.assertTrue((candidate_dir / "highlight.mp4").exists())
            self.assertTrue((candidate_dir / "highlight_report.json").exists())
            self.assertTrue((candidate_dir / "highlight_window_validation.json").exists())
            self.assertTrue((candidate_dir / "highlight_candidate_comparison.json").exists())
            self.assertTrue((candidate_dir / "candidate_manifest.json").exists())
            self.assertEqual(12, report["frame_count"])
            self.assertEqual(1.2, report["duration_seconds"])
            self.assertEqual(-2, report["pre_frame_delta"])
            self.assertEqual(2, report["post_frame_delta"])
            self.assertTrue(report["core_window_preserved"])
            self.assertEqual(4, report["required_tail_frames"])
            self.assertEqual(6, report["actual_tail_frames"])
            self.assertEqual("preserved", report["tail_status"])
            self.assertFalse(report["source_end_clamp"])
            comparison = json.loads((candidate_dir / "highlight_candidate_comparison.json").read_text(encoding="utf-8"))
            self.assertEqual(4, comparison["required_tail_frames"])
            self.assertEqual(6, comparison["actual_tail_frames"])
            highlight_report = json.loads((candidate_dir / "highlight_report.json").read_text(encoding="utf-8"))
            self.assertEqual({"start_frame": 5, "end_frame": 8}, highlight_report["core_window"])
            self.assertEqual({"start_frame": 3, "end_frame": 14}, highlight_report["render_window"])
            self.assertTrue(highlight_report["core_window_preserved"])
            self.assertEqual(4, highlight_report["required_tail_frames"])
            self.assertEqual(6, highlight_report["actual_tail_frames"])
            self.assertEqual("preserved", highlight_report["tail_status"])
            self.assertFalse(highlight_report["source_end_clamp"])
            self.assertTrue(highlight_report["window_validation"]["core_window_preserved"])
            self.assertEqual(4, highlight_report["window_validation"]["required_tail_frames"])
            self.assertEqual(6, highlight_report["window_validation"]["actual_tail_frames"])
            manifest = json.loads((candidate_dir / "candidate_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(4, manifest["required_tail_frames"])
            self.assertEqual(6, manifest["actual_tail_frames"])
            registry = load_candidate_registry(output_dir)
            self.assertEqual("loaded", registry["artifact_status"])
            self.assertEqual(["highlight-candidate-1"], [item["candidate_id"] for item in registry["candidates"]])
            self.assertEqual(1, registry["summary"]["counts_by_problem_type"]["highlight"])
            lifecycle = build_ai_candidate_lifecycle(output_dir)
            self.assertEqual("highlight", lifecycle["candidates"][0]["problem_type"])
            self.assertEqual("pass", lifecycle["candidates"][0]["comparison_status"])
            manifest = build_final_artifact_manifest(
                baseline_output={"path": str(output_dir), "status": "baseline"},
                candidate_outputs=[
                    {
                        "id": report["candidate_id"],
                        "candidate_id": report["candidate_id"],
                        "problem_type": "highlight",
                        "path": report["candidate_dir"],
                        "type": "clip",
                        "candidate_artifacts": report["candidate_artifacts"],
                    }
                ],
                final_artifacts=[],
                comparison_reports=[report],
                quality_gate_status={"status": "pass"},
            )
            self.assertEqual(1, manifest["summary"]["candidate_output_count"])
            self.assertEqual(1, len(manifest["clips"]))
            self.assertEqual(0, manifest["summary"]["final_artifact_count"])

    def test_source_end_clamped_highlight_renders_through_last_source_frame(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            output_dir = root / "baseline"
            input_video = root / "input.mp4"
            self.write_video(input_video, frame_count=12, fps=6.0)
            self.write_event_candidates(output_dir, source_frames=12, core_start=5, core_end=9, pre=2, post=6)

            report = execute_highlight_candidate(
                output_dir,
                {
                    "approval_id": "highlight_1",
                    "candidate_id": "highlight-candidate-1",
                    "event_candidate_id": "event-1",
                    "approved_action": "adjust_highlight_window",
                    "suggested_window": {"start_frame": 3, "end_frame": 11},
                },
                input_video=input_video,
            )
            candidate_dir = output_dir / "ai_candidates" / "highlight" / "highlight-candidate-1"
            validation = json.loads((candidate_dir / "highlight_window_validation.json").read_text(encoding="utf-8"))

        self.assertEqual("pass", report["comparison_status"])
        self.assertTrue(report["source_end_clamp"])
        self.assertEqual({"start_frame": 3, "end_frame": 11}, report["render_window"])
        self.assertEqual(9, report["frame_count"])
        self.assertEqual("source_end_clamped", report["tail_status"])
        self.assertEqual(6, report["required_tail_frames"])
        self.assertEqual(2, report["actual_tail_frames"])
        self.assertTrue(report["core_window_preserved"])
        source_bounds = self.check(report, "source_bounds")
        self.assertTrue(source_bounds["source_end_clamp"])
        validation_source_bounds = self.check(validation, "source_bounds")
        self.assertTrue(validation_source_bounds["source_end_clamp"])

    def test_invalid_approved_window_keeps_failed_comparison_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            output_dir = root / "baseline"
            input_video = root / "input.mp4"
            self.write_video(input_video, frame_count=20, fps=10.0)
            self.write_event_candidates(output_dir, source_frames=20)

            report = execute_highlight_candidate(
                output_dir,
                {
                    "approval_id": "highlight_1",
                    "candidate_id": "highlight-candidate-1",
                    "event_candidate_id": "event-1",
                    "approved_action": "render_suggested_highlight",
                    "suggested_window": {"start_frame": 3, "end_frame": 9},
                },
                input_video=input_video,
            )

            candidate_dir = output_dir / "ai_candidates" / "highlight" / "highlight-candidate-1"
            self.assertEqual("fail", report["comparison_status"])
            self.assertTrue(report["core_window_preserved"])
            self.assertEqual(4, report["required_tail_frames"])
            self.assertEqual(1, report["actual_tail_frames"])
            self.assertEqual("cut_available_tail", report["tail_status"])
            self.assertFalse((candidate_dir / "highlight.mp4").exists())
            self.assertTrue((candidate_dir / "highlight_window_validation.json").exists())
            self.assertTrue((candidate_dir / "highlight_candidate_comparison.json").exists())
            self.assertTrue((candidate_dir / "candidate_manifest.json").exists())
            registry = load_candidate_registry(output_dir)
            self.assertEqual("loaded", registry["artifact_status"])
            self.assertEqual("fail", registry["candidates"][0]["comparison_status"])

    def test_candidate_id_must_be_safe_single_directory_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            for candidate_id in ("../x", "bad\\id", "CON", "trailingspace ", "dot."):
                with self.subTest(candidate_id=candidate_id):
                    with self.assertRaises(ValueError):
                        highlight_candidate_output_dir(output_dir, candidate_id)

    def test_render_failure_removes_candidate_dir_without_registry_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            output_dir = root / "baseline"
            self.write_event_candidates(output_dir, source_frames=20)

            with self.assertRaisesRegex(RuntimeError, "Unable to open input video"):
                execute_highlight_candidate(
                    output_dir,
                    {
                        "approval_id": "highlight_1",
                        "candidate_id": "highlight-candidate-1",
                        "event_candidate_id": "event-1",
                        "approved_action": "render_suggested_highlight",
                        "suggested_window": {"start_frame": 3, "end_frame": 14},
                    },
                    input_video=root / "missing.mp4",
                )

            candidate_dir = output_dir / "ai_candidates" / "highlight" / "highlight-candidate-1"
            self.assertFalse(candidate_dir.exists())
            self.assertEqual("missing", load_candidate_registry(output_dir)["artifact_status"])

    def write_video(self, path: Path, *, frame_count: int, fps: float) -> None:
        writer = cv2.VideoWriter(
            str(path),
            cv2.VideoWriter.fourcc(*"mp4v"),
            fps,
            (160, 90),
        )
        if not writer.isOpened():
            self.skipTest("OpenCV video writer is unavailable in this environment.")
        for frame_index in range(frame_count):
            frame = np.full((90, 160, 3), frame_index * 10, dtype=np.uint8)
            writer.write(frame)
        writer.release()

    def write_event_candidates(
        self,
        output_dir: Path,
        *,
        source_frames: int,
        core_start: int = 5,
        core_end: int = 8,
        pre: int = 4,
        post: int = 4,
    ) -> None:
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "event_candidates.json").write_text(
            json.dumps(
                {
                    "summary": {"total_source_frames": source_frames},
                    "candidates": [
                        {
                            "id": "event-1",
                            "core_window": {"start_frame": core_start, "end_frame": core_end},
                            "render_window": {
                                "start_frame": max(0, core_start - pre),
                                "end_frame": min(source_frames - 1, core_end + post),
                            },
                            "buffer_policy": {
                                "pre_buffer_frames": pre,
                                "post_buffer_frames": post,
                                "min_tail_frames": post,
                            },
                        }
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def check(self, payload: dict[str, object], name: str) -> dict[str, object]:
        checks = payload["checks"]
        self.assertIsInstance(checks, list)
        for check in checks:
            if isinstance(check, dict) and check.get("name") == name:
                return check
        raise AssertionError(f"missing check {name}")


if __name__ == "__main__":
    unittest.main()
