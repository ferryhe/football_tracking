from __future__ import annotations

import base64
import json
import math
import mimetypes
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from football_tracking.ai_contracts import AI_FAILURE_TAGS, AI_ROOT_CAUSE_MODULES

SCHEMA_VERSION = "1.0"

VERDICT_ACCEPT_HIGHLIGHT = "accept_highlight"
VERDICT_NEEDS_HUMAN_REVIEW = "needs_human_review"
VERDICT_REJECT_NOISE = "reject_noise"
VERDICTS = (VERDICT_ACCEPT_HIGHLIGHT, VERDICT_NEEDS_HUMAN_REVIEW, VERDICT_REJECT_NOISE)

MATCH_BALL_VISIBLE_YES = "yes"
MATCH_BALL_VISIBLE_PARTIAL = "partial"
MATCH_BALL_VISIBLE_NO = "no"
MATCH_BALL_VISIBLE_UNCLEAR = "unclear"
MATCH_BALL_VISIBLE_VALUES = (
    MATCH_BALL_VISIBLE_YES,
    MATCH_BALL_VISIBLE_PARTIAL,
    MATCH_BALL_VISIBLE_NO,
    MATCH_BALL_VISIBLE_UNCLEAR,
)

MARKER_ALIGNMENT_GOOD = "good"
MARKER_ALIGNMENT_MIXED = "mixed"
MARKER_ALIGNMENT_OFF = "off"
MARKER_ALIGNMENT_UNCLEAR = "unclear"
MARKER_ALIGNMENT_VALUES = (
    MARKER_ALIGNMENT_GOOD,
    MARKER_ALIGNMENT_MIXED,
    MARKER_ALIGNMENT_OFF,
    MARKER_ALIGNMENT_UNCLEAR,
)

RECOMMENDED_ACTION_KEEP_HIGHLIGHT = "keep_highlight"
RECOMMENDED_ACTION_SEND_TO_HUMAN = "send_to_human"
RECOMMENDED_ACTION_DISCARD = "discard"
RECOMMENDED_ACTIONS = (
    RECOMMENDED_ACTION_KEEP_HIGHLIGHT,
    RECOMMENDED_ACTION_SEND_TO_HUMAN,
    RECOMMENDED_ACTION_DISCARD,
)
TUNING_DIRECTIONS = ("tighten", "loosen", "split_packets", "rerank_events", "retrack_segment", "none")
OPTIONAL_RESPONSE_FIELDS = (
    "failure_tags",
    "root_cause_module",
    "suggested_fixes",
    "likely_ball_region",
    "local_search_roi",
    "best_subclip",
    "tuning_direction",
)
LEGACY_REQUIRED_RESPONSE_FIELDS = (
    "verdict",
    "confidence",
    "reason",
    "match_ball_visible",
    "marker_alignment",
    "highlight_publishable",
    "recommended_action",
    "visual_evidence",
)
STRICT_RESPONSE_FIELDS = (*LEGACY_REQUIRED_RESPONSE_FIELDS, *OPTIONAL_RESPONSE_FIELDS)

AI_VISUAL_REVIEW_INSTRUCTIONS = (
    "You are a conservative football highlight visual review agent. "
    "Review the contact sheet, crop sheet, and packet metadata. "
    "Return strict JSON with every schema key. Core keys are verdict, confidence, reason, match_ball_visible, "
    "marker_alignment, highlight_publishable, recommended_action, visual_evidence. "
    "Diagnostic keys are failure_tags, root_cause_module, suggested_fixes, "
    "likely_ball_region, local_search_roi, best_subclip, tuning_direction; use empty arrays, null, "
    "unknown, or none when the evidence is unavailable. "
    "Allowed verdict values: accept_highlight, needs_human_review, reject_noise. "
    "Allowed match_ball_visible values: yes, partial, no, unclear. "
    "Allowed marker_alignment values: good, mixed, off, unclear. "
    "Allowed recommended_action values: keep_highlight, send_to_human, discard. "
    "Use failure_tags and root_cause_module only from the shared packet metadata vocabulary. "
    "Do not treat the packet decision label highlight_worthy as ground truth. "
    "Only choose accept_highlight when the match ball is visible in multiple frames, the marker "
    "stays consistently close to the real match ball, and the candidate clip appears publishable. "
    "If the ball is occluded, the marker drifts, or the target could be a shoe, line, sideline object, "
    "ad board, spectator item, or other non-ball object, choose needs_human_review. "
    "Choose reject_noise only when the packet is clearly not tracking a real match ball. "
    "For missing-ball or reacquire packets, use the contact sheet and crop sheet to localize the real ball "
    "only when the ball is visible; if it is not visible, set likely_ball_region.description to 'not visible' "
    "and set local_search_roi to null."
)

AI_VISUAL_REVIEW_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": list(VERDICTS)},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "reason": {"type": "string"},
        "match_ball_visible": {"type": "string", "enum": list(MATCH_BALL_VISIBLE_VALUES)},
        "marker_alignment": {"type": "string", "enum": list(MARKER_ALIGNMENT_VALUES)},
        "highlight_publishable": {"type": "boolean"},
        "recommended_action": {"type": "string", "enum": list(RECOMMENDED_ACTIONS)},
        "visual_evidence": {
            "type": "array",
            "items": {"type": "string"},
        },
        "failure_tags": {
            "type": "array",
            "items": {"type": "string", "enum": sorted(AI_FAILURE_TAGS)},
        },
        "root_cause_module": {"type": "string", "enum": sorted(AI_ROOT_CAUSE_MODULES)},
        "suggested_fixes": {
            "type": "array",
            "items": {"type": "string"},
        },
        "likely_ball_region": {
            "anyOf": [
                {
                    "type": "object",
                    "properties": {
                        "frame": {"type": "integer", "minimum": 0},
                        "description": {"type": "string"},
                        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    },
                    "required": ["frame", "description", "confidence"],
                    "additionalProperties": False,
                },
                {"type": "null"},
            ]
        },
        "local_search_roi": {
            "anyOf": [
                {
                    "type": "object",
                    "properties": {
                        "coordinate_space": {"type": "string", "enum": ["image"]},
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
        "best_subclip": {
            "anyOf": [
                {
                    "type": "object",
                    "properties": {
                        "start_frame": {"type": "integer", "minimum": 0},
                        "end_frame": {"type": "integer", "minimum": 0},
                        "reason": {"type": "string"},
                    },
                    "required": ["start_frame", "end_frame", "reason"],
                    "additionalProperties": False,
                },
                {"type": "null"},
            ]
        },
        "tuning_direction": {"type": "string", "enum": list(TUNING_DIRECTIONS)},
    },
    "required": list(STRICT_RESPONSE_FIELDS),
    "additionalProperties": False,
}


class OpenAIVisualReviewClient:
    def __init__(self, responses_client: Any) -> None:
        self.responses_client = responses_client

    def review_packet(
        self,
        *,
        packet: dict[str, Any],
        metadata: dict[str, Any],
        contact_sheet_data_url: str,
        crop_sheet_data_url: str,
        model: str | None,
    ) -> dict[str, Any]:
        prompt = _build_prompt(metadata)
        return self.responses_client.create_json_vision_response(
            instructions=AI_VISUAL_REVIEW_INSTRUCTIONS,
            prompt=prompt,
            images=[
                {"label": "contact_sheet", "data_url": contact_sheet_data_url},
                {"label": "crop_sheet", "data_url": crop_sheet_data_url},
            ],
            model=model,
            json_schema=AI_VISUAL_REVIEW_RESPONSE_SCHEMA,
            temperature=0.0,
        )


def build_ai_visual_review_report(
    output_dir: Path,
    *,
    client: Any = None,
    model: str | None = None,
    max_packets: int | None = None,
    only_labels: Sequence[str] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    output_dir = Path(output_dir)
    if max_packets is not None and max_packets < 1:
        raise ValueError("max_packets must be at least 1 when provided.")

    packet_report = _read_json(output_dir / "review_packets.json")
    raw_packets = packet_report.get("packets")
    packets = [packet for packet in raw_packets if isinstance(packet, dict)] if isinstance(raw_packets, list) else []
    selected_packets = _select_packets(packets, only_labels=only_labels, max_packets=max_packets)

    active_client = None if dry_run else client
    if active_client is None and not dry_run:
        active_client = _build_default_client()
    selected_model, model_selection_source = _select_visual_model(client or active_client, model, dry_run=dry_run)
    model_selection = {
        "model": selected_model,
        "source": model_selection_source,
        "provider_dry_run": bool(dry_run),
        "provider_mode": "dry-run" if dry_run else "real",
    }

    reviews: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for index, packet in enumerate(selected_packets, start=1):
        packet_id = _packet_id(packet, index)
        visual_review_id = _visual_review_id(packet_id)
        review_item = _base_review_item(packet, packet_id, visual_review_id)
        try:
            if selected_model is None and model_selection_source == "strong_model_unavailable" and not dry_run:
                raise RuntimeError("Strong visual review model is not configured.")
            if dry_run:
                review_item["review"] = _with_review_linkage(_dry_run_review(), packet_id, visual_review_id)
            else:
                contact_sheet = _packet_media_path(output_dir, packet, "contact_sheet")
                crop_sheet = _packet_media_path(output_dir, packet, "crop_sheet")
                metadata = _packet_metadata(packet, packet_id)
                response = active_client.review_packet(
                    packet=packet,
                    metadata=metadata,
                    contact_sheet_data_url=_image_data_url(contact_sheet),
                    crop_sheet_data_url=_image_data_url(crop_sheet),
                    model=selected_model,
                )
                review_item["review"] = _with_review_linkage(
                    _validate_review_response(
                        response,
                        frame_dimensions=_packet_frame_dimensions(packet),
                        window=packet.get("window") if isinstance(packet.get("window"), dict) else None,
                    ),
                    packet_id,
                    visual_review_id,
                )
        except Exception as exc:
            error = {
                "packet_id": packet_id,
                "error": _safe_error_message(exc),
                "error_type": "strong_visual_model_unavailable"
                if selected_model is None and model_selection_source == "strong_model_unavailable" and not dry_run
                else exc.__class__.__name__,
            }
            review_item["error"] = error["error"]
            errors.append(error)
        reviews.append(review_item)

    summary = _summary(reviews, errors)
    provider_mode = "dry-run" if dry_run else "real"
    can_lead_to_executable_candidates = (
        not dry_run
        and selected_model is not None
        and summary.get("status") not in {"unavailable", "error"}
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now_iso(),
        "output_dir": str(output_dir.resolve()),
        "source_review_packets": str((output_dir / "review_packets.json").resolve()),
        "model": selected_model,
        "model_selection": model_selection,
        "candidate_intent": "visual_localization",
        "provider_mode": provider_mode,
        "can_lead_to_executable_candidates": can_lead_to_executable_candidates,
        "dry_run": bool(dry_run),
        "filters": {
            "only_labels": list(only_labels) if only_labels is not None else None,
            "max_packets": max_packets,
        },
        "prompt_version": "visual-review-v2",
        "summary": summary,
        "reviews": reviews,
        "errors": errors,
    }


def write_ai_visual_review_report(
    output_dir: Path,
    *,
    client: Any = None,
    model: str | None = None,
    max_packets: int | None = None,
    only_labels: Sequence[str] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    output_dir = Path(output_dir)
    report = build_ai_visual_review_report(
        output_dir,
        client=client,
        model=model,
        max_packets=max_packets,
        only_labels=only_labels,
        dry_run=dry_run,
    )
    _write_json(output_dir / "ai_visual_review.json", report)
    return report


def compact_ai_visual_review_summary(report: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(report, dict):
        return None
    summary = report.get("summary")
    if not isinstance(summary, dict):
        return None
    counts_by_verdict = summary.get("counts_by_verdict")
    return {
        "schema_version": report.get("schema_version"),
        "generated_at": report.get("generated_at"),
        "packet_count": _safe_int(summary.get("packet_count")),
        "reviewed_count": _safe_int(summary.get("reviewed_count")),
        "error_count": _safe_int(summary.get("error_count")),
        "counts_by_verdict": counts_by_verdict if isinstance(counts_by_verdict, dict) else {},
        "accepted_highlight_count": _safe_int(summary.get("accepted_highlight_count")),
        "needs_human_review_count": _safe_int(summary.get("needs_human_review_count")),
        "reject_noise_count": _safe_int(summary.get("reject_noise_count")),
    }


def _select_packets(
    packets: list[dict[str, Any]],
    *,
    only_labels: Sequence[str] | None,
    max_packets: int | None,
) -> list[dict[str, Any]]:
    selected = packets
    if only_labels:
        labels = {str(label) for label in only_labels}
        selected = [packet for packet in selected if _packet_label(packet) in labels]
    if max_packets is not None:
        selected = selected[:max_packets]
    return selected


def _base_review_item(packet: dict[str, Any], packet_id: str, visual_review_id: str) -> dict[str, Any]:
    return {
        "packet_id": packet_id,
        "visual_review_id": visual_review_id,
        "packet_label": _packet_label(packet),
        "source": packet.get("source") if isinstance(packet.get("source"), dict) else {},
        "window": packet.get("window") if isinstance(packet.get("window"), dict) else {},
        "track_summary": packet.get("track_summary") if isinstance(packet.get("track_summary"), dict) else {},
        "media": packet.get("media") if isinstance(packet.get("media"), dict) else {},
        "media_warnings": packet.get("media_warnings") if isinstance(packet.get("media_warnings"), list) else [],
    }


def _packet_metadata(packet: dict[str, Any], packet_id: str) -> dict[str, Any]:
    return {
        "packet_id": packet_id,
        "decision": packet.get("decision") if isinstance(packet.get("decision"), dict) else {},
        "source": packet.get("source") if isinstance(packet.get("source"), dict) else {},
        "window": packet.get("window") if isinstance(packet.get("window"), dict) else {},
        "track_summary": packet.get("track_summary") if isinstance(packet.get("track_summary"), dict) else {},
        "suspected_failure_tags": packet.get("suspected_failure_tags")
        if isinstance(packet.get("suspected_failure_tags"), list)
        else [],
        "root_cause_candidates": packet.get("root_cause_candidates")
        if isinstance(packet.get("root_cause_candidates"), list)
        else [],
        "packet_purpose": packet.get("packet_purpose") if isinstance(packet.get("packet_purpose"), str) else "",
        "frame_dimensions": _packet_frame_dimensions(packet) or {},
        "media": packet.get("media") if isinstance(packet.get("media"), dict) else {},
        "media_warnings": packet.get("media_warnings") if isinstance(packet.get("media_warnings"), list) else [],
    }


def _packet_label(packet: dict[str, Any]) -> str | None:
    decision = packet.get("decision")
    if not isinstance(decision, dict):
        return None
    label = decision.get("label")
    return label if isinstance(label, str) else None


def _packet_id(packet: dict[str, Any], index: int) -> str:
    packet_id = packet.get("packet_id")
    if isinstance(packet_id, str) and packet_id.strip():
        return packet_id
    return f"packet_{index:03d}"


def _packet_frame_dimensions(packet: dict[str, Any]) -> dict[str, int] | None:
    dimensions = packet.get("frame_dimensions")
    if not isinstance(dimensions, dict):
        return None
    width = _safe_int(dimensions.get("width"))
    height = _safe_int(dimensions.get("height"))
    if width <= 0 or height <= 0:
        return None
    return {"width": width, "height": height}


def _visual_review_id(packet_id: str) -> str:
    safe_packet_id = "".join(ch if ch.isalnum() or ch in {"_", "-", ":"} else "_" for ch in packet_id).strip("_")
    return f"visual_review:{safe_packet_id or 'packet'}"


def _with_review_linkage(review: dict[str, Any], packet_id: str, visual_review_id: str) -> dict[str, Any]:
    linked = dict(review)
    linked["source_packet_id"] = packet_id
    linked["visual_review_id"] = visual_review_id
    linked["provenance"] = {
        "source": "ai_visual_review",
        "source_packet_id": packet_id,
        "visual_review_id": visual_review_id,
    }
    return linked


def _dry_run_review() -> dict[str, Any]:
    return {
        "verdict": VERDICT_NEEDS_HUMAN_REVIEW,
        "confidence": 0.0,
        "reason": "dry-run: no model was called, so this packet defaults to human review.",
        "match_ball_visible": MATCH_BALL_VISIBLE_UNCLEAR,
        "marker_alignment": MARKER_ALIGNMENT_UNCLEAR,
        "highlight_publishable": False,
        "recommended_action": RECOMMENDED_ACTION_SEND_TO_HUMAN,
        "visual_evidence": ["dry-run default; no visual model was called."],
        "failure_tags": ["unknown"],
        "root_cause_module": "unknown",
        "suggested_fixes": [],
        "likely_ball_region": {"frame": 0, "description": "not visible", "confidence": 0.0},
        "local_search_roi": None,
        "best_subclip": None,
        "tuning_direction": "none",
    }


def _validate_review_response(
    response: Any,
    *,
    frame_dimensions: dict[str, int] | None = None,
    window: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise ValueError("Model response must be a JSON object.")

    required = LEGACY_REQUIRED_RESPONSE_FIELDS
    missing = [key for key in required if key not in response]
    if missing:
        raise ValueError(f"Model response missing required fields: {', '.join(missing)}")
    allowed_fields = {*required, *OPTIONAL_RESPONSE_FIELDS}
    unexpected = [key for key in response if key not in allowed_fields]
    if unexpected:
        raise ValueError(f"Model response has unsupported fields: {', '.join(sorted(unexpected))}")

    verdict = _enum_value(response, "verdict", VERDICTS)
    match_ball_visible = _enum_value(response, "match_ball_visible", MATCH_BALL_VISIBLE_VALUES)
    marker_alignment = _enum_value(response, "marker_alignment", MARKER_ALIGNMENT_VALUES)
    recommended_action = _enum_value(response, "recommended_action", RECOMMENDED_ACTIONS)
    confidence = _confidence(response.get("confidence"))
    reason = response.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("Model response reason must be a non-empty string.")
    highlight_publishable = response.get("highlight_publishable")
    if not isinstance(highlight_publishable, bool):
        raise ValueError("Model response highlight_publishable must be a boolean.")
    visual_evidence = response.get("visual_evidence")
    if not isinstance(visual_evidence, list) or not visual_evidence:
        raise ValueError("Model response visual_evidence must be a non-empty list.")
    evidence_items = []
    for item in visual_evidence:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("Model response visual_evidence must contain non-empty strings.")
        evidence_items.append(item.strip())

    validated = {
        "verdict": verdict,
        "confidence": confidence,
        "reason": reason.strip(),
        "match_ball_visible": match_ball_visible,
        "marker_alignment": marker_alignment,
        "highlight_publishable": highlight_publishable,
        "recommended_action": recommended_action,
        "visual_evidence": evidence_items,
    }
    if "failure_tags" in response:
        validated["failure_tags"] = _failure_tags(response.get("failure_tags"))
    else:
        validated["failure_tags"] = []
    if "root_cause_module" in response:
        validated["root_cause_module"] = _enum_value(response, "root_cause_module", tuple(sorted(AI_ROOT_CAUSE_MODULES)))
    else:
        validated["root_cause_module"] = "unknown"
    if "suggested_fixes" in response:
        validated["suggested_fixes"] = _string_list(response.get("suggested_fixes"), "suggested_fixes")
    else:
        validated["suggested_fixes"] = []
    if "likely_ball_region" in response:
        validated["likely_ball_region"] = _likely_ball_region(response.get("likely_ball_region"))
    else:
        validated["likely_ball_region"] = None
    if "local_search_roi" in response:
        validated["local_search_roi"] = _local_search_roi(
            response.get("local_search_roi"),
            frame_dimensions=frame_dimensions,
            window=window,
        )
    else:
        validated["local_search_roi"] = None
    if "best_subclip" in response:
        validated["best_subclip"] = _best_subclip(response.get("best_subclip"))
    else:
        validated["best_subclip"] = None
    if "tuning_direction" in response:
        validated["tuning_direction"] = _enum_value(response, "tuning_direction", TUNING_DIRECTIONS)
    else:
        validated["tuning_direction"] = "none"
    return validated


def _enum_value(response: dict[str, Any], key: str, allowed: tuple[str, ...]) -> str:
    value = response.get(key)
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(f"Model response {key} must be one of: {', '.join(allowed)}")
    return value


def _failure_tags(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("Model response failure_tags must be a list.")
    tags: list[str] = []
    for item in value:
        if not isinstance(item, str) or item not in AI_FAILURE_TAGS:
            raise ValueError(f"Model response failure_tags must use known tags: {', '.join(sorted(AI_FAILURE_TAGS))}")
        tags.append(item)
    return tags


def _string_list(value: Any, key: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"Model response {key} must be a list.")
    strings: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"Model response {key} must contain non-empty strings.")
        strings.append(item.strip())
    return strings


def _likely_ball_region(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    region = _object_with_fields(value, "likely_ball_region", ("frame", "description", "confidence"))
    description = region["description"]
    if not isinstance(description, str) or not description.strip():
        raise ValueError("Model response likely_ball_region.description must be a non-empty string.")
    return {
        "frame": _nonnegative_int(region["frame"], "likely_ball_region.frame"),
        "description": description.strip(),
        "confidence": _bounded_number(region["confidence"], "likely_ball_region.confidence", minimum=0.0, maximum=1.0),
    }


def _local_search_roi(
    value: Any,
    *,
    frame_dimensions: dict[str, int] | None,
    window: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if value is None:
        return None
    roi = _object_with_fields(
        value,
        "local_search_roi",
        ("coordinate_space", "frame", "x", "y", "width", "height", "confidence"),
    )
    if roi["coordinate_space"] != "image":
        raise ValueError("Model response local_search_roi.coordinate_space must be image.")
    frame = _nonnegative_int(roi["frame"], "local_search_roi.frame")
    x = _bounded_number(roi["x"], "local_search_roi.x", minimum=0.0)
    y = _bounded_number(roi["y"], "local_search_roi.y", minimum=0.0)
    width = _bounded_number(roi["width"], "local_search_roi.width", minimum=0.0, exclusive_minimum=True)
    height = _bounded_number(roi["height"], "local_search_roi.height", minimum=0.0, exclusive_minimum=True)
    if frame_dimensions is not None:
        frame_width = frame_dimensions["width"]
        frame_height = frame_dimensions["height"]
        if x + width > frame_width or y + height > frame_height:
            raise ValueError("Model response local_search_roi must fit inside known frame dimensions.")
    if window is not None:
        start_frame = _safe_int(window.get("start_frame"))
        end_frame = _safe_int(window.get("end_frame"))
        if end_frame >= start_frame and (frame < start_frame or frame > end_frame):
            raise ValueError("Model response local_search_roi.frame must fall inside the packet window.")
    return {
        "coordinate_space": "image",
        "frame": frame,
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "confidence": _bounded_number(roi["confidence"], "local_search_roi.confidence", minimum=0.0, maximum=1.0),
    }


def _best_subclip(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    subclip = _object_with_fields(value, "best_subclip", ("start_frame", "end_frame", "reason"))
    start_frame = _nonnegative_int(subclip["start_frame"], "best_subclip.start_frame")
    end_frame = _nonnegative_int(subclip["end_frame"], "best_subclip.end_frame")
    if end_frame < start_frame:
        raise ValueError("Model response best_subclip.end_frame must be greater than or equal to start_frame.")
    reason = subclip["reason"]
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("Model response best_subclip.reason must be a non-empty string.")
    return {"start_frame": start_frame, "end_frame": end_frame, "reason": reason.strip()}


def _object_with_fields(value: Any, key: str, required: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"Model response {key} must be an object or null.")
    missing = [field for field in required if field not in value]
    if missing:
        raise ValueError(f"Model response {key} missing fields: {', '.join(missing)}")
    unexpected = [field for field in value if field not in required]
    if unexpected:
        raise ValueError(f"Model response {key} has unsupported fields: {', '.join(sorted(unexpected))}")
    return value


def _nonnegative_int(value: Any, key: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"Model response {key} must be a nonnegative integer.")
    return value


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


def _confidence(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Model response confidence must be a number between 0 and 1.")
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0.0 or parsed > 1.0:
        raise ValueError("Model response confidence must be between 0 and 1.")
    return parsed


def _summary(reviews: list[dict[str, Any]], errors: list[dict[str, Any]]) -> dict[str, Any]:
    verdicts = [
        item["review"]["verdict"]
        for item in reviews
        if isinstance(item.get("review"), dict) and isinstance(item["review"].get("verdict"), str)
    ]
    counts = Counter(verdicts)
    if errors and not verdicts and all(error.get("error_type") == "strong_visual_model_unavailable" for error in errors):
        status = "unavailable"
    elif errors and not verdicts:
        status = "error"
    elif errors:
        status = "warn"
    else:
        status = "ok"
    return {
        "status": status,
        "packet_count": len(reviews),
        "reviewed_count": len(verdicts),
        "error_count": len(errors),
        "counts_by_verdict": dict(sorted(counts.items())),
        "accepted_highlight_count": counts.get(VERDICT_ACCEPT_HIGHLIGHT, 0),
        "needs_human_review_count": counts.get(VERDICT_NEEDS_HUMAN_REVIEW, 0),
        "reject_noise_count": counts.get(VERDICT_REJECT_NOISE, 0),
    }


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


def _packet_media_path(output_dir: Path, packet: dict[str, Any], key: str) -> Path:
    media = packet.get("media")
    if not isinstance(media, dict):
        raise ValueError(f"Packet has no media object for {key}.")
    raw_path = media.get(key)
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError(f"Packet is missing media.{key}.")
    path = _resolve_packet_media_path(output_dir, Path(raw_path))
    if path is None or not path.exists() or not path.is_file():
        raise FileNotFoundError(f"Packet media not found: {key}")
    return path


def _resolve_packet_media_path(output_dir: Path, raw_path: Path) -> Path | None:
    output_root = output_dir.resolve()
    candidates = (
        [raw_path]
        if raw_path.is_absolute()
        else [output_dir / raw_path, _python_backend_root() / raw_path, _repo_root() / raw_path]
    )
    for candidate in candidates:
        resolved = candidate.resolve()
        if not resolved.exists() or not resolved.is_file():
            continue
        if not _is_relative_to(resolved, output_root):
            raise ValueError("Packet media must resolve inside output_dir.")
        return resolved
    return None


def _safe_error_message(exc: Exception) -> str:
    message = str(exc)
    message = re.sub(r"data:[^\s\"']*;base64,[^\s\"']+", "data:<redacted-base64>", message)
    message = re.sub(r"Bearer\s+[^\s\"']+", "Bearer <redacted>", message, flags=re.IGNORECASE)
    message = re.sub(r"sk-[A-Za-z0-9_-]+", "sk-<redacted>", message)
    return message


def _image_data_url(path: Path) -> str:
    mime_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    payload = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{payload}"


def _build_prompt(metadata: dict[str, Any]) -> str:
    return (
        "Review this packet using the supplied images and metadata. "
        "The deterministic decision label is only a routing hint, not truth. "
        "For missing-ball or reacquire diagnosis packets, inspect the contact_sheet and crop_sheet media. "
        "Return likely_ball_region and local_search_roi only when the ball is visible; if the ball is not visible, "
        "use likely_ball_region.description='not visible' and local_search_roi=null.\n\n"
        f"{json.dumps({'metadata': metadata}, ensure_ascii=False, indent=2)}"
    )


def _build_default_client() -> OpenAIVisualReviewClient:
    from football_tracking.api.ai_provider import OpenAIResponsesClient, load_provider_settings

    repo_root = Path(__file__).resolve().parents[2]
    return OpenAIVisualReviewClient(OpenAIResponsesClient(load_provider_settings(repo_root)))


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _python_backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _is_relative_to(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        loaded = json.load(handle)
    if not isinstance(loaded, dict):
        raise ValueError(f"Expected JSON object in {path}.")
    return loaded


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _safe_int(value: Any) -> int:
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
