from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

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

    def test_suggest_field_setup_returns_preview_and_config_patch(self) -> None:
        video_path = self.write_video("data/field_preview.avi")

        suggestion = self.service.suggest_field_setup(str(video_path))

        self.assertTrue(suggestion["preview_data_url"].startswith("data:image/jpeg;base64,"))
        self.assertEqual("detected", suggestion["confidence"])
        self.assertEqual(640, suggestion["frame_width"])
        self.assertEqual(360, suggestion["frame_height"])
        field_roi = suggestion["field_roi"]
        expanded_roi = suggestion["expanded_roi"]
        self.assertLess(field_roi[0], field_roi[2])
        self.assertLess(field_roi[1], field_roi[3])
        self.assertLessEqual(expanded_roi[0], field_roi[0])
        self.assertGreaterEqual(expanded_roi[2], field_roi[2])
        self.assertEqual(list(expanded_roi), suggestion["config_patch"]["filtering"]["roi"])
        self.assertEqual(list(field_roi), suggestion["config_patch"]["scene_bias"]["ground_zones"][0]["roi"])

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
        self.assertEqual(3, run["stats"]["raw"]["frame_count"])
        self.assertEqual(2, run["stats"]["cleaned"]["detected"])
        self.assertEqual("cleaned", run["stats"]["follow_cam"]["track_source"])
        self.assertIn("follow_cam.mp4", {artifact["name"] for artifact in run["artifacts"]})

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
            "/api/v1/inputs/field-suggestion",
            "/api/v1/configs",
            "/api/v1/configs/{name:path}",
            "/api/v1/runs",
            "/api/v1/runs/{run_id}",
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
