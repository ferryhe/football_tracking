from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from football_tracking.ai_candidate_comparison import CANDIDATE_STATUSES, comparison_payload_status
from football_tracking.final_artifact_manifest import FINAL_ARTIFACT_MANIFEST_NAME
from football_tracking.media_integrity import inspect_frame

MEDIA_TYPES = {"video", "clip"}
TRACK_TYPES = {"track"}
HIGHLIGHT_EVENT_TYPES = {"goal_candidate", "shot_candidate"}
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
    ball_audit_artifact: dict[str, Any] | None = None,
    camera_motion_audit_artifact: dict[str, Any] | None = None,
    event_candidates_artifact: dict[str, Any] | None = None,
    max_samples: int = 5,
) -> dict[str, Any]:
    if mode not in {"dry-run", "artifact-only", "real"}:
        raise ValueError("mode must be one of dry-run, artifact-only, real")

    output_dir = Path(output_dir)
    final_manifest_artifact = final_manifest_artifact or _load_artifact(output_dir / FINAL_ARTIFACT_MANIFEST_NAME)
    ball_audit_artifact = ball_audit_artifact or _load_artifact(output_dir / "ball_audit.json")
    camera_motion_audit_artifact = camera_motion_audit_artifact or _load_artifact(output_dir / "camera_motion_audit.json")
    event_candidates_artifact = event_candidates_artifact or _load_artifact(output_dir / "event_candidates.json")
    review_media = _review_media_evidence(
        {
            "review_packets": review_packets_artifact,
            "ai_visual_localization": ai_visual_localization_artifact,
        },
        output_dir=output_dir,
        mode=mode,
    )
    selected: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []
    reason: str | None = None
    media_status = "unavailable"
    selected_media_count = 0
    if final_manifest_artifact["status"] != "loaded":
        reason = f"{FINAL_ARTIFACT_MANIFEST_NAME} missing or unreadable"
        status = "fail" if mode == "real" else "unavailable"
        summary = _stable_summary(
            status=status,
            selected=selected,
            media_status=status,
            review_media=review_media,
            track_quality=_track_quality_evidence(ball_audit_artifact, mode=mode),
            camera_motion=_camera_motion_evidence(camera_motion_audit_artifact),
            candidate_comparison=_candidate_comparison_evidence(output_dir, final_manifest_artifact),
            highlight_coverage=_highlight_coverage_evidence(
                selected,
                event_candidates_artifact,
                candidate_comparison={"reports": []},
                mode=mode,
            ),
            reasons=[reason],
        )
        return _check(
            "fail" if mode == "real" else "unavailable",
            reason=reason,
            selected_media_count=0,
            artifacts=[],
            review_media=review_media,
            summary=summary,
        )

    raw_selected = final_manifest_artifact["payload"].get("final_selected_artifacts")
    selected = [item for item in raw_selected if isinstance(item, dict)] if isinstance(raw_selected, list) else []
    track_quality = _track_quality_evidence(ball_audit_artifact, mode=mode)
    camera_motion = _camera_motion_evidence(camera_motion_audit_artifact)
    candidate_comparison = _candidate_comparison_evidence(output_dir, final_manifest_artifact)
    highlight_coverage = _highlight_coverage_evidence(
        selected,
        event_candidates_artifact,
        candidate_comparison=candidate_comparison,
        mode=mode,
    )
    supplemental_statuses = _statuses_that_gate_summary(
        track_quality,
        camera_motion,
        candidate_comparison,
        highlight_coverage,
    )

    if not isinstance(raw_selected, list) or not selected:
        reason = "final_selected_artifacts is missing or empty"
        status = _worst_status(["fail" if mode == "real" else "unavailable", *supplemental_statuses])
        summary = _stable_summary(
            status=status,
            selected=selected,
            media_status="fail" if mode == "real" else "unavailable",
            review_media=review_media,
            track_quality=track_quality,
            camera_motion=camera_motion,
            candidate_comparison=candidate_comparison,
            highlight_coverage=highlight_coverage,
            reasons=[reason],
        )
        return _check(
            status,
            reason=reason,
            selected_media_count=0,
            artifacts=[],
            review_media=review_media,
            summary=summary,
        )

    media_items = [item for item in selected if isinstance(item, dict) and _artifact_type(item) in MEDIA_TYPES]
    selected_media_count = len(media_items)
    if not media_items:
        track_only_status = _non_media_selection_status(review_media["status"])
        reason = "final_selected_artifacts contains no video or clip artifacts"
        status = _worst_status([track_only_status, *supplemental_statuses])
        summary = _stable_summary(
            status=status,
            selected=selected,
            media_status=track_only_status,
            review_media=review_media,
            track_quality=track_quality,
            camera_motion=camera_motion,
            candidate_comparison=candidate_comparison,
            highlight_coverage=highlight_coverage,
            reasons=[reason],
        )
        return _check(
            status,
            reason=reason,
            selected_media_count=0,
            artifacts=[],
            review_media=review_media,
            summary=summary,
        )

    artifacts = [_inspect_selected_media(output_dir, item, max_samples=max_samples) for item in media_items]
    statuses = [artifact["status"] for artifact in artifacts]
    review_status = _review_media_status_for_gate(review_media["status"], mode=mode)
    if review_status in {"unavailable", "warn", "fail"}:
        statuses.append(review_status)
    media_status = _worst_status(statuses)
    status = _worst_status([media_status, *supplemental_statuses])
    summary = _stable_summary(
        status=status,
        selected=selected,
        media_status=media_status,
        review_media=review_media,
        track_quality=track_quality,
        camera_motion=camera_motion,
        candidate_comparison=candidate_comparison,
        highlight_coverage=highlight_coverage,
        reasons=_summary_reasons(
            artifacts=artifacts,
            track_quality=track_quality,
            camera_motion=camera_motion,
            candidate_comparison=candidate_comparison,
            highlight_coverage=highlight_coverage,
            review_media=review_media,
        ),
    )
    return _check(
        status,
        selected_media_count=selected_media_count,
        artifacts=artifacts,
        review_media=review_media,
        summary=summary,
    )


def _stable_summary(
    *,
    status: str,
    selected: list[dict[str, Any]],
    media_status: str,
    review_media: dict[str, Any],
    track_quality: dict[str, Any],
    camera_motion: dict[str, Any],
    candidate_comparison: dict[str, Any],
    highlight_coverage: dict[str, Any],
    reasons: list[str],
) -> dict[str, Any]:
    selected_media_count = sum(1 for item in selected if _artifact_type(item) in MEDIA_TYPES)
    selected_video_count = sum(1 for item in selected if _artifact_type(item) == "video")
    selected_clip_count = sum(1 for item in selected if _artifact_type(item) == "clip")
    selected_track_count = sum(1 for item in selected if _artifact_type(item) in TRACK_TYPES)
    return {
        "status": status,
        "selected_media_count": selected_media_count,
        "selected_track_count": selected_track_count,
        "selected_video_count": selected_video_count,
        "selected_clip_count": selected_clip_count,
        "track_quality_status": track_quality["status"],
        "camera_motion_status": camera_motion["status"],
        "candidate_comparison_status": candidate_comparison["status"],
        "highlight_coverage_status": highlight_coverage["status"],
        "review_media_status": review_media["status"],
        "media_status": media_status,
        "reasons": _short_reasons(reasons),
    }


def _statuses_that_gate_summary(*checks: dict[str, Any]) -> list[str]:
    return [check["status"] for check in checks if check.get("status") in {"fail", "warn", "unavailable"}]


def _summary_reasons(
    *,
    artifacts: list[dict[str, Any]],
    track_quality: dict[str, Any],
    camera_motion: dict[str, Any],
    candidate_comparison: dict[str, Any],
    highlight_coverage: dict[str, Any],
    review_media: dict[str, Any],
) -> list[str]:
    reasons: list[str] = []
    for artifact in artifacts:
        if artifact.get("status") not in {"pass", None}:
            media_type = artifact.get("type") or "media"
            reason = artifact.get("reason") or artifact.get("status")
            reasons.append(f"{media_type} {reason}")
    if review_media.get("status") in {"fail", "warn", "unavailable"}:
        reasons.append(f"review_media {review_media['status']}")
    for label, check in (
        ("track_quality", track_quality),
        ("camera_motion", camera_motion),
        ("candidate_comparison", candidate_comparison),
        ("highlight_coverage", highlight_coverage),
    ):
        if check.get("status") in {"fail", "warn", "unavailable"}:
            reasons.append(str(check.get("reason") or f"{label} {check['status']}"))
    return reasons


def _short_reasons(reasons: list[str]) -> list[str]:
    result: list[str] = []
    for reason in reasons:
        if not isinstance(reason, str):
            continue
        normalized = " ".join(reason.strip().split())
        if normalized and normalized not in result:
            result.append(normalized[:160])
    return result[:8]


def _track_quality_evidence(artifact: dict[str, Any], *, mode: str) -> dict[str, Any]:
    _ = mode
    if artifact["status"] != "loaded":
        return {
            "status": "unavailable",
            "artifact_status": artifact["status"],
            "reason": "ball_audit.json missing or unreadable",
        }
    payload = artifact["payload"]
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    summary_status = str(summary.get("status") or "").casefold()
    review_events = payload.get("review_events") if isinstance(payload.get("review_events"), list) else []
    review_event_count = _count_value(summary.get("review_event_count"), default=len(review_events))
    lost_gap_count = _count_value(summary.get("lost_gap_count"), default=_event_type_count(review_events, "lost_gap"))
    if summary_status in {"unavailable", "missing", "invalid", "corrupt"} or (not summary and not review_events):
        return _audit_status("unavailable", "ball_audit has no usable track quality evidence", artifact_status="loaded")
    if summary_status in {"fail", "failed", "error"}:
        return _audit_status("fail", "ball_audit summary is fail/error", artifact_status="loaded")
    if review_event_count > 0 or lost_gap_count > 0:
        return _audit_status(
            "warn",
            "ball_audit has review events or lost gaps",
            artifact_status="loaded",
            review_event_count=review_event_count,
            lost_gap_count=lost_gap_count,
        )
    return _audit_status("pass", None, artifact_status="loaded", review_event_count=review_event_count)


def _camera_motion_evidence(artifact: dict[str, Any]) -> dict[str, Any]:
    if artifact["status"] != "loaded":
        return {
            "status": "unavailable",
            "artifact_status": artifact["status"],
            "reason": "camera_motion_audit.json missing or unreadable",
        }
    payload = artifact["payload"]
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    review_events = payload.get("review_events") if isinstance(payload.get("review_events"), list) else []
    severities = {str(event.get("severity") or "").casefold() for event in review_events if isinstance(event, dict)}
    summary_status = str(summary.get("status") or "").casefold()
    review_event_count = _count_value(summary.get("review_event_count"), default=len(review_events))
    if summary_status in {"unavailable", "missing", "invalid", "corrupt"}:
        return _audit_status(
            "unavailable",
            "camera_motion_audit summary is unavailable",
            artifact_status="loaded",
            review_event_count=review_event_count,
        )
    if "fail" in severities or summary_status in {"fail", "failed", "error"}:
        return _audit_status(
            "fail",
            "camera_motion_audit has fail review events",
            artifact_status="loaded",
            review_event_count=review_event_count,
        )
    if "warn" in severities or summary_status in {"warn", "warning"}:
        return _audit_status(
            "warn",
            "camera_motion_audit has warn review events",
            artifact_status="loaded",
            review_event_count=review_event_count,
        )
    if review_event_count == 0 or summary_status in {"ok", "pass"}:
        return _audit_status("pass", None, artifact_status="loaded", review_event_count=review_event_count)
    return _audit_status("warn", "camera_motion_audit has review events", artifact_status="loaded", review_event_count=review_event_count)


def _candidate_comparison_evidence(output_dir: Path, final_manifest: dict[str, Any]) -> dict[str, Any]:
    reports = _candidate_comparison_reports(output_dir, final_manifest)
    status_counts = {status: 0 for status in CANDIDATE_STATUSES}
    for report in reports:
        status = report.get("status")
        if status not in status_counts:
            status = "unavailable"
        status_counts[status] += 1
    if reports:
        expanded_statuses = [
            status_value
            for status_value, count in status_counts.items()
            for _index in range(count)
        ]
        status = _worst_candidate_status(expanded_statuses)
    else:
        status = "unavailable" if _manifest_has_candidate_outputs(final_manifest) else "pass"
    reason = None
    if status == "fail":
        reason = "candidate comparison report failed"
    elif status == "warn":
        reason = "candidate comparison report warned"
    elif status == "unavailable":
        reason = "candidate comparison report unavailable"
    return {
        "status": status,
        "report_count": len(reports),
        "status_counts": status_counts,
        "reports": reports,
        "reason": reason,
    }


def _candidate_comparison_reports(output_dir: Path, final_manifest: dict[str, Any]) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    output_root = output_dir.resolve()
    if final_manifest["status"] != "loaded":
        return reports
    manifest_reports = final_manifest["payload"].get("comparison_reports")
    if not isinstance(manifest_reports, list):
        return reports
    seen_paths: set[str] = set()
    for item in manifest_reports:
        if not isinstance(item, dict):
            continue
        path_value = item.get("path") or item.get("report_path")
        if isinstance(path_value, str) and path_value.strip():
            resolved, path_error = _resolve_output_path(output_dir, path_value.strip())
            if path_error is not None or resolved is None:
                reports.append(_manifest_comparison_summary(item, status="unavailable", artifact_status=path_error or "unsafe_path"))
                continue
            loaded_summary = _comparison_report_summary(
                _load_artifact(resolved),
                path=resolved,
                output_root=output_root,
                manifest_entry=item,
            )
            merged = (
                _merge_comparison_summaries(loaded_summary, _manifest_comparison_summary(item))
                if _manifest_comparison_has_status(item)
                else loaded_summary
            )
            if str(resolved) not in seen_paths or merged["status"] != loaded_summary["status"]:
                reports.append(merged)
                seen_paths.add(str(resolved))
            continue
        reports.append(_manifest_comparison_summary(item))
    return reports


def _comparison_report_summary(
    artifact: dict[str, Any],
    *,
    path: Path,
    output_root: Path,
    manifest_entry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if artifact["status"] != "loaded":
        return {
            "path": str(path),
            "problem_type": None,
            "candidate_id": None,
            "status": "unavailable",
            "artifact_status": artifact["status"],
        }
    payload = artifact["payload"]
    status_payload = comparison_payload_status(payload)
    candidate = payload.get("candidate") if isinstance(payload.get("candidate"), dict) else {}
    missing_candidate_artifacts = _missing_candidate_artifacts(payload, output_root=output_root)
    status = status_payload["status"]
    artifact_status = status_payload["artifact_status"]
    if missing_candidate_artifacts:
        status = "fail"
        artifact_status = "candidate_artifacts_missing"
    expected_candidate_id = _optional_string((manifest_entry or {}).get("candidate_id"))
    expected_problem_type = _optional_string((manifest_entry or {}).get("problem_type"))
    payload_candidate_id = _optional_string(payload.get("candidate_id") or candidate.get("id") or candidate.get("candidate_id"))
    payload_problem_type = _optional_string(payload.get("problem_type"))
    if (
        (expected_candidate_id is not None and payload_candidate_id != expected_candidate_id)
        or (expected_problem_type is not None and payload_problem_type != expected_problem_type)
    ):
        status = "unavailable"
        artifact_status = "manifest_comparison_mismatch"
    result = {
        "path": str(path),
        "problem_type": payload_problem_type,
        "candidate_id": payload_candidate_id,
        "status": status,
        "artifact_status": artifact_status,
    }
    if missing_candidate_artifacts:
        result["missing_candidate_artifacts"] = missing_candidate_artifacts
    return result


def _manifest_comparison_summary(
    item: dict[str, Any],
    *,
    status: str | None = None,
    artifact_status: str = "manifest",
) -> dict[str, Any]:
    summary = item.get("summary") if isinstance(item.get("summary"), dict) else {}
    item_status = status or item.get("status") or summary.get("status")
    if item_status not in CANDIDATE_STATUSES:
        item_status = "unavailable"
    return {
        "path": item.get("path") or item.get("report_path"),
        "problem_type": item.get("problem_type"),
        "candidate_id": item.get("candidate_id"),
        "status": item_status,
        "artifact_status": item.get("artifact_status") or artifact_status,
    }


def _manifest_comparison_has_status(item: dict[str, Any]) -> bool:
    if item.get("status") in CANDIDATE_STATUSES:
        return True
    summary = item.get("summary")
    return isinstance(summary, dict) and summary.get("status") in CANDIDATE_STATUSES


def _merge_comparison_summaries(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    status = _worst_candidate_status([left.get("status"), right.get("status")])
    result = dict(left)
    result["status"] = status
    result["problem_type"] = left.get("problem_type") or right.get("problem_type")
    result["candidate_id"] = left.get("candidate_id") or right.get("candidate_id")
    if right.get("status") != left.get("status"):
        result["manifest_status"] = right.get("status")
    return result


def _highlight_coverage_evidence(
    selected: list[dict[str, Any]],
    event_candidates: dict[str, Any],
    *,
    candidate_comparison: dict[str, Any],
    mode: str,
) -> dict[str, Any]:
    selected_clip_count = sum(1 for item in selected if _artifact_type(item) == "clip")
    highlight_report_count = sum(
        1
        for report in candidate_comparison.get("reports", [])
        if isinstance(report, dict) and report.get("problem_type") == "highlight"
    )
    if event_candidates["status"] != "loaded":
        return {
            "status": "unavailable",
            "artifact_status": event_candidates["status"],
            "selected_clip_count": selected_clip_count,
            "highlight_comparison_report_count": highlight_report_count,
            "event_candidate_count": 0,
            "reason": "event_candidates.json missing or unreadable",
        }
    event_ids = _highlight_candidate_ids(event_candidates["payload"])
    candidate_count = len(event_ids) or _highlight_candidate_count(event_candidates["payload"])
    if candidate_count <= 0:
        return {
            "status": "pass",
            "selected_clip_count": selected_clip_count,
            "highlight_comparison_report_count": highlight_report_count,
            "event_candidate_count": candidate_count,
        }
    covered_ids = _highlight_covered_event_ids(selected, candidate_comparison=candidate_comparison)
    uncovered_ids = sorted(event_id for event_id in event_ids if not _highlight_event_is_covered(event_id, covered_ids))
    if not uncovered_ids:
        if candidate_count > 0 and not event_ids:
            status = "unavailable" if mode == "artifact-only" else "warn"
            return {
                "status": status,
                "selected_clip_count": selected_clip_count,
                "highlight_comparison_report_count": highlight_report_count,
                "event_candidate_count": candidate_count,
                "covered_event_candidate_count": 0,
                "uncovered_event_candidate_ids": [],
                "reason": "event candidates are summarized without event ids",
            }
        return {
            "status": "pass",
            "selected_clip_count": selected_clip_count,
            "highlight_comparison_report_count": highlight_report_count,
            "event_candidate_count": candidate_count,
            "covered_event_candidate_count": candidate_count,
            "uncovered_event_candidate_ids": [],
        }
    status = "unavailable" if mode == "artifact-only" else "warn"
    return {
        "status": status,
        "selected_clip_count": selected_clip_count,
        "highlight_comparison_report_count": highlight_report_count,
        "event_candidate_count": candidate_count,
        "covered_event_candidate_count": max(0, candidate_count - len(uncovered_ids)),
        "uncovered_event_candidate_ids": uncovered_ids[:20],
        "reason": "event candidates have no selected highlight clip or highlight comparison",
    }


def _audit_status(status: str, reason: str | None, **kwargs: Any) -> dict[str, Any]:
    result = {"status": status, **kwargs}
    if reason is not None:
        result["reason"] = reason
    return result


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


def _manifest_has_candidate_outputs(final_manifest: dict[str, Any]) -> bool:
    if final_manifest["status"] != "loaded":
        return False
    payload = final_manifest["payload"]
    for key in ("candidate_outputs", "final_selected_artifacts"):
        items = payload.get(key)
        if isinstance(items, list) and any(isinstance(item, dict) for item in items):
            return True
    return False


def _missing_candidate_artifacts(payload: dict[str, Any], *, output_root: Path) -> list[str]:
    artifacts = payload.get("candidate_artifacts")
    if not isinstance(artifacts, list):
        return []
    missing: list[str] = []
    for item in artifacts:
        if not isinstance(item, str) or not item.strip():
            missing.append(str(item))
            continue
        path = Path(item.strip())
        display = str(path) if path.is_absolute() else path.as_posix()
        resolved = path.resolve() if path.is_absolute() else (output_root / path).resolve()
        if not _is_relative_to(resolved, output_root) or not resolved.exists():
            missing.append(display)
    return missing


def _highlight_candidate_count(payload: dict[str, Any]) -> int:
    ids = _highlight_candidate_ids(payload)
    if ids:
        return len(ids)
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    counts_by_type = summary.get("counts_by_type") if isinstance(summary.get("counts_by_type"), dict) else {}
    return sum(_count_value(counts_by_type.get(event_type), default=0) for event_type in HIGHLIGHT_EVENT_TYPES)


def _highlight_candidate_ids(payload: dict[str, Any]) -> set[str]:
    candidates = payload.get("candidates")
    if isinstance(candidates, list):
        ids: set[str] = set()
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            candidate_type = str(candidate.get("type") or candidate.get("candidate_type") or "").casefold()
            candidate_id = _optional_string(candidate.get("id"))
            candidate_id_text = str(candidate_id or "").casefold()
            if candidate_type in HIGHLIGHT_EVENT_TYPES or any(event_type in candidate_id_text for event_type in HIGHLIGHT_EVENT_TYPES):
                ids.add(candidate_id or f"{candidate_type}:{len(ids) + 1}")
        return ids
    return set()


def _highlight_covered_event_ids(
    selected: list[dict[str, Any]],
    *,
    candidate_comparison: dict[str, Any],
) -> set[str]:
    values: set[str] = set()
    for item in selected:
        if _artifact_type(item) != "clip":
            continue
        values.update(_highlight_ref_values(item))
    for report in candidate_comparison.get("reports", []):
        if not isinstance(report, dict) or report.get("problem_type") != "highlight":
            continue
        values.update(_highlight_ref_values(report))
    return values


def _highlight_ref_values(item: dict[str, Any]) -> set[str]:
    values: set[str] = set()
    for key in ("event_candidate_id", "source_event_candidate_id", "candidate_id", "id"):
        value = _optional_string(item.get(key))
        if value is not None:
            values.add(value)
    return values


def _highlight_event_is_covered(event_id: str, covered_ids: set[str]) -> bool:
    return any(event_id == covered_id or event_id in covered_id for covered_id in covered_ids)


def _event_type_count(events: list[Any], event_type: str) -> int:
    return sum(
        1
        for event in events
        if isinstance(event, dict) and str(event.get("type") or "").casefold() == event_type
    )


def _count_value(value: Any, *, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float) and math.isfinite(value):
        return max(0, int(value))
    if isinstance(value, str):
        try:
            return max(0, int(float(value.strip())))
        except ValueError:
            return default
    return default


def _optional_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _worst_candidate_status(statuses: list[Any]) -> str:
    valid = [status for status in statuses if status in CANDIDATE_STATUSES]
    if not valid:
        return "unavailable"
    return max(valid, key=lambda status: STATUS_RANK[status])


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
