from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable, Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
PYTHON_BACKEND_ROOT = REPO_ROOT / "python_backend"
sys.path.insert(0, str(PYTHON_BACKEND_ROOT))

PINNED_MODEL_IDS = (
    "current-coco-yolov8n",
    "official-coco-yolo11n",
    "official-coco-yolo11s",
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run bounded direct/SAHI load observations for exact built-in detector weights. "
            "Arbitrary model paths are not accepted."
        )
    )
    parser.add_argument(
        "model_ids",
        nargs="+",
        choices=PINNED_MODEL_IDS,
        help="One or more fixed built-in model IDs.",
    )
    return parser.parse_args(argv)


def observe_models(
    model_ids: Sequence[str],
    *,
    observer: Callable[[Path, str], dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    if not model_ids or any(model_id not in PINNED_MODEL_IDS for model_id in model_ids):
        raise ValueError("runtime observation accepts fixed built-in model IDs only")
    if len(set(model_ids)) != len(model_ids):
        raise ValueError("runtime observation model IDs must be unique")
    if observer is None:
        from football_tracking.detector_model_registry import observe_pinned_model_runtime

        observer = observe_pinned_model_runtime
    return [observer(PYTHON_BACKEND_ROOT, model_id) for model_id in model_ids]


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    for observation in observe_models(args.model_ids):
        print(json.dumps(observation, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
