from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

import cv2
import numpy as np

QualityStatus = Literal["pass", "warn", "fail"]

PASS_THRESHOLD = 0.75
WARN_THRESHOLD = 0.45


def status_for_score(score: float) -> QualityStatus:
    if score >= PASS_THRESHOLD:
        return "pass"
    if score >= WARN_THRESHOLD:
        return "warn"
    return "fail"


def assess_video_quality(
    *,
    input_video: str,
    samples: Sequence[dict[str, Any]],
    field_coverages: Sequence[float],
    calibration_confidence: str | None,
) -> dict[str, Any]:
    if not samples:
        raise RuntimeError(f"Unable to read quality-check frames from input video: {input_video}")

    frames = [sample["frame"] for sample in samples]
    middle_sample = samples[len(samples) // 2]
    checks = [
        _brightness_check(frames),
        _blur_check(frames),
        _field_visibility_check(field_coverages),
        _camera_stability_check(frames),
        _calibration_check(calibration_confidence),
    ]
    overall_score = round(sum(float(check["score"]) for check in checks) / len(checks), 3)
    return {
        "input_video": input_video,
        "frame_width": int(middle_sample["frame_width"]),
        "frame_height": int(middle_sample["frame_height"]),
        "sample_count": len(samples),
        "overall_score": overall_score,
        "overall_status": overall_status_for_checks(checks, overall_score),
        "checks": checks,
        "recommendations": _recommendations(checks),
    }


def _brightness_check(frames: Sequence[Any]) -> dict[str, Any]:
    luma_values = [_mean_luma(frame) for frame in frames]
    value = _median(luma_values)
    score = _clamp_score((value - 30.0) / 50.0)
    return _check(
        key="brightness",
        label="Brightness",
        score=score,
        value=round(value, 1),
        unit="luma",
        guidance={
            "pass": "Lighting should be adequate for small-ball contrast.",
            "warn": "Add light or avoid underexposed footage.",
            "fail": "The sample is very dark; add light or use a brighter recording.",
        },
    )


def _blur_check(frames: Sequence[Any]) -> dict[str, Any]:
    blur_values = [_laplacian_variance(frame) for frame in frames]
    value = _median(blur_values)
    score = _clamp_score((value - 25.0) / 175.0)
    return _check(
        key="blur",
        label="Focus",
        score=score,
        value=round(value, 1),
        unit="laplacian_var",
        guidance={
            "pass": "Edges look sharp enough for small-ball tracking.",
            "warn": "Refocus before recording or use a sharper source clip.",
            "fail": "The sample is too soft; refocus or use a sharper clip.",
        },
    )


def _field_visibility_check(field_coverages: Sequence[float]) -> dict[str, Any]:
    coverage = _median([max(0.0, min(1.0, float(value))) for value in field_coverages])
    score = _clamp_score(coverage / 0.35)
    return _check(
        key="field_visibility",
        label="Field visibility",
        score=score,
        value=round(coverage * 100.0, 1),
        unit="%",
        guidance={
            "pass": "The field is visible enough for scene guidance.",
            "warn": "Keep more of the pitch visible in the frame.",
            "fail": "The field is hard to see; keep the pitch in view.",
        },
    )


def _camera_stability_check(frames: Sequence[Any]) -> dict[str, Any]:
    if len(frames) < 2:
        return _check(
            key="camera_stability",
            label="Camera stability",
            score=1.0,
            value=0.0,
            unit="px/sample",
            guidance={
                "pass": "Sampled frames do not show large camera jumps.",
                "warn": "Use a steadier tripod or reduce fast pans.",
                "fail": "The camera shifts heavily between samples; stabilize the recording.",
            },
        )

    shifts = [_frame_shift_pixels(first, second) for first, second in zip(frames, frames[1:])]
    value = _median(shifts)
    frame_height, frame_width = frames[0].shape[:2]
    fail_shift = max(12.0, min(float(frame_width), float(frame_height)) * 0.10)
    score = _clamp_score(1.0 - (value / fail_shift))
    return _check(
        key="camera_stability",
        label="Camera stability",
        score=score,
        value=round(value, 1),
        unit="px/sample",
        guidance={
            "pass": "Sampled frames do not show large camera jumps.",
            "warn": "Use a steadier tripod or reduce fast pans.",
            "fail": "The camera shifts heavily between samples; stabilize the recording.",
        },
    )


def _calibration_check(calibration_confidence: str | None) -> dict[str, Any]:
    normalized = calibration_confidence or "missing"
    scores = {
        "config": 1.0,
        "estimated": 0.75,
        "low": 0.45,
        "missing": 0.0,
    }
    return _check(
        key="calibration",
        label="Calibration",
        score=scores.get(normalized, 0.0),
        value=normalized,
        unit="confidence",
        guidance={
            "pass": "Field calibration is available for operator review.",
            "warn": "Review or add a field polygon before long runs.",
            "fail": "Add a field polygon or run field suggestion first.",
        },
    )


def _check(
    *,
    key: str,
    label: str,
    score: float,
    value: Any,
    unit: str,
    guidance: dict[QualityStatus, str],
) -> dict[str, Any]:
    rounded_score = round(_clamp_score(score), 3)
    status = status_for_score(rounded_score)
    return {
        "key": key,
        "label": label,
        "score": rounded_score,
        "status": status,
        "value": value,
        "unit": unit,
        "guidance": guidance[status],
    }


def _recommendations(checks: Sequence[dict[str, Any]]) -> list[str]:
    actions_by_key = {
        "brightness": "Add light or use a brighter recording.",
        "blur": "Refocus before recording or use a sharper clip.",
        "field_visibility": "Keep more of the pitch visible in the frame.",
        "camera_stability": "Stabilize the camera or reduce fast pans.",
        "calibration": "Review the field polygon before a long run.",
    }
    recommendations: list[str] = []
    for check in checks:
        if check["status"] == "pass":
            continue
        action = actions_by_key.get(str(check["key"]))
        if action and action not in recommendations:
            recommendations.append(action)
    return recommendations or ["Proceed with a normal tracking run."]


def overall_status_for_checks(checks: Sequence[dict[str, Any]], overall_score: float) -> QualityStatus:
    statuses = {str(check.get("status")) for check in checks}
    if "fail" in statuses:
        return "fail"
    if "warn" in statuses:
        return "warn"
    return status_for_score(overall_score)


def _mean_luma(frame: Any) -> float:
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return float(np.mean(gray))


def _laplacian_variance(frame: Any) -> float:
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def _frame_shift_pixels(first: Any, second: Any) -> float:
    first_gray, first_scale = _phase_frame(first)
    second_gray, _ = _phase_frame(second)
    shift, _response = cv2.phaseCorrelate(first_gray, second_gray)
    return float((shift[0] ** 2 + shift[1] ** 2) ** 0.5 * first_scale)


def _phase_frame(frame: Any) -> tuple[Any, float]:
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    height, width = gray.shape[:2]
    target_width = min(320, width)
    if width <= target_width:
        return gray.astype(np.float32), 1.0
    scale = width / float(target_width)
    target_height = max(1, int(round(height / scale)))
    resized = cv2.resize(gray, (target_width, target_height), interpolation=cv2.INTER_AREA)
    return resized.astype(np.float32), scale


def _median(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    return float(np.median(np.array(values, dtype=np.float64)))


def _clamp_score(value: float) -> float:
    return max(0.0, min(1.0, float(value)))
