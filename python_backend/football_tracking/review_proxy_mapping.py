from __future__ import annotations

import math
from typing import Any

from football_tracking.detector_development_common import canonical_sha256, require_sha256


class ReviewProxyError(ValueError):
    """A review proxy cannot be trusted as exact-frame evidence."""


def build_review_proxy_manifest(
    *,
    source: dict[str, Any],
    proxy: dict[str, Any],
    mappings: list[dict[str, Any]],
    expected_frame_indices: list[int],
    decoder_fingerprint_sha256: str,
    requested_decode_mode: str,
    effective_decode_mode: str,
    map_time_tolerance_msec: float,
    declared_offset_msec: float,
) -> dict[str, Any]:
    """Validate a complete source-to-proxy frame/PTS map and bind its hashes."""

    source_binding = _media_binding(source, "source", require_identity=True)
    proxy_binding = _media_binding(proxy, "proxy", require_identity=False)
    require_sha256(decoder_fingerprint_sha256, "decoder fingerprint sha256")
    if requested_decode_mode not in {"sequential", "preroll", "direct"}:
        raise ReviewProxyError("requested decode mode is invalid")
    if effective_decode_mode not in {"sequential", "preroll_verified", "direct_verified", "sequential_fallback"}:
        raise ReviewProxyError("effective decode mode is invalid")
    if proxy_binding["frame_count"] != source_binding["frame_count"]:
        raise ReviewProxyError("review proxy decoder shortfall is not allowed")
    if abs(proxy_binding["fps"] - source_binding["fps"]) > 1e-6:
        raise ReviewProxyError("review proxy frame-rate mapping mismatch")
    time_tolerance_msec = _finite_nonnegative(map_time_tolerance_msec, "review proxy time tolerance")
    normalized_declared_offset_msec = _finite_decoder_pos_msec(
        declared_offset_msec, "review proxy declared time offset"
    )
    if not isinstance(mappings, list) or not mappings:
        raise ReviewProxyError("review proxy requires at least one exact-frame mapping")
    if (
        not isinstance(expected_frame_indices, list)
        or not expected_frame_indices
        or expected_frame_indices != sorted(set(expected_frame_indices))
        or any(
            isinstance(frame_index, bool)
            or not isinstance(frame_index, int)
            or not 0 <= frame_index < source_binding["frame_count"]
            for frame_index in expected_frame_indices
        )
    ):
        raise ReviewProxyError("review proxy expected frozen frame set is invalid")
    scale_x = proxy_binding["width"] / source_binding["width"]
    scale_y = proxy_binding["height"] / source_binding["height"]
    if abs(scale_x - scale_y) > 1e-9:
        raise ReviewProxyError("review proxy coordinate transform is distorted")

    normalized: list[dict[str, Any]] = []
    seen: set[int] = set()
    for mapping in mappings:
        if not isinstance(mapping, dict):
            raise ReviewProxyError("review proxy mapping must be an object")
        source_index = _frame_index(mapping.get("source_frame_index"), source_binding["frame_count"], "source")
        proxy_index = _frame_index(mapping.get("proxy_frame_index"), proxy_binding["frame_count"], "proxy")
        if source_index in seen:
            raise ReviewProxyError("review proxy contains a duplicate source mapping")
        seen.add(source_index)
        source_timing_status = mapping.get("source_timing_status")
        raw_source_pos_msec = mapping.get("source_decoder_pos_msec")
        if raw_source_pos_msec is None:
            if source_timing_status != "not_collected":
                raise ReviewProxyError("review proxy missing source timing must be explicit")
            source_pos_msec = None
        else:
            if source_timing_status not in {None, "observed"}:
                raise ReviewProxyError("review proxy source timing status is invalid")
            source_timing_status = "observed"
            source_pos_msec = _finite_decoder_pos_msec(raw_source_pos_msec, "source decoder POS_MSEC")
        if mapping.get("proxy_timing_basis") != "verified_cfr_frame_index_time_v1":
            raise ReviewProxyError("review proxy timing basis is invalid")
        proxy_pos_msec = _finite_decoder_pos_msec(mapping.get("proxy_cfr_time_msec"), "proxy verified CFR time")
        if (
            source_index != proxy_index
            or (
                source_pos_msec is not None
                and abs((proxy_pos_msec - source_pos_msec) - normalized_declared_offset_msec) > time_tolerance_msec
            )
            or (
                source_pos_msec is None
                and abs(proxy_pos_msec - proxy_index / proxy_binding["fps"] * 1000.0) > time_tolerance_msec
            )
        ):
            raise ReviewProxyError("review proxy source-to-proxy mapping mismatch")
        source_frame_sha = require_sha256(mapping.get("source_frame_sha256"), "source frame sha256")
        proxy_frame_sha = require_sha256(mapping.get("proxy_frame_sha256"), "proxy frame sha256")
        integrity = mapping.get("media_integrity")
        if (
            not isinstance(integrity, dict)
            or integrity.get("status") != "ok"
            or integrity.get("gray") is not False
            or integrity.get("low_information") is not False
            or integrity.get("likely_corrupt") is not False
        ):
            raise ReviewProxyError("review proxy frame integrity failed")
        normalized.append(
            {
                "source_frame_index": source_index,
                "source_timing_status": source_timing_status,
                "source_decoder_pos_msec": source_pos_msec,
                "proxy_frame_index": proxy_index,
                "proxy_timing_basis": "verified_cfr_frame_index_time_v1",
                "proxy_cfr_time_msec": proxy_pos_msec,
                "source_frame_sha256": source_frame_sha,
                "proxy_frame_sha256": proxy_frame_sha,
                "media_integrity": {
                    "status": "ok",
                    "gray": False,
                    "low_information": False,
                    "likely_corrupt": False,
                },
            }
        )
    normalized.sort(key=lambda item: item["source_frame_index"])
    if [item["source_frame_index"] for item in normalized] != expected_frame_indices:
        raise ReviewProxyError("review proxy map does not match the exact frozen frame set")
    # Source decoder timestamps may be the defect that requires this proxy;
    # exact frame hashes/indices and the bounded map preserve their evidence.
    proxy_times = [item["proxy_cfr_time_msec"] for item in normalized]
    if any(current <= previous for previous, current in zip(proxy_times, proxy_times[1:])):
        raise ReviewProxyError("review proxy decoder time must strictly increase across mapped frames")
    mapping_sha256 = canonical_sha256(normalized)
    integrity_report = [{"frame_index": item["source_frame_index"], **item["media_integrity"]} for item in normalized]
    manifest: dict[str, Any] = {
        "schema_version": "1.0",
        "artifact_type": "ball_review_proxy",
        "source": source_binding,
        "proxy": proxy_binding,
        "decoder_fingerprint_sha256": decoder_fingerprint_sha256,
        "requested_decode_mode": requested_decode_mode,
        "effective_decode_mode": effective_decode_mode,
        "map_time_tolerance_msec": time_tolerance_msec,
        "declared_offset_msec": normalized_declared_offset_msec,
        "coordinate_transform": {
            "kind": "uniform_source_to_proxy_scale_v1",
            "scale_x": scale_x,
            "scale_y": scale_y,
            "source_origin": [0.0, 0.0],
            "proxy_origin": [0.0, 0.0],
        },
        "expected_frame_indices": list(expected_frame_indices),
        "mappings": normalized,
        "mapping_sha256": mapping_sha256,
        "integrity_report_sha256": canonical_sha256(integrity_report),
    }
    manifest["manifest_sha256"] = canonical_sha256(manifest)
    return manifest


def validate_review_proxy_manifest(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReviewProxyError("review proxy manifest must be an object")
    expected_fields = {
        "schema_version",
        "artifact_type",
        "source",
        "proxy",
        "decoder_fingerprint_sha256",
        "requested_decode_mode",
        "effective_decode_mode",
        "map_time_tolerance_msec",
        "declared_offset_msec",
        "coordinate_transform",
        "expected_frame_indices",
        "mappings",
        "mapping_sha256",
        "integrity_report_sha256",
        "manifest_sha256",
    }
    if set(value) != expected_fields:
        raise ReviewProxyError("review proxy manifest fields are invalid")
    if value.get("schema_version") != "1.0" or value.get("artifact_type") != "ball_review_proxy":
        raise ReviewProxyError("review proxy manifest type is invalid")
    rebuilt = build_review_proxy_manifest(
        source=value.get("source"),
        proxy=value.get("proxy"),
        mappings=value.get("mappings"),
        expected_frame_indices=value.get("expected_frame_indices"),
        decoder_fingerprint_sha256=value.get("decoder_fingerprint_sha256"),
        requested_decode_mode=value.get("requested_decode_mode"),
        effective_decode_mode=value.get("effective_decode_mode"),
        map_time_tolerance_msec=value.get("map_time_tolerance_msec"),
        declared_offset_msec=value.get("declared_offset_msec"),
    )
    if value != rebuilt:
        raise ReviewProxyError("review proxy manifest is not canonical")
    return rebuilt


def _media_binding(value: Any, label: str, *, require_identity: bool) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReviewProxyError(f"{label} binding must be an object")
    result: dict[str, Any] = {
        "sha256": require_sha256(value.get("sha256"), f"{label} sha256"),
        "size_bytes": _positive_int(value.get("size_bytes"), f"{label} size"),
        "width": _positive_int(value.get("width"), f"{label} width"),
        "height": _positive_int(value.get("height"), f"{label} height"),
        "fps": _finite_positive(value.get("fps"), f"{label} fps"),
        "frame_count": _positive_int(value.get("frame_count"), f"{label} frame_count"),
        "codec": value.get("codec"),
    }
    if not isinstance(result["codec"], str) or not result["codec"]:
        raise ReviewProxyError(f"{label} codec is required")
    if require_identity:
        result["file_identity_sha256"] = require_sha256(
            value.get("file_identity_sha256"), f"{label} file identity sha256"
        )
    return result


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ReviewProxyError(f"{label} must be a positive integer")
    return value


def _frame_index(value: Any, frame_count: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < frame_count:
        raise ReviewProxyError(f"{label} frame index is invalid")
    return value


def _finite_positive(value: Any, label: str) -> float:
    number = _finite_nonnegative(value, label)
    if number <= 0:
        raise ReviewProxyError(f"{label} must be positive")
    return number


def _finite_nonnegative(value: Any, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        raise ReviewProxyError(f"{label} must be finite and non-negative")
    return float(value)


def _finite_decoder_pos_msec(value: Any, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or abs(float(value)) > 7 * 24 * 60 * 60 * 1000.0
    ):
        raise ReviewProxyError(f"{label} must be finite and bounded")
    return float(value)


__all__ = [
    "ReviewProxyError",
    "build_review_proxy_manifest",
    "validate_review_proxy_manifest",
]
