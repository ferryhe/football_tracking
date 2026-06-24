from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from football_tracking.final_artifact_manifest import FINAL_ARTIFACT_MANIFEST_NAME
from football_tracking.media_integrity import inspect_frame

MEDIA_TYPES = {"video", "clip"}
REVIEW_MEDIA_ARTIFACTS = {
    "review_packets": "review_packets.json",
    "ai_visual_localization": "ai_visual_localization.json",
}
STATUS_RANK = {"pass": 0, "warn": 1, "unavailable": 2, "fail": 3}


def evaluate_stable_final_outputs(
    output_dir: Path,
    *,
    mode: str = "artifact-only",
    final_manifest_artifact: dict[str, Any] | None = None,
    review_packets_artifact: dict[str, Any] | None = None,
    ai_visual_localization_artifact: dict[str, Any] | None = None,
    max_samples: int = 5,
) -> dict[str, Any]:
    if mode not in {"dry-run", "artifact-only", "real"}:
        raise ValueError("mode must be one of dry-run, artifact-only, real")

    output_dir = Path(output_dir)
    final_manifest_artifact = final_manifest_artifact or _load_artifact(output_dir / FINAL_ARTIFACT_MANIFEST_NAME)
    review_media = _review_media_evidence(
        {
            "review_packets": review_packets_artifact,
            "ai_visual_localization": ai_visual_localization_artifact,
        },
        output_dir=output_dir,
        mode=mode,
    )
    if final_manifest_artifact["status"] != "loaded":
        return _check(
            "fail" if mode == "real" else "unavailable",
            reason=f"{FINAL_ARTIFACT_MANIFEST_NAME} missing or unreadable",
            selected_media_count=0,
            artifacts=[],
            review_media=review_media,
        )

    selected = final_manifest_artifact["payload"].get("final_selected_artifacts")
    if not isinstance(selected, list) or not selected:
        return _check(
            "fail" if mode == "real" else "unavailable",
            reason="final_selected_artifacts is missing or empty",
            selected_media_count=0,
            artifacts=[],
            review_media=review_media,
        )

    media_items = [item for item in selected if isinstance(item, dict) and _artifact_type(item) in MEDIA_TYPES]
    if not media_items:
        track_only_status = _non_media_selection_status(review_media["status"])
        return _check(
            track_only_status,
            reason="final_selected_artifacts contains no video or clip artifacts",
            selected_media_count=0,
            artifacts=[],
            review_media=review_media,
        )

    artifacts = [_inspect_selected_media(output_dir, item, max_samples=max_samples) for item in media_items]
    statuses = [artifact["status"] for artifact in artifacts]
    review_status = _review_media_status_for_gate(review_media["status"], mode=mode)
    if review_status in {"unavailable", "warn", "fail"}:
        statuses.append(review_status)
    return _check(
        _worst_status(statuses),
        selected_media_count=len(media_items),
        artifacts=artifacts,
        review_media=review_media,
    )


def _inspect_selected_media(output_dir: Path, item: dict[str, Any], *, max_samples: int) -> dict[str, Any]:
    path_text = item.get("path")
    media_type = _artifact_type(item)
    evidence = {
        "candidate_id": item.get("candidate_id") or item.get("id"),
        "type": media_type,
        "path": path_text,
        "resolved_path": None,
        "status": "fail",
        "sample_count": 0,
        "dimensions": {"width": 0, "height": 0},
        "frame_count": None,
        "fps": None,
        "low_information_sample_count": 0,
        "gray_sample_count": 0,
        "sampled_frames": [],
    }
    if not isinstance(path_text, str) or not path_text.strip():
        return {**evidence, "reason": "missing_path"}
    resolved, path_error = _resolve_output_path(output_dir, path_text.strip())
    if path_error is not None or resolved is None:
        return {**evidence, "reason": path_error or "unsafe_path"}
    evidence["resolved_path"] = str(resolved)
    if not resolved.exists() or resolved.stat().st_size <= 0:
        return {**evidence, "reason": "missing"}

    import cv2

    capture = cv2.VideoCapture(str(resolved))
    try:
        if not capture.isOpened():
            return {**evidence, "reason": "unreadable"}
        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        frame_count = _optional_positive_int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
        fps = _optional_positive_float(capture.get(cv2.CAP_PROP_FPS))
        evidence["dimensions"] = {"width": width, "height": height}
        evidence["frame_count"] = frame_count
        evidence["fps"] = round(fps, 3) if fps is not None else None
        if width <= 0 or height <= 0:
            return {**evidence, "reason": "invalid_dimensions"}

        sample_results = _sample_frames(capture, frame_count=frame_count, max_samples=max_samples)
    finally:
        capture.release()

    evidence["sampled_frames"] = sample_results
    evidence["sample_count"] = sum(1 for sample in sample_results if sample["decoded"])
    evidence["low_information_sample_count"] = sum(
        1 for sample in sample_results if sample["decoded"] and sample["low_information"]
    )
    evidence["gray_sample_count"] = sum(1 for sample in sample_results if sample["decoded"] and sample["gray"])
    if not sample_results or evidence["sample_count"] <= 0:
        return {**evidence, "reason": "no_decodable_frames"}
    if any(not sample["decoded"] for sample in sample_results):
        return {**evidence, "status": "warn", "reason": "some_sample_decodes_failed"}
    if (
        evidence["low_information_sample_count"] == evidence["sample_count"]
        or evidence["gray_sample_count"] == evidence["sample_count"]
    ):
        return {**evidence, "status": "fail", "reason": "all_samples_low_information"}
    if evidence["low_information_sample_count"] or evidence["gray_sample_count"]:
        return {**evidence, "status": "warn", "reason": "some_samples_low_information"}
    return {**evidence, "status": "pass"}


def _sample_frames(capture: Any, *, frame_count: int | None, max_samples: int) -> list[dict[str, Any]]:
    import cv2

    if frame_count is not None and frame_count > 0:
        samples: list[dict[str, Any]] = []
        for index in _sample_indexes(frame_count, max_samples=max_samples):
            capture.set(cv2.CAP_PROP_POS_FRAMES, index)
            ok, frame = capture.read()
            if not ok and index > 0:
                capture.set(cv2.CAP_PROP_POS_FRAMES, max(0, index - 1))
                ok, frame = capture.read()
            samples.append(_frame_sample(index, frame if ok else None))
        if samples and all(sample["decoded"] for sample in samples):
            return samples
        capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
        sequential_samples: list[dict[str, Any]] = []
        for index in range(max(1, max_samples)):
            ok, frame = capture.read()
            if not ok:
                break
            sequential_samples.append(_frame_sample(index, frame))
        expected_count = min(max(1, max_samples), frame_count)
        if len(sequential_samples) >= expected_count:
            return sequential_samples
        return samples

    samples = []
    for index in range(max(1, max_samples)):
        ok, frame = capture.read()
        if not ok:
            break
        samples.append(_frame_sample(index, frame))
    return samples


def _frame_sample(index: int, frame: Any | None) -> dict[str, Any]:
    if frame is None or getattr(frame, "size", 0) == 0:
        return {"frame_index": index, "decoded": False, "gray": False, "low_information": False, "reasons": ["decode_failed"]}
    metrics = _frame_information_metrics(frame)
    return {"frame_index": index, "decoded": True, **metrics}


def _frame_information_metrics(frame: Any) -> dict[str, Any]:
    metrics = inspect_frame(frame)
    return {
        key: metrics[key]
        for key in ("gray", "low_information", "std_luma", "texture_tile_ratio", "dominant_color_ratio", "reasons")
    }


def _sample_indexes(frame_count: int, *, max_samples: int) -> list[int]:
    sample_count = min(max(1, max_samples), frame_count)
    if sample_count == 1:
        return [0]
    indexes = {
        int(round(position * (frame_count - 1) / (sample_count - 1)))
        for position in range(sample_count)
    }
    return sorted(indexes)


def _review_media_evidence(artifacts: dict[str, dict[str, Any] | None], *, output_dir: Path, mode: str) -> dict[str, Any]:
    sources: list[dict[str, Any]] = []
    statuses: list[str] = []
    for key, file_name in REVIEW_MEDIA_ARTIFACTS.items():
        artifact = artifacts.get(key) or _load_artifact(output_dir / file_name)
        if artifact["status"] == "missing":
            continue
        source = {
            "artifact": file_name,
            "artifact_status": artifact["status"],
            "status": "unavailable",
            "media_integrity": None,
            "review_source": None,
        }
        if artifact["status"] == "loaded":
            payload = artifact["payload"]
            integrity = payload.get("media_integrity")
            if not isinstance(integrity, dict):
                summary = payload.get("summary")
                integrity = summary.get("media_integrity") if isinstance(summary, dict) else None
            source["media_integrity"] = integrity if isinstance(integrity, dict) else None
            source["review_source"] = payload.get("review_source") if isinstance(payload.get("review_source"), dict) else None
            source["status"] = _media_integrity_status(source["media_integrity"])
        statuses.append(str(source["status"]))
        sources.append(source)
    return {
        "status": _worst_status(statuses) if statuses else "unavailable",
        "sources": sources,
    }


def _media_integrity_status(media_integrity: Any) -> str:
    if not isinstance(media_integrity, dict):
        return "unavailable"
    if _positive_count(media_integrity.get("likely_corrupt_image_count")):
        return "fail"
    if (
        _positive_count(media_integrity.get("low_information_image_count"))
        or _positive_count(media_integrity.get("gray_image_count"))
    ):
        return "warn"
    status = str(media_integrity.get("status") or "").casefold()
    if status in {"fail", "failed", "error"}:
        return "fail"
    if status in {"warn", "warning"}:
        return "warn"
    return "pass"


def _review_media_status_for_gate(status: Any, *, mode: str) -> str:
    _ = mode
    return str(status or "pass")


def _non_media_selection_status(review_media_status: Any) -> str:
    status = str(review_media_status or "pass")
    if status == "fail":
        return "warn"
    if status == "warn":
        return "warn"
    return "unavailable"


def _artifact_type(item: dict[str, Any]) -> str:
    return str(item.get("type") or item.get("media_type") or "").strip().casefold()


def _resolve_output_path(output_dir: Path, path_text: str) -> tuple[Path | None, str | None]:
    raw = path_text.replace("\\", "/").strip()
    if not raw:
        return None, "missing_path"
    path = Path(raw)
    if ":" in raw and not path.is_absolute():
        return None, "unsafe_path"
    if ".." in path.parts:
        return None, "unsafe_path"
    output_root = Path(output_dir).resolve()
    resolved = path.resolve() if path.is_absolute() else (output_root / path).resolve()
    if not _is_relative_to(resolved, output_root):
        return None, "unsafe_path"
    return resolved, None


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _load_artifact(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"status": "missing", "payload": {}, "path": str(path)}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return {"status": "corrupt", "payload": {}, "path": str(path), "error": str(exc)}
    if not isinstance(loaded, dict):
        return {"status": "invalid", "payload": {}, "path": str(path)}
    return {"status": "loaded", "payload": loaded, "path": str(path)}


def _optional_positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)) and value > 0:
        return int(round(value))
    return None


def _optional_positive_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)) and value > 0:
        return float(value)
    return None


def _positive_count(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return value > 0
    return False


def _worst_status(statuses: list[Any]) -> str:
    valid = [status for status in statuses if status in STATUS_RANK]
    if not valid:
        return "pass"
    return max(valid, key=lambda status: STATUS_RANK[status])


def _check(status: str, **kwargs: Any) -> dict[str, Any]:
    return {"status": status, **kwargs}
