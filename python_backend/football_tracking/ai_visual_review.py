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

AI_VISUAL_REVIEW_INSTRUCTIONS = (
    "You are a conservative football highlight visual review agent. "
    "Review the contact sheet, crop sheet, and packet metadata. "
    "Return strict JSON only with keys: verdict, confidence, reason, match_ball_visible, "
    "marker_alignment, highlight_publishable, recommended_action, visual_evidence. "
    "Allowed verdict values: accept_highlight, needs_human_review, reject_noise. "
    "Allowed match_ball_visible values: yes, partial, no, unclear. "
    "Allowed marker_alignment values: good, mixed, off, unclear. "
    "Allowed recommended_action values: keep_highlight, send_to_human, discard. "
    "Do not treat the packet decision label highlight_worthy as ground truth. "
    "Only choose accept_highlight when the match ball is visible in multiple frames, the marker "
    "stays consistently close to the real match ball, and the candidate clip appears publishable. "
    "If the ball is occluded, the marker drifts, or the target could be a shoe, line, sideline object, "
    "ad board, spectator item, or other non-ball object, choose needs_human_review. "
    "Choose reject_noise only when the packet is clearly not tracking a real match ball."
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
    },
    "required": [
        "verdict",
        "confidence",
        "reason",
        "match_ball_visible",
        "marker_alignment",
        "highlight_publishable",
        "recommended_action",
        "visual_evidence",
    ],
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

    reviews: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for index, packet in enumerate(selected_packets, start=1):
        packet_id = _packet_id(packet, index)
        review_item = _base_review_item(packet, packet_id)
        try:
            if dry_run:
                review_item["review"] = _dry_run_review()
            else:
                contact_sheet = _packet_media_path(output_dir, packet, "contact_sheet")
                crop_sheet = _packet_media_path(output_dir, packet, "crop_sheet")
                metadata = _packet_metadata(packet, packet_id)
                response = active_client.review_packet(
                    packet=packet,
                    metadata=metadata,
                    contact_sheet_data_url=_image_data_url(contact_sheet),
                    crop_sheet_data_url=_image_data_url(crop_sheet),
                    model=model,
                )
                review_item["review"] = _validate_review_response(response)
        except Exception as exc:
            error = {
                "packet_id": packet_id,
                "error": _safe_error_message(exc),
                "error_type": exc.__class__.__name__,
            }
            review_item["error"] = error["error"]
            errors.append(error)
        reviews.append(review_item)

    summary = _summary(reviews, errors)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now_iso(),
        "output_dir": str(output_dir.resolve()),
        "source_review_packets": str((output_dir / "review_packets.json").resolve()),
        "model": model,
        "dry_run": bool(dry_run),
        "filters": {
            "only_labels": list(only_labels) if only_labels is not None else None,
            "max_packets": max_packets,
        },
        "prompt_version": "visual-review-v1",
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


def _base_review_item(packet: dict[str, Any], packet_id: str) -> dict[str, Any]:
    return {
        "packet_id": packet_id,
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
    }


def _validate_review_response(response: Any) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise ValueError("Model response must be a JSON object.")

    required = (
        "verdict",
        "confidence",
        "reason",
        "match_ball_visible",
        "marker_alignment",
        "highlight_publishable",
        "recommended_action",
        "visual_evidence",
    )
    missing = [key for key in required if key not in response]
    if missing:
        raise ValueError(f"Model response missing required fields: {', '.join(missing)}")

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

    return {
        "verdict": verdict,
        "confidence": confidence,
        "reason": reason.strip(),
        "match_ball_visible": match_ball_visible,
        "marker_alignment": marker_alignment,
        "highlight_publishable": highlight_publishable,
        "recommended_action": recommended_action,
        "visual_evidence": evidence_items,
    }


def _enum_value(response: dict[str, Any], key: str, allowed: tuple[str, ...]) -> str:
    value = response.get(key)
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(f"Model response {key} must be one of: {', '.join(allowed)}")
    return value


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
    return {
        "packet_count": len(reviews),
        "reviewed_count": len(verdicts),
        "error_count": len(errors),
        "counts_by_verdict": dict(sorted(counts.items())),
        "accepted_highlight_count": counts.get(VERDICT_ACCEPT_HIGHLIGHT, 0),
        "needs_human_review_count": counts.get(VERDICT_NEEDS_HUMAN_REVIEW, 0),
        "reject_noise_count": counts.get(VERDICT_REJECT_NOISE, 0),
    }


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
    candidates = [raw_path] if raw_path.is_absolute() else [output_dir / raw_path, _repo_root() / raw_path]
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
        "The deterministic decision label is only a routing hint, not truth.\n\n"
        f"{json.dumps({'metadata': metadata}, ensure_ascii=False, indent=2)}"
    )


def _build_default_client() -> OpenAIVisualReviewClient:
    from football_tracking.api.ai_provider import OpenAIResponsesClient, load_provider_settings

    repo_root = Path(__file__).resolve().parents[2]
    return OpenAIVisualReviewClient(OpenAIResponsesClient(load_provider_settings(repo_root)))


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


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
