from __future__ import annotations

import json
import math
import re
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from football_tracking.ai_contracts import (
    AI_CLIP_ACTIONS,
    AI_FAILURE_TAGS,
    AI_RECOMMENDED_ACTIONS,
    AI_ROOT_CAUSE_MODULES,
)

SCHEMA_VERSION = "1.0"
REPORT_FILE_NAME = "ai_improvement_report.json"
MAX_CONTEXT_ITEMS = 100

_SOURCE_ARTIFACTS: tuple[tuple[str, str], ...] = (
    ("ball_audit", "ball_audit.json"),
    ("ai_review_triggers", "ai_review_triggers.json"),
    ("review_packets", "review_packets.json"),
    ("ai_visual_review", "ai_visual_review.json"),
    ("camera_motion_audit", "camera_motion_audit.json"),
    ("event_candidates", "event_candidates.json"),
)

_VALID_STATUSES = {"ok", "needs_rerun", "unavailable", "error"}
_REQUIRED_IMPROVEMENT_FIELDS = (
    "priority",
    "area",
    "failure_tags",
    "root_cause_module",
    "recommended_action",
    "confidence",
)
_MISSING_BALL_TAGS = {"ball_lost", "missing_ball", "lost_gap", "ball_not_visible", "missed_ball"}
_PROVIDER_PATH_KEYS = {
    "output_dir",
    "path",
    "artifact_path",
    "input_video",
    "follow_cam_video",
    "contact_sheet",
    "crop_sheet",
    "clip",
}


def build_ai_improvement_context(output_dir: Path, max_items: int = 20) -> dict[str, Any]:
    if max_items < 1:
        raise ValueError("max_items must be at least 1.")
    if max_items > MAX_CONTEXT_ITEMS:
        raise ValueError(f"max_items must be at most {MAX_CONTEXT_ITEMS}.")

    output_dir = Path(output_dir)
    artifacts: dict[str, Any] = {}
    artifact_status: dict[str, str] = {}
    source_artifacts: dict[str, str | None] = {}
    warnings: list[str] = []

    for artifact_key, file_name in _SOURCE_ARTIFACTS:
        path = output_dir / file_name
        loaded, status, warning = _read_optional_json(path)
        artifact_status[artifact_key] = "available" if status == "loaded" else status
        source_artifacts[artifact_key] = file_name if status == "loaded" else None
        if warning is not None:
            warnings.append(warning)
        if loaded is not None:
            artifacts[artifact_key] = _limit_artifact_payload(artifact_key, _strip_data_urls(loaded), max_items=max_items)

    return {
        "output_dir": str(output_dir.resolve()),
        "max_items": max_items,
        "source_artifacts": source_artifacts,
        "artifact_status": artifact_status,
        "available_artifact_count": len(artifacts),
        "artifacts": artifacts,
        "warnings": warnings,
    }


def build_ai_improvement_report(
    output_dir: Path,
    *,
    client: Any = None,
    model: str | None = None,
    dry_run: bool = False,
    max_items: int = 20,
    objective: str | None = None,
    language: str | None = None,
) -> dict[str, Any]:
    output_dir = Path(output_dir)
    context = build_ai_improvement_context(output_dir, max_items=max_items)
    selected_model = _selected_model(client, model)

    if context["available_artifact_count"] <= 0:
        return _report(
            output_dir=output_dir,
            context=context,
            model=selected_model,
            dry_run=dry_run,
            status="unavailable",
            warnings=context["warnings"],
        )

    if dry_run:
        return _dry_run_report(output_dir=output_dir, context=context, model=selected_model)

    active_client = client
    if active_client is None:
        active_client = _build_default_client()
        selected_model = _selected_model(active_client, model)

    if hasattr(active_client, "is_enabled") and not active_client.is_enabled():
        return _report(
            output_dir=output_dir,
            context=context,
            model=selected_model,
            dry_run=False,
            status="unavailable",
            warnings=[*context["warnings"], "OpenAI provider is not configured."],
        )

    try:
        response = active_client.create_json_response(
            instructions=_instructions(language=language),
            prompt=_prompt(context=context, objective=objective, language=language),
            model=model,
            temperature=0.1,
        )
        improvements, highlight_adjustments, validation_warnings, summary_status, primary_issue = _validate_model_report(response)
    except Exception as exc:
        return _report(
            output_dir=output_dir,
            context=context,
            model=selected_model,
            dry_run=False,
            status="error",
            warnings=context["warnings"],
            error=_safe_error_message(exc, _client_api_key(active_client)),
        )

    return _report(
        output_dir=output_dir,
        context=context,
        model=selected_model,
        dry_run=False,
        status=summary_status,
        primary_issue=primary_issue,
        improvements=improvements,
        highlight_adjustments=highlight_adjustments,
        warnings=[*context["warnings"], *validation_warnings],
    )


def write_ai_improvement_report(
    output_dir: Path,
    *,
    client: Any = None,
    model: str | None = None,
    dry_run: bool = False,
    max_items: int = 20,
    objective: str | None = None,
    language: str | None = None,
) -> dict[str, Any]:
    output_dir = Path(output_dir)
    report = build_ai_improvement_report(
        output_dir,
        client=client,
        model=model,
        dry_run=dry_run,
        max_items=max_items,
        objective=objective,
        language=language,
    )
    _write_json(output_dir / REPORT_FILE_NAME, report)
    return report


def compact_ai_improvement_summary(report: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(report, dict):
        return None
    summary = report.get("summary")
    if not isinstance(summary, dict):
        return None
    status = summary.get("status")
    if not isinstance(status, str):
        return None
    return {
        "status": status,
        "primary_issue": summary.get("primary_issue") if isinstance(summary.get("primary_issue"), str) else None,
        "improvement_count": _safe_int(summary.get("improvement_count")),
        "targeted_rerun_count": _safe_int(summary.get("targeted_rerun_count")),
        "config_patch_count": _safe_int(summary.get("config_patch_count")),
        "highlight_adjustment_count": _safe_int(summary.get("highlight_adjustment_count")),
    }


def _dry_run_report(*, output_dir: Path, context: dict[str, Any], model: str | None) -> dict[str, Any]:
    lost_gap = _first_lost_gap(context)
    if lost_gap is None:
        return _report(
            output_dir=output_dir,
            context=context,
            model=model,
            dry_run=True,
            status="ok",
            warnings=context["warnings"],
        )

    start_frame = _safe_int(lost_gap.get("start_frame"))
    end_frame = _safe_int(lost_gap.get("end_frame"))
    if end_frame < start_frame:
        start_frame, end_frame = end_frame, start_frame
    midpoint = start_frame + max(0, end_frame - start_frame) // 2
    improvement = {
        "id": "imp_001",
        "priority": "P1" if str(lost_gap.get("severity") or "") != "fail" else "P0",
        "area": "tracking",
        "failure_tags": ["ball_lost"],
        "root_cause_module": "reacquisition",
        "start_frame": start_frame,
        "end_frame": end_frame,
        "diagnosis": "dry-run: deterministic lost-gap review suggests a targeted rerun window.",
        "recommended_action": "targeted_rerun",
        "config_patch": {},
        "rerun_scope": {
            "start_frame": max(0, start_frame - 30),
            "end_frame": end_frame + 30,
        },
        "likely_ball_region": {
            "frame": midpoint,
            "description": "not visible",
            "confidence": 0.0,
        },
        "evidence": [str(lost_gap.get("reason") or "lost gap artifact")],
        "confidence": 0.4,
    }
    return _report(
        output_dir=output_dir,
        context=context,
        model=model,
        dry_run=True,
        status="needs_rerun",
        primary_issue="tracking",
        improvements=[improvement],
        warnings=context["warnings"],
    )


def _validate_model_report(
    response: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str], str, str | None]:
    if not isinstance(response, dict):
        raise ValueError("Model response must be a JSON object.")

    summary = response.get("summary")
    if not isinstance(summary, dict):
        raise ValueError("Model response summary must be a JSON object.")
    status = summary.get("status")
    if not isinstance(status, str) or status not in _VALID_STATUSES:
        raise ValueError("Model response summary.status must be one of: ok, needs_rerun, unavailable, error.")

    raw_improvements = response.get("improvements")
    if raw_improvements is None:
        raw_improvements = []
    if not isinstance(raw_improvements, list):
        raise ValueError("Model response improvements must be a list.")

    warnings: list[str] = []
    improvements = []
    for index, raw in enumerate(raw_improvements, start=1):
        improvement, patch_warnings = _validate_improvement(raw, index)
        warnings.extend(patch_warnings)
        improvements.append(improvement)

    raw_highlight_adjustments = response.get("highlight_adjustments")
    if raw_highlight_adjustments is None:
        raw_highlight_adjustments = []
    if not isinstance(raw_highlight_adjustments, list):
        raise ValueError("Model response highlight_adjustments must be a list.")
    highlight_adjustments = [
        _validate_highlight_adjustment(raw, index) for index, raw in enumerate(raw_highlight_adjustments, start=1)
    ]

    if status == "ok" and (improvements or highlight_adjustments):
        status = "needs_rerun"
        warnings.append("summary.status normalized from ok to needs_rerun because actions were returned.")

    primary_issue = summary.get("primary_issue")
    return improvements, highlight_adjustments, warnings, status, primary_issue if isinstance(primary_issue, str) else None


def _validate_improvement(raw: Any, index: int) -> tuple[dict[str, Any], list[str]]:
    if not isinstance(raw, dict):
        raise ValueError(f"Improvement {index} must be a JSON object.")
    missing = [field for field in _REQUIRED_IMPROVEMENT_FIELDS if field not in raw]
    if missing:
        raise ValueError(f"Improvement {index} missing required fields: {', '.join(missing)}")

    failure_tags = raw.get("failure_tags")
    if not isinstance(failure_tags, list):
        raise ValueError(f"Improvement {index} failure_tags must be a list.")
    normalized_tags = [str(item) for item in failure_tags if str(item).strip()]
    if not normalized_tags:
        raise ValueError(f"Improvement {index} failure_tags must contain at least one tag.")
    invalid_tags = [tag for tag in normalized_tags if tag not in AI_FAILURE_TAGS]
    if invalid_tags:
        raise ValueError(f"Improvement {index} failure_tags contain unsupported values: {', '.join(invalid_tags)}")
    confidence = _confidence(raw.get("confidence"), f"Improvement {index} confidence")
    recommended_action = _required_string(raw, "recommended_action", index)
    if recommended_action not in AI_RECOMMENDED_ACTIONS:
        raise ValueError(f"Improvement {index} recommended_action is unsupported: {recommended_action}")
    root_cause_module = _required_string(raw, "root_cause_module", index)
    if root_cause_module not in AI_ROOT_CAUSE_MODULES:
        raise ValueError(f"Improvement {index} root_cause_module is unsupported: {root_cause_module}")

    rerun_scope = raw.get("rerun_scope")
    if recommended_action == "targeted_rerun":
        if not isinstance(rerun_scope, dict):
            raise ValueError(f"Improvement {index} targeted_rerun requires rerun_scope.")
        rerun_scope = _frame_window(rerun_scope, f"Improvement {index} rerun_scope")
    elif isinstance(rerun_scope, dict):
        rerun_scope = _frame_window(rerun_scope, f"Improvement {index} rerun_scope")
    else:
        rerun_scope = None

    likely_ball_region = _likely_ball_region(raw.get("likely_ball_region"), index)
    local_search_roi = _local_search_roi(raw.get("local_search_roi"), index)
    if _is_missing_ball_improvement(normalized_tags, raw) and likely_ball_region is None and local_search_roi is None:
        raise ValueError(f"Improvement {index} missing-ball suggestions require likely_ball_region or local_search_roi.")

    config_patch_raw = raw.get("config_patch") if isinstance(raw.get("config_patch"), dict) else {}
    config_patch, patch_warnings = _filter_config_patch(config_patch_raw)

    item: dict[str, Any] = {
        "id": str(raw.get("id") or f"imp_{index:03d}"),
        "priority": _required_string(raw, "priority", index),
        "area": _required_string(raw, "area", index),
        "failure_tags": normalized_tags,
        "root_cause_module": root_cause_module,
        "diagnosis": str(raw.get("diagnosis") or ""),
        "recommended_action": recommended_action,
        "config_patch": config_patch,
        "evidence": _evidence(raw.get("evidence")),
        "confidence": confidence,
    }
    for key in ("start_frame", "end_frame"):
        value = _optional_int(raw.get(key))
        if value is not None:
            if value < 0:
                raise ValueError(f"Improvement {index} {key} must be non-negative.")
            item[key] = value
    if rerun_scope is not None:
        item["rerun_scope"] = rerun_scope
    if likely_ball_region is not None:
        item["likely_ball_region"] = likely_ball_region
    if local_search_roi is not None:
        item["local_search_roi"] = local_search_roi
    return item, patch_warnings


def _validate_highlight_adjustment(raw: Any, index: int) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"Highlight adjustment {index} must be a JSON object.")
    candidate_id = raw.get("candidate_id")
    if not isinstance(candidate_id, str) or not candidate_id.strip():
        raise ValueError(f"Highlight adjustment {index} requires candidate_id.")
    reason = raw.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError(f"Highlight adjustment {index} requires reason.")
    return {
        "candidate_id": candidate_id.strip(),
        "current_window": _frame_window(raw.get("current_window"), f"Highlight adjustment {index} current_window"),
        "suggested_window": _frame_window(raw.get("suggested_window"), f"Highlight adjustment {index} suggested_window"),
        "reason": reason.strip(),
        "confidence": _confidence(raw.get("confidence"), f"Highlight adjustment {index} confidence"),
        **_optional_clip_action(raw.get("clip_action"), index),
    }


def _optional_clip_action(value: Any, index: int) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Highlight adjustment {index} clip_action must be a string.")
    action = value.strip()
    if action not in AI_CLIP_ACTIONS:
        raise ValueError(f"Highlight adjustment {index} clip_action is unsupported: {action}")
    return {"clip_action": action}


def _report(
    *,
    output_dir: Path,
    context: dict[str, Any],
    model: str | None,
    dry_run: bool,
    status: str,
    primary_issue: str | None = None,
    improvements: list[dict[str, Any]] | None = None,
    highlight_adjustments: list[dict[str, Any]] | None = None,
    warnings: list[str] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    improvements = improvements or []
    highlight_adjustments = highlight_adjustments or []
    if status not in _VALID_STATUSES:
        status = "error"
    summary = _summary(
        status=status,
        primary_issue=primary_issue,
        improvements=improvements,
        highlight_adjustments=highlight_adjustments,
    )
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now_iso(),
        "output_dir": str(output_dir.resolve()),
        "model": model,
        "dry_run": bool(dry_run),
        "source_artifacts": context.get("source_artifacts") or {},
        "artifact_status": context.get("artifact_status") or {},
        "summary": summary,
        "improvements": improvements,
        "highlight_adjustments": highlight_adjustments,
        "warnings": warnings or [],
    }
    if error is not None:
        report["error"] = error
    return report


def _summary(
    *,
    status: str,
    primary_issue: str | None,
    improvements: list[dict[str, Any]],
    highlight_adjustments: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "status": status,
        "primary_issue": primary_issue,
        "improvement_count": len(improvements),
        "targeted_rerun_count": sum(1 for item in improvements if item.get("recommended_action") == "targeted_rerun"),
        "config_patch_count": sum(1 for item in improvements if bool(item.get("config_patch"))),
        "highlight_adjustment_count": len(highlight_adjustments),
    }


def _instructions(language: str | None) -> str:
    language_instruction = (
        "Write human-readable fields in Simplified Chinese."
        if language == "zh"
        else "Write human-readable fields in English."
    )
    return (
        "You are diagnosing football tracking run artifacts and producing an advisory improvement report. "
        "Return strict JSON only with keys: summary, improvements, highlight_adjustments. "
        "summary.status must be one of ok, needs_rerun, unavailable, error. "
        "Each improvement must include priority, area, failure_tags, root_cause_module, recommended_action, confidence. "
        "targeted_rerun improvements must include rerun_scope. "
        "Missing-ball suggestions must include likely_ball_region or local_search_roi; use likely_ball_region.description='not visible' "
        "when the ball cannot be localized. "
        "config_patch is advisory only and may only suggest known fields under follow_cam, postprocess, "
        "scene_bias.dynamic_air_recovery, selection, or tracking. "
        "Do not include image base64 or claim files that are not present in the supplied context. "
        f"{language_instruction}"
    )


def _provider_safe_context(context: dict[str, Any]) -> dict[str, Any]:
    safe = _redact_provider_paths(context)
    return safe if isinstance(safe, dict) else {}


def _prompt(*, context: dict[str, Any], objective: str | None, language: str | None) -> str:
    payload = {
        "objective": objective,
        "response_language": "zh" if language == "zh" else "en",
        "context": _provider_safe_context(context),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _redact_provider_paths(value: Any, key_hint: str = "") -> Any:
    if isinstance(value, dict):
        return {str(key): _redact_provider_paths(child, str(key)) for key, child in value.items()}
    if isinstance(value, list):
        return [_redact_provider_paths(item, key_hint) for item in value]
    if isinstance(value, str):
        lowered = key_hint.casefold()
        if lowered in _PROVIDER_PATH_KEYS or lowered.endswith("_path"):
            return "<redacted-path>"
        if value.startswith("data:"):
            return "<redacted-data-url>"
    return value


def _read_optional_json(path: Path) -> tuple[dict[str, Any] | None, str, str | None]:
    if not path.exists():
        return None, "missing", f"{path.name} missing"
    try:
        with path.open("r", encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
        return None, "corrupt", f"{path.name} corrupt: {_safe_error_message(exc)}"
    if not isinstance(loaded, dict):
        return None, "invalid", f"{path.name} invalid: expected JSON object"
    return loaded, "loaded", None


def _limit_artifact_payload(artifact_key: str, payload: dict[str, Any], *, max_items: int) -> dict[str, Any]:
    list_keys_by_artifact = {
        "ball_audit": {"review_events", "tracklets", "sources"},
        "ai_review_triggers": {"triggers"},
        "review_packets": {"packets"},
        "ai_visual_review": {"reviews", "errors"},
        "camera_motion_audit": {"events", "review_events", "motion_events"},
        "event_candidates": {"candidates"},
    }
    list_keys = list_keys_by_artifact.get(artifact_key, set())
    limited: dict[str, Any] = {}
    for key, value in payload.items():
        if key in list_keys and isinstance(value, list):
            limited[key] = value[:max_items]
        elif isinstance(value, list):
            limited[key] = value[:max_items]
        else:
            limited[key] = value
    return limited


def _strip_data_urls(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _strip_data_urls(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_strip_data_urls(item) for item in value]
    if isinstance(value, str) and value.startswith("data:"):
        return "<redacted-data-url>"
    return value


def _first_lost_gap(context: dict[str, Any]) -> dict[str, Any] | None:
    artifacts = context.get("artifacts") if isinstance(context.get("artifacts"), dict) else {}
    ball_audit = artifacts.get("ball_audit") if isinstance(artifacts.get("ball_audit"), dict) else {}
    events = ball_audit.get("review_events") if isinstance(ball_audit.get("review_events"), list) else []
    for event in events:
        if isinstance(event, dict) and str(event.get("type") or "").casefold() == "lost_gap":
            return event

    triggers_report = artifacts.get("ai_review_triggers") if isinstance(artifacts.get("ai_review_triggers"), dict) else {}
    triggers = triggers_report.get("triggers") if isinstance(triggers_report.get("triggers"), list) else []
    for trigger in triggers:
        if isinstance(trigger, dict) and str(trigger.get("type") or "").casefold() == "lost_gap":
            return trigger
    return None


def _is_missing_ball_improvement(failure_tags: list[str], raw: dict[str, Any]) -> bool:
    lowered_tags = {tag.casefold() for tag in failure_tags}
    if lowered_tags & _MISSING_BALL_TAGS:
        return True
    text = " ".join(
        str(raw.get(key) or "")
        for key in ("area", "root_cause_module", "diagnosis", "recommended_action")
    ).casefold()
    return "missing ball" in text or "lost ball" in text or "lost_gap" in text


def _likely_ball_region(value: Any, index: int) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"Improvement {index} likely_ball_region must be an object.")
    description = value.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError(f"Improvement {index} likely_ball_region requires description.")
    region = {"description": description.strip()}
    frame = _optional_int(value.get("frame"))
    if frame is not None:
        if frame < 0:
            raise ValueError(f"Improvement {index} likely_ball_region frame must be non-negative.")
        region["frame"] = frame
    if "confidence" in value:
        region["confidence"] = _confidence(value.get("confidence"), f"Improvement {index} likely_ball_region confidence")
    return region


def _local_search_roi(value: Any, index: int) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"Improvement {index} local_search_roi must be an object.")
    required = ("coordinate_space", "frame", "x", "y", "width", "height", "confidence")
    missing = [key for key in required if key not in value]
    if missing:
        raise ValueError(f"Improvement {index} local_search_roi missing fields: {', '.join(missing)}")
    coordinate_space = value.get("coordinate_space")
    if coordinate_space != "image":
        raise ValueError(f"Improvement {index} local_search_roi coordinate_space must be image.")
    width = _positive_number(value.get("width"), f"Improvement {index} local_search_roi width")
    height = _positive_number(value.get("height"), f"Improvement {index} local_search_roi height")
    return {
        "coordinate_space": coordinate_space,
        "frame": _required_nonnegative_int(value.get("frame"), f"Improvement {index} local_search_roi frame"),
        "x": _nonnegative_number(value.get("x"), f"Improvement {index} local_search_roi x"),
        "y": _nonnegative_number(value.get("y"), f"Improvement {index} local_search_roi y"),
        "width": width,
        "height": height,
        "confidence": _confidence(value.get("confidence"), f"Improvement {index} local_search_roi confidence"),
    }


def _frame_window(value: Any, label: str) -> dict[str, int]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object.")
    start = _required_int(value.get("start_frame"), f"{label}.start_frame")
    end = _required_int(value.get("end_frame"), f"{label}.end_frame")
    if start < 0 or end < 0:
        raise ValueError(f"{label} frames must be non-negative.")
    if end < start:
        raise ValueError(f"{label}.end_frame must be greater than or equal to start_frame.")
    return {"start_frame": start, "end_frame": end}


def _required_string(raw: dict[str, Any], key: str, index: int) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Improvement {index} {key} must be a non-empty string.")
    return value.strip()


def _evidence(value: Any) -> list[Any]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, (str, dict))]


def _confidence(value: Any, label: str) -> float:
    parsed = _number(value, label)
    if parsed < 0.0 or parsed > 1.0:
        raise ValueError(f"{label} must be between 0 and 1.")
    return round(parsed, 4)


def _positive_number(value: Any, label: str) -> float:
    parsed = _number(value, label)
    if parsed <= 0:
        raise ValueError(f"{label} must be positive.")
    return parsed


def _nonnegative_number(value: Any, label: str) -> float:
    parsed = _number(value, label)
    if parsed < 0:
        raise ValueError(f"{label} must be non-negative.")
    return parsed


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number.")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"{label} must be finite.")
    return parsed


def _required_int(value: Any, label: str) -> int:
    parsed = _optional_int(value)
    if parsed is None:
        raise ValueError(f"{label} must be an integer.")
    return parsed


def _required_nonnegative_int(value: Any, label: str) -> int:
    parsed = _required_int(value, label)
    if parsed < 0:
        raise ValueError(f"{label} must be non-negative.")
    return parsed


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    return int(parsed)


def _safe_int(value: Any) -> int:
    parsed = _optional_int(value)
    return 0 if parsed is None else max(0, parsed)


def _filter_config_patch(patch: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    filtered, warnings = _filter_config_patch_node(patch, prefix="")
    return filtered if isinstance(filtered, dict) else {}, warnings


def _filter_config_patch_node(value: Any, *, prefix: str) -> tuple[Any | None, list[str]]:
    warnings: list[str] = []
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, child in value.items():
            key_text = str(key)
            child_path = f"{prefix}.{key_text}" if prefix else key_text
            if not key_text or "." in key_text:
                warnings.append(f"config_patch path rejected by allowlist: {child_path}")
                continue
            filtered_child, child_warnings = _filter_config_patch_node(child, prefix=child_path)
            warnings.extend(child_warnings)
            if filtered_child is not None:
                result[key_text] = filtered_child
        return (result if result else None), warnings

    validator = _CONFIG_PATCH_VALIDATORS.get(prefix)
    if validator is None:
        warnings.append(f"config_patch path rejected by allowlist: {prefix}")
        return None, warnings
    if not validator(value):
        warnings.append(f"config_patch path rejected by validator: {prefix}")
        return None, warnings
    return value, warnings


def _number_between(minimum: float, maximum: float) -> Callable[[Any], bool]:
    def validate(value: Any) -> bool:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        parsed = float(value)
        return math.isfinite(parsed) and minimum <= parsed <= maximum

    return validate


def _int_between(minimum: int, maximum: int) -> Callable[[Any], bool]:
    def validate(value: Any) -> bool:
        if isinstance(value, bool) or not isinstance(value, int):
            return False
        return minimum <= value <= maximum

    return validate


def _bool_value(value: Any) -> bool:
    return isinstance(value, bool)


def _enum_value(*values: str) -> Callable[[Any], bool]:
    allowed = set(values)

    def validate(value: Any) -> bool:
        return isinstance(value, str) and value in allowed

    return validate


_CONFIG_PATCH_VALIDATORS: dict[str, Callable[[Any], bool]] = {
    "follow_cam.enabled": _bool_value,
    "follow_cam.pan_smoothing": _number_between(0.0, 1.0),
    "follow_cam.zoom_smoothing": _number_between(0.0, 1.0),
    "follow_cam.glide_pan_smoothing": _number_between(0.0, 1.0),
    "follow_cam.glide_max_pan_per_frame_x": _number_between(0.0, 5000.0),
    "follow_cam.glide_max_pan_per_frame_y": _number_between(0.0, 5000.0),
    "follow_cam.catch_up_pan_smoothing": _number_between(0.0, 1.0),
    "follow_cam.zoom_out_confirm_frames": _int_between(0, 240),
    "follow_cam.zoom_in_confirm_frames": _int_between(0, 240),
    "follow_cam.zoom_hold_frames_after_change": _int_between(0, 300),
    "follow_cam.catch_up_max_pan_per_frame_x": _number_between(0.0, 5000.0),
    "follow_cam.catch_up_max_pan_per_frame_y": _number_between(0.0, 5000.0),
    "follow_cam.predicted_pan_decay": _number_between(0.0, 1.0),
    "follow_cam.dead_zone_ratio_x": _number_between(0.0, 1.0),
    "follow_cam.dead_zone_ratio_y": _number_between(0.0, 1.0),
    "postprocess.enabled": _bool_value,
    "postprocess.max_detected_island_length": _int_between(0, 60),
    "postprocess.low_confidence_threshold": _number_between(0.0, 1.0),
    "scene_bias.dynamic_air_recovery.enabled": _bool_value,
    "scene_bias.dynamic_air_recovery.tentative_reacquire_confidence_threshold": _number_between(0.0, 1.0),
    "scene_bias.dynamic_air_recovery.tentative_reacquire_score_threshold": _number_between(0.0, 1.0),
    "scene_bias.dynamic_air_recovery.reacquire_confidence_threshold": _number_between(0.0, 1.0),
    "scene_bias.dynamic_air_recovery.reacquire_image_size": _int_between(320, 4096),
    "scene_bias.dynamic_air_recovery.edge_reentry_expand_x": _number_between(0.0, 6000.0),
    "scene_bias.dynamic_air_recovery.edge_reentry_expand_y": _number_between(0.0, 3000.0),
    "selection.min_accept_score": _number_between(0.0, 1.0),
    "selection.stable_history_length": _int_between(1, 120),
    "selection.priors.player_foot_radius_px": _number_between(0.0, 1000.0),
    "selection.priors.player_foot_bonus": _number_between(-1.0, 1.0),
    "selection.priors.recent_player_frame_window": _int_between(0, 120),
    "selection.priors.pitch_boundary_penalty": _number_between(-1.0, 1.0),
    "selection.priors.pitch_boundary_margin_m": _number_between(0.0, 20.0),
    "tracking.max_lost_frames": _int_between(0, 1000),
    "tracking.match_distance": _number_between(0.0, 10000.0),
    "tracking.max_speed": _number_between(0.0, 10000.0),
    "tracking.max_acceleration": _number_between(0.0, 10000.0),
    "tracking.prediction_mode": _enum_value("none", "linear", "constant_velocity"),
    "tracking.predicted_confidence_decay": _number_between(0.0, 1.0),
}


def _selected_model(client: Any, model: str | None) -> str | None:
    if model:
        return model
    settings = getattr(client, "settings", None)
    chat_model = getattr(settings, "chat_model", None)
    return chat_model if isinstance(chat_model, str) and chat_model else None


def _build_default_client() -> Any:
    from football_tracking.api.ai_provider import OpenAIResponsesClient, load_provider_settings

    repo_root = Path(__file__).resolve().parents[2]
    return OpenAIResponsesClient(load_provider_settings(repo_root))


def _client_api_key(client: Any) -> str:
    settings = getattr(client, "settings", None)
    api_key = getattr(settings, "api_key", "")
    return api_key if isinstance(api_key, str) else ""


def _safe_error_message(exc: Exception, api_key: str = "") -> str:
    message = str(exc)
    message = re.sub(r"data:[^\s\"']*;base64,[^\s\"']+", "data:<redacted-base64>", message)
    message = re.sub(r"Bearer\s+[^\s\"']+", "Bearer <redacted>", message, flags=re.IGNORECASE)
    message = re.sub(r"sk-[A-Za-z0-9_-]+", "sk-<redacted>", message)
    if api_key:
        message = message.replace(api_key, "<redacted-api-key>")
    return message


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
