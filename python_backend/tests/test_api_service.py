from __future__ import annotations

import base64
import json
import threading
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import cv2
import numpy as np
import yaml

from football_tracking.api.app import create_app
from football_tracking.api.service import ApiService
from football_tracking.config import load_config


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

    def create_output_bundle(self, folder_name: str) -> Path:
        output_dir = self.repo_root / "outputs" / folder_name
        output_dir.mkdir(parents=True, exist_ok=True)
        self.write_text(f"outputs/{folder_name}/annotated.cleaned.mp4", "fake mp4")
        self.write_text(f"outputs/{folder_name}/follow_cam.mp4", "fake follow cam")
        self.write_csv(
            f"outputs/{folder_name}/ball_track.csv",
            [
                {"Frame": 0, "Status": "Detected"},
                {"Frame": 1, "Status": "Predicted"},
                {"Frame": 2, "Status": "Lost"},
            ],
        )
        self.write_csv(
            f"outputs/{folder_name}/ball_track.cleaned.csv",
            [
                {"Frame": 0, "Status": "Detected"},
                {"Frame": 1, "Status": "Detected"},
                {"Frame": 2, "Status": "Lost"},
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
        return output_dir

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
        self.assertIn("follow_cam.mp4", {artifact["name"] for artifact in run["artifacts"]})

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
        self.assertIn("/outputs/runs/input/", Path(completed_run["output_dir"]).resolve().as_posix())

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

    def test_create_app_registers_expected_routes(self) -> None:
        app = create_app(self.repo_root)
        route_paths = {route.path for route in app.routes}

        expected_paths = {
            "/api/v1/health",
            "/api/v1/inputs",
            "/api/v1/inputs/field-preview",
            "/api/v1/inputs/field-suggestion",
            "/api/v1/configs",
            "/api/v1/configs/{name:path}",
            "/api/v1/runs",
            "/api/v1/runs/asset-groups",
            "/api/v1/runs/{run_id}",
            "/api/v1/runs/{run_id}/follow-cam-render",
            "/api/v1/runs/{run_id}/artifacts",
            "/api/v1/runs/{run_id}/artifacts/{artifact_name:path}",
            "/api/v1/runs/{run_id}/cleanup-report",
            "/api/v1/runs/{run_id}/follow-cam-report",
            "/api/v1/runs/{run_id}/camera-path",
            "/api/v1/ai/explain",
            "/api/v1/ai/recommend",
            "/api/v1/ai/config-diff",
        }

        self.assertTrue(expected_paths.issubset(route_paths))


if __name__ == "__main__":
    unittest.main()
