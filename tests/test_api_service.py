from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import yaml

from football_tracking.api.app import create_app
from football_tracking.api.service import ApiService


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

    def test_create_app_registers_expected_routes(self) -> None:
        app = create_app(self.repo_root)
        route_paths = {route.path for route in app.routes}

        expected_paths = {
            "/api/v1/health",
            "/api/v1/inputs",
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
