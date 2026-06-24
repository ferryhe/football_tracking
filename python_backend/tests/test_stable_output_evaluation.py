from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from football_tracking.stable_output_evaluation import evaluate_stable_final_outputs


class StableOutputEvaluationTests(unittest.TestCase):
    def test_missing_manifest_is_unavailable_in_artifact_mode_and_fail_in_real_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)

            artifact_mode = evaluate_stable_final_outputs(output_dir, mode="artifact-only")
            real_mode = evaluate_stable_final_outputs(output_dir, mode="real")

        self.assertEqual("unavailable", artifact_mode["status"])
        self.assertIn("final_ai_improvement_artifact_manifest.json", artifact_mode["reason"])
        self.assertEqual("fail", real_mode["status"])

    def test_empty_final_selection_is_unavailable_in_artifact_mode_and_fail_in_real_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_json(output_dir / "final_ai_improvement_artifact_manifest.json", {"final_selected_artifacts": []})

            artifact_mode = evaluate_stable_final_outputs(output_dir, mode="artifact-only")
            real_mode = evaluate_stable_final_outputs(output_dir, mode="real")

        self.assertEqual("unavailable", artifact_mode["status"])
        self.assertEqual("fail", real_mode["status"])
        self.assertEqual(0, artifact_mode["selected_media_count"])

    def test_track_only_final_selection_does_not_fail_stable_media_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_json(
                output_dir / "final_ai_improvement_artifact_manifest.json",
                {"final_selected_artifacts": [{"candidate_id": "track-only", "type": "track", "path": "ball_track.csv"}]},
            )

            check = evaluate_stable_final_outputs(output_dir, mode="real")

        self.assertEqual("unavailable", check["status"])
        self.assertEqual(0, check["selected_media_count"])
        self.assertEqual("final_selected_artifacts contains no video or clip artifacts", check["reason"])

    def test_track_only_final_selection_with_clean_review_media_is_not_stable_media_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_review_media_pass(output_dir)
            _write_json(
                output_dir / "final_ai_improvement_artifact_manifest.json",
                {"final_selected_artifacts": [{"candidate_id": "track-only", "type": "track", "path": "ball_track.csv"}]},
            )

            check = evaluate_stable_final_outputs(output_dir, mode="real")

        self.assertEqual("unavailable", check["status"])
        self.assertEqual(0, check["selected_media_count"])

    def test_playable_final_video_with_useful_samples_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_useful_video(output_dir / "final" / "selected.mp4", frame_count=6)
            _write_review_media_pass(output_dir)
            _write_json(
                output_dir / "final_ai_improvement_artifact_manifest.json",
                {
                    "final_selected_artifacts": [
                        {"candidate_id": "candidate-good", "type": "video", "path": "final/selected.mp4"}
                    ]
                },
            )

            check = evaluate_stable_final_outputs(output_dir, mode="real")

        self.assertEqual("pass", check["status"])
        self.assertEqual(1, check["selected_media_count"])
        self.assertEqual("pass", check["artifacts"][0]["status"])
        self.assertGreaterEqual(check["artifacts"][0]["sample_count"], 3)
        self.assertEqual({"width": 64, "height": 48}, check["artifacts"][0]["dimensions"])
        self.assertEqual(0, check["artifacts"][0]["low_information_sample_count"])
        self.assertEqual(0, check["artifacts"][0]["gray_sample_count"])

    def test_playable_final_video_without_review_media_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_useful_video(output_dir / "final" / "selected.mp4", frame_count=6)
            _write_json(
                output_dir / "final_ai_improvement_artifact_manifest.json",
                {
                    "final_selected_artifacts": [
                        {"candidate_id": "candidate-good", "type": "video", "path": "final/selected.mp4"}
                    ]
                },
            )

            check = evaluate_stable_final_outputs(output_dir, mode="real")

        self.assertEqual("unavailable", check["status"])
        self.assertEqual("pass", check["artifacts"][0]["status"])
        self.assertEqual("unavailable", check["review_media"]["status"])

    def test_missing_final_clip_fails_with_per_artifact_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_json(
                output_dir / "final_ai_improvement_artifact_manifest.json",
                {
                    "final_selected_artifacts": [
                        {"candidate_id": "candidate-missing", "type": "clip", "path": "final/missing.mp4"}
                    ]
                },
            )

            check = evaluate_stable_final_outputs(output_dir, mode="real")

        self.assertEqual("fail", check["status"])
        self.assertEqual("fail", check["artifacts"][0]["status"])
        self.assertEqual("missing", check["artifacts"][0]["reason"])
        self.assertEqual(0, check["artifacts"][0]["sample_count"])

    def test_final_media_path_traversal_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name) / "output"
            output_dir.mkdir()
            _write_useful_video(Path(temp_name) / "outside.mp4", frame_count=4)
            _write_json(
                output_dir / "final_ai_improvement_artifact_manifest.json",
                {
                    "final_selected_artifacts": [
                        {"candidate_id": "candidate-unsafe", "type": "video", "path": "../outside.mp4"}
                    ]
                },
            )

            check = evaluate_stable_final_outputs(output_dir, mode="real")

        self.assertEqual("fail", check["status"])
        self.assertEqual("unsafe_path", check["artifacts"][0]["reason"])
        self.assertIsNone(check["artifacts"][0]["resolved_path"])

    def test_absolute_final_media_outside_output_dir_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name) / "output"
            output_dir.mkdir()
            outside = Path(temp_name) / "outside.mp4"
            _write_useful_video(outside, frame_count=4)
            _write_json(
                output_dir / "final_ai_improvement_artifact_manifest.json",
                {
                    "final_selected_artifacts": [
                        {"candidate_id": "candidate-unsafe", "type": "video", "path": str(outside)}
                    ]
                },
            )

            check = evaluate_stable_final_outputs(output_dir, mode="real")

        self.assertEqual("fail", check["status"])
        self.assertEqual("unsafe_path", check["artifacts"][0]["reason"])
        self.assertIsNone(check["artifacts"][0]["resolved_path"])

    def test_gray_low_information_video_does_not_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_gray_video(output_dir / "final" / "gray.mp4", frame_count=5)
            _write_json(
                output_dir / "final_ai_improvement_artifact_manifest.json",
                {
                    "final_selected_artifacts": [
                        {"candidate_id": "candidate-gray", "type": "video", "path": "final/gray.mp4"}
                    ]
                },
            )

            check = evaluate_stable_final_outputs(output_dir, mode="real")

        self.assertEqual("fail", check["status"])
        self.assertEqual("fail", check["artifacts"][0]["status"])
        self.assertEqual(check["artifacts"][0]["sample_count"], check["artifacts"][0]["gray_sample_count"])
        self.assertEqual(check["artifacts"][0]["sample_count"], check["artifacts"][0]["low_information_sample_count"])

    def test_review_media_integrity_warning_propagates_to_stable_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_useful_video(output_dir / "final" / "selected.mp4", frame_count=4)
            _write_json(
                output_dir / "final_ai_improvement_artifact_manifest.json",
                {
                    "final_selected_artifacts": [
                        {"candidate_id": "candidate-good", "type": "video", "path": "final/selected.mp4"}
                    ]
                },
            )
            _write_json(
                output_dir / "review_packets.json",
                {
                    "review_source": {"input_video": "match.hevc", "used_review_friendly_source": False},
                    "media_integrity": {
                        "status": "warn",
                        "image_count": 2,
                        "low_information_image_count": 1,
                        "likely_corrupt_image_count": 0,
                    },
                },
            )

            check = evaluate_stable_final_outputs(output_dir, mode="real")

        self.assertEqual("warn", check["status"])
        self.assertEqual("pass", check["artifacts"][0]["status"])
        self.assertEqual("warn", check["review_media"]["status"])
        self.assertEqual("review_packets.json", check["review_media"]["sources"][0]["artifact"])

    def test_likely_corrupt_review_media_integrity_fails_even_when_summary_status_is_warn(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_useful_video(output_dir / "final" / "selected.mp4", frame_count=4)
            _write_json(
                output_dir / "final_ai_improvement_artifact_manifest.json",
                {
                    "final_selected_artifacts": [
                        {"candidate_id": "candidate-good", "type": "video", "path": "final/selected.mp4"}
                    ]
                },
            )
            _write_json(
                output_dir / "review_packets.json",
                {"media_integrity": {"status": "warn", "likely_corrupt_image_count": 1}},
            )

            check = evaluate_stable_final_outputs(output_dir, mode="real")

        self.assertEqual("fail", check["status"])
        self.assertEqual("fail", check["review_media"]["status"])

    def test_corrupt_review_media_artifact_does_not_pass_with_selected_video(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_useful_video(output_dir / "final" / "selected.mp4", frame_count=4)
            _write_json(
                output_dir / "final_ai_improvement_artifact_manifest.json",
                {
                    "final_selected_artifacts": [
                        {"candidate_id": "candidate-good", "type": "video", "path": "final/selected.mp4"}
                    ]
                },
            )
            (output_dir / "review_packets.json").write_text("{not-json", encoding="utf-8")

            check = evaluate_stable_final_outputs(output_dir, mode="real")

        self.assertEqual("unavailable", check["status"])
        self.assertEqual("pass", check["artifacts"][0]["status"])
        self.assertEqual("unavailable", check["review_media"]["status"])
        self.assertEqual("corrupt", check["review_media"]["sources"][0]["artifact_status"])


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _write_review_media_pass(output_dir: Path) -> None:
    _write_json(
        output_dir / "review_packets.json",
        {"media_integrity": {"status": "ok", "image_count": 2, "low_information_image_count": 0}},
    )


def _write_useful_video(path: Path, *, frame_count: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter.fourcc(*"mp4v"), 8.0, (64, 48))
    if not writer.isOpened():
        raise RuntimeError("OpenCV VideoWriter could not create a tiny test video.")
    for index in range(frame_count):
        frame = np.zeros((48, 64, 3), dtype=np.uint8)
        frame[:, :, 0] = np.arange(64, dtype=np.uint8)
        frame[:, :, 1] = (np.arange(48, dtype=np.uint8)[:, None] * 3 + index * 11) % 255
        frame[:, :, 2] = ((frame[:, :, 0].astype(np.uint16) * 5 + index * 17) % 255).astype(np.uint8)
        cv2.line(frame, (0, 12 + index), (63, 40 - index), (240, 240, 240), 2)
        cv2.circle(frame, (10 + index * 5, 24), 4, (20, 220, 40), -1)
        writer.write(frame)
    writer.release()


def _write_gray_video(path: Path, *, frame_count: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter.fourcc(*"mp4v"), 8.0, (64, 48))
    if not writer.isOpened():
        raise RuntimeError("OpenCV VideoWriter could not create a tiny test video.")
    for _index in range(frame_count):
        writer.write(np.full((48, 64, 3), 128, dtype=np.uint8))
    writer.release()


if __name__ == "__main__":
    unittest.main()
