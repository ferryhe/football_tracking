from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import cv2
import numpy as np
import yaml
from fastapi import HTTPException
from pydantic import ValidationError

from football_tracking.api.app import create_app
from football_tracking.api.routes import inputs as input_routes
from football_tracking.api.routes.ai import approve_improvements as approve_improvements_route
from football_tracking.api.routes.ai import improve as improve_route
from football_tracking.api.routes import runs as run_routes
from football_tracking.api.routes.artifacts import get_artifact
from football_tracking.api.schemas import (
    AIFrameWindow,
    AIImproveApprovalRequest,
    AIImproveRequest,
    CreateRunRequest,
    HighlightRenderRequest,
)
from football_tracking.api.service import ApiService
from football_tracking.config import load_config
from football_tracking.metrics import write_run_artifacts


def build_sample_config(output_dir: str = "./outputs/kept_baseline") -> dict[str, object]:
    return {
        "input_video": "./data/input.mp4",
        "output_dir": output_dir,
        "detector": {
            "model_path": "./weights/football_ball_yolo.pt",
        },
        "postprocess": {
            "enabled": True,
            "max_detected_island_length": 2,
            "low_confidence_threshold": 0.45,
        },
        "follow_cam": {
            "enabled": True,
            "glide_pan_smoothing": 0.12,
            "catch_up_pan_smoothing": 0.24,
            "zoom_out_confirm_frames": 4,
            "zoom_in_confirm_frames": 8,
            "zoom_hold_frames_after_change": 10,
        },
        "scene_bias": {
            "dynamic_air_recovery": {
                "enabled": True,
                "tentative_reacquire_confidence_threshold": 0.30,
                "tentative_reacquire_score_threshold": 0.38,
            }
        },
    }


class ApiServiceSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_root = Path(self.temp_dir.name)
        for relative_path in ("config", "data", "outputs", "weights"):
            (self.repo_root / relative_path).mkdir(parents=True, exist_ok=True)

        self.write_text("data/input.mp4", "fake video")
        self.write_text("data/clip.mov", "supported second video")
        self.write_text("data/ignore.txt", "not a video")
        self.write_text("weights/football_ball_yolo.pt", "fake model")
        self.write_yaml("config/default.yaml", build_sample_config())
        self.write_yaml("config/alt.yaml", build_sample_config("./outputs/alt_run"))

        self.service = ApiService(self.repo_root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def write_text(self, relative_path: str, content: str) -> Path:
        path = self.repo_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def write_json(self, relative_path: str, payload: object) -> Path:
        path = self.repo_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def decode_preview_image(self, data_url: str) -> np.ndarray:
        encoded = data_url.split(",", 1)[1]
        buffer = np.frombuffer(base64.b64decode(encoded), dtype=np.uint8)
        image = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
        self.assertIsNotNone(image)
        return image

    def write_yaml(self, relative_path: str, payload: object) -> Path:
        path = self.repo_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=False), encoding="utf-8")
        return path

    def write_csv(self, relative_path: str, rows: list[dict[str, object]]) -> Path:
        if not rows:
            raise ValueError("rows must not be empty")
        headers = list(rows[0].keys())
        lines = [",".join(headers)]
        for row in rows:
            lines.append(",".join(str(row[key]) for key in headers))
        return self.write_text(relative_path, "\n".join(lines) + "\n")

    def file_fingerprint(self, path: Path) -> tuple[str, int]:
        return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns

    def write_video(self, relative_path: str) -> Path:
        path = self.repo_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        writer = cv2.VideoWriter(
            str(path),
            cv2.VideoWriter_fourcc(*"MJPG"),
            6.0,
            (640, 360),
        )
        if not writer.isOpened():
            self.skipTest("OpenCV video writer is unavailable in this environment.")
        for frame_index in range(12):
            frame = np.zeros((360, 640, 3), dtype=np.uint8)
            cv2.rectangle(frame, (70, 72), (580, 308), (10, 150 + frame_index * 6, 10), thickness=-1)
            writer.write(frame)
        writer.release()
        return path

    def write_wide_video(self, relative_path: str) -> Path:
        path = self.repo_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        writer = cv2.VideoWriter(
            str(path),
            cv2.VideoWriter_fourcc(*"MJPG"),
            6.0,
            (1280, 360),
        )
        if not writer.isOpened():
            self.skipTest("OpenCV video writer is unavailable in this environment.")
        polygon = np.array([[180, 78], [1100, 74], [1238, 340], [42, 344]], dtype=np.int32)
        for frame_index in range(12):
            frame = np.zeros((360, 1280, 3), dtype=np.uint8)
            cv2.fillPoly(frame, [polygon], (8, 150 + frame_index * 5, 8))
            writer.write(frame)
        writer.release()
        return path

    def write_quality_video(self, relative_path: str, *, poor: bool = False) -> Path:
        path = self.repo_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        writer = cv2.VideoWriter(
            str(path),
            cv2.VideoWriter_fourcc(*"MJPG"),
            8.0,
            (640, 360),
        )
        if not writer.isOpened():
            self.skipTest("OpenCV video writer is unavailable in this environment.")
        field_polygon = np.array([[54, 70], [586, 68], [632, 332], [8, 334]], dtype=np.int32)
        for frame_index in range(18):
            frame = np.zeros((360, 640, 3), dtype=np.uint8)
            if poor:
                shift = frame_index * 9
                dim_polygon = np.array(
                    [[170 + shift, 132], [470 + shift, 132], [500 + shift, 250], [140 + shift, 250]],
                    dtype=np.int32,
                )
                cv2.fillPoly(frame, [dim_polygon], (3, 18, 3))
                frame = cv2.GaussianBlur(frame, (31, 31), 0)
            else:
                cv2.fillPoly(frame, [field_polygon], (20, 145, 20))
                cv2.polylines(frame, [field_polygon], isClosed=True, color=(235, 235, 235), thickness=3)
                cv2.line(frame, (320, 78), (320, 326), (230, 230, 230), thickness=2)
                cv2.circle(frame, (320, 200), 48, (230, 230, 230), thickness=2)
                cv2.circle(frame, (250 + frame_index, 190), 6, (245, 245, 245), thickness=-1)
            writer.write(frame)
        writer.release()
        return path

    def write_neutral_field_video(self, relative_path: str) -> Path:
        path = self.repo_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        writer = cv2.VideoWriter(
            str(path),
            cv2.VideoWriter_fourcc(*"MJPG"),
            8.0,
            (640, 360),
        )
        if not writer.isOpened():
            self.skipTest("OpenCV video writer is unavailable in this environment.")
        field_polygon = np.array([[54, 70], [586, 68], [632, 332], [8, 334]], dtype=np.int32)
        for frame_index in range(18):
            frame = np.full((360, 640, 3), 28, dtype=np.uint8)
            cv2.fillPoly(frame, [field_polygon], (130, 130, 130))
            cv2.polylines(frame, [field_polygon], isClosed=True, color=(235, 235, 235), thickness=3)
            cv2.line(frame, (320, 78), (320, 326), (230, 230, 230), thickness=2)
            cv2.circle(frame, (250 + frame_index, 190), 6, (245, 245, 245), thickness=-1)
            writer.write(frame)
        writer.release()
        return path

    def create_output_bundle(self, folder_name: str) -> Path:
        output_dir = self.repo_root / "outputs" / folder_name
        output_dir.mkdir(parents=True, exist_ok=True)
        self.write_text(f"outputs/{folder_name}/annotated.cleaned.mp4", "fake mp4")
        self.write_text(f"outputs/{folder_name}/follow_cam.mp4", "fake follow cam")
        self.write_csv(
            f"outputs/{folder_name}/ball_track.csv",
            [
                {"Frame": 0, "X": 10, "Y": 20, "Confidence": "0.9000", "Status": "Detected"},
                {"Frame": 1, "X": 15, "Y": 25, "Confidence": "0.5000", "Status": "Predicted"},
                {"Frame": 2, "X": "", "Y": "", "Confidence": "0.0000", "Status": "Lost"},
            ],
        )
        self.write_csv(
            f"outputs/{folder_name}/ball_track.cleaned.csv",
            [
                {"Frame": 0, "X": 10, "Y": 20, "Confidence": "0.9000", "Status": "Detected"},
                {"Frame": 1, "X": 15, "Y": 25, "Confidence": "0.9000", "Status": "Detected"},
                {"Frame": 2, "X": "", "Y": "", "Confidence": "0.0000", "Status": "Lost"},
            ],
        )
        self.write_csv(
            f"outputs/{folder_name}/camera_path.csv",
            [
                {"frame": 0, "center_x": 100, "center_y": 200},
                {"frame": 1, "center_x": 120, "center_y": 210},
            ],
        )
        self.write_json(
            f"outputs/{folder_name}/cleanup_report.json",
            {
                "scrubbed_frame_count": 1,
                "scrubbed_segment_count": 1,
                "actions": [{"frame": 2, "action": "scrub"}],
            },
        )
        self.write_json(
            f"outputs/{folder_name}/follow_cam_report.json",
            {
                "track_source": "cleaned",
                "target_resolution": [1920, 1080],
                "mean_crop_height": 1015.0,
                "status_counts": {"Detected": 2, "Lost": 1},
            },
        )
        self.write_json(
            f"outputs/{folder_name}/ball_audit.json",
            {
                "schema_version": "1.0",
                "generated_at": "2026-01-01T00:00:00+00:00",
                "summary": {
                    "frame_count": 3,
                    "source_count": 2,
                    "tracklet_count": 2,
                    "suspicious_tracklet_count": 0,
                    "review_event_count": 0,
                    "lost_gap_count": 0,
                    "max_step_px": None,
                },
                "sources": [],
                "tracklets": [],
                "review_events": [],
            },
        )
        self.write_json(
            f"outputs/{folder_name}/ai_review_triggers.json",
            {
                "schema_version": "1.0",
                "generated_at": "2026-01-01T00:00:00+00:00",
                "decision": {
                    "needs_ai_review": True,
                    "priority": "medium",
                    "reason": "medium_priority_triggers",
                    "trigger_count": 1,
                    "recommended_review_windows": [
                        {"start_frame": 2, "end_frame": 2, "reason": "postprocess_action"}
                    ],
                },
                "triggers": [
                    {
                        "id": "event:0:postprocess_action:2-2",
                        "type": "postprocess_action",
                        "priority": "medium",
                        "source": "postprocess",
                        "start_frame": 2,
                        "end_frame": 2,
                        "frame_count": 1,
                        "reason": "scrub",
                        "evidence": {"action": "scrub"},
                    }
                ],
                "summary": {
                    "counts_by_type": {"postprocess_action": 1},
                    "counts_by_priority": {"medium": 1},
                    "max_trigger_priority": "medium",
                },
            },
        )
        self.write_json(
            f"outputs/{folder_name}/event_candidates.json",
            {
                "schema_version": "1.0",
                "source": {"name": "cleaned", "path": "ball_track.cleaned.csv", "row_count": 3},
                "summary": {
                    "frame_count": 3,
                    "detected_frame_count": 2,
                    "candidate_count": 1,
                    "counts_by_type": {"shot_candidate": 1},
                    "min_frame": 0,
                    "max_frame": 2,
                },
                "candidates": [
                    {
                        "id": "cleaned:shot_candidate:0-1",
                        "type": "shot_candidate",
                        "label": "candidate",
                        "start_frame": 0,
                        "end_frame": 1,
                        "frame_count": 2,
                        "score": 0.62,
                        "reason": "Sustained ball track contains a speed burst.",
                        "core_window": {"start_frame": 0, "end_frame": 1},
                        "render_window": {"start_frame": 0, "end_frame": 7},
                        "buffer_policy": {
                            "fps": 6.0,
                            "fps_source": "test_fixture",
                            "pre_buffer_seconds": 0.75,
                            "post_buffer_seconds": 4.5,
                            "pre_buffer_frames": 4,
                            "post_buffer_frames": 27,
                            "min_post_event_frames": 27,
                            "min_tail_frames": 27,
                        },
                        "evidence": {
                            "max_speed_px_per_frame": 35.0,
                            "mean_confidence": 0.9,
                            "goal_side": None,
                        },
                    }
                ],
            },
        )
        self.write_json(
            f"outputs/{folder_name}/player_tracks.json",
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
                "tracks": [
                    {
                        "id": "P001",
                        "start_frame": 0,
                        "end_frame": 1,
                        "length": 2,
                        "team": "home",
                        "mean_confidence": 0.85,
                        "first_foot_point": {"x": 10.0, "y": 50.0},
                        "last_foot_point": {"x": 12.0, "y": 50.0},
                        "max_step_px": 2.0,
                        "samples": [
                            {
                                "frame": 0,
                                "bbox": [0.0, 0.0, 20.0, 50.0],
                                "foot_point": {"x": 10.0, "y": 50.0},
                                "confidence": 0.9,
                                "label": "person",
                                "team": "home",
                            },
                            {
                                "frame": 1,
                                "bbox": [2.0, 0.0, 22.0, 50.0],
                                "foot_point": {"x": 12.0, "y": 50.0},
                                "confidence": 0.8,
                                "label": "person",
                                "team": "home",
                            },
                        ],
                    }
                ],
            },
        )
        self.write_text(
            f"outputs/{folder_name}/player_detections.jsonl",
            "\n".join(
                [
                    json.dumps(
                        {
                            "frame": 0,
                            "bbox": [0.0, 0.0, 20.0, 50.0],
                            "confidence": 0.9,
                            "label": "person",
                            "team": "home",
                        }
                    ),
                    json.dumps(
                        {
                            "frame": 1,
                            "bbox": [2.0, 0.0, 22.0, 50.0],
                            "confidence": 0.8,
                            "label": "person",
                            "team": "home",
                        }
                    ),
                ]
            )
            + "\n",
        )
        return output_dir

    def write_temporal_chunk_artifacts(self, folder_name: str) -> None:
        self.write_json(
            f"outputs/{folder_name}/temporal_chunks_report.json",
            {
                "chunk_count": 2,
                "frame_count": 3,
                "chunks": [
                    {
                        "index": 0,
                        "name": "chunk_0000",
                        "start_frame": 0,
                        "end_frame": 2,
                        "core_start_frame": 0,
                        "core_end_frame": 1,
                    },
                    {
                        "index": 1,
                        "name": "chunk_0001",
                        "start_frame": 1,
                        "end_frame": 3,
                        "core_start_frame": 2,
                        "core_end_frame": 2,
                    },
                ],
                "boundary_events": [{"frame": 1}],
                "execution": {
                    "status": "succeeded",
                    "mode": "subprocess",
                    "requested_workers": 2,
                    "effective_workers": 2,
                },
                "stitch": {"status": "succeeded"},
            },
        )
        self.write_csv(
            f"outputs/{folder_name}/chunks/chunk_0000/ball_track.csv",
            [
                {"Frame": 0, "X": 10, "Y": 20, "Confidence": "0.9000", "Status": "Detected"},
                {"Frame": 1, "X": 11, "Y": 20, "Confidence": "0.9000", "Status": "Detected"},
            ],
        )
        self.write_text(
            f"outputs/{folder_name}/chunks/chunk_0000/debug.jsonl",
            json.dumps({"frame": 0, "candidates": []}) + "\n",
        )
        self.write_text(
            f"outputs/{folder_name}/chunks/chunk_0000/worker.stdout.log",
            "chunk complete\n",
        )
        self.write_text(
            f"outputs/{folder_name}/chunks/chunk_0000/frames/frame_000001.jpg",
            "frame image placeholder",
        )

    def test_list_input_videos_filters_supported_suffixes(self) -> None:
        catalog = self.service.list_input_videos()

        self.assertEqual(self.repo_root.joinpath("data").resolve().as_posix(), Path(catalog["root_dir"]).as_posix())
        self.assertEqual(["clip.mov", "input.mp4"], [item["name"] for item in catalog["videos"]])
        self.assertTrue(all(item["path"].endswith((".mov", ".mp4")) for item in catalog["videos"]))

    def test_derive_config_writes_generated_yaml(self) -> None:
        derived = self.service.derive_config(
            base_config_name="default.yaml",
            output_name="../unsafe_name",
            patch={
                "follow_cam": {
                    "zoom_out_confirm_frames": 6,
                }
            },
        )

        self.assertEqual("generated/unsafe_name.yaml", derived["name"])
        generated_path = self.repo_root / "config" / "generated" / "unsafe_name.yaml"
        self.assertTrue(generated_path.exists())
        generated_raw = yaml.safe_load(generated_path.read_text(encoding="utf-8"))
        self.assertEqual(6, generated_raw["follow_cam"]["zoom_out_confirm_frames"])

    def test_list_configs_includes_created_at(self) -> None:
        configs = self.service.list_configs()

        default_config = next(item for item in configs if item["name"] == "default.yaml")
        self.assertIn("created_at", default_config)
        self.assertTrue(default_config["created_at"].endswith("+00:00"))

    def test_list_configs_prefers_embedded_created_at_before_file_timestamp(self) -> None:
        self.write_yaml(
            "config/embedded_time.yaml",
            {
                **build_sample_config("./outputs/embedded_time"),
                "metadata": {
                    "created_at": "2024-01-02T03:04:05Z",
                },
            },
        )

        configs = self.service.list_configs()

        embedded = next(item for item in configs if item["name"] == "embedded_time.yaml")
        self.assertEqual("2024-01-02T03:04:05+00:00", embedded["created_at"])

    def test_suggest_field_setup_returns_preview_and_config_patch(self) -> None:
        video_path = self.write_video("data/field_preview.avi")

        suggestion = self.service.suggest_field_setup(str(video_path))

        self.assertTrue(suggestion["preview_data_url"].startswith("data:image/jpeg;base64,"))
        self.assertIn(suggestion["confidence"], {"detected", "fallback"})
        self.assertEqual(640, suggestion["frame_width"])
        self.assertEqual(360, suggestion["frame_height"])
        self.assertEqual(4, len(suggestion["preview_bounds"]))
        self.assertEqual(9, len(suggestion["field_polygon"]))
        self.assertEqual(9, len(suggestion["expanded_polygon"]))
        field_roi = suggestion["field_roi"]
        expanded_roi = suggestion["expanded_roi"]
        self.assertLess(field_roi[0], field_roi[2])
        self.assertLess(field_roi[1], field_roi[3])
        self.assertLessEqual(expanded_roi[0], field_roi[0])
        self.assertGreaterEqual(expanded_roi[2], field_roi[2])
        self.assertEqual(list(expanded_roi), suggestion["config_patch"]["filtering"]["roi"])
        self.assertEqual(9, len(suggestion["config_patch"]["scene_bias"]["ground_zones"][0]["points"]))
        calibration = suggestion["calibration"]
        self.assertIsNotNone(calibration)
        self.assertEqual(4, len(calibration["image_points"]))
        self.assertEqual(4, len(calibration["pitch_points"]))
        self.assertEqual(3, len(calibration["image_to_pitch_matrix"]))
        self.assertEqual(3, len(calibration["pitch_to_image_matrix"]))
        self.assertEqual({"length_m": 105.0, "width_m": 68.0}, calibration["pitch_dimensions"])
        self.assertEqual(f"{suggestion['source']}:field-polygon-corners", calibration["source"])
        self.assertEqual("estimated" if suggestion["confidence"] == "detected" else "low", calibration["confidence"])
        self.assertEqual(list(suggestion["field_polygon"][0]), calibration["image_points"][0])

    def test_capture_field_preview_returns_fixed_preview_frame(self) -> None:
        video_path = self.write_video("data/preview_only.avi")

        preview = self.service.capture_field_preview(str(video_path))

        self.assertTrue(preview["preview_data_url"].startswith("data:image/jpeg;base64,"))
        self.assertEqual(640, preview["frame_width"])
        self.assertEqual(360, preview["frame_height"])
        self.assertGreaterEqual(preview["frame_index"], 0)
        self.assertEqual(2, preview["sample_index"])
        self.assertEqual(3, preview["sample_count"])

    def test_capture_field_preview_can_select_specific_sample(self) -> None:
        video_path = self.write_video("data/preview_cycle.avi")

        preview_first = self.service.capture_field_preview(str(video_path), sample_index=1)
        preview_last = self.service.capture_field_preview(str(video_path), sample_index=3)

        self.assertEqual(1, preview_first["sample_index"])
        self.assertEqual(3, preview_last["sample_index"])
        self.assertEqual(3, preview_first["sample_count"])
        self.assertEqual(3, preview_last["sample_count"])
        self.assertNotEqual(preview_first["frame_index"], preview_last["frame_index"])

    def test_suggest_field_setup_prefers_existing_config_polygon(self) -> None:
        video_path = self.write_video("data/config_preview.avi")
        self.write_yaml(
            "config/polygon.yaml",
            {
                **build_sample_config(),
                "scene_bias": {
                    "enabled": True,
                    "ground_zones": [
                        {
                            "name": "main_pitch",
                            "points": [[32, 96], [608, 92], [632, 340], [12, 344]],
                        }
                    ],
                    "positive_rois": [
                        {
                            "name": "main_pitch_buffer",
                            "points": [[8, 80], [632, 80], [640, 356], [0, 356]],
                        }
                    ],
                },
            },
        )

        suggestion = self.service.suggest_field_setup(str(video_path), config_name="polygon.yaml")

        self.assertEqual("config", suggestion["confidence"])
        self.assertEqual("config:polygon.yaml", suggestion["source"])
        self.assertEqual((32, 96), suggestion["field_polygon"][0])
        self.assertEqual((8, 80), suggestion["expanded_polygon"][0])
        self.assertEqual(
            [[32.0, 96.0], [608.0, 92.0], [632.0, 340.0], [12.0, 344.0]],
            suggestion["calibration"]["image_points"],
        )
        self.assertEqual("config", suggestion["calibration"]["confidence"])
        self.assertEqual("config:polygon.yaml:field-polygon-corners", suggestion["calibration"]["source"])
        preview_image = self.decode_preview_image(suggestion["preview_data_url"])
        self.assertLessEqual(preview_image.shape[1], 1600)
        self.assertAlmostEqual(640 / 360, preview_image.shape[1] / preview_image.shape[0], places=2)

    def test_suggest_field_setup_keeps_full_frame_preview_for_wide_video(self) -> None:
        video_path = self.write_wide_video("data/fisheye_preview.avi")

        suggestion = self.service.suggest_field_setup(str(video_path))

        self.assertEqual((0, 0, 1280, 360), suggestion["preview_bounds"])
        self.assertEqual(9, len(suggestion["field_polygon"]))
        self.assertLess(suggestion["field_polygon"][3][1], suggestion["field_polygon"][0][1])
        self.assertLess(suggestion["field_polygon"][3][1], suggestion["field_polygon"][6][1])
        self.assertGreater(suggestion["field_polygon"][7][1], suggestion["field_polygon"][3][1])

    def test_check_input_quality_passes_good_synthetic_video(self) -> None:
        video_path = self.write_quality_video("data/good_quality.avi")
        self.write_yaml(
            "config/quality_polygon.yaml",
            {
                **build_sample_config(),
                "scene_bias": {
                    "enabled": True,
                    "ground_zones": [
                        {
                            "name": "main_pitch",
                            "points": [[54, 70], [586, 68], [632, 332], [8, 334]],
                        }
                    ],
                },
            },
        )
        if not hasattr(self.service, "check_input_quality"):
            self.fail("ApiService.check_input_quality is missing")

        quality = self.service.check_input_quality(str(video_path), config_name="quality_polygon.yaml")

        self.assertEqual(str(video_path.resolve()), quality["input_video"])
        self.assertEqual(640, quality["frame_width"])
        self.assertEqual(360, quality["frame_height"])
        self.assertEqual(3, quality["sample_count"])
        self.assertGreaterEqual(quality["overall_score"], 0.75)
        self.assertEqual("pass", quality["overall_status"])
        checks = {check["key"]: check for check in quality["checks"]}
        self.assertEqual(
            {"brightness", "blur", "field_visibility", "camera_stability", "calibration"},
            set(checks),
        )
        self.assertEqual("pass", checks["calibration"]["status"])
        self.assertEqual("config", checks["calibration"]["value"])
        self.assertIn("Proceed with a normal tracking run.", quality["recommendations"])

    def test_check_input_quality_fails_poor_synthetic_video(self) -> None:
        video_path = self.write_quality_video("data/poor_quality.avi", poor=True)
        if not hasattr(self.service, "check_input_quality"):
            self.fail("ApiService.check_input_quality is missing")

        quality = self.service.check_input_quality(str(video_path))

        self.assertEqual("fail", quality["overall_status"])
        self.assertLess(quality["overall_score"], 0.45)
        checks = {check["key"]: check for check in quality["checks"]}
        self.assertEqual("fail", checks["brightness"]["status"])
        self.assertEqual("fail", checks["blur"]["status"])
        self.assertEqual("fail", checks["field_visibility"]["status"])
        self.assertTrue(any("light" in recommendation.lower() for recommendation in quality["recommendations"]))

    def test_check_input_quality_uses_config_polygon_for_field_visibility(self) -> None:
        video_path = self.write_neutral_field_video("data/non_green_quality.avi")
        self.write_yaml(
            "config/non_green_quality.yaml",
            {
                **build_sample_config(),
                "scene_bias": {
                    "enabled": True,
                    "ground_zones": [
                        {
                            "name": "main_pitch",
                            "points": [[54, 70], [586, 68], [632, 332], [8, 334]],
                        }
                    ],
                },
            },
        )

        quality = self.service.check_input_quality(str(video_path), config_name="non_green_quality.yaml")

        checks = {check["key"]: check for check in quality["checks"]}
        self.assertEqual("pass", checks["field_visibility"]["status"])
        self.assertEqual("config", checks["calibration"]["value"])

    def test_quality_check_route_reports_missing_input_video(self) -> None:
        if not hasattr(input_routes, "check_input_quality"):
            self.fail("inputs.check_input_quality route is missing")

        with self.assertRaises(HTTPException) as raised:
            input_routes.check_input_quality(
                SimpleNamespace(input_video=str((self.repo_root / "data" / "missing.mp4").resolve()), config_name=None),
                service=self.service,
            )

        self.assertEqual(404, raised.exception.status_code)

    def test_quality_check_route_reports_unreadable_input_video(self) -> None:
        if not hasattr(input_routes, "check_input_quality"):
            self.fail("inputs.check_input_quality route is missing")

        with self.assertRaises(HTTPException) as raised:
            input_routes.check_input_quality(
                SimpleNamespace(input_video=str((self.repo_root / "data" / "ignore.txt").resolve()), config_name=None),
                service=self.service,
            )

        self.assertEqual(400, raised.exception.status_code)

    def test_create_app_registers_quality_check_route(self) -> None:
        app = create_app(self.repo_root)
        route_paths = {route.path for route in app.routes}

        self.assertIn("/api/v1/inputs/quality-check", route_paths)

    def test_create_run_route_reports_value_error_as_bad_request(self) -> None:
        class BadRequestService:
            def create_run(self, request):
                raise ValueError("bad request")

        with self.assertRaises(HTTPException) as raised:
            run_routes.create_run(
                CreateRunRequest(config_name="default.yaml"),
                service=BadRequestService(),  # type: ignore[arg-type]
            )

        self.assertEqual(400, raised.exception.status_code)

    def test_quality_check_route_uses_service_response(self) -> None:
        if not hasattr(input_routes, "check_input_quality"):
            self.fail("inputs.check_input_quality route is missing")

        response = input_routes.check_input_quality(
            SimpleNamespace(input_video="video.mp4", config_name="default.yaml"),
            service=mock.Mock(
                check_input_quality=mock.Mock(
                    return_value={
                        "input_video": "video.mp4",
                        "frame_width": 640,
                        "frame_height": 360,
                        "sample_count": 3,
                        "overall_score": 0.8,
                        "overall_status": "pass",
                        "checks": [],
                        "recommendations": ["Proceed with a normal tracking run."],
                    }
                )
            ),
        )

        self.assertEqual("pass", response.overall_status)
        self.assertEqual("video.mp4", response.input_video)

    def test_sample_video_frames_uses_warmup_before_target_seek(self) -> None:
        class FakeCapture:
            def __init__(self) -> None:
                self.current = 0
                self.set_calls: list[int] = []

            def isOpened(self) -> bool:
                return True

            def get(self, prop: int) -> float:
                if prop == cv2.CAP_PROP_FPS:
                    return 20.0
                if prop == cv2.CAP_PROP_FRAME_COUNT:
                    return 100.0
                return 0.0

            def set(self, prop: int, value: float) -> bool:
                if prop == cv2.CAP_PROP_POS_FRAMES:
                    self.current = int(value)
                    self.set_calls.append(int(value))
                return True

            def read(self) -> tuple[bool, np.ndarray]:
                frame = np.full((8, 12, 3), self.current % 255, dtype=np.uint8)
                self.current += 1
                return True, frame

            def release(self) -> None:
                return None

        fake_capture = FakeCapture()

        with mock.patch("football_tracking.api.service.cv2.VideoCapture", return_value=fake_capture):
            samples = self.service._sample_video_frames(Path("dummy.mp4"))

        self.assertEqual(3, len(samples))
        self.assertEqual([0, 2, 33], fake_capture.set_calls)
        self.assertEqual([18, 50, 81], [sample["frame_index"] for sample in samples])

    def test_materialize_run_config_writes_generated_patch_file(self) -> None:
        config_path, relative_name = self.service._resolve_config_path("default.yaml")

        materialized_path, materialized_name = self.service._materialize_run_config(
            base_config_path=config_path,
            base_config_name=relative_name,
            run_id="run_demo1234",
            patch={"filtering": {"roi": [10, 20, 300, 320]}},
            suffix="field_setup",
        )

        self.assertTrue(materialized_path.exists())
        self.assertEqual("generated/default_field_setup_run_demo1234.yaml", materialized_name)
        generated_raw = yaml.safe_load(materialized_path.read_text(encoding="utf-8"))
        self.assertEqual([10, 20, 300, 320], generated_raw["filtering"]["roi"])
        resolved = load_config(materialized_path)
        self.assertEqual((self.repo_root / "data" / "input.mp4").resolve().as_posix(), resolved.input_video.as_posix())

    def test_list_runs_discovers_output_dirs_and_summarizes_stats(self) -> None:
        self.create_output_bundle("kept_baseline")

        runs = self.service.list_runs()

        self.assertEqual(1, len(runs))
        run = runs[0]
        self.assertEqual("scan_kept_baseline", run["run_id"])
        self.assertEqual("default.yaml", run["config_name"])
        self.assertIsNotNone(run["completed_at"])
        self.assertEqual(3, run["stats"]["raw"]["frame_count"])
        self.assertEqual(2, run["stats"]["cleaned"]["detected"])
        self.assertEqual("cleaned", run["stats"]["follow_cam"]["track_source"])
        self.assertEqual(2, run["stats"]["ball_audit"]["tracklet_count"])
        self.assertEqual("medium", run["stats"]["ai_review_triggers"]["priority"])
        self.assertEqual(1, run["stats"]["event_candidates"]["candidate_count"])
        self.assertEqual(1, run["stats"]["player_tracks"]["track_count"])
        self.assertIn("follow_cam.mp4", {artifact["name"] for artifact in run["artifacts"]})
        output_dir = self.repo_root / "outputs" / "kept_baseline"
        self.assertFalse((output_dir / "run_manifest.json").exists())
        self.assertFalse((output_dir / "metrics_report.json").exists())

    def test_list_runs_collects_metrics_artifacts_and_stats(self) -> None:
        output_dir = self.create_output_bundle("kept_baseline")
        write_run_artifacts(
            output_dir=output_dir,
            run={
                "run_id": "scan_kept_baseline",
                "source": "scan",
                "status": "completed",
                "created_at": "2026-01-01T00:00:00+00:00",
                "started_at": "2026-01-01T00:00:00+00:00",
                "completed_at": "2026-01-01T00:00:01+00:00",
                "config_name": "default.yaml",
                "config_path": str((self.repo_root / "config" / "default.yaml").resolve()),
                "input_video": str((self.repo_root / "data" / "input.mp4").resolve()),
                "output_dir": str(output_dir),
                "modules_enabled": {"postprocess": True, "follow_cam": True},
                "notes": None,
            },
        )

        run = self.service.list_runs()[0]

        artifact_names = {artifact["name"] for artifact in run["artifacts"]}
        self.assertIn("run_manifest.json", artifact_names)
        self.assertIn("metrics_report.json", artifact_names)
        self.assertIn("metrics_report", run["stats"])
        self.assertEqual(3, run["stats"]["raw"]["frame_count"])
        self.assertEqual({"Detected": 1, "Predicted": 1, "Lost": 1}, run["stats"]["raw"]["status_counts"])
        self.assertEqual(1, run["stats"]["cleanup"]["scrubbed_frame_count"])
        self.assertEqual("cleaned", run["stats"]["follow_cam"]["track_source"])
        self.assertIn("ball_audit.json", artifact_names)
        self.assertEqual(2, run["stats"]["ball_audit"]["tracklet_count"])
        self.assertIn("ai_review_triggers.json", artifact_names)
        self.assertIn("ai_review_triggers", run["stats"])
        self.assertTrue(run["stats"]["ai_review_triggers"]["needs_ai_review"])
        self.assertIn("event_candidates.json", artifact_names)
        self.assertIn("event_candidates", run["stats"])
        self.assertEqual("cleaned", run["stats"]["event_candidates"]["source_name"])
        self.assertIn("player_tracks.json", artifact_names)
        self.assertEqual(1, run["stats"]["player_tracks"]["track_count"])

    def test_list_runs_exposes_temporal_chunk_report_and_nested_artifacts(self) -> None:
        output_dir = self.create_output_bundle("chunked_baseline")
        self.write_temporal_chunk_artifacts("chunked_baseline")
        write_run_artifacts(
            output_dir=output_dir,
            run={
                "run_id": "scan_chunked_baseline",
                "source": "scan",
                "status": "completed",
                "created_at": "2026-01-01T00:00:00+00:00",
                "started_at": "2026-01-01T00:00:00+00:00",
                "completed_at": "2026-01-01T00:00:01+00:00",
                "config_name": "default.yaml",
                "config_path": str((self.repo_root / "config" / "default.yaml").resolve()),
                "input_video": str((self.repo_root / "data" / "input.mp4").resolve()),
                "output_dir": str(output_dir),
                "modules_enabled": {"postprocess": True, "follow_cam": True, "temporal_chunks": True},
                "notes": None,
            },
        )

        run = self.service.list_runs()[0]

        artifact_by_name = {artifact["name"]: artifact for artifact in run["artifacts"]}
        self.assertEqual("json", artifact_by_name["temporal_chunks_report.json"]["kind"])
        self.assertEqual("csv", artifact_by_name["chunks/chunk_0000/ball_track.csv"]["kind"])
        self.assertEqual("jsonl", artifact_by_name["chunks/chunk_0000/debug.jsonl"]["kind"])
        self.assertEqual("file", artifact_by_name["chunks/chunk_0000/worker.stdout.log"]["kind"])
        self.assertNotIn("chunks/chunk_0000/frames/frame_000001.jpg", artifact_by_name)
        self.assertEqual(2, run["stats"]["temporal_chunks"]["chunk_count"])
        self.assertEqual(2, run["stats"]["temporal_chunks"]["effective_workers"])
        nested_artifact = self.service.get_artifact_path(run["run_id"], "chunks/chunk_0000/debug.jsonl")
        self.assertEqual((output_dir / "chunks" / "chunk_0000" / "debug.jsonl").resolve(), nested_artifact)

    def test_download_nested_temporal_chunk_artifact_uses_relative_artifact_metadata(self) -> None:
        output_dir = self.create_output_bundle("chunked_download")
        self.write_temporal_chunk_artifacts("chunked_download")
        write_run_artifacts(
            output_dir=output_dir,
            run={
                "run_id": "scan_chunked_download",
                "source": "scan",
                "status": "completed",
                "created_at": "2026-01-01T00:00:00+00:00",
                "config_name": "default.yaml",
                "config_path": str((self.repo_root / "config" / "default.yaml").resolve()),
                "input_video": str((self.repo_root / "data" / "input.mp4").resolve()),
                "output_dir": str(output_dir),
                "modules_enabled": {"temporal_chunks": True},
            },
        )
        run = self.service.list_runs()[0]

        response = get_artifact(run["run_id"], "chunks/chunk_0000/ball_track.csv", service=self.service)
        expected_content_type = next(
            artifact["content_type"]
            for artifact in self.service.list_artifacts(run["run_id"])
            if artifact["name"] == "chunks/chunk_0000/ball_track.csv"
        )

        self.assertTrue(str(response.path).endswith("chunks\\chunk_0000\\ball_track.csv") or str(response.path).endswith("chunks/chunk_0000/ball_track.csv"))
        self.assertEqual(expected_content_type, response.media_type)
        self.assertEqual("ball_track.csv", response.filename)

    def test_list_runs_exposes_custom_temporal_chunk_artifact_root(self) -> None:
        output_dir = self.create_output_bundle("custom_chunk_root")
        self.write_json(
            "outputs/custom_chunk_root/temporal_chunks_report.json",
            {
                "chunk_count": 1,
                "frame_count": 2,
                "chunks": [{"index": 0, "name": "chunk_0000"}],
                "execution": {"status": "succeeded", "effective_workers": 1},
                "stitch": {"status": "succeeded"},
            },
        )
        self.write_csv(
            "outputs/custom_chunk_root/segments/chunk_0000/ball_track.csv",
            [
                {"Frame": 0, "X": 10, "Y": 20, "Confidence": "0.9000", "Status": "Detected"},
                {"Frame": 1, "X": 11, "Y": 20, "Confidence": "0.9000", "Status": "Detected"},
            ],
        )
        write_run_artifacts(
            output_dir=output_dir,
            run={
                "run_id": "scan_custom_chunk_root",
                "source": "scan",
                "status": "completed",
                "created_at": "2026-01-01T00:00:00+00:00",
                "config_name": "default.yaml",
                "config_path": str((self.repo_root / "config" / "default.yaml").resolve()),
                "input_video": str((self.repo_root / "data" / "input.mp4").resolve()),
                "output_dir": str(output_dir),
                "modules_enabled": {"temporal_chunks": True},
            },
        )

        run = self.service.list_runs()[0]

        artifact_names = {artifact["name"] for artifact in run["artifacts"]}
        self.assertIn("segments/chunk_0000/ball_track.csv", artifact_names)

    def test_get_ball_audit_report_loads_json_artifact(self) -> None:
        self.create_output_bundle("kept_baseline")
        run = self.service.list_runs()[0]

        report = self.service.get_ball_audit_report(run["run_id"])

        self.assertEqual("1.0", report["schema_version"])
        self.assertEqual(2, report["summary"]["tracklet_count"])

    def test_get_ai_review_triggers_report_loads_json_artifact(self) -> None:
        self.create_output_bundle("kept_baseline")
        run = self.service.list_runs()[0]

        report = self.service.get_ai_review_triggers_report(run["run_id"])

        self.assertEqual("1.0", report["schema_version"])
        self.assertTrue(report["decision"]["needs_ai_review"])
        self.assertEqual("medium", report["decision"]["priority"])

    def test_get_player_tracks_report_loads_json_artifact(self) -> None:
        self.create_output_bundle("kept_baseline")
        run = self.service.list_runs()[0]

        report = self.service.get_player_tracks_report(run["run_id"])

        self.assertEqual("1.0", report["schema_version"])
        self.assertEqual(1, report["summary"]["track_count"])
        self.assertEqual("P001", report["tracks"][0]["id"])

    def test_get_event_candidates_report_loads_json_artifact(self) -> None:
        self.create_output_bundle("kept_baseline")
        run = self.service.list_runs()[0]

        report = self.service.get_event_candidates_report(run["run_id"])

        self.assertEqual("1.0", report["schema_version"])
        self.assertEqual("cleaned", report["source"]["name"])
        self.assertEqual(1, report["summary"]["candidate_count"])
        self.assertEqual("shot_candidate", report["candidates"][0]["type"])

    def test_list_runs_ignores_malformed_audit_artifacts(self) -> None:
        output_dir = self.create_output_bundle("kept_baseline")
        (output_dir / "ball_audit.json").write_bytes(b"\xff\xfe\xff")
        (output_dir / "ai_review_triggers.json").write_text("{", encoding="utf-8")
        (output_dir / "event_candidates.json").write_text("{", encoding="utf-8")
        (output_dir / "player_tracks.json").write_bytes(b"\xff\xfe\xff")

        runs = self.service.list_runs()

        self.assertEqual(1, len(runs))
        self.assertNotIn("ball_audit", runs[0]["stats"])
        self.assertNotIn("ai_review_triggers", runs[0]["stats"])
        self.assertNotIn("event_candidates", runs[0]["stats"])
        self.assertNotIn("player_tracks", runs[0]["stats"])

    def test_report_loaders_treat_malformed_json_as_missing(self) -> None:
        output_dir = self.create_output_bundle("kept_baseline")
        run = self.service.list_runs()[0]
        (output_dir / "ball_audit.json").write_text("{", encoding="utf-8")
        (output_dir / "ai_review_triggers.json").write_bytes(b"\xff\xfe\xff")
        (output_dir / "event_candidates.json").write_text("{", encoding="utf-8")
        (output_dir / "player_tracks.json").write_text("{", encoding="utf-8")

        with self.assertRaises(FileNotFoundError):
            self.service.get_ball_audit_report(run["run_id"])
        with self.assertRaises(FileNotFoundError):
            self.service.get_ai_review_triggers_report(run["run_id"])
        with self.assertRaises(FileNotFoundError):
            self.service.get_event_candidates_report(run["run_id"])
        with self.assertRaises(FileNotFoundError):
            self.service.get_player_tracks_report(run["run_id"])

    def test_list_runs_falls_back_when_metrics_report_is_not_an_object(self) -> None:
        output_dir = self.create_output_bundle("kept_baseline")
        (output_dir / "metrics_report.json").write_text("[]", encoding="utf-8")

        run = self.service.list_runs()[0]

        self.assertNotIn("metrics_report", run["stats"])
        self.assertEqual(3, run["stats"]["raw"]["frame_count"])
        self.assertEqual(2, run["stats"]["cleaned"]["detected"])

    def test_list_asset_groups_groups_by_input_and_keeps_unbound_legacy(self) -> None:
        self.create_output_bundle("kept_baseline")
        self.create_output_bundle("legacy_only")

        groups = self.service.list_asset_groups()

        input_group = next(group for group in groups if group["input_video"] and group["input_video"]["name"] == "input.mp4")
        self.assertEqual(1, input_group["run_count"])
        self.assertGreaterEqual(input_group["config_count"], 1)
        self.assertEqual(1, input_group["output_count"])
        self.assertEqual("scan_kept_baseline", input_group["runs"][0]["run_id"])
        self.assertEqual("scan_kept_baseline", input_group["outputs"][0]["run_id"])

        unbound_group = next(group for group in groups if group["is_unbound"])
        self.assertEqual("Unbound / Legacy", unbound_group["title"])
        self.assertEqual("scan_legacy_only", unbound_group["runs"][0]["run_id"])
        self.assertEqual("scan_legacy_only", unbound_group["outputs"][0]["run_id"])

    def test_create_run_uses_grouped_output_dir_layout(self) -> None:
        class PassiveThread:
            def __init__(self, *, target, args, name, daemon) -> None:
                self._alive = False

            def start(self) -> None:
                self._alive = False

            def is_alive(self) -> bool:
                return self._alive

        with mock.patch("football_tracking.api.service.threading.Thread", PassiveThread):
            created_run = self.service.create_run(
                {
                    "config_name": "default.yaml",
                    "input_video": str((self.repo_root / "data" / "input.mp4").resolve()),
                    "output_dir_name": "baseline_probe_run",
                }
            )

        self.assertEqual(
            (self.repo_root / "outputs" / "runs" / "input" / "baseline_probe_run").resolve().as_posix(),
            Path(created_run["output_dir"]).resolve().as_posix(),
        )
        self.assertTrue(Path(created_run["output_dir"]).exists())

    def test_create_run_thread_start_failure_cleans_reservation_registry_and_output(self) -> None:
        class FailingStartThread:
            def __init__(self, *, target, args, name, daemon) -> None:
                pass

            def start(self) -> None:
                raise RuntimeError("thread start failed")

            def is_alive(self) -> bool:
                return False

        with mock.patch("football_tracking.api.service.threading.Thread", FailingStartThread):
            with self.assertRaisesRegex(RuntimeError, "thread start failed"):
                self.service.create_run(
                    {
                        "config_name": "default.yaml",
                        "input_video": str((self.repo_root / "data" / "input.mp4").resolve()),
                        "output_dir_name": "baseline_start_failed",
                    }
                )

        self.assertFalse((self.repo_root / "outputs" / "runs" / "input" / "baseline_start_failed").exists())
        self.assertEqual([], [run for run in self.service.list_runs() if run["run_id"] == "baseline_start_failed"])
        self.assertNotIn("baseline_start_failed", self.service._active_threads)
        self.assertNotIn("baseline_start_failed", self.service._cancel_events)

    def test_normal_create_run_still_requires_config_name(self) -> None:
        with self.assertRaises(ValidationError):
            CreateRunRequest(output_dir_name="missing_config")

        with self.assertRaisesRegex(ValueError, "config_name"):
            self.service.create_run({"output_dir_name": "missing_config"})

    def test_create_run_request_strips_approved_child_fields(self) -> None:
        request = CreateRunRequest(
            parent_run_id=" parent_run ",
            approved_action_ids=[" approval_001 ", " ", ""],
            approved_actions_artifact_name=" approvals.json ",
        )

        self.assertEqual("parent_run", request.parent_run_id)
        self.assertEqual(["approval_001"], request.approved_action_ids)
        self.assertEqual("approvals.json", request.approved_actions_artifact_name)

        with self.assertRaises(ValidationError):
            CreateRunRequest(parent_run_id=" ", approved_actions_artifact_name=" ")

    def test_approved_child_run_executes_selected_id_without_mutating_parent(self) -> None:
        parent_output_dir = self.create_output_bundle("kept_baseline")
        self.write_json(
            "outputs/kept_baseline/ai_improvement_approved_actions.json",
            {
                "schema_version": "1.0",
                "source_report": "ai_improvement_report.json",
                "metadata": {"kept": True},
                "approved_actions": [
                    {
                        "approval_id": "approval_keep",
                        "improvement_id": "imp_keep",
                        "approved_action": "targeted_rerun",
                        "approval_source": "api",
                        "approved_at": "2026-06-22T00:00:00+00:00",
                        "approved_by": "operator-a",
                        "rerun_scope": {"start_frame": 4, "end_frame": 6},
                    },
                    {
                        "approval_id": "approval_skip",
                        "improvement_id": "imp_skip",
                        "approved_action": "targeted_rerun",
                        "approval_source": "api",
                        "approved_at": "2026-06-22T00:00:00+00:00",
                        "approved_by": "operator-a",
                        "rerun_scope": {"start_frame": 20, "end_frame": 30},
                    },
                ],
            },
        )
        parent_run = self.service.list_runs()[0]
        watched_paths = [
            parent_output_dir / "ball_track.csv",
            parent_output_dir / "ball_track.cleaned.csv",
            parent_output_dir / "follow_cam.mp4",
            Path(parent_run["config_path"]),
        ]
        before = {path: self.file_fingerprint(path) for path in watched_paths}

        class ImmediateThread:
            def __init__(self, *, target, args, name, daemon) -> None:
                self._target = target
                self._args = args
                self._alive = False

            def start(self) -> None:
                self._alive = True
                try:
                    self._target(*self._args)
                finally:
                    self._alive = False

            def is_alive(self) -> bool:
                return self._alive

        with (
            mock.patch("football_tracking.api.service.threading.Thread", ImmediateThread),
            mock.patch(
                "football_tracking.api.service.run_high_recall_windows",
                return_value={
                    "windows": [{"start_frame": 4, "end_frame": 6}],
                    "execution": {"status": "succeeded"},
                },
            ) as rerun,
        ):
            created = self.service.create_run(
                {
                    "parent_run_id": parent_run["run_id"],
                    "approved_action_ids": ["approval_keep"],
                    "output_dir_name": "approved_child_run",
                }
            )

        child = self.service.get_run(created["run_id"])
        child_output_dir = Path(child["output_dir"])
        selected_artifact = json.loads((child_output_dir / "ai_improvement_approved_actions.json").read_text(encoding="utf-8"))
        child_config = yaml.safe_load((child_output_dir / "approved_targeted_rerun_config.yaml").read_text(encoding="utf-8"))
        runner_config = rerun.call_args.args[0]

        self.assertEqual("completed", child["status"])
        self.assertEqual(parent_run["run_id"], child["parent_run_id"])
        self.assertEqual(["approval_keep"], [item["approval_id"] for item in selected_artifact["approved_actions"]])
        self.assertEqual({"kept": True}, selected_artifact["metadata"])
        self.assertTrue((child_output_dir / "ball_track.csv").exists())
        self.assertTrue((child_output_dir / "ball_track.cleaned.csv").exists())
        self.assertEqual(
            (child_output_dir / "approved_targeted_rerun_config.yaml").resolve(),
            Path(child["config_path"]).resolve(),
        )
        self.assertTrue(child_config["high_recall_windows"]["approved_only"])
        self.assertEqual(0, child_config["high_recall_windows"]["margin_frames"])
        self.assertEqual(0, child_config["high_recall_windows"]["merge_gap_frames"])
        self.assertFalse(child_config["postprocess"]["enabled"])
        self.assertFalse(child_config["follow_cam"]["enabled"])
        self.assertFalse(child_config["temporal_chunks"]["enabled"])
        self.assertTrue(runner_config.high_recall_windows.enabled)
        self.assertTrue(runner_config.high_recall_windows.approved_only)
        self.assertEqual(3, runner_config.high_recall_windows.max_total_frames)
        self.assertFalse(runner_config.postprocess.enabled)
        self.assertFalse(runner_config.follow_cam.enabled)
        self.assertFalse(runner_config.temporal_chunks.enabled)
        self.assertEqual(before, {path: self.file_fingerprint(path) for path in watched_paths})

    def test_approved_child_artifact_only_custom_artifact_uses_all_actions_and_preserves_metadata(self) -> None:
        self.create_output_bundle("kept_baseline")
        custom_artifact = self.write_json(
            "outputs/kept_baseline/approvals/custom_actions.json",
            {
                "schema_version": "1.0",
                "source_report": "custom_report.json",
                "metadata": {"mode": "artifact-only"},
                "approved_actions": [
                    {
                        "approval_id": "approval_001",
                        "improvement_id": "imp_001",
                        "approved_action": "targeted_rerun",
                        "approval_source": "api",
                        "approved_at": "2026-06-22T00:00:00+00:00",
                        "approved_by": "operator-a",
                        "rerun_scope": {"start_frame": 1, "end_frame": 1},
                    },
                    {
                        "approval_id": "approval_manual",
                        "improvement_id": "imp_manual",
                        "approved_action": "manual_review",
                        "approval_source": "api",
                        "approved_at": "2026-06-22T00:00:00+00:00",
                        "approved_by": "operator-a",
                    },
                ],
            },
        )
        parent_run = self.service.list_runs()[0]

        class PassiveThread:
            def __init__(self, *, target, args, name, daemon) -> None:
                self._alive = False

            def start(self) -> None:
                self._alive = False

            def is_alive(self) -> bool:
                return self._alive

        with mock.patch("football_tracking.api.service.threading.Thread", PassiveThread):
            created = self.service.create_run(
                {
                    "parent_run_id": parent_run["run_id"],
                    "approved_actions_artifact_name": "approvals/custom_actions.json",
                    "output_dir_name": "artifact_only_child",
                }
            )

        child_artifact_path = Path(created["output_dir"]) / "ai_improvement_approved_actions.json"
        child_artifact = json.loads(child_artifact_path.read_text(encoding="utf-8"))

        self.assertEqual("queued", created["status"])
        self.assertEqual(["approval_001", "approval_manual"], [item["approval_id"] for item in child_artifact["approved_actions"]])
        self.assertEqual({"mode": "artifact-only"}, child_artifact["metadata"])
        self.assertEqual("custom_report.json", child_artifact["source_report"])
        self.assertEqual(str(custom_artifact), child_artifact["source_approved_actions_path"])

    def test_approved_child_rejects_frame_budget_over_config_limit_before_output(self) -> None:
        self.create_output_bundle("kept_baseline")
        self.write_json(
            "outputs/kept_baseline/ai_improvement_approved_actions.json",
            {
                "schema_version": "1.0",
                "approved_actions": [
                    {
                        "approval_id": "approval_huge",
                        "improvement_id": "imp_huge",
                        "approved_action": "targeted_rerun",
                        "rerun_scope": {"start_frame": 0, "end_frame": 1800},
                    }
                ],
            },
        )
        parent_run = self.service.list_runs()[0]

        with self.assertRaisesRegex(ValueError, "frame budget"):
            self.service.create_run(
                {
                    "parent_run_id": parent_run["run_id"],
                    "approved_action_ids": ["approval_huge"],
                    "output_dir_name": "huge_budget_child",
                }
            )

        self.assertFalse((self.repo_root / "outputs" / "runs" / "input" / "huge_budget_child").exists())

    def test_approved_child_rejects_fractional_frame_before_output_dir_creation(self) -> None:
        self.create_output_bundle("kept_baseline")
        self.write_json(
            "outputs/kept_baseline/ai_improvement_approved_actions.json",
            {
                "schema_version": "1.0",
                "approved_actions": [
                    {
                        "approval_id": "approval_bad",
                        "improvement_id": "imp_bad",
                        "approved_action": "targeted_rerun",
                        "rerun_scope": {"start_frame": 1.5, "end_frame": 4},
                    }
                ],
            },
        )
        parent_run = self.service.list_runs()[0]

        with self.assertRaisesRegex(ValueError, "integer start_frame"):
            self.service.create_run(
                {
                    "parent_run_id": parent_run["run_id"],
                    "approved_action_ids": ["approval_bad"],
                    "output_dir_name": "bad_fractional_child",
                }
            )

        self.assertFalse((self.repo_root / "outputs" / "runs" / "input" / "bad_fractional_child").exists())

    def test_approved_child_rejects_unsafe_output_dir_name_before_output_dir_creation(self) -> None:
        self.create_output_bundle("kept_baseline")
        self.write_json(
            "outputs/kept_baseline/ai_improvement_approved_actions.json",
            {
                "schema_version": "1.0",
                "approved_actions": [
                    {
                        "approval_id": "approval_001",
                        "improvement_id": "imp_001",
                        "approved_action": "targeted_rerun",
                        "rerun_scope": {"start_frame": 1, "end_frame": 2},
                    }
                ],
            },
        )
        parent_run = self.service.list_runs()[0]

        with self.assertRaisesRegex(ValueError, "output_dir_name"):
            self.service.create_run(
                {
                    "parent_run_id": parent_run["run_id"],
                    "approved_action_ids": ["approval_001"],
                    "output_dir_name": "..",
                }
            )

        self.assertFalse((self.repo_root / "outputs" / "runs" / "input").exists())

    def test_approved_child_rejects_active_run_before_output_dir_creation(self) -> None:
        self.create_output_bundle("kept_baseline")
        self.write_json(
            "outputs/kept_baseline/ai_improvement_approved_actions.json",
            {
                "schema_version": "1.0",
                "approved_actions": [
                    {
                        "approval_id": "approval_001",
                        "improvement_id": "imp_001",
                        "approved_action": "targeted_rerun",
                        "rerun_scope": {"start_frame": 1, "end_frame": 2},
                    }
                ],
            },
        )
        parent_run = self.service.list_runs()[0]

        class ReservedThread:
            def is_alive(self) -> bool:
                return False

        self.service._active_threads["other_run"] = ReservedThread()  # type: ignore[assignment]
        self.service._cancel_events["other_run"] = threading.Event()

        with self.assertRaisesRegex(RuntimeError, "Another run is already active"):
            self.service.create_run(
                {
                    "parent_run_id": parent_run["run_id"],
                    "approved_action_ids": ["approval_001"],
                    "output_dir_name": "blocked_active_child",
                }
            )

        self.assertFalse((self.repo_root / "outputs" / "runs" / "input" / "blocked_active_child").exists())

    def test_approved_child_existing_empty_output_dir_is_not_deleted(self) -> None:
        self.create_output_bundle("kept_baseline")
        self.write_json(
            "outputs/kept_baseline/ai_improvement_approved_actions.json",
            {
                "schema_version": "1.0",
                "approved_actions": [
                    {
                        "approval_id": "approval_001",
                        "improvement_id": "imp_001",
                        "approved_action": "targeted_rerun",
                        "rerun_scope": {"start_frame": 1, "end_frame": 2},
                    }
                ],
            },
        )
        parent_run = self.service.list_runs()[0]
        existing_dir = self.repo_root / "outputs" / "runs" / "input" / "existing_empty_child"
        existing_dir.mkdir(parents=True)

        with self.assertRaises(FileExistsError):
            self.service.create_run(
                {
                    "parent_run_id": parent_run["run_id"],
                    "approved_action_ids": ["approval_001"],
                    "output_dir_name": "existing_empty_child",
                }
            )

        self.assertTrue(existing_dir.exists())
        self.assertEqual([], list(existing_dir.iterdir()))

    def test_approved_child_no_executable_runner_windows_marks_failed(self) -> None:
        self.create_output_bundle("kept_baseline")
        self.write_json(
            "outputs/kept_baseline/ai_improvement_approved_actions.json",
            {
                "schema_version": "1.0",
                "approved_actions": [
                    {
                        "approval_id": "approval_001",
                        "improvement_id": "imp_001",
                        "approved_action": "targeted_rerun",
                        "rerun_scope": {"start_frame": 1, "end_frame": 2},
                    }
                ],
            },
        )
        parent_run = self.service.list_runs()[0]

        class ImmediateThread:
            def __init__(self, *, target, args, name, daemon) -> None:
                self._target = target
                self._args = args

            def start(self) -> None:
                self._target(*self._args)

            def is_alive(self) -> bool:
                return False

        with (
            mock.patch("football_tracking.api.service.threading.Thread", ImmediateThread),
            mock.patch(
                "football_tracking.api.service.run_high_recall_windows",
                return_value={"windows": [], "execution": {"status": "skipped"}},
            ),
        ):
            created = self.service.create_run(
                {
                    "parent_run_id": parent_run["run_id"],
                    "approved_action_ids": ["approval_001"],
                    "output_dir_name": "no_windows_child",
                }
            )

        child = self.service.get_run(created["run_id"])
        self.assertEqual("failed", child["status"])
        self.assertIn("no executable windows", child["error"])

    def test_approved_child_failed_runner_still_reports_parent_mutation(self) -> None:
        parent_output = self.create_output_bundle("kept_baseline")
        self.write_json(
            "outputs/kept_baseline/ai_improvement_approved_actions.json",
            {
                "schema_version": "1.0",
                "approved_actions": [
                    {
                        "approval_id": "approval_001",
                        "improvement_id": "imp_001",
                        "approved_action": "targeted_rerun",
                        "rerun_scope": {"start_frame": 1, "end_frame": 2},
                    }
                ],
            },
        )
        parent_run = self.service.list_runs()[0]

        class ImmediateThread:
            def __init__(self, *, target, args, name, daemon) -> None:
                self._target = target
                self._args = args

            def start(self) -> None:
                self._target(*self._args)

            def is_alive(self) -> bool:
                return False

        def mutate_parent_and_fail(*args, **kwargs):
            (parent_output / "follow_cam.mp4").write_text("mutated", encoding="utf-8")
            raise RuntimeError("runner failed")

        with (
            mock.patch("football_tracking.api.service.threading.Thread", ImmediateThread),
            mock.patch("football_tracking.api.service.run_high_recall_windows", side_effect=mutate_parent_and_fail),
        ):
            created = self.service.create_run(
                {
                    "parent_run_id": parent_run["run_id"],
                    "approved_action_ids": ["approval_001"],
                    "output_dir_name": "mutated_parent_child",
                }
            )

        child = self.service.get_run(created["run_id"])
        self.assertEqual("failed", child["status"])
        self.assertIn("runner failed", child["error"])
        self.assertIn("Parent run artifact changed", child["error"])

    def test_approved_child_detects_parent_artifact_created_during_run(self) -> None:
        parent_output = self.create_output_bundle("kept_baseline")
        self.assertFalse((parent_output / "highlight.mp4").exists())
        self.write_json(
            "outputs/kept_baseline/ai_improvement_approved_actions.json",
            {
                "schema_version": "1.0",
                "approved_actions": [
                    {
                        "approval_id": "approval_001",
                        "improvement_id": "imp_001",
                        "approved_action": "targeted_rerun",
                        "rerun_scope": {"start_frame": 1, "end_frame": 2},
                    }
                ],
            },
        )
        parent_run = self.service.list_runs()[0]

        class ImmediateThread:
            def __init__(self, *, target, args, name, daemon) -> None:
                self._target = target
                self._args = args

            def start(self) -> None:
                self._target(*self._args)

            def is_alive(self) -> bool:
                return False

        def create_parent_highlight(*args, **kwargs):
            (parent_output / "highlight.mp4").write_text("new parent highlight", encoding="utf-8")
            return {"windows": [{"start_frame": 1, "end_frame": 2}], "execution": {"status": "succeeded"}}

        with (
            mock.patch("football_tracking.api.service.threading.Thread", ImmediateThread),
            mock.patch("football_tracking.api.service.run_high_recall_windows", side_effect=create_parent_highlight),
        ):
            created = self.service.create_run(
                {
                    "parent_run_id": parent_run["run_id"],
                    "approved_action_ids": ["approval_001"],
                    "output_dir_name": "created_parent_artifact_child",
                }
            )

        child = self.service.get_run(created["run_id"])
        self.assertEqual("failed", child["status"])
        self.assertIn("highlight.mp4", child["error"])

    def test_approved_child_thread_start_failure_cleans_registry_and_output(self) -> None:
        self.create_output_bundle("kept_baseline")
        self.write_json(
            "outputs/kept_baseline/ai_improvement_approved_actions.json",
            {
                "schema_version": "1.0",
                "approved_actions": [
                    {
                        "approval_id": "approval_001",
                        "improvement_id": "imp_001",
                        "approved_action": "targeted_rerun",
                        "rerun_scope": {"start_frame": 1, "end_frame": 2},
                    }
                ],
            },
        )
        parent_run = self.service.list_runs()[0]

        class FailingStartThread:
            def __init__(self, *, target, args, name, daemon) -> None:
                pass

            def start(self) -> None:
                raise RuntimeError("thread start failed")

            def is_alive(self) -> bool:
                return False

        with mock.patch("football_tracking.api.service.threading.Thread", FailingStartThread):
            with self.assertRaisesRegex(RuntimeError, "thread start failed"):
                self.service.create_run(
                    {
                        "parent_run_id": parent_run["run_id"],
                        "approved_action_ids": ["approval_001"],
                        "output_dir_name": "thread_start_failed_child",
                    }
                )

        self.assertFalse((self.repo_root / "outputs" / "runs" / "input" / "thread_start_failed_child").exists())
        self.assertEqual([], [run for run in self.service.list_runs() if run["run_id"] == "thread_start_failed_child"])
        self.assertNotIn("thread_start_failed_child", self.service._active_threads)
        self.assertNotIn("thread_start_failed_child", self.service._cancel_events)

    def test_create_follow_cam_render_creates_standalone_deliverable_task(self) -> None:
        self.create_output_bundle("kept_baseline")
        source_run = self.service.list_runs()[0]

        class ImmediateThread:
            def __init__(self, *, target, args, name, daemon) -> None:
                self._target = target
                self._args = args
                self._alive = False

            def start(self) -> None:
                self._alive = True
                try:
                    self._target(*self._args)
                finally:
                    self._alive = False

            def is_alive(self) -> bool:
                return self._alive

        class FakeFollowCamGenerator:
            def __init__(self, app_config) -> None:
                self.app_config = app_config

            def run(self) -> None:
                output_dir = self.app_config.output_dir
                (output_dir / self.app_config.follow_cam.output_video_name).write_text("deliverable", encoding="utf-8")
                (output_dir / self.app_config.follow_cam.camera_path_name).write_text(
                    "Frame,CenterX,CenterY\n0,100,200\n",
                    encoding="utf-8",
                )
                report = {
                    "track_source": "cleaned",
                    "target_resolution": [
                        self.app_config.follow_cam.target_width,
                        self.app_config.follow_cam.target_height,
                    ],
                    "mean_crop_height": 980.0,
                    "status_counts": {"Detected": 2, "Lost": 1},
                }
                (output_dir / self.app_config.follow_cam.report_name).write_text(
                    json.dumps(report, ensure_ascii=False),
                    encoding="utf-8",
                )

        with mock.patch("football_tracking.api.service.threading.Thread", ImmediateThread), mock.patch(
            "football_tracking.api.service.FollowCamGenerator", FakeFollowCamGenerator
        ):
            created_run = self.service.create_follow_cam_render(source_run["run_id"], {})

        completed_run = self.service.get_run(created_run["run_id"])

        self.assertEqual("follow_cam_render", completed_run["source"])
        self.assertEqual(source_run["run_id"], completed_run["parent_run_id"])
        self.assertEqual("completed", completed_run["status"])
        self.assertFalse(completed_run["modules_enabled"]["postprocess"])
        self.assertTrue(completed_run["modules_enabled"]["follow_cam"])
        self.assertEqual([1920, 1080], completed_run["stats"]["follow_cam"]["target_resolution"])
        self.assertIn("deliverable_16x9.mp4", {artifact["name"] for artifact in completed_run["artifacts"]})
        self.assertTrue((Path(completed_run["output_dir"]) / "ball_track.cleaned.csv").exists())
        self.assertTrue((Path(completed_run["output_dir"]) / "run_manifest.json").exists())
        self.assertTrue((Path(completed_run["output_dir"]) / "metrics_report.json").exists())
        self.assertIn("/outputs/runs/input/", Path(completed_run["output_dir"]).resolve().as_posix())

    def test_create_follow_cam_render_rejects_tracking_rerun_required_plan(self) -> None:
        self.create_output_bundle("kept_baseline")
        self.write_json(
            "outputs/kept_baseline/follow_cam_rerender_plan.json",
            {
                "schema_version": "1.0",
                "source": "ai_improvement_approved_action",
                "approval_id": "approval_001",
                "improvement_id": "imp_camera_track",
                "approved_action": "tracking_rerun_before_follow_cam",
                "requires_tracking_rerun": True,
                "tracking_rerun_scope": {"start_frame": 28, "end_frame": 54},
                "reason": "Track rerun is required before follow-cam rerender.",
            },
        )
        source_run = self.service.list_runs()[0]

        with self.assertRaisesRegex(RuntimeError, "requires tracking rerun"):
            self.service.create_follow_cam_render(
                source_run["run_id"],
                {"output_dir_name": "blocked_follow_cam_only"},
            )

        blocked_dir = self.repo_root / "outputs" / "runs" / "input" / "blocked_follow_cam_only"
        self.assertFalse(blocked_dir.exists())

    def test_create_follow_cam_render_rejects_corrupt_rerender_plan(self) -> None:
        self.create_output_bundle("kept_baseline")
        output_dir = self.repo_root / "outputs" / "kept_baseline"
        (output_dir / "follow_cam_rerender_plan.json").write_text("{", encoding="utf-8")
        source_run = self.service.list_runs()[0]

        with self.assertRaisesRegex(RuntimeError, "follow_cam_rerender_plan.json is corrupt"):
            self.service.create_follow_cam_render(
                source_run["run_id"],
                {"output_dir_name": "blocked_follow_cam_corrupt_plan"},
            )

        blocked_dir = self.repo_root / "outputs" / "runs" / "input" / "blocked_follow_cam_corrupt_plan"
        self.assertFalse(blocked_dir.exists())

    def test_create_highlight_render_creates_child_task_from_event_candidate(self) -> None:
        self.create_output_bundle("kept_baseline")
        source_run = self.service.list_runs()[0]

        class ImmediateThread:
            def __init__(self, *, target, args, name, daemon) -> None:
                self._target = target
                self._args = args
                self._alive = False

            def start(self) -> None:
                self._alive = True
                try:
                    self._target(*self._args)
                finally:
                    self._alive = False

            def is_alive(self) -> bool:
                return self._alive

        calls: list[dict[str, object]] = []

        def fake_render_highlight_clip(
            *,
            input_video: Path,
            output_path: Path,
            start_frame: int,
            end_frame: int,
            progress_callback=None,
            should_cancel=None,
        ) -> dict[str, object]:
            calls.append(
                {
                    "input_video": input_video,
                    "output_path": output_path,
                    "start_frame": start_frame,
                    "end_frame": end_frame,
                    "should_cancel": should_cancel is not None,
                }
            )
            if progress_callback is not None:
                progress_callback({"stage": "render", "current_frame": 0, "total_frames": end_frame - start_frame + 1})
            output_path.write_text("highlight", encoding="utf-8")
            return {"frame_count": end_frame - start_frame + 1, "fps": 6.0}

        with mock.patch("football_tracking.api.service.threading.Thread", ImmediateThread), mock.patch(
            "football_tracking.api.service.render_highlight_clip",
            fake_render_highlight_clip,
        ):
            created_run = self.service.create_highlight_render(
                source_run["run_id"],
                {
                    "candidate_id": "cleaned:shot_candidate:0-1",
                    "output_dir_name": "candidate_highlight_run",
                },
            )

        completed_run = self.service.get_run(created_run["run_id"])
        output_dir = Path(completed_run["output_dir"])
        report = json.loads((output_dir / "highlight_report.json").read_text(encoding="utf-8"))

        self.assertEqual("highlight_render", completed_run["source"])
        self.assertEqual(source_run["run_id"], completed_run["parent_run_id"])
        self.assertEqual("completed", completed_run["status"])
        self.assertFalse(completed_run["modules_enabled"]["postprocess"])
        self.assertFalse(completed_run["modules_enabled"]["follow_cam"])
        self.assertEqual(0, calls[0]["start_frame"])
        self.assertEqual(7, calls[0]["end_frame"])
        self.assertEqual("highlight.mp4", Path(calls[0]["output_path"]).name)
        self.assertEqual("cleaned:shot_candidate:0-1", report["candidate_id"])
        self.assertEqual({"start_frame": 0, "end_frame": 7}, report["window"])
        self.assertEqual({"frame_count": 8, "fps": 6.0}, report["renderer"])
        self.assertEqual("candidate_render_window", report["selection_source"])
        self.assertIn("highlight.mp4", {artifact["name"] for artifact in completed_run["artifacts"]})
        self.assertIn("highlight_report.json", {artifact["name"] for artifact in completed_run["artifacts"]})
        self.assertTrue((output_dir / "ball_track.cleaned.csv").exists())
        self.assertTrue((output_dir / "event_candidates.json").exists())
        self.assertTrue((output_dir / "run_manifest.json").exists())
        self.assertTrue((output_dir / "metrics_report.json").exists())

    def test_create_highlight_render_allows_manual_roll_override_for_candidate(self) -> None:
        self.create_output_bundle("kept_baseline")
        source_run = self.service.list_runs()[0]

        class ImmediateThread:
            def __init__(self, *, target, args, name, daemon) -> None:
                self._target = target
                self._args = args
                self._alive = False

            def start(self) -> None:
                self._alive = True
                try:
                    self._target(*self._args)
                finally:
                    self._alive = False

            def is_alive(self) -> bool:
                return self._alive

        calls: list[dict[str, object]] = []

        def fake_render_highlight_clip(
            *,
            input_video: Path,
            output_path: Path,
            start_frame: int,
            end_frame: int,
            progress_callback=None,
            should_cancel=None,
        ) -> dict[str, object]:
            calls.append({"start_frame": start_frame, "end_frame": end_frame})
            output_path.write_text("highlight", encoding="utf-8")
            return {"frame_count": end_frame - start_frame + 1, "fps": 6.0}

        with mock.patch("football_tracking.api.service.threading.Thread", ImmediateThread), mock.patch(
            "football_tracking.api.service.render_highlight_clip",
            fake_render_highlight_clip,
        ):
            created_run = self.service.create_highlight_render(
                source_run["run_id"],
                {
                    "candidate_id": "cleaned:shot_candidate:0-1",
                    "pre_roll_frames": 2,
                    "post_roll_frames": 3,
                    "output_dir_name": "manual_roll_highlight_run",
                },
            )

        completed_run = self.service.get_run(created_run["run_id"])
        report = json.loads((Path(completed_run["output_dir"]) / "highlight_report.json").read_text(encoding="utf-8"))

        self.assertEqual(0, calls[0]["start_frame"])
        self.assertEqual(4, calls[0]["end_frame"])
        self.assertEqual({"start_frame": 0, "end_frame": 4}, report["window"])
        self.assertEqual("manual_candidate_roll", report["selection_source"])
        self.assertTrue(any("minimum post-event tail" in warning for warning in report["warnings"]))

    def test_create_highlight_render_can_use_explicit_approved_ai_window(self) -> None:
        source_output = self.create_output_bundle("kept_baseline")
        self.write_json(
            "outputs/kept_baseline/ai_improvement_approved_actions.json",
            {
                "schema_version": "1.0",
                "approved_actions": [
                    {
                        "approval_id": "approval_highlight_001",
                        "improvement_id": "imp_highlight",
                        "approved_action": "render_suggested_highlight",
                        "approval_source": "api",
                        "approved_at": "2026-06-22T00:00:00+00:00",
                        "approved_by": "operator-a",
                        "candidate_id": "cleaned:shot_candidate:0-1",
                        "clip_action": "extend_tail",
                        "suggested_window": {"start_frame": 0, "end_frame": 30},
                        "provenance": {"source": "ai_improvement", "model": "gpt-improve"},
                    }
                ],
            },
        )
        source_run = self.service.list_runs()[0]

        class ImmediateThread:
            def __init__(self, *, target, args, name, daemon) -> None:
                self._target = target
                self._args = args
                self._alive = False

            def start(self) -> None:
                self._alive = True
                try:
                    self._target(*self._args)
                finally:
                    self._alive = False

            def is_alive(self) -> bool:
                return self._alive

        calls: list[dict[str, object]] = []

        def fake_render_highlight_clip(
            *,
            input_video: Path,
            output_path: Path,
            start_frame: int,
            end_frame: int,
            progress_callback=None,
            should_cancel=None,
        ) -> dict[str, object]:
            calls.append({"start_frame": start_frame, "end_frame": end_frame})
            output_path.write_text("highlight", encoding="utf-8")
            return {"frame_count": end_frame - start_frame + 1, "fps": 6.0}

        with mock.patch("football_tracking.api.service.threading.Thread", ImmediateThread), mock.patch(
            "football_tracking.api.service.render_highlight_clip",
            fake_render_highlight_clip,
        ):
            created_run = self.service.create_highlight_render(
                source_run["run_id"],
                {
                    "approved_action_id": "approval_highlight_001",
                    "output_dir_name": "approved_ai_highlight_run",
                },
            )

        completed_run = self.service.get_run(created_run["run_id"])
        report = json.loads((Path(completed_run["output_dir"]) / "highlight_report.json").read_text(encoding="utf-8"))

        self.assertTrue(source_output.exists())
        self.assertEqual({"start_frame": 0, "end_frame": 30}, report["window"])
        self.assertEqual(30, calls[0]["end_frame"])
        self.assertEqual("approved_ai_suggested_window", report["selection_source"])
        self.assertEqual("approval_highlight_001", report["approval"]["approval_id"])
        self.assertEqual("imp_highlight", report["approval"]["improvement_id"])
        self.assertEqual("ai_improvement_approved_actions.json", report["approval"]["source_approved_actions"])

    def test_create_highlight_render_rejects_approved_ai_window_that_excludes_core(self) -> None:
        self.create_output_bundle("kept_baseline")
        self.write_json(
            "outputs/kept_baseline/ai_improvement_approved_actions.json",
            {
                "schema_version": "1.0",
                "approved_actions": [
                    {
                        "approval_id": "approval_highlight_core",
                        "improvement_id": "imp_highlight",
                        "approved_action": "render_suggested_highlight",
                        "candidate_id": "cleaned:shot_candidate:0-1",
                        "suggested_window": {"start_frame": 1, "end_frame": 30},
                    }
                ],
            },
        )
        source_run = self.service.list_runs()[0]

        with self.assertRaisesRegex(RuntimeError, "core_window"):
            self.service.create_highlight_render(
                source_run["run_id"],
                {
                    "approved_action_id": "approval_highlight_core",
                    "output_dir_name": "bad_approved_core_highlight",
                },
            )

        self.assertFalse((self.repo_root / "outputs" / "runs" / "input" / "bad_approved_core_highlight").exists())

    def test_create_highlight_render_rejects_approved_ai_window_that_trims_tail(self) -> None:
        self.create_output_bundle("kept_baseline")
        self.write_json(
            "outputs/kept_baseline/ai_improvement_approved_actions.json",
            {
                "schema_version": "1.0",
                "approved_actions": [
                    {
                        "approval_id": "approval_highlight_tail",
                        "improvement_id": "imp_highlight",
                        "approved_action": "adjust_highlight_window",
                        "candidate_id": "cleaned:shot_candidate:0-1",
                        "suggested_window": {"start_frame": 0, "end_frame": 4},
                    }
                ],
            },
        )
        source_run = self.service.list_runs()[0]

        with self.assertRaisesRegex(RuntimeError, "minimum post-event tail"):
            self.service.create_highlight_render(
                source_run["run_id"],
                {
                    "approved_action_id": "approval_highlight_tail",
                    "output_dir_name": "bad_approved_tail_highlight",
                },
            )

        self.assertFalse((self.repo_root / "outputs" / "runs" / "input" / "bad_approved_tail_highlight").exists())

    def test_create_highlight_render_uses_post_buffer_frames_tail_fallback_for_approved_ai_window(self) -> None:
        output_dir = self.create_output_bundle("kept_baseline")
        event_candidates_path = output_dir / "event_candidates.json"
        event_candidates = json.loads(event_candidates_path.read_text(encoding="utf-8"))
        policy = event_candidates["candidates"][0]["buffer_policy"]
        policy.pop("min_tail_frames", None)
        policy.pop("min_post_event_frames", None)
        event_candidates_path.write_text(json.dumps(event_candidates, ensure_ascii=False, indent=2), encoding="utf-8")
        self.write_json(
            "outputs/kept_baseline/ai_improvement_approved_actions.json",
            {
                "schema_version": "1.0",
                "approved_actions": [
                    {
                        "approval_id": "approval_highlight_post_buffer",
                        "improvement_id": "imp_highlight",
                        "approved_action": "render_suggested_highlight",
                        "candidate_id": "cleaned:shot_candidate:0-1",
                        "suggested_window": {"start_frame": 0, "end_frame": 4},
                    }
                ],
            },
        )
        source_run = self.service.list_runs()[0]

        with self.assertRaisesRegex(RuntimeError, "minimum post-event tail"):
            self.service.create_highlight_render(
                source_run["run_id"],
                {
                    "approved_action_id": "approval_highlight_post_buffer",
                    "output_dir_name": "bad_approved_post_buffer_highlight",
                },
            )

        self.assertFalse((self.repo_root / "outputs" / "runs" / "input" / "bad_approved_post_buffer_highlight").exists())

    def test_create_highlight_render_creates_child_task_from_frame_window(self) -> None:
        self.create_output_bundle("kept_baseline")
        source_run = self.service.list_runs()[0]

        class ImmediateThread:
            def __init__(self, *, target, args, name, daemon) -> None:
                self._target = target
                self._args = args
                self._alive = False

            def start(self) -> None:
                self._alive = True
                try:
                    self._target(*self._args)
                finally:
                    self._alive = False

            def is_alive(self) -> bool:
                return self._alive

        calls: list[dict[str, object]] = []

        def fake_render_highlight_clip(
            *,
            input_video: Path,
            output_path: Path,
            start_frame: int,
            end_frame: int,
            progress_callback=None,
            should_cancel=None,
        ) -> dict[str, object]:
            calls.append(
                {
                    "input_video": input_video,
                    "output_path": output_path,
                    "start_frame": start_frame,
                    "end_frame": end_frame,
                }
            )
            output_path.write_text("highlight", encoding="utf-8")
            return {"frame_count": end_frame - start_frame + 1, "fps": 6.0}

        with mock.patch("football_tracking.api.service.threading.Thread", ImmediateThread), mock.patch(
            "football_tracking.api.service.render_highlight_clip",
            fake_render_highlight_clip,
        ):
            created_run = self.service.create_highlight_render(
                source_run["run_id"],
                {
                    "start_frame": 2,
                    "end_frame": 4,
                    "output_dir_name": "manual_highlight_run",
                    "output_video_name": "manual_clip.mp4",
                },
            )

        completed_run = self.service.get_run(created_run["run_id"])
        report = json.loads((Path(completed_run["output_dir"]) / "highlight_report.json").read_text(encoding="utf-8"))

        self.assertEqual("completed", completed_run["status"])
        self.assertEqual(2, calls[0]["start_frame"])
        self.assertEqual(4, calls[0]["end_frame"])
        self.assertEqual("manual_clip.mp4", Path(calls[0]["output_path"]).name)
        self.assertIsNone(report["candidate_id"])
        self.assertEqual({"start_frame": 2, "end_frame": 4}, report["window"])

    def test_create_highlight_render_requires_completed_source_run_and_window(self) -> None:
        active_input = (self.repo_root / "data" / "input.mp4").resolve()
        active_config = (self.repo_root / "config" / "default.yaml").resolve()
        output_dir = self.repo_root / "outputs" / "active_demo"
        output_dir.mkdir(parents=True, exist_ok=True)
        self.service._write_registry(
            {
                "runs": [
                    {
                        "run_id": "active_demo",
                        "source": "api",
                        "status": "running",
                        "created_at": "2026-01-01T00:00:00+00:00",
                        "started_at": "2026-01-01T00:00:00+00:00",
                        "completed_at": None,
                        "config_name": "default.yaml",
                        "config_path": str(active_config),
                        "input_video": str(active_input),
                        "parent_run_id": None,
                        "output_dir": str(output_dir.resolve()),
                        "modules_enabled": {"postprocess": True, "follow_cam": False},
                        "artifacts": [],
                        "stats": {},
                        "progress": {"stage": "tracking", "percent": 42.0},
                        "notes": None,
                        "error": None,
                    }
                ]
            }
        )

        with self.assertRaises(RuntimeError):
            self.service.create_highlight_render("active_demo", {"start_frame": 0, "end_frame": 4})

        self.service._write_registry({"runs": []})
        self.create_output_bundle("kept_baseline")
        completed_run = self.service.list_runs()[0]

        with self.assertRaises(RuntimeError):
            self.service.create_highlight_render(completed_run["run_id"], {})
        with self.assertRaises(FileNotFoundError):
            self.service.create_highlight_render(completed_run["run_id"], {"candidate_id": "missing"})
        with self.assertRaises(RuntimeError):
            self.service.create_highlight_render(
                completed_run["run_id"],
                {"start_frame": 0, "end_frame": 4, "output_video_name": "metrics_report.json"},
            )
        with self.assertRaises(RuntimeError):
            self.service.create_highlight_render(
                completed_run["run_id"],
                {"start_frame": 0, "end_frame": 4, "output_video_name": "clips/highlight.mp4"},
            )

    def test_highlight_render_request_validates_selection_and_route_maps_missing_candidate(self) -> None:
        self.create_output_bundle("kept_baseline")
        source_run = self.service.list_runs()[0]

        with self.assertRaises(ValidationError):
            HighlightRenderRequest()
        with self.assertRaises(ValidationError):
            HighlightRenderRequest(start_frame=5, end_frame=4)
        with self.assertRaises(ValidationError):
            HighlightRenderRequest(candidate_id="cleaned:shot_candidate:0-1", start_frame=0, end_frame=4)
        with self.assertRaises(ValidationError):
            HighlightRenderRequest(candidate_id="cleaned:shot_candidate:0-1", approved_action_id="approval_001")

        with self.assertRaises(HTTPException) as raised:
            run_routes.create_highlight_render(
                source_run["run_id"],
                HighlightRenderRequest(candidate_id="missing"),
                self.service,
            )

        self.assertEqual(404, raised.exception.status_code)

    def test_delete_input_video_blocks_active_run_reference(self) -> None:
        active_input = (self.repo_root / "data" / "input.mp4").resolve()
        active_config = (self.repo_root / "config" / "default.yaml").resolve()
        self.service._write_registry(
            {
                "runs": [
                    {
                        "run_id": "active_demo",
                        "status": "running",
                        "input_video": str(active_input),
                        "config_path": str(active_config),
                    }
                ]
            }
        )

        with self.assertRaises(RuntimeError):
            self.service.delete_input_video("input.mp4")

    def test_cancel_run_requests_active_thread_stop(self) -> None:
        active_input = (self.repo_root / "data" / "input.mp4").resolve()
        active_config = (self.repo_root / "config" / "default.yaml").resolve()
        cancel_event = threading.Event()
        self.service._cancel_events["active_demo"] = cancel_event
        self.service._write_registry(
            {
                "runs": [
                    {
                        "run_id": "active_demo",
                        "source": "api",
                        "status": "running",
                        "created_at": "2026-01-01T00:00:00+00:00",
                        "started_at": "2026-01-01T00:00:00+00:00",
                        "completed_at": None,
                        "config_name": "default.yaml",
                        "config_path": str(active_config),
                        "input_video": str(active_input),
                        "parent_run_id": None,
                        "output_dir": str((self.repo_root / "outputs" / "active_demo").resolve()),
                        "modules_enabled": {"postprocess": True, "follow_cam": False},
                        "artifacts": [],
                        "stats": {},
                        "progress": {"stage": "tracking", "percent": 42.0},
                        "notes": None,
                        "error": None,
                    }
                ]
            }
        )

        updated = self.service.cancel_run("active_demo")

        self.assertTrue(cancel_event.is_set())
        self.assertEqual("running", updated["status"])
        self.assertEqual("cancelling", updated["progress"]["stage"])
        self.assertEqual(42.0, updated["progress"]["percent"])

    def test_cancel_run_without_active_thread_writes_artifacts(self) -> None:
        active_input = (self.repo_root / "data" / "input.mp4").resolve()
        active_config = (self.repo_root / "config" / "default.yaml").resolve()
        output_dir = self.repo_root / "outputs" / "queued_demo"
        output_dir.mkdir(parents=True, exist_ok=True)
        self.write_csv(
            "outputs/queued_demo/ball_track.csv",
            [
                {"Frame": 0, "X": 10, "Y": 20, "Confidence": "0.9000", "Status": "Detected"},
                {"Frame": 1, "X": "", "Y": "", "Confidence": "0.0000", "Status": "Lost"},
            ],
        )
        self.service._write_registry(
            {
                "runs": [
                    {
                        "run_id": "queued_demo",
                        "source": "api",
                        "status": "queued",
                        "created_at": "2026-01-01T00:00:00+00:00",
                        "started_at": None,
                        "completed_at": None,
                        "config_name": "default.yaml",
                        "config_path": str(active_config),
                        "input_video": str(active_input),
                        "parent_run_id": None,
                        "output_dir": str(output_dir.resolve()),
                        "modules_enabled": {"postprocess": True, "follow_cam": False},
                        "artifacts": [],
                        "stats": {},
                        "progress": {"stage": "queued", "percent": 0.0},
                        "notes": None,
                        "error": None,
                    }
                ]
            }
        )

        updated = self.service.cancel_run("queued_demo")

        self.assertEqual("cancelled", updated["status"])
        self.assertTrue((output_dir / "run_manifest.json").exists())
        self.assertTrue((output_dir / "metrics_report.json").exists())
        self.assertIn("metrics_report.json", {artifact["name"] for artifact in updated["artifacts"]})
        self.assertEqual(2, updated["stats"]["raw"]["frame_count"])

    def test_failed_pipeline_run_writes_artifacts(self) -> None:
        active_input = (self.repo_root / "data" / "input.mp4").resolve()
        active_config = (self.repo_root / "config" / "default.yaml").resolve()
        output_dir = self.repo_root / "outputs" / "failed_demo"
        self.service._write_registry(
            {
                "runs": [
                    {
                        "run_id": "failed_demo",
                        "source": "api",
                        "status": "queued",
                        "created_at": "2026-01-01T00:00:00+00:00",
                        "started_at": None,
                        "completed_at": None,
                        "config_name": "default.yaml",
                        "config_path": str(active_config),
                        "input_video": str(active_input),
                        "parent_run_id": None,
                        "output_dir": str(output_dir.resolve()),
                        "modules_enabled": {"postprocess": True, "follow_cam": False},
                        "artifacts": [],
                        "stats": {},
                        "progress": {"stage": "queued", "percent": 0.0},
                        "notes": None,
                        "error": None,
                    }
                ]
            }
        )

        class FailingPipeline:
            def __init__(self, app_config) -> None:
                self.app_config = app_config

            def run(self) -> None:
                self.app_config.output_dir.mkdir(parents=True, exist_ok=True)
                (self.app_config.output_dir / "ball_track.csv").write_text(
                    "Frame,X,Y,Confidence,Status\n0,10,20,0.9000,Detected\n",
                    encoding="utf-8",
                )
                raise RuntimeError("pipeline exploded")

        config = load_config(active_config)
        config.output_dir = output_dir
        with mock.patch("football_tracking.api.service.BallTrackingPipeline", FailingPipeline):
            self.service._execute_run("failed_demo", config, threading.Event())

        failed_run = self.service.get_run("failed_demo")
        self.assertEqual("failed", failed_run["status"])
        self.assertIn("pipeline exploded", failed_run["error"])
        self.assertTrue((output_dir / "run_manifest.json").exists())
        self.assertTrue((output_dir / "metrics_report.json").exists())
        self.assertEqual(1, failed_run["stats"]["raw"]["frame_count"])

    def test_execute_run_dispatches_temporal_chunks_when_enabled(self) -> None:
        active_input = (self.repo_root / "data" / "input.mp4").resolve()
        active_config = (self.repo_root / "config" / "default.yaml").resolve()
        output_dir = self.repo_root / "outputs" / "chunked_api_demo"
        self.service._write_registry(
            {
                "runs": [
                    {
                        "run_id": "chunked_api_demo",
                        "source": "api",
                        "status": "queued",
                        "created_at": "2026-01-01T00:00:00+00:00",
                        "started_at": None,
                        "completed_at": None,
                        "config_name": "default.yaml",
                        "config_path": str(active_config),
                        "input_video": str(active_input),
                        "parent_run_id": None,
                        "output_dir": str(output_dir.resolve()),
                        "modules_enabled": {"postprocess": True, "follow_cam": False, "temporal_chunks": True},
                        "artifacts": [],
                        "stats": {},
                        "progress": {"stage": "queued", "percent": 0.0},
                        "notes": None,
                        "error": None,
                    }
                ]
            }
        )
        calls: list[dict[str, object]] = []

        def fake_run_temporal_chunks(app_config, progress_callback=None, should_cancel=None) -> None:
            calls.append({"output_dir": app_config.output_dir, "should_cancel": should_cancel})
            self.assertFalse(should_cancel())
            if progress_callback is not None:
                progress_callback(
                    {
                        "stage": "temporal_chunks",
                        "chunk_index": 0,
                        "chunk_count": 2,
                        "current_frame": 1,
                        "total_frames": 3,
                    }
                )
                progress_snapshot = self.service.get_run("chunked_api_demo")["progress"]
                self.assertEqual("temporal_chunks", progress_snapshot["stage"])
                self.assertEqual(0, progress_snapshot["chunk_index"])
                self.assertEqual(2, progress_snapshot["chunk_count"])
            app_config.output_dir.mkdir(parents=True, exist_ok=True)
            (app_config.output_dir / "ball_track.csv").write_text(
                "Frame,X,Y,Confidence,Status\n0,10,20,0.9000,Detected\n",
                encoding="utf-8",
            )
            (app_config.output_dir / "temporal_chunks_report.json").write_text(
                json.dumps(
                    {
                        "chunk_count": 2,
                        "frame_count": 1,
                        "chunks": [{"index": 0, "name": "chunk_0000"}],
                        "boundary_events": [],
                        "execution": {"status": "succeeded", "mode": "in_process", "effective_workers": 1},
                        "stitch": {"status": "succeeded"},
                    }
                ),
                encoding="utf-8",
            )

        class ForbiddenPipeline:
            def __init__(self, app_config) -> None:
                self.app_config = app_config

            def run(self, *args, **kwargs) -> None:
                raise AssertionError("BallTrackingPipeline should not run when temporal chunks are enabled.")

        config = load_config(active_config)
        config.output_dir = output_dir
        config.temporal_chunks.enabled = True
        config.follow_cam.enabled = False
        with mock.patch("football_tracking.api.service.run_temporal_chunks", side_effect=fake_run_temporal_chunks, create=True), mock.patch(
            "football_tracking.api.service.BallTrackingPipeline",
            ForbiddenPipeline,
        ):
            self.service._execute_run("chunked_api_demo", config, threading.Event())

        completed_run = self.service.get_run("chunked_api_demo")
        self.assertEqual("completed", completed_run["status"])
        self.assertEqual(output_dir.resolve(), calls[0]["output_dir"])
        self.assertTrue(completed_run["modules_enabled"]["temporal_chunks"])
        self.assertEqual(2, completed_run["stats"]["temporal_chunks"]["chunk_count"])
        self.assertIn("temporal_chunks_report.json", {artifact["name"] for artifact in completed_run["artifacts"]})

    def test_delete_config_and_input_video_remove_files(self) -> None:
        deleted_video = self.service.delete_input_video("clip.mov")
        deleted_config = self.service.delete_config("alt.yaml")

        self.assertTrue(deleted_video["deleted"])
        self.assertTrue(deleted_config["deleted"])
        self.assertFalse((self.repo_root / "data" / "clip.mov").exists())
        self.assertFalse((self.repo_root / "config" / "alt.yaml").exists())

    def test_delete_run_output_removes_output_folder_and_registry_entry(self) -> None:
        self.create_output_bundle("kept_baseline")
        run = self.service.list_runs()[0]

        deleted = self.service.delete_run_output(run["run_id"])

        self.assertTrue(deleted["deleted"])
        self.assertFalse(Path(run["output_dir"]).exists())
        self.assertEqual([], self.service.list_runs())

    def test_ai_recommend_camera_objective_returns_follow_cam_patch(self) -> None:
        self.create_output_bundle("kept_baseline")
        run = self.service.list_runs()[0]

        recommendation = self.service.ai_recommend(
            run_id=run["run_id"],
            objective="Keep the camera steadier during fast pan and zoom moments",
        )

        self.assertEqual("Follow-Cam Stabilization", recommendation["title"])
        self.assertIn("follow_cam", recommendation["patch"])
        self.assertTrue(any(line.startswith("follow_cam.") for line in recommendation["patch_preview"]))
        self.assertTrue(recommendation["output_name_suggestion"].startswith("default_"))

    def test_ai_explain_language_zh_returns_chinese_summary(self) -> None:
        self.create_output_bundle("kept_baseline")
        run = self.service.list_runs()[0]

        explanation = self.service.ai_explain(
            run_id=run["run_id"],
            config_name=run["config_name"],
            focus="\u7a33\u5b9a\u955c\u5934",
            language="zh",
        )

        self.assertIn("\u8fd0\u884c", explanation["summary"])
        self.assertIn("\u5f53\u524d\u76ee\u6807", explanation["summary"])
        self.assertTrue(any("\u8fd0\u884c\u72b6\u6001" in item for item in explanation["evidence"]))

    def test_ai_recommend_language_zh_returns_chinese_copy(self) -> None:
        self.create_output_bundle("kept_baseline")
        run = self.service.list_runs()[0]

        recommendation = self.service.ai_recommend(
            run_id=run["run_id"],
            objective="\u8ba9\u955c\u5934\u66f4\u7a33\u4e00\u4e9b",
            language="zh",
        )

        self.assertEqual("\u8ddf\u968f\u955c\u5934\u7a33\u5b9a\u5316", recommendation["title"])
        self.assertIn("\u5e73\u79fb", recommendation["recommendation"])
        self.assertTrue(any("\u8ddf\u968f\u955c\u5934" in item for item in recommendation["evidence"]))

    def test_ai_improve_writes_report_and_updates_run_artifacts(self) -> None:
        output_dir = self.create_output_bundle("improve_baseline")
        self.write_json(
            "outputs/improve_baseline/ball_audit.json",
            {
                "schema_version": "1.0",
                "summary": {
                    "frame_count": 30,
                    "source_count": 1,
                    "tracklet_count": 1,
                    "suspicious_tracklet_count": 0,
                    "review_event_count": 1,
                    "lost_gap_count": 1,
                },
                "sources": [],
                "tracklets": [],
                "review_events": [
                    {
                        "source": "cleaned",
                        "type": "lost_gap",
                        "severity": "fail",
                        "start_frame": 10,
                        "end_frame": 20,
                        "frame_count": 11,
                        "reason": "Ball track is lost between tracklets.",
                    }
                ],
            },
        )
        run = self.service.list_runs()[0]

        response = self.service.ai_improve(
            run_id=run["run_id"],
            objective="recover the missing ball",
            model="gpt-improve",
            dry_run=True,
            max_items=1,
            language="en",
        )

        refreshed = self.service.get_run(run["run_id"])
        artifact_names = {artifact["name"] for artifact in refreshed["artifacts"]}
        self.assertEqual("ai_improvement_report.json", response["artifact_name"])
        self.assertEqual(str((output_dir / "ai_improvement_report.json").resolve()), response["artifact_path"])
        self.assertEqual("needs_rerun", response["summary"]["status"])
        self.assertEqual("targeted_rerun", response["improvements"][0]["recommended_action"])
        self.assertIn("ai_improvement_report.json", artifact_names)
        self.assertIn("metrics_report.json", artifact_names)
        self.assertEqual("needs_rerun", refreshed["stats"]["ai_improvement"]["status"])
        metrics_report = json.loads((output_dir / "metrics_report.json").read_text(encoding="utf-8"))
        self.assertEqual("needs_rerun", metrics_report["ai_improvement"]["status"])

    def test_ai_improve_route_writes_report(self) -> None:
        self.create_output_bundle("improve_route_baseline")
        run = self.service.list_runs()[0]

        response = improve_route(
            AIImproveRequest(run_id=run["run_id"], dry_run=True, max_items=1),
            service=self.service,
        )

        self.assertEqual("ai_improvement_report.json", response.artifact_name)
        self.assertTrue(Path(response.artifact_path).exists())

    def test_ai_improve_preserves_tracks_and_does_not_create_apply_artifacts(self) -> None:
        output_dir = self.create_output_bundle("improve_only_baseline")
        run = self.service.list_runs()[0]
        raw_path = output_dir / "ball_track.csv"
        cleaned_path = output_dir / "ball_track.cleaned.csv"
        before = {
            raw_path.name: self.file_fingerprint(raw_path),
            cleaned_path.name: self.file_fingerprint(cleaned_path),
        }

        response = self.service.ai_improve(
            run_id=run["run_id"],
            dry_run=True,
            max_items=1,
            language="en",
        )

        after = {
            raw_path.name: self.file_fingerprint(raw_path),
            cleaned_path.name: self.file_fingerprint(cleaned_path),
        }
        refreshed = self.service.get_run(run["run_id"])
        artifact_names = {artifact["name"] for artifact in refreshed["artifacts"]}

        self.assertEqual(before, after)
        self.assertEqual("ai_improvement_report.json", response["artifact_name"])
        self.assertIn("ai_improvement_report.json", artifact_names)
        self.assertNotIn("ai_improvement_approved_config_patch.json", artifact_names)
        self.assertNotIn("follow_cam_rerender_plan.json", artifact_names)
        self.assertNotIn("highlight_report.json", artifact_names)
        self.assertFalse((output_dir / "highlight.mp4").exists())

    def test_ai_improve_route_preserves_camera_summary_and_item_fields(self) -> None:
        output_dir = self.create_output_bundle("improve_camera_route_baseline")
        self.write_json(
            "outputs/improve_camera_route_baseline/camera_motion_audit.json",
            {
                "schema_version": "1.0",
                "summary": {"status": "ok", "review_event_count": 1},
                "review_events": [
                    {
                        "type": "camera_motion_spike",
                        "severity": "warn",
                        "start_frame": 40,
                        "end_frame": 40,
                        "reason": "Camera step exceeded warning threshold.",
                        "evidence": {"max_step_px": 120.0},
                    }
                ],
            },
        )
        (output_dir / "ball_track.csv").write_text(
            "Frame,Status,X,Y\n"
            "28,Detected,100,100\n"
            "36,Detected,102,100\n"
            "40,Detected,105,100\n"
            "44,Detected,108,100\n"
            "52,Detected,110,100\n",
            encoding="utf-8",
        )
        run = self.service.list_runs()[0]

        response = improve_route(
            AIImproveRequest(run_id=run["run_id"], dry_run=True, max_items=1),
            service=self.service,
        )

        self.assertEqual(1, response.summary.camera_improvement_count)
        self.assertEqual({"warn": 1}, response.summary.camera_severity_counts)
        self.assertEqual({"adjust_follow_cam": 1}, response.summary.camera_action_counts)
        self.assertEqual("cam_event_001", response.improvements[0].camera_motion_event_id)
        self.assertEqual("warn", response.improvements[0].camera_motion_severity)
        self.assertEqual("stable_detected", response.improvements[0].evidence_payload["nearby_ball_track"]["classification"])

    def test_ai_improvement_approve_writes_approved_actions(self) -> None:
        output_dir = self.create_output_bundle("approve_baseline")
        self.write_json(
            "outputs/approve_baseline/review_packets.json",
            {
                "summary": {"packet_count": 1},
                "packets": [
                    {
                        "packet_id": "packet_001",
                        "source": {"kind": "trigger", "type": "lost_gap", "start_frame": 10, "end_frame": 20},
                        "window": {"start_frame": 0, "end_frame": 35},
                    }
                ],
            },
        )
        self.write_json(
            "outputs/approve_baseline/ai_improvement_report.json",
            {
                "schema_version": "1.0",
                "generated_at": "2026-06-22T00:00:00+00:00",
                "model": "gpt-improve",
                "summary": {"status": "needs_rerun"},
                "improvements": [
                    {
                        "id": "imp_001",
                        "priority": "P0",
                        "area": "tracking",
                        "failure_tags": ["ball_lost"],
                        "root_cause_module": "reacquisition",
                        "diagnosis": "Recover localized ball.",
                        "recommended_action": "targeted_rerun",
                        "rerun_scope": {"start_frame": 10, "end_frame": 20},
                        "local_search_roi": {
                            "coordinate_space": "image",
                            "frame": 15,
                            "x": 120,
                            "y": 40,
                            "width": 80,
                            "height": 50,
                            "confidence": 0.72,
                        },
                        "evidence_payload": {"source_packet_id": "packet_001"},
                        "confidence": 0.82,
                    }
                ],
            },
        )
        run = self.service.list_runs()[0]

        response = self.service.ai_improvement_approve(
            run_id=run["run_id"],
            improvement_ids=["imp_001"],
            approved_by="operator-a",
        )

        refreshed = self.service.get_run(run["run_id"])
        artifact_names = {artifact["name"] for artifact in refreshed["artifacts"]}
        written = json.loads((output_dir / "ai_improvement_approved_actions.json").read_text(encoding="utf-8"))
        self.assertEqual("ai_improvement_approved_actions.json", response["artifact_name"])
        self.assertEqual(response["approved_actions"], written["approved_actions"])
        self.assertEqual("targeted_rerun", response["approved_actions"][0]["approved_action"])
        self.assertEqual(1, response["summary"]["approved_action_count"])
        self.assertEqual({"targeted_rerun": 1}, response["summary"]["approved_action_counts"])
        self.assertTrue(response["summary"]["requires_execution"])
        self.assertTrue(response["summary"]["requires_high_recall_rerun"])
        self.assertFalse(response["summary"]["requires_tracking_rerun"])
        self.assertEqual("ai_improvement_approved_actions.json", response["summary"]["artifacts"]["approved_actions"]["name"])
        self.assertIn("ai_improvement_approved_actions.json", artifact_names)

    def test_ai_improvement_approve_route_validates_config_patch_overrides(self) -> None:
        output_dir = self.create_output_bundle("approve_route_baseline")
        self.write_json(
            "outputs/approve_route_baseline/ai_improvement_report.json",
            {
                "schema_version": "1.0",
                "generated_at": "2026-06-22T00:00:00+00:00",
                "model": "gpt-improve",
                "summary": {"status": "needs_rerun"},
                "improvements": [
                    {
                        "id": "imp_filter",
                        "priority": "P1",
                        "area": "tracking",
                        "failure_tags": ["foot_confusion"],
                        "root_cause_module": "selection",
                        "start_frame": 40,
                        "end_frame": 52,
                        "diagnosis": "Noise filter can tighten.",
                        "recommended_action": "noise_filter_adjustment",
                        "false_positive_class": "foot_confusion",
                        "config_patch": {"selection": {"min_accept_score": 0.55}},
                        "confidence": 0.7,
                    }
                ],
            },
        )
        run = self.service.list_runs()[0]

        response = approve_improvements_route(
            run["run_id"],
            AIImproveApprovalRequest(
                improvement_ids=["imp_filter"],
                config_patch_overrides={
                    "imp_filter": {
                        "selection": {"min_accept_score": 0.6},
                        "detector": {"confidence_threshold": 0.01},
                    }
                },
            ),
            service=self.service,
        )

        self.assertEqual({"selection": {"min_accept_score": 0.6}}, response.approved_actions[0].config_patch)
        self.assertEqual("foot_confusion", response.approved_actions[0].false_positive_class)
        self.assertEqual(1, response.summary.approved_action_count)
        self.assertEqual({"noise_filter_adjustment": 1}, response.summary.approved_action_counts)
        self.assertEqual("ai_improvement_approved_config_patch.json", response.summary.artifacts["config_patch"].name)
        self.assertTrue((output_dir / "ai_improvement_approved_config_patch.json").exists())
        self.assertTrue(any("detector.confidence_threshold" in warning for warning in response.warnings))

    def test_ai_improvement_approve_exposes_follow_cam_rerender_plan_artifact(self) -> None:
        output_dir = self.create_output_bundle("approve_follow_cam_baseline")
        self.write_json(
            "outputs/approve_follow_cam_baseline/ai_improvement_report.json",
            {
                "schema_version": "1.0",
                "generated_at": "2026-06-22T00:00:00+00:00",
                "model": "gpt-improve",
                "summary": {"status": "needs_rerun"},
                "improvements": [
                    {
                        "id": "imp_camera",
                        "priority": "P1",
                        "area": "camera_motion",
                        "failure_tags": ["camera_catchup_spike"],
                        "root_cause_module": "follow_cam",
                        "start_frame": 40,
                        "end_frame": 40,
                        "diagnosis": "Stable tracking but follow-cam jumped.",
                        "recommended_action": "adjust_follow_cam",
                        "config_patch": {"follow_cam": {"glide_pan_smoothing": 0.2}},
                        "confidence": 0.74,
                    }
                ],
            },
        )
        run = self.service.list_runs()[0]

        response = self.service.ai_improvement_approve(
            run_id=run["run_id"],
            improvement_ids=["imp_camera"],
            approved_by="operator-a",
        )

        refreshed = self.service.get_run(run["run_id"])
        artifact_names = {artifact["name"] for artifact in refreshed["artifacts"]}
        plan = json.loads((output_dir / "follow_cam_rerender_plan.json").read_text(encoding="utf-8"))
        self.assertEqual("follow_cam_rerender_plan.json", response["follow_cam_rerender_plan_artifact_name"])
        self.assertEqual(str((output_dir / "follow_cam_rerender_plan.json").resolve()), response["follow_cam_rerender_plan_artifact_path"])
        self.assertIn("follow_cam_rerender_plan.json", artifact_names)
        self.assertFalse(plan["requires_tracking_rerun"])
        self.assertEqual({"follow_cam": {"glide_pan_smoothing": 0.2}}, plan["recommended_config_patch"])

    def test_ai_improvement_approve_tracking_rerun_plan_is_not_follow_cam_only(self) -> None:
        output_dir = self.create_output_bundle("approve_tracking_rerun_baseline")
        self.write_json(
            "outputs/approve_tracking_rerun_baseline/ai_improvement_report.json",
            {
                "schema_version": "1.0",
                "generated_at": "2026-06-22T00:00:00+00:00",
                "model": "gpt-improve",
                "summary": {"status": "needs_rerun"},
                "improvements": [
                    {
                        "id": "imp_camera_track",
                        "priority": "P0",
                        "area": "camera_motion",
                        "failure_tags": ["camera_catchup_spike", "ball_lost"],
                        "root_cause_module": "follow_cam",
                        "start_frame": 40,
                        "end_frame": 42,
                        "diagnosis": "Camera jump is track-driven.",
                        "recommended_action": "tracking_rerun_before_follow_cam",
                        "rerun_scope": {"start_frame": 28, "end_frame": 54},
                        "confidence": 0.8,
                    }
                ],
            },
        )
        run = self.service.list_runs()[0]

        response = self.service.ai_improvement_approve(
            run_id=run["run_id"],
            improvement_ids=["imp_camera_track"],
            approved_by="operator-a",
        )

        plan = json.loads((output_dir / "follow_cam_rerender_plan.json").read_text(encoding="utf-8"))
        self.assertTrue(plan["requires_tracking_rerun"])
        self.assertEqual({"start_frame": 28, "end_frame": 54}, plan["tracking_rerun_scope"])
        self.assertTrue(response["summary"]["requires_tracking_rerun"])
        self.assertFalse(response["summary"]["requires_follow_cam_rerender"])
        self.assertEqual("tracking_rerun_before_follow_cam", response["approved_actions"][0]["approved_action"])

    def test_ai_improvement_approve_clears_stale_config_patch_artifact_from_response(self) -> None:
        output_dir = self.create_output_bundle("approve_no_patch_baseline")
        self.write_json(
            "outputs/approve_no_patch_baseline/ai_improvement_approved_config_patch.json",
            {"schema_version": "1.0", "merged_config_patch": {"selection": {"min_accept_score": 0.99}}},
        )
        self.write_json(
            "outputs/approve_no_patch_baseline/ai_improvement_report.json",
            {
                "schema_version": "1.0",
                "generated_at": "2026-06-22T00:00:00+00:00",
                "model": "gpt-improve",
                "summary": {"status": "needs_rerun"},
                "improvements": [
                    {
                        "id": "imp_manual",
                        "priority": "P2",
                        "area": "tracking",
                        "failure_tags": ["unknown"],
                        "root_cause_module": "unknown",
                        "diagnosis": "Manual inspection only.",
                        "recommended_action": "manual_review",
                        "confidence": 0.5,
                    }
                ],
            },
        )
        run = self.service.list_runs()[0]

        response = self.service.ai_improvement_approve(
            run["run_id"],
            improvement_ids=["imp_manual"],
            approved_by="operator-a",
        )

        self.assertIsNone(response["config_patch_artifact_name"])
        self.assertIsNone(response["config_patch_artifact_path"])
        self.assertFalse((output_dir / "ai_improvement_approved_config_patch.json").exists())

    def test_ai_frame_window_requires_ordered_frames(self) -> None:
        with self.assertRaises(ValidationError):
            AIFrameWindow(start_frame=30, end_frame=10)

    def test_create_app_registers_expected_routes(self) -> None:
        app = create_app(self.repo_root)
        route_paths = {route.path for route in app.routes}

        expected_paths = {
            "/api/v1/health",
            "/api/v1/inputs",
            "/api/v1/inputs/field-preview",
            "/api/v1/inputs/field-suggestion",
            "/api/v1/inputs/quality-check",
            "/api/v1/configs",
            "/api/v1/configs/{name:path}",
            "/api/v1/runs",
            "/api/v1/runs/asset-groups",
            "/api/v1/runs/{run_id}",
            "/api/v1/runs/{run_id}/follow-cam-render",
            "/api/v1/runs/{run_id}/highlight-render",
            "/api/v1/runs/{run_id}/artifacts",
            "/api/v1/runs/{run_id}/artifacts/{artifact_name:path}",
            "/api/v1/runs/{run_id}/cleanup-report",
            "/api/v1/runs/{run_id}/follow-cam-report",
            "/api/v1/runs/{run_id}/ball-audit",
            "/api/v1/runs/{run_id}/ai-review-triggers",
            "/api/v1/runs/{run_id}/event-candidates",
            "/api/v1/runs/{run_id}/player-tracks",
            "/api/v1/runs/{run_id}/camera-path",
            "/api/v1/ai/explain",
            "/api/v1/ai/recommend",
            "/api/v1/ai/improve",
            "/api/v1/ai/improve/{run_id}/approve",
            "/api/v1/ai/config-diff",
        }

        self.assertTrue(expected_paths.issubset(route_paths))

    def test_create_app_documents_quality_check_domain_errors(self) -> None:
        app = create_app(self.repo_root)
        operation = app.openapi()["paths"]["/api/v1/inputs/quality-check"]["post"]

        self.assertEqual("#/components/schemas/ApiErrorResponse", operation["responses"]["400"]["content"]["application/json"]["schema"]["$ref"])
        self.assertEqual("#/components/schemas/ApiErrorResponse", operation["responses"]["404"]["content"]["application/json"]["schema"]["$ref"])
        self.assertEqual("#/components/schemas/HTTPValidationError", operation["responses"]["422"]["content"]["application/json"]["schema"]["$ref"])

    def test_create_app_documents_ball_audit_response_schema(self) -> None:
        app = create_app(self.repo_root)
        operation = app.openapi()["paths"]["/api/v1/runs/{run_id}/ball-audit"]["get"]

        self.assertEqual(
            "#/components/schemas/BallAuditReport",
            operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
        )
        self.assertEqual(
            "#/components/schemas/ApiErrorResponse",
            operation["responses"]["404"]["content"]["application/json"]["schema"]["$ref"],
        )

    def test_create_app_documents_ai_review_triggers_response_schema(self) -> None:
        app = create_app(self.repo_root)
        operation = app.openapi()["paths"]["/api/v1/runs/{run_id}/ai-review-triggers"]["get"]

        self.assertEqual(
            "#/components/schemas/AIReviewTriggerReport",
            operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
        )
        self.assertEqual(
            "#/components/schemas/ApiErrorResponse",
            operation["responses"]["404"]["content"]["application/json"]["schema"]["$ref"],
        )

    def test_create_app_documents_player_tracks_response_schema(self) -> None:
        app = create_app(self.repo_root)
        operation = app.openapi()["paths"]["/api/v1/runs/{run_id}/player-tracks"]["get"]

        self.assertEqual(
            "#/components/schemas/PlayerTracksReport",
            operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
        )
        self.assertEqual(
            "#/components/schemas/ApiErrorResponse",
            operation["responses"]["404"]["content"]["application/json"]["schema"]["$ref"],
        )

    def test_create_app_documents_event_candidates_response_schema(self) -> None:
        app = create_app(self.repo_root)
        openapi = app.openapi()
        operation = openapi["paths"]["/api/v1/runs/{run_id}/event-candidates"]["get"]

        self.assertEqual(
            "#/components/schemas/EventCandidateReport",
            operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
        )
        self.assertEqual(
            "#/components/schemas/ApiErrorResponse",
            operation["responses"]["404"]["content"]["application/json"]["schema"]["$ref"],
        )
        report_schema = openapi["components"]["schemas"]["EventCandidateReport"]
        self.assertIn("warnings", report_schema["properties"])
        self.assertEqual("array", report_schema["properties"]["warnings"]["type"])
        candidate_schema = openapi["components"]["schemas"]["EventCandidate"]
        self.assertIn("core_window", candidate_schema["required"])
        self.assertIn("buffer_policy", candidate_schema["required"])
        self.assertEqual(
            "#/components/schemas/EventCandidateBufferPolicy",
            candidate_schema["properties"]["buffer_policy"]["$ref"],
        )
        buffer_schema = openapi["components"]["schemas"]["EventCandidateBufferPolicy"]
        self.assertIn("fps_source", buffer_schema["required"])
        self.assertIn("min_tail_frames", buffer_schema["required"])

    def test_create_app_documents_highlight_render_request_schema(self) -> None:
        app = create_app(self.repo_root)
        openapi = app.openapi()
        operation = openapi["paths"]["/api/v1/runs/{run_id}/highlight-render"]["post"]

        self.assertEqual(
            "#/components/schemas/HighlightRenderRequest",
            operation["requestBody"]["content"]["application/json"]["schema"]["$ref"],
        )
        request_schema = openapi["components"]["schemas"]["HighlightRenderRequest"]
        self.assertIn("oneOf", request_schema)
        self.assertEqual(3, len(request_schema["oneOf"]))
        self.assertEqual(
            "#/components/schemas/RunRecord",
            operation["responses"]["202"]["content"]["application/json"]["schema"]["$ref"],
        )
        self.assertEqual(
            "#/components/schemas/ApiErrorResponse",
            operation["responses"]["404"]["content"]["application/json"]["schema"]["$ref"],
        )
        self.assertEqual(
            "#/components/schemas/ApiErrorResponse",
            operation["responses"]["409"]["content"]["application/json"]["schema"]["$ref"],
        )

    def test_create_app_documents_ai_improve_schema(self) -> None:
        app = create_app(self.repo_root)
        operation = app.openapi()["paths"]["/api/v1/ai/improve"]["post"]

        self.assertEqual(
            "#/components/schemas/AIImproveRequest",
            operation["requestBody"]["content"]["application/json"]["schema"]["$ref"],
        )
        self.assertEqual(
            "#/components/schemas/AIImproveResponse",
            operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
        )

    def test_create_app_documents_ai_improve_approval_schema(self) -> None:
        app = create_app(self.repo_root)
        operation = app.openapi()["paths"]["/api/v1/ai/improve/{run_id}/approve"]["post"]

        self.assertEqual(
            "#/components/schemas/AIImproveApprovalRequest",
            operation["requestBody"]["content"]["application/json"]["schema"]["$ref"],
        )
        self.assertEqual(
            "#/components/schemas/AIImproveApprovalResponse",
            operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
        )


if __name__ == "__main__":
    unittest.main()
