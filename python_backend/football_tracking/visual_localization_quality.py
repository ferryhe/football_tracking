from __future__ import annotations

from typing import Any

BAD_MEDIA_WARNING_SUFFIXES = ("_failed", "_unreadable", "_low_information")
BAD_MEDIA_INTEGRITY_STATUSES = {"warn", "fail", "corrupt", "low_information"}


def visual_localization_items(report: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for key in ("requests", "localizations", "reviews"):
        value = report.get(key)
        if isinstance(value, list):
            items.extend(item for item in value if isinstance(item, dict))
    return items


def visual_localization_sources(item: dict[str, Any]) -> list[dict[str, Any]]:
    sources = [item]
    for key in ("localization", "review", "provenance"):
        value = item.get(key)
        if isinstance(value, dict):
            sources.append(value)
    frames = item.get("frames")
    if isinstance(frames, list):
        sources.extend(frame for frame in frames if isinstance(frame, dict))
    return sources


def visual_localization_has_clean_executable_evidence(item: dict[str, Any]) -> bool:
    return _visual_localization_media_is_clean(item) and _visual_localization_has_useful_evidence(item)


def _visual_localization_media_is_clean(item: dict[str, Any]) -> bool:
    for source in visual_localization_sources(item):
        warnings = source.get("media_warnings")
        if isinstance(warnings, list):
            if any(_visual_localization_media_warning_is_bad(warning) for warning in warnings):
                return False
        elif _visual_localization_media_warning_is_bad(warnings):
            return False
        if not _media_integrity_is_clean(source.get("media_integrity")):
            return False
    return True


def _visual_localization_media_warning_is_bad(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    return value.strip().casefold().endswith(BAD_MEDIA_WARNING_SUFFIXES)


def _media_integrity_is_clean(value: Any) -> bool:
    if value in (None, "", {}):
        return True
    if isinstance(value, str):
        return value.strip().casefold() not in BAD_MEDIA_INTEGRITY_STATUSES
    if not isinstance(value, dict):
        return True
    status = str(value.get("status") or "").strip().casefold()
    if status in BAD_MEDIA_INTEGRITY_STATUSES:
        return False
    for key in ("likely_corrupt", "low_information", "gray"):
        if value.get(key) is True:
            return False
    for key in (
        "likely_corrupt_image_count",
        "corrupt_image_count",
        "low_information_image_count",
        "gray_image_count",
        "failed_image_count",
        "unreadable_image_count",
    ):
        count = _optional_int(value.get(key))
        if count is not None and count > 0:
            return False
    for nested in value.values():
        if isinstance(nested, dict) and not _media_integrity_is_clean(nested):
            return False
    return True


def _visual_localization_has_useful_evidence(item: dict[str, Any]) -> bool:
    for source in visual_localization_sources(item):
        if _visual_localization_has_accepted_roi(source):
            return True
        if source.get("ball_visible") is True and str(source.get("status") or "").strip().casefold() == "localized":
            return True
    return False


def _visual_localization_has_accepted_roi(source: dict[str, Any]) -> bool:
    roi = source.get("local_search_roi")
    if not _has_valid_local_search_roi(roi):
        return False
    roi_status = source.get("roi_status")
    return not isinstance(roi_status, str) or roi_status.strip().casefold() == "accepted"


def _has_valid_local_search_roi(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    required = ("coordinate_space", "frame", "x", "y", "width", "height", "confidence")
    if any(key not in value for key in required):
        return False
    if str(value.get("coordinate_space") or "").strip().casefold() not in {"image", "original"}:
        return False
    if _optional_int(value.get("frame")) is None or _optional_int(value.get("frame")) < 0:
        return False
    x = _optional_float(value.get("x"))
    y = _optional_float(value.get("y"))
    width = _optional_float(value.get("width"))
    height = _optional_float(value.get("height"))
    confidence = _optional_float(value.get("confidence"))
    if x is None or y is None or x < 0 or y < 0:
        return False
    if width is None or height is None or width <= 0 or height <= 0:
        return False
    return confidence is not None and 0.0 <= confidence <= 1.0


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _optional_float(value: Any) -> float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None
