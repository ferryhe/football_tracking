from __future__ import annotations

from pathlib import Path

import pytest

from scripts.observe_detector_model_runtime import PINNED_MODEL_IDS, observe_models, parse_args


def test_runtime_observation_cli_exposes_only_the_fixed_initial_catalog() -> None:
    assert PINNED_MODEL_IDS == (
        "current-coco-yolov8n",
        "official-coco-yolo11n",
        "official-coco-yolo11s",
    )
    with pytest.raises(SystemExit):
        parse_args(["https://attacker.invalid/model.pt"])


def test_runtime_observation_dispatches_exact_unique_ids() -> None:
    calls: list[tuple[Path, str]] = []

    def observer(root: Path, model_id: str) -> dict[str, object]:
        calls.append((root, model_id))
        return {"model_id": model_id, "direct_load_passed": True, "sahi_load_passed": True}

    results = observe_models(PINNED_MODEL_IDS, observer=observer)

    assert [result["model_id"] for result in results] == list(PINNED_MODEL_IDS)
    assert all(root.name == "python_backend" for root, _ in calls)


@pytest.mark.parametrize("model_ids", [[], ["unknown"], [PINNED_MODEL_IDS[0], PINNED_MODEL_IDS[0]]])
def test_runtime_observation_rejects_empty_unknown_or_duplicate_ids(model_ids: list[str]) -> None:
    with pytest.raises(ValueError):
        observe_models(model_ids, observer=lambda _root, _model_id: {})
