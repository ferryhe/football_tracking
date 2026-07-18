from __future__ import annotations

import unittest
from pathlib import Path

from scripts.observe_detector_model_runtime import PINNED_MODEL_IDS, observe_models, parse_args


class ObserveDetectorModelRuntimeTests(unittest.TestCase):
    def test_runtime_observation_cli_exposes_only_the_fixed_initial_catalog(self) -> None:
        self.assertEqual(
            (
                "current-coco-yolov8n",
                "official-coco-yolo11n",
                "official-coco-yolo11s",
            ),
            PINNED_MODEL_IDS,
        )
        with self.assertRaises(SystemExit):
            parse_args(["https://attacker.invalid/model.pt"])

    def test_runtime_observation_dispatches_exact_unique_ids(self) -> None:
        calls: list[tuple[Path, str]] = []

        def observer(root: Path, model_id: str) -> dict[str, object]:
            calls.append((root, model_id))
            return {
                "model_id": model_id,
                "direct_load_passed": True,
                "sahi_load_passed": True,
            }

        results = observe_models(PINNED_MODEL_IDS, observer=observer)

        self.assertEqual(list(PINNED_MODEL_IDS), [result["model_id"] for result in results])
        self.assertTrue(all(root.name == "python_backend" for root, _ in calls))

    def test_runtime_observation_rejects_empty_unknown_or_duplicate_ids(self) -> None:
        cases = ([], ["unknown"], [PINNED_MODEL_IDS[0], PINNED_MODEL_IDS[0]])
        for model_ids in cases:
            with self.subTest(model_ids=model_ids), self.assertRaises(ValueError):
                observe_models(model_ids, observer=lambda _root, _model_id: {})


if __name__ == "__main__":
    unittest.main()
