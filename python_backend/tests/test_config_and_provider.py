from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import yaml

from football_tracking.api.ai_provider import OpenAIProviderSettings, OpenAIResponsesClient, load_provider_settings
from football_tracking.config import DetectorConfig, SahiConfig, load_config
from football_tracking.detector import YOLOSahiBallDetector


class _TensorList:
    def __init__(self, values: list) -> None:
        self.values = values

    def cpu(self) -> "_TensorList":
        return self

    def tolist(self) -> list:
        return self.values


class _FakeDirectModel:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def predict(self, frame, **kwargs):
        self.calls.append({"frame": frame, **kwargs})
        boxes = SimpleNamespace(
            xyxy=_TensorList([[1.0, 2.0, 9.0, 10.0]]),
            conf=_TensorList([0.91]),
            cls=_TensorList([0]),
        )
        return [SimpleNamespace(boxes=boxes, names={0: "ball"})]


class _FakeSahiBox:
    def to_xyxy(self) -> list[float]:
        return [3.0, 4.0, 11.0, 12.0]


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
        self.assertEqual("direct_full_frame", config.detector.inference_mode)
        self.assertFalse(config.temporal_chunks.enabled)
        self.assertEqual(1200, config.temporal_chunks.chunk_frames)
        self.assertEqual(80, config.temporal_chunks.overlap_frames)
        self.assertEqual(1, config.temporal_chunks.max_workers)
        self.assertFalse(config.temporal_chunks.allow_gpu_oversubscription)
        self.assertEqual((), config.temporal_chunks.devices)
        self.assertEqual(120, config.temporal_chunks.decode_preroll_frames)
        self.assertEqual("chunks", config.temporal_chunks.output_dir_name)
        self.assertEqual("overlap_quality", config.temporal_chunks.merge_strategy)

    def test_load_config_parses_detector_inference_mode_and_temporal_chunks(self) -> None:
        config_path = self.write_yaml(
            "config/direct.yaml",
            {
                "input_video": "./data/input.mp4",
                "output_dir": "./outputs/run_a",
                "detector": {
                    "model_path": "./weights/model.pt",
                    "inference_mode": "direct_full_frame",
                },
                "temporal_chunks": {
                    "enabled": True,
                    "chunk_frames": 600,
                    "overlap_frames": 40,
                    "max_workers": 2,
                    "allow_gpu_oversubscription": True,
                    "devices": ["cuda:0", "cuda:1"],
                    "decode_preroll_frames": 24,
                    "output_dir_name": "temporal_parts",
                    "merge_strategy": "overlap_quality",
                },
            },
        )

        config = load_config(config_path)

        self.assertEqual("direct_full_frame", config.detector.inference_mode)
        self.assertTrue(config.temporal_chunks.enabled)
        self.assertEqual(600, config.temporal_chunks.chunk_frames)
        self.assertEqual(40, config.temporal_chunks.overlap_frames)
        self.assertEqual(2, config.temporal_chunks.max_workers)
        self.assertTrue(config.temporal_chunks.allow_gpu_oversubscription)
        self.assertEqual(("cuda:0", "cuda:1"), config.temporal_chunks.devices)
        self.assertEqual(24, config.temporal_chunks.decode_preroll_frames)
        self.assertEqual("temporal_parts", config.temporal_chunks.output_dir_name)
        self.assertEqual("overlap_quality", config.temporal_chunks.merge_strategy)

    def test_load_config_rejects_unknown_detector_inference_mode(self) -> None:
        config_path = self.write_yaml(
            "config/unknown_detector_mode.yaml",
            {
                "input_video": "./data/input.mp4",
                "output_dir": "./outputs/run_a",
                "detector": {
                    "model_path": "./weights/model.pt",
                    "inference_mode": "pyramid",
                },
            },
        )

        with self.assertRaises(ValueError):
            load_config(config_path)

    def test_load_config_rejects_invalid_temporal_chunk_settings(self) -> None:
        invalid_settings = [
            {"chunk_frames": 0},
            {"chunk_frames": 100, "overlap_frames": 100},
            {"max_workers": 0},
            {"decode_preroll_frames": -1},
            {"output_dir_name": "../chunks"},
            {"merge_strategy": "latest_wins"},
        ]

        for index, temporal_chunks in enumerate(invalid_settings):
            with self.subTest(temporal_chunks=temporal_chunks):
                config_path = self.write_yaml(
                    f"config/invalid_temporal_chunks_{index}.yaml",
                    {
                        "input_video": "./data/input.mp4",
                        "output_dir": "./outputs/run_a",
                        "detector": {
                            "model_path": "./weights/model.pt",
                        },
                        "temporal_chunks": temporal_chunks,
                    },
                )

                with self.assertRaises(ValueError):
                    load_config(config_path)

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
                    "PROVIDER_OPENAI_IMPROVEMENT_MODEL=gpt-improve",
                ]
            ),
            encoding="utf-8",
        )

        with patch.dict(os.environ, {}, clear=True):
            settings = load_provider_settings(self.repo_root)

        self.assertEqual("dotenv-key", settings.api_key)
        self.assertEqual("https://example.invalid/v1", settings.base_url)
        self.assertEqual("gpt-test", settings.chat_model)
        self.assertEqual("gpt-improve", settings.improvement_model)
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
                "PROVIDER_OPENAI_IMPROVEMENT_MODEL": "gpt-env-improve",
            },
            clear=True,
        ):
            settings = load_provider_settings(self.repo_root)

        self.assertEqual("env-key", settings.api_key)
        self.assertEqual("https://override.invalid/v1", settings.base_url)
        self.assertEqual("gpt-env", settings.chat_model)
        self.assertEqual("gpt-env-improve", settings.improvement_model)

    def test_create_json_vision_response_uses_structured_output_schema(self) -> None:
        captured: dict[str, object] = {}

        class FakeHTTPResponse:
            def __enter__(self) -> "FakeHTTPResponse":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self) -> bytes:
                return json.dumps({"output_text": json.dumps({"verdict": "needs_human_review"})}).encode("utf-8")

        def fake_urlopen(request: object, timeout: int) -> FakeHTTPResponse:
            captured["timeout"] = timeout
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            captured["auth"] = request.headers.get("Authorization")
            return FakeHTTPResponse()

        client = OpenAIResponsesClient(OpenAIProviderSettings(api_key="secret", chat_model="gpt-test"))

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = client.create_json_vision_response(
                instructions="Return JSON.",
                prompt="review packet",
                images=[{"label": "contact", "data_url": "data:image/jpeg;base64,abc"}],
                json_schema={
                    "type": "object",
                    "properties": {"verdict": {"type": "string"}},
                    "required": ["verdict"],
                    "additionalProperties": False,
                },
            )

        payload = captured["payload"]
        self.assertEqual({"verdict": "needs_human_review"}, result)
        self.assertEqual("gpt-test", payload["model"])
        self.assertEqual("json_schema", payload["text"]["format"]["type"])
        self.assertTrue(payload["text"]["format"]["strict"])
        self.assertEqual("ai_visual_review", payload["text"]["format"]["name"])
        self.assertEqual("input_image", payload["input"][0]["content"][2]["type"])
        self.assertEqual("Bearer secret", captured["auth"])

    def test_create_json_response_keeps_text_only_payload_shape(self) -> None:
        captured: dict[str, object] = {}

        class FakeHTTPResponse:
            def __enter__(self) -> "FakeHTTPResponse":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self) -> bytes:
                return json.dumps({"output_text": json.dumps({"ok": True})}).encode("utf-8")

        def fake_urlopen(request: object, timeout: int) -> FakeHTTPResponse:
            captured["timeout"] = timeout
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            return FakeHTTPResponse()

        client = OpenAIResponsesClient(OpenAIProviderSettings(api_key="secret", chat_model="gpt-test"))

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = client.create_json_response(instructions="Return JSON.", prompt="plain prompt")

        payload = captured["payload"]
        self.assertEqual({"ok": True}, result)
        self.assertEqual("plain prompt", payload["input"])
        self.assertEqual("json_object", payload["text"]["format"]["type"])
        self.assertNotIsInstance(payload["input"], list)

    def test_create_json_response_accepts_model_override(self) -> None:
        captured: dict[str, object] = {}

        class FakeHTTPResponse:
            def __enter__(self) -> "FakeHTTPResponse":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self) -> bytes:
                return json.dumps({"output_text": json.dumps({"ok": True})}).encode("utf-8")

        def fake_urlopen(request: object, timeout: int) -> FakeHTTPResponse:
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            captured["auth"] = request.headers.get("Authorization")
            return FakeHTTPResponse()

        client = OpenAIResponsesClient(OpenAIProviderSettings(api_key="secret", chat_model="gpt-default"))

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = client.create_json_response(
                instructions="Return JSON.",
                prompt="plain prompt",
                model="gpt-override",
            )

        payload = captured["payload"]
        self.assertEqual({"ok": True}, result)
        self.assertEqual("gpt-override", payload["model"])
        self.assertNotIn("secret", json.dumps(payload))
        self.assertEqual("Bearer secret", captured["auth"])

    def test_provider_http_errors_are_redacted(self) -> None:
        def fake_urlopen(request: object, timeout: int) -> object:
            raise urllib.error.HTTPError(
                url="https://api.openai.com/v1/responses",
                code=400,
                msg="bad request",
                hdrs={},
                fp=io.BytesIO(
                    b'{"error":"plain-secret Bearer sk-secret-token data:image/jpeg;base64,abcdef"}'
                ),
            )

        client = OpenAIResponsesClient(OpenAIProviderSettings(api_key="plain-secret", chat_model="gpt-test"))

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with self.assertRaises(RuntimeError) as context:
                client.create_json_response(instructions="Return JSON.", prompt="plain prompt")

        message = str(context.exception)
        self.assertNotIn("plain-secret", message)
        self.assertNotIn("sk-secret-token", message)
        self.assertNotIn("abcdef", message)
        self.assertIn("<redacted", message)


class YOLOSahiBallDetectorModeTests(unittest.TestCase):
    def make_config(self, inference_mode: str) -> DetectorConfig:
        return DetectorConfig(
            model_path=Path("model.pt"),
            device="cpu",
            use_half=False,
            allowed_labels=["ball"],
            inference_mode=inference_mode,
        )

    def make_detector(self, inference_mode: str) -> YOLOSahiBallDetector:
        detector = YOLOSahiBallDetector.__new__(YOLOSahiBallDetector)
        detector.detector_config = self.make_config(inference_mode)
        detector.sahi_config = SahiConfig()
        detector.model = object()
        detector.direct_model = None
        return detector

    def test_direct_full_frame_init_does_not_load_sahi_dependencies(self) -> None:
        with (
            patch.object(YOLOSahiBallDetector, "_build_model", side_effect=AssertionError("SAHI model should not load")),
            patch.object(
                YOLOSahiBallDetector,
                "_load_sahi_predictor",
                side_effect=AssertionError("SAHI predictor should not load"),
            ),
        ):
            detector = YOLOSahiBallDetector(self.make_config("direct_full_frame"), SahiConfig())

        self.assertIsNone(detector.model)
        self.assertIsNone(detector.get_sliced_prediction)
        self.assertIsNone(detector.direct_model)

    def test_sahi_init_loads_sahi_dependencies(self) -> None:
        with (
            patch.object(YOLOSahiBallDetector, "_build_model", return_value="sahi-model") as build_model,
            patch.object(
                YOLOSahiBallDetector,
                "_load_sahi_predictor",
                return_value="sahi-predictor",
            ) as load_sahi_predictor,
        ):
            detector = YOLOSahiBallDetector(self.make_config("sahi"), SahiConfig())

        build_model.assert_called_once()
        load_sahi_predictor.assert_called_once()
        self.assertEqual("sahi-model", detector.model)
        self.assertEqual("sahi-predictor", detector.get_sliced_prediction)

    def test_detect_uses_sahi_slicing_for_sahi_mode(self) -> None:
        detector = self.make_detector("sahi")
        prediction = SimpleNamespace(
            category=SimpleNamespace(name="ball"),
            bbox=_FakeSahiBox(),
            score=SimpleNamespace(value=0.82),
        )
        detector.get_sliced_prediction = Mock(
            return_value=SimpleNamespace(object_prediction_list=[prediction]),
        )
        detector._get_direct_model = Mock(side_effect=AssertionError("direct model should not be used"))

        candidates = detector.detect(frame=object(), frame_index=7)

        detector.get_sliced_prediction.assert_called_once()
        self.assertEqual(1, len(candidates))
        self.assertEqual(7, candidates[0].frame_index)
        self.assertEqual("yolo_sahi", candidates[0].source)
        self.assertEqual(
            (3.0, 4.0, 11.0, 12.0),
            (candidates[0].x1, candidates[0].y1, candidates[0].x2, candidates[0].y2),
        )

    def test_detect_uses_direct_full_frame_model_without_sahi_slicing(self) -> None:
        detector = self.make_detector("direct_full_frame")
        fake_model = _FakeDirectModel()
        frame = object()
        detector.get_sliced_prediction = Mock(side_effect=AssertionError("SAHI slicing should not be called"))
        detector._get_direct_model = Mock(return_value=fake_model)

        candidates = detector.detect(frame=frame, frame_index=11)

        detector.get_sliced_prediction.assert_not_called()
        detector._get_direct_model.assert_called_once()
        self.assertEqual(1, len(fake_model.calls))
        self.assertIs(frame, fake_model.calls[0]["frame"])
        self.assertEqual(0.15, fake_model.calls[0]["conf"])
        self.assertEqual(1280, fake_model.calls[0]["imgsz"])
        self.assertEqual("cpu", fake_model.calls[0]["device"])
        self.assertFalse(fake_model.calls[0]["half"])
        self.assertEqual(1, len(candidates))
        self.assertEqual(11, candidates[0].frame_index)
        self.assertEqual("yolo_direct", candidates[0].source)
        self.assertEqual(
            (1.0, 2.0, 9.0, 10.0),
            (candidates[0].x1, candidates[0].y1, candidates[0].x2, candidates[0].y2),
        )


if __name__ == "__main__":
    unittest.main()
