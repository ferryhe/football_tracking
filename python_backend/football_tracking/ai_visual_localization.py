from __future__ import annotations

import base64
import hashlib
import json
import math
import mimetypes
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0"
REPORT_NAME = "ai_visual_localization.json"
DEFAULT_SAMPLE_COUNT = 5

AI_VISUAL_LOCALIZATION_INSTRUCTIONS = (
    "You are a conservative football ball localization agent. Inspect the full contact sheet and labeled crop sheet. "
    "Return local_search_roi only when the real match ball is visible and the ROI fits inside the decoded source image. "
    "If evidence is ambiguous or the ball is not visible, set ball_visible=false and local_search_roi=null. "
    "Use coverage.covered_subwindows only for frame ranges supported by visual evidence."
)

AI_VISUAL_LOCALIZATION_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reason": {"type": "string"},
        "frames": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "frame": {"type": "integer", "minimum": 0},
                    "ball_visible": {"type": "boolean"},
                    "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    "visual_evidence": {"type": "array", "items": {"type": "string"}},
                    "local_search_roi": {
                        "anyOf": [
                            {
                                "type": "object",
                                "properties": {
                                    "coordinate_space": {"type": "string", "enum": ["image", "original"]},
                                    "frame": {"type": "integer", "minimum": 0},
                                    "x": {"type": "number", "minimum": 0.0},
                                    "y": {"type": "number", "minimum": 0.0},
                                    "width": {"type": "number", "exclusiveMinimum": 0.0},
                                    "height": {"type": "number", "exclusiveMinimum": 0.0},
                                    "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                                },
                                "required": ["coordinate_space", "frame", "x", "y", "width", "height", "confidence"],
                                "additionalProperties": False,
                            },
                            {"type": "null"},
                        ]
                    },
                },
                "required": ["frame", "ball_visible", "confidence", "visual_evidence", "local_search_roi"],
                "additionalProperties": False,
            },
        },
        "coverage": {
            "type": "object",
            "properties": {
                "covered_subwindows": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "start_frame": {"type": "integer", "minimum": 0},
                            "end_frame": {"type": "integer", "minimum": 0},
                            "status": {"type": "string"},
                        },
                        "required": ["start_frame", "end_frame", "status"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["covered_subwindows"],
            "additionalProperties": False,
        },
    },
    "required": ["reason", "frames", "coverage"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class LocalizationWindow:
    start_frame: int
    end_frame: int
    label: str

    @property
    def frame_count(self) -> int:
        return self.end_frame - self.start_frame + 1

    @property
    def safe_label(self) -> str:
        return _safe_token(self.label)

    @property
    def visual_localization_id(self) -> str:
        return f"visual_localization:{self.start_frame}_{self.end_frame}_{self.safe_label}"

    @property
    def source_packet_id(self) -> str:
        return f"packet_ai_localize_{self.start_frame}_{self.end_frame}_{self.safe_label}"


TargetedLocalizationWindow = LocalizationWindow


class OpenAIVisualLocalizationClient:
    def __init__(self, responses_client: Any) -> None:
        self.responses_client = responses_client

    def localize_window(
        self,
        *,
        metadata: dict[str, Any],
        contact_sheet_data_url: str,
        crop_sheet_data_url: str,
        model: str | None,
    ) -> dict[str, Any]:
        return self.responses_client.create_json_vision_response(
            instructions=AI_VISUAL_LOCALIZATION_INSTRUCTIONS,
            prompt=_build_prompt(metadata),
            images=[
                {"label": "contact_sheet", "data_url": contact_sheet_data_url},
                {"label": "crop_sheet", "data_url": crop_sheet_data_url},
            ],
            model=model,
            json_schema=AI_VISUAL_LOCALIZATION_RESPONSE_SCHEMA,
            temperature=0.0,
        )


def parse_targeted_localization_window(value: str) -> LocalizationWindow:
    return _parse_window(value)


def write_ai_visual_localization_report(
    output_dir: Path | str,
    input_video: Path | str | None,
    windows: Sequence[str | Mapping[str, Any] | LocalizationWindow],
    *,
    model: str | None = None,
    client: Any = None,
    dry_run: bool = False,
    report_name: str = REPORT_NAME,
) -> dict[str, Any]:
    output_path = Path(output_dir)
    report = build_ai_visual_localization_report(
        output_path,
        input_video=input_video,
        windows=windows,
        model=model,
        client=client,
        dry_run=dry_run,
    )
    _write_json(output_path / report_name, report)
    return report


def build_ai_visual_localization_report(
    output_dir: Path | str,
    *,
    input_video: Path | str | None,
    windows: Sequence[str | Mapping[str, Any] | LocalizationWindow],
    model: str | None = None,
    client: Any = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    output_path = Path(output_dir)
    parsed_windows = [_parse_window(window) for window in windows]
    if not parsed_windows:
        raise ValueError("At least one targeted localization window is required.")

    source_path = Path(input_video) if input_video is not None else None
    source_video: dict[str, Any] = {
        "path": str(source_path.resolve()) if source_path is not None else None,
        "status": "unavailable",
        "dimension_source": "opencv",
    }
    if source_path is None or not source_path.exists():
        reason = "input video missing or not supplied"
        requests = [_unavailable_request(window, reason) for window in parsed_windows]
        return _base_report(
            output_dir=output_path,
            source_video=source_video,
            model=model,
            model_selection=_model_selection(model, "explicit" if model else "unavailable", dry_run=dry_run),
            dry_run=dry_run,
            requests=requests,
            errors=[{"error": reason, "error_type": "FileNotFoundError"}],
        )

    try:
        video_info = _read_video_info(source_path)
    except Exception as exc:
        source_video["path"] = str(source_path.resolve())
        message = _safe_error_message(exc)
        requests = [_unavailable_request(window, message) for window in parsed_windows]
        return _base_report(
            output_dir=output_path,
            source_video=source_video,
            model=model,
            model_selection=_model_selection(model, "explicit" if model else "unavailable", dry_run=dry_run),
            dry_run=dry_run,
            requests=requests,
            errors=[{"error": message, "error_type": exc.__class__.__name__}],
        )

    source_video = {
        "path": str(source_path.resolve()),
        "status": "available",
        "width": video_info["width"],
        "height": video_info["height"],
        "fps": video_info["fps"],
        "frame_count": video_info["frame_count"],
        "dimension_source": "opencv",
    }

    active_client = None if dry_run else client
    if active_client is None and not dry_run:
        active_client = _build_default_client()
    selected_model, model_source = _select_visual_model(client or active_client, model, dry_run=dry_run)

    requests: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for window in parsed_windows:
        request = _base_request(window)
        try:
            media, media_warnings, seed_crop = _write_window_media(
                output_dir=output_path,
                input_video=source_path,
                window=window,
                video_info=video_info,
            )
            request["crop"] = seed_crop
            request["media"] = media
            request["media_warnings"] = media_warnings
            if dry_run:
                request.update(_dry_run_request_payload(window))
            else:
                if selected_model is None and model_source == "strong_model_unavailable":
                    raise RuntimeError("Strong visual localization model is not configured.")
                if active_client is None:
                    raise RuntimeError("Visual localization client is unavailable.")
                if not media.get("contact_sheet") or not media.get("crop_sheet"):
                    raise RuntimeError("Targeted localization media could not be generated.")
                response = active_client.localize_window(
                    metadata=_window_metadata(window, video_info=video_info, media=media, seed_crop=seed_crop),
                    contact_sheet_data_url=_image_data_url(output_path / str(media["contact_sheet"])),
                    crop_sheet_data_url=_image_data_url(output_path / str(media["crop_sheet"])),
                    model=selected_model,
                )
                request.update(_validate_localization_response(response, window=window, video_info=video_info))
        except Exception as exc:
            message = _safe_error_message(exc)
            error_type = (
                "strong_visual_model_unavailable"
                if selected_model is None and model_source == "strong_model_unavailable" and not dry_run
                else exc.__class__.__name__
            )
            errors.append(
                {
                    "visual_localization_id": window.visual_localization_id,
                    "error": message,
                    "error_type": error_type,
                }
            )
            request["status"] = "unavailable" if error_type == "strong_visual_model_unavailable" else "error"
            request["error"] = message
            request["frames"] = []
            request["coverage"] = _coverage([], window.start_frame, window.end_frame)
            request["requested_action"] = _request_targeted_localization_payload(window, reason=message)
            request["invalid_roi_count"] = 0
        requests.append(request)

    return _base_report(
        output_dir=output_path,
        source_video=source_video,
        model=selected_model,
        model_selection=_model_selection(selected_model, model_source, dry_run=dry_run),
        dry_run=dry_run,
        requests=requests,
        errors=errors,
    )


def _base_report(
    *,
    output_dir: Path,
    source_video: dict[str, Any],
    model: str | None,
    model_selection: dict[str, Any],
    dry_run: bool,
    requests: list[dict[str, Any]],
    errors: list[dict[str, Any]],
) -> dict[str, Any]:
    summary = _summary(requests, errors)
    width = _safe_int(source_video.get("width"))
    height = _safe_int(source_video.get("height"))
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now_iso(),
        "output_dir": str(output_dir.resolve()),
        "source_video": source_video,
        "video_width": width,
        "video_height": height,
        "model": model,
        "model_selection": model_selection,
        "provider_mode": "dry-run" if dry_run else "real",
        "dry_run": bool(dry_run),
        "candidate_intent": "visual_localization",
        "can_lead_to_executable_candidates": (
            not dry_run and model is not None and summary.get("status") not in {"unavailable", "error"}
        ),
        "prompt_version": "visual-localization-v1",
        "summary": summary,
        "requests": requests,
        "windows": [
            {
                "visual_localization_id": item.get("visual_localization_id"),
                "start_frame": item.get("start_frame"),
                "end_frame": item.get("end_frame"),
                "label": item.get("label"),
                "status": item.get("status"),
            }
            for item in requests
        ],
        "errors": errors,
    }


def _summary(requests: list[dict[str, Any]], errors: list[dict[str, Any]]) -> dict[str, Any]:
    invalid_roi_count = sum(_safe_int(item.get("invalid_roi_count")) for item in requests)
    uncovered_subwindow_count = 0
    localized_request_count = 0
    for item in requests:
        coverage = item.get("coverage") if isinstance(item.get("coverage"), dict) else {}
        uncovered = coverage.get("uncovered_subwindows") if isinstance(coverage.get("uncovered_subwindows"), list) else []
        uncovered_subwindow_count += len(uncovered)
        localized_request_count += int(item.get("status") == "localized")
    statuses = {str(item.get("status") or "") for item in requests}
    if statuses <= {"planned"}:
        status = "planned"
    elif statuses <= {"unavailable"}:
        status = "unavailable"
    elif any(item.get("status") == "error" for item in requests):
        status = "error"
    elif any(item.get("status") in {"needs_review", "warn"} for item in requests):
        status = "warn"
    elif errors or invalid_roi_count or uncovered_subwindow_count:
        status = "warn"
    else:
        status = "ok"
    return {
        "status": status,
        "request_count": len(requests),
        "localized_request_count": localized_request_count,
        "error_count": len(errors),
        "invalid_roi_count": invalid_roi_count,
        "uncovered_subwindow_count": uncovered_subwindow_count,
    }


def _base_request(window: LocalizationWindow) -> dict[str, Any]:
    return {
        "visual_localization_id": window.visual_localization_id,
        "source_packet_id": window.source_packet_id,
        "start_frame": window.start_frame,
        "end_frame": window.end_frame,
        "label": window.label,
    }


def _unavailable_request(window: LocalizationWindow, reason: str) -> dict[str, Any]:
    request = _base_request(window)
    request.update(
        {
            "status": "unavailable",
            "reason": reason,
            "frames": [],
            "coverage": _coverage([], window.start_frame, window.end_frame),
            "requested_action": _request_targeted_localization_payload(window, reason=reason),
            "invalid_roi_count": 0,
        }
    )
    return request


def _dry_run_request_payload(window: LocalizationWindow) -> dict[str, Any]:
    return {
        "status": "planned",
        "reason": "dry-run: no visual model was called.",
        "frames": [],
        "coverage": _coverage([], window.start_frame, window.end_frame),
        "requested_action": _request_targeted_localization_payload(window, reason="dry-run: no visual model was called."),
        "invalid_roi_count": 0,
    }


def _request_targeted_localization_payload(window: LocalizationWindow, *, reason: str) -> dict[str, Any]:
    return {
        "recommended_action": "request_targeted_localization",
        "requested_action": "localize_ball_roi",
        "visual_localization_id": window.visual_localization_id,
        "rerun_scope": {"start_frame": window.start_frame, "end_frame": window.end_frame},
        "reason": reason,
    }


def _validate_localization_response(response: Any, *, window: LocalizationWindow, video_info: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise ValueError("Model response must be a JSON object.")
    reason = response.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("Model response reason must be a non-empty string.")
    raw_frames = response.get("frames")
    if not isinstance(raw_frames, list):
        raise ValueError("Model response frames must be a list.")

    frames: list[dict[str, Any]] = []
    valid_roi_frames: list[int] = []
    invalid_roi_count = 0
    first_valid_roi: dict[str, Any] | None = None
    for raw_frame in raw_frames:
        frame, valid_roi, invalid_roi = _validate_frame_localization(raw_frame, window=window, video_info=video_info)
        if valid_roi is not None:
            valid_roi_frames.append(frame["frame"])
            first_valid_roi = first_valid_roi or valid_roi
        invalid_roi_count += int(invalid_roi)
        frames.append(frame)

    covered = _covered_subwindows_from_valid_frames(valid_roi_frames)
    payload: dict[str, Any] = {
        "status": "localized" if valid_roi_frames and invalid_roi_count == 0 else "warn" if valid_roi_frames else "needs_review",
        "reason": reason.strip(),
        "frames": frames,
        "coverage": _coverage(covered, window.start_frame, window.end_frame),
        "invalid_roi_count": invalid_roi_count,
    }
    if first_valid_roi is not None:
        payload["suggestions"] = [
            {
                "recommended_action": "localize_ball_roi",
                "visual_localization_id": window.visual_localization_id,
                "rerun_scope": {"start_frame": window.start_frame, "end_frame": window.end_frame},
                "local_search_roi": first_valid_roi,
                "reason": reason.strip(),
            }
        ]
    else:
        payload["requested_action"] = _request_targeted_localization_payload(window, reason=reason.strip())
    return payload


def _validate_frame_localization(
    value: Any,
    *,
    window: LocalizationWindow,
    video_info: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None, bool]:
    if not isinstance(value, dict):
        raise ValueError("Model response frame entries must be objects.")
    frame = _nonnegative_int(value.get("frame"), "frames[].frame")
    if frame < window.start_frame or frame > window.end_frame:
        raise ValueError("Model response frame must fall inside the requested localization window.")
    ball_visible = value.get("ball_visible")
    if not isinstance(ball_visible, bool):
        raise ValueError("Model response ball_visible must be a boolean.")
    item: dict[str, Any] = {
        "frame": frame,
        "ball_visible": ball_visible,
        "confidence": _bounded_number(value.get("confidence"), "frames[].confidence", minimum=0.0, maximum=1.0),
        "visual_evidence": _string_list(value.get("visual_evidence"), "frames[].visual_evidence"),
        "local_search_roi": None,
    }
    roi_value = value.get("local_search_roi")
    if roi_value is None:
        return item, None, False
    try:
        roi = _local_search_roi(roi_value, frame=frame, window=window, video_info=video_info)
    except ValueError as exc:
        item["roi_status"] = "rejected"
        item["rejection_reason"] = str(exc)
        item["rejected_local_search_roi"] = _redacted_roi(roi_value)
        return item, None, True
    item["local_search_roi"] = roi
    item["roi_status"] = "accepted"
    return item, roi, False


def _local_search_roi(value: Any, *, frame: int, window: LocalizationWindow, video_info: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("local_search_roi must be an object or null.")
    required = ("coordinate_space", "frame", "x", "y", "width", "height", "confidence")
    missing = [key for key in required if key not in value]
    if missing:
        raise ValueError(f"local_search_roi missing fields: {', '.join(missing)}")
    if value.get("coordinate_space") not in {"image", "original"}:
        raise ValueError("local_search_roi.coordinate_space must be image or original.")
    roi_frame = _nonnegative_int(value.get("frame"), "local_search_roi.frame")
    if roi_frame != frame or roi_frame < window.start_frame or roi_frame > window.end_frame:
        raise ValueError("local_search_roi.frame must match the frame entry and requested window.")
    x = _bounded_number(value.get("x"), "local_search_roi.x", minimum=0.0)
    y = _bounded_number(value.get("y"), "local_search_roi.y", minimum=0.0)
    width = _bounded_number(value.get("width"), "local_search_roi.width", minimum=0.0, exclusive_minimum=True)
    height = _bounded_number(value.get("height"), "local_search_roi.height", minimum=0.0, exclusive_minimum=True)
    if x + width > int(video_info["width"]) or y + height > int(video_info["height"]):
        raise ValueError("local_search_roi must fit inside decoded source video dimensions.")
    return {
        "coordinate_space": "image",
        "frame": roi_frame,
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "confidence": _bounded_number(value.get("confidence"), "local_search_roi.confidence", minimum=0.0, maximum=1.0),
    }


def _covered_subwindows_from_valid_frames(valid_roi_frames: list[int]) -> list[dict[str, Any]]:
    return _merge_subwindows(
        [
            {"start_frame": frame, "end_frame": frame, "status": "localized"}
            for frame in sorted(set(valid_roi_frames))
        ]
    )


def _coverage(covered_subwindows: list[dict[str, Any]], start_frame: int, end_frame: int) -> dict[str, Any]:
    covered = _merge_subwindows(covered_subwindows)
    uncovered: list[dict[str, Any]] = []
    cursor = start_frame
    for item in covered:
        item_start = int(item["start_frame"])
        item_end = int(item["end_frame"])
        if cursor < item_start:
            uncovered.append({"start_frame": cursor, "end_frame": item_start - 1, "status": "needs_review"})
        cursor = max(cursor, item_end + 1)
    if cursor <= end_frame:
        uncovered.append({"start_frame": cursor, "end_frame": end_frame, "status": "needs_review"})
    return {"covered_subwindows": covered, "uncovered_subwindows": uncovered}


def _merge_subwindows(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(items, key=lambda item: (int(item["start_frame"]), int(item["end_frame"])))
    merged: list[dict[str, Any]] = []
    for item in ordered:
        start = int(item["start_frame"])
        end = int(item["end_frame"])
        if not merged or start > int(merged[-1]["end_frame"]) + 1:
            merged.append({"start_frame": start, "end_frame": end, "status": str(item.get("status") or "localized")})
            continue
        merged[-1]["end_frame"] = max(int(merged[-1]["end_frame"]), end)
        merged[-1]["status"] = "localized"
    return merged


def _write_window_media(
    *,
    output_dir: Path,
    input_video: Path,
    window: LocalizationWindow,
    video_info: dict[str, Any],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    import cv2

    media_dir = output_dir / "ai_visual_localization" / _safe_token(window.visual_localization_id)
    media_dir.mkdir(parents=True, exist_ok=True)
    contact_path = media_dir / "contact_sheet.jpg"
    crop_path = media_dir / "crop_sheet.jpg"
    seed_crop = _seed_crop_for_label(window.label, width=int(video_info["width"]), height=int(video_info["height"]))
    frames = _read_sampled_frames(input_video, _sample_frames(window.start_frame, window.end_frame, DEFAULT_SAMPLE_COUNT))
    if not frames:
        return {}, ["no_sample_frames"], seed_crop

    contact_thumbs: list[Any] = []
    crop_thumbs: list[Any] = []
    for frame_index, frame in frames:
        full = frame.copy()
        crop = _crop_frame(full, seed_crop)
        _draw_label(full, f"frame {frame_index}")
        _draw_label(crop, f"frame {frame_index}", scale=0.8, y=40)
        contact_thumbs.append(cv2.resize(full, (960, 270), interpolation=cv2.INTER_AREA))
        crop_thumbs.append(cv2.resize(crop, (480, 270), interpolation=cv2.INTER_AREA))
    if not cv2.imwrite(str(contact_path), cv2.vconcat(contact_thumbs)):
        return {}, ["contact_sheet_failed"], seed_crop
    crop_rows: list[Any] = []
    for index in range(0, len(crop_thumbs), 2):
        row = crop_thumbs[index : index + 2]
        if len(row) == 1:
            row.append(_blank_like(row[0]))
        crop_rows.append(cv2.hconcat(row))
    if not cv2.imwrite(str(crop_path), cv2.vconcat(crop_rows)):
        contact_path.unlink(missing_ok=True)
        return {}, ["crop_sheet_failed"], seed_crop
    if not _is_nonempty_file(contact_path) or not _is_nonempty_file(crop_path):
        contact_path.unlink(missing_ok=True)
        crop_path.unlink(missing_ok=True)
        return {}, ["media_empty"], seed_crop

    contact_rel = _relative_path(output_dir, contact_path)
    crop_rel = _relative_path(output_dir, crop_path)
    contact_hash = _sha256_file(contact_path)
    crop_hash = _sha256_file(crop_path)
    return (
        {
            "contact_sheet": contact_rel,
            "crop_sheet": crop_rel,
            "contact_sheet_sha256": contact_hash,
            "crop_sheet_sha256": crop_hash,
            "sha256": _sha256_text(f"{contact_hash}:{crop_hash}"),
        },
        [],
        seed_crop,
    )


def _read_sampled_frames(input_video: Path, frame_indices: list[int]) -> list[tuple[int, Any]]:
    import cv2

    capture = cv2.VideoCapture(str(input_video))
    try:
        if not capture.isOpened():
            return []
        frames: list[tuple[int, Any]] = []
        for frame_index in frame_indices:
            capture.set(cv2.CAP_PROP_POS_FRAMES, max(0, frame_index))
            ok, frame = capture.read()
            if ok:
                frames.append((frame_index, frame))
        return frames
    finally:
        capture.release()


def _read_video_info(path: Path) -> dict[str, Any]:
    import cv2

    capture = cv2.VideoCapture(str(path))
    try:
        if not capture.isOpened():
            raise ValueError("input video could not be opened by OpenCV.")
        ok, frame = capture.read()
        if not ok or frame is None:
            raise ValueError("input video first frame could not be decoded by OpenCV.")
        height, width = frame.shape[:2]
        fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
        frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    finally:
        capture.release()
    if width <= 0 or height <= 0:
        raise ValueError("input video decoded dimensions are unavailable.")
    return {"width": int(width), "height": int(height), "fps": fps if math.isfinite(fps) and fps > 0 else 0.0, "frame_count": max(0, frame_count)}


def _seed_crop_for_label(label: str, *, width: int, height: int) -> dict[str, Any]:
    normalized = label.casefold().replace("-", "_")
    crop_width = min(width, max(1, int(round(width * 0.34))))
    crop_height = min(height, max(1, int(round(height * 0.64))))
    if "corner" in normalized:
        crop_width = min(width, max(crop_width, min(width, 1720)))
        crop_height = min(height, max(crop_height, min(height, 920)))
    if "right" in normalized:
        x = width - crop_width
    elif "left" in normalized:
        x = 0
    else:
        x = max(0, (width - crop_width) // 2)
    if "lower" in normalized or "bottom" in normalized or "right_corner" in normalized or "left_corner" in normalized:
        y = height - crop_height
    elif "upper" in normalized or "top" in normalized:
        y = 0
    else:
        y = max(0, (height - crop_height) // 2)
    return {"x": int(max(0, x)), "y": int(max(0, y)), "width": int(max(1, crop_width)), "height": int(max(1, crop_height)), "coordinate_space": "image", "label_hint": label}


def _crop_frame(frame: Any, crop: dict[str, Any]) -> Any:
    x = int(crop["x"])
    y = int(crop["y"])
    width = int(crop["width"])
    height = int(crop["height"])
    sliced = frame[y : y + height, x : x + width]
    return frame if getattr(sliced, "size", 0) == 0 else sliced


def _window_metadata(window: LocalizationWindow, *, video_info: dict[str, Any], media: dict[str, Any], seed_crop: dict[str, Any]) -> dict[str, Any]:
    return {
        "visual_localization_id": window.visual_localization_id,
        "source_packet_id": window.source_packet_id,
        "requested_window": {
            "start_frame": window.start_frame,
            "end_frame": window.end_frame,
            "label": window.label,
            "sample_frames": _sample_frames(window.start_frame, window.end_frame, DEFAULT_SAMPLE_COUNT),
        },
        "source_video": {
            "width": video_info["width"],
            "height": video_info["height"],
            "fps": video_info["fps"],
            "frame_count": video_info["frame_count"],
            "dimension_source": "opencv",
        },
        "seed_crop": seed_crop,
        "media": {key: value for key, value in media.items() if key in {"contact_sheet", "crop_sheet", "contact_sheet_sha256", "crop_sheet_sha256"}},
    }


def _build_prompt(metadata: dict[str, Any]) -> str:
    return (
        "Locate the real match ball for this requested window. The crop sheet is only a hint, not ground truth. "
        "Return local_search_roi only when the ball is visible and the ROI fits inside decoded source_video dimensions. "
        "If only part of the requested window is localized, list only that range in coverage.covered_subwindows.\n\n"
        f"{json.dumps({'metadata': metadata}, ensure_ascii=False, indent=2)}"
    )


def _parse_window(value: str | Mapping[str, Any] | LocalizationWindow) -> LocalizationWindow:
    if isinstance(value, LocalizationWindow):
        return value
    if isinstance(value, str):
        parts = value.split(":", 2)
        if len(parts) != 3:
            raise ValueError("targeted localization windows must use start:end:label")
        start = _optional_int(parts[0])
        end = _optional_int(parts[1])
        label = parts[2].strip()
    elif isinstance(value, Mapping):
        start = _optional_int(value.get("start_frame"))
        end = _optional_int(value.get("end_frame"))
        label_value = value.get("label")
        label = label_value.strip() if isinstance(label_value, str) else ""
    else:
        raise ValueError("targeted localization windows must be strings or objects.")
    if start is None or end is None or start < 0 or end < start:
        raise ValueError("targeted localization window frame bounds are invalid.")
    if not label:
        raise ValueError("targeted localization window label is required.")
    return LocalizationWindow(start_frame=start, end_frame=end, label=label)


def _select_visual_model(client: Any, model: str | None, *, dry_run: bool) -> tuple[str | None, str]:
    if model:
        return model, "explicit"
    settings = getattr(client, "settings", None)
    if settings is None:
        responses_client = getattr(client, "responses_client", None)
        settings = getattr(responses_client, "settings", None)
    if settings is None and client is not None:
        return None, "client_supplied"
    visual_model = getattr(settings, "visual_review_model", None)
    if isinstance(visual_model, str) and visual_model.strip():
        return visual_model.strip(), "visual_review_model"
    improvement_model = getattr(settings, "improvement_model", None)
    if isinstance(improvement_model, str) and improvement_model.strip():
        return improvement_model.strip(), "improvement_model"
    chat_model = getattr(settings, "chat_model", None)
    if dry_run and isinstance(chat_model, str) and chat_model.strip():
        return chat_model.strip(), "chat_model_fallback"
    return None, "strong_model_unavailable"


def _model_selection(model: str | None, source: str, *, dry_run: bool) -> dict[str, Any]:
    return {"model": model, "source": source, "provider_dry_run": bool(dry_run), "provider_mode": "dry-run" if dry_run else "real"}


def _build_default_client() -> OpenAIVisualLocalizationClient:
    from football_tracking.api.ai_provider import OpenAIResponsesClient, load_provider_settings

    repo_root = Path(__file__).resolve().parents[2]
    return OpenAIVisualLocalizationClient(OpenAIResponsesClient(load_provider_settings(repo_root)))


def _sample_frames(start_frame: int, end_frame: int, count: int) -> list[int]:
    if count <= 1 or end_frame <= start_frame:
        return [start_frame]
    step = (end_frame - start_frame) / float(count - 1)
    return sorted({int(round(start_frame + step * index)) for index in range(count)})


def _image_data_url(path: Path) -> str:
    mime_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    payload = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{payload}"


def _safe_error_message(exc: Exception) -> str:
    message = str(exc)
    message = re.sub(r"data:[^\s\"']*;base64,[^\s\"']+", "data:<redacted-base64>", message)
    message = re.sub(r"Bearer\s+[^\s\"']+", "Bearer <redacted>", message, flags=re.IGNORECASE)
    message = re.sub(r"sk-[A-Za-z0-9_-]+", "sk-<redacted>", message)
    return message


def _safe_token(value: str) -> str:
    token = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in value).strip("_")
    return token or "window"


def _relative_path(root: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")
    except ValueError:
        return str(path.resolve())


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _is_nonempty_file(path: Path) -> bool:
    return path.exists() and path.stat().st_size > 0


def _draw_label(frame: Any, label: str, *, scale: float = 1.0, y: int = 44) -> None:
    import cv2

    cv2.putText(frame, label, (16, y), cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), 5, cv2.LINE_AA)
    cv2.putText(frame, label, (16, y), cv2.FONT_HERSHEY_SIMPLEX, scale, (255, 255, 255), 2, cv2.LINE_AA)


def _blank_like(frame: Any) -> Any:
    import numpy as np

    return np.zeros_like(frame)


def _redacted_roi(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return None
    return {key: value.get(key) for key in ("coordinate_space", "frame", "x", "y", "width", "height", "confidence")}


def _string_list(value: Any, key: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"Model response {key} must be a list.")
    strings = [item.strip() for item in value if isinstance(item, str) and item.strip()]
    if len(strings) != len(value):
        raise ValueError(f"Model response {key} must contain non-empty strings.")
    return strings


def _nonnegative_int(value: Any, key: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"Model response {key} must be a nonnegative integer.")
    return value


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or not parsed.is_integer():
        return None
    return int(parsed)


def _safe_int(value: Any) -> int:
    parsed = _optional_int(value)
    return 0 if parsed is None else max(0, parsed)


def _bounded_number(
    value: Any,
    key: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    exclusive_minimum: bool = False,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Model response {key} must be a number.")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"Model response {key} must be finite.")
    if minimum is not None:
        if exclusive_minimum and parsed <= minimum:
            raise ValueError(f"Model response {key} must be greater than {minimum}.")
        if not exclusive_minimum and parsed < minimum:
            raise ValueError(f"Model response {key} must be at least {minimum}.")
    if maximum is not None and parsed > maximum:
        raise ValueError(f"Model response {key} must be at most {maximum}.")
    return parsed


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
