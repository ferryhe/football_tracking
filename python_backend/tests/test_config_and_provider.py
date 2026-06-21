from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from football_tracking.api.ai_provider import load_provider_settings
from football_tracking.config import load_config


class ConfigAndProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_root = Path(self.temp_dir.name)
        (self.repo_root / "config").mkdir(parents=True, exist_ok=True)
        (self.repo_root / "data").mkdir(parents=True, exist_ok=True)
        (self.repo_root / "weights").mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def write_yaml(self, relative_path: str, payload: object) -> Path:
        path = self.repo_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=False), encoding="utf-8")
        return path

    def test_load_config_resolves_relative_paths_and_normalizes_runtime(self) -> None:
        config_path = self.write_yaml(
            "config/default.yaml",
            {
                "input_video": "./data/input.mp4",
                "output_dir": "./outputs/run_a",
                "detector": {
                    "model_path": "./weights/model.pt",
                },
                "runtime": {
                    "start_frame": -5,
                    "max_frames": 0,
                },
            },
        )

        config = load_config(config_path)

        self.assertEqual((self.repo_root / "data" / "input.mp4").resolve(), config.input_video)
        self.assertEqual((self.repo_root / "outputs" / "run_a").resolve(), config.output_dir)
        self.assertEqual((self.repo_root / "weights" / "model.pt").resolve(), config.detector.model_path)
        self.assertEqual(0, config.runtime.start_frame)
        self.assertIsNone(config.runtime.max_frames)
        self.assertFalse(config.selection.priors.enabled)

    def test_load_config_rejects_invalid_filter_roi(self) -> None:
        config_path = self.write_yaml(
            "config/invalid.yaml",
            {
                "input_video": "./data/input.mp4",
                "output_dir": "./outputs/run_a",
                "detector": {
                    "model_path": "./weights/model.pt",
                },
                "filtering": {
                    "roi": [1, 2, 3],
                },
            },
        )

        with self.assertRaises(ValueError):
            load_config(config_path)

    def test_load_config_parses_selection_priors(self) -> None:
        config_path = self.write_yaml(
            "config/selection_priors.yaml",
            {
                "input_video": "./data/input.mp4",
                "output_dir": "./outputs/run_a",
                "detector": {
                    "model_path": "./weights/model.pt",
                },
                "selection": {
                    "priors": {
                        "player_foot_radius_px": 42.0,
                        "player_foot_bonus": 0.07,
                        "recent_player_frame_window": 3,
                        "pitch_boundary_penalty": -0.18,
                        "pitch_boundary_margin_m": 1.5,
                        "player_tracks_path": "./outputs/player_tracks.json",
                    }
                },
            },
        )

        config = load_config(config_path)

        self.assertTrue(config.selection.priors.enabled)
        self.assertEqual(42.0, config.selection.priors.player_foot_radius_px)
        self.assertEqual(0.07, config.selection.priors.player_foot_bonus)
        self.assertEqual(3, config.selection.priors.recent_player_frame_window)
        self.assertEqual(-0.18, config.selection.priors.pitch_boundary_penalty)
        self.assertEqual(1.5, config.selection.priors.pitch_boundary_margin_m)
        self.assertEqual((self.repo_root / "outputs" / "player_tracks.json").resolve(), config.selection.priors.player_tracks_path)

    def test_load_config_allows_selection_priors_to_be_disabled(self) -> None:
        config_path = self.write_yaml(
            "config/selection_priors_disabled.yaml",
            {
                "input_video": "./data/input.mp4",
                "output_dir": "./outputs/run_a",
                "detector": {
                    "model_path": "./weights/model.pt",
                },
                "selection": {
                    "priors": {
                        "enabled": False,
                        "player_foot_radius_px": 42.0,
                    }
                },
            },
        )

        config = load_config(config_path)

        self.assertFalse(config.selection.priors.enabled)
        self.assertEqual(42.0, config.selection.priors.player_foot_radius_px)

    def test_load_provider_settings_reads_dotenv_defaults(self) -> None:
        dotenv_path = self.repo_root / ".env"
        dotenv_path.write_text(
            "\n".join(
                [
                    "PROVIDER_OPENAI_API_KEY=dotenv-key",
                    "PROVIDER_OPENAI_BASE_URL=https://example.invalid/v1",
                    "PROVIDER_OPENAI_CHAT_MODEL=gpt-test",
                ]
            ),
            encoding="utf-8",
        )

        with patch.dict(os.environ, {}, clear=True):
            settings = load_provider_settings(self.repo_root)

        self.assertEqual("dotenv-key", settings.api_key)
        self.assertEqual("https://example.invalid/v1", settings.base_url)
        self.assertEqual("gpt-test", settings.chat_model)
        self.assertTrue(settings.enabled)

    def test_load_provider_settings_prefers_environment_over_dotenv(self) -> None:
        dotenv_path = self.repo_root / ".env"
        dotenv_path.write_text("PROVIDER_OPENAI_API_KEY=dotenv-key\n", encoding="utf-8")

        with patch.dict(
            os.environ,
            {
                "PROVIDER_OPENAI_API_KEY": "env-key",
                "PROVIDER_OPENAI_BASE_URL": "https://override.invalid/v1/",
                "PROVIDER_OPENAI_CHAT_MODEL": "gpt-env",
            },
            clear=True,
        ):
            settings = load_provider_settings(self.repo_root)

        self.assertEqual("env-key", settings.api_key)
        self.assertEqual("https://override.invalid/v1", settings.base_url)
        self.assertEqual("gpt-env", settings.chat_model)


if __name__ == "__main__":
    unittest.main()
