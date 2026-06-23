from __future__ import annotations

import csv
import json
import math
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from football_tracking.ai_candidate_comparison import (
    build_candidate_comparison,
    write_candidate_comparison_report,
)
from football_tracking.ai_candidate_registry import load_candidate_registry, write_candidate_registry
from football_tracking.ball_audit import write_ball_audit_report

NOISE_CANDIDATE_COMPARISON_NAME = "noise_candidate_comparison.json"
NOISE_APPROVAL_ACTIONS = {"noise_filter_adjustment", "tighten_noise_filter", "reject_noise"}
MIN_SUSTAINED_RUN_FRAMES = 5
FALSE_POSITIVE_PASS_MIN_RATIO = 0.20
FALSE_POSITIVE_WARN_MIN_RATIO = 0.05
FALSE_POSITIVE_FAIL_INCREASE_RATIO = 0.10
SUSTAINED_COVERAGE_TOLERANCE = 0.02
SHORT_ISLAND_MERGE_GAP_FRAMES = 1
DEFAULT_STRATEGY_PROVENANCE = {
    "strategy": "temporal_chunk",
    "full_video_sahi": False,
    "full_video_spatial_split": False,
    "bounded_window_required": True,
}
ARTIFACT_NAMES = (
    "ball_track.cleaned.csv",
    "cleanup_report.json",
    "ball_audit.json",
    NOISE_CANDIDATE_COMPARISON_NAME,
    "candidate_manifest.json",
)


def build_noise_candidate_comparison(
    baseline_track_path: Path,
    candidate_track_path: Path,
    *,
    candidate_id: str,
    approval: dict[str, Any] | None = None,
    target_window: dict[str, Any] | None = None,
    baseline_audit_path: Path | None = None,
    candidate_audit_path: Path | None = None,
    strategy_provenance: dict[str, Any] | None = None,
    min_sustained_run_frames: int = MIN_SUSTAINED_RUN_FRAMES,
    require_candidate_audit: bool = False,
    candidate_dir: str | None = None,
    comparison_report: str = NOISE_CANDIDATE_COMPARISON_NAME,
    candidate_artifacts: list[str] | None = None,
) -> dict[str, Any]:
    baseline_path = Path(baseline_track_path)
    candidate_path = Path(candidate_track_path)
    candidate_id = _safe_candidate_id(candidate_id)
    artifact_check = _artifact_check(baseline_path, candidate_path)
    if artifact_check is not None:
        checks = [artifact_check]
        metrics: dict[str, Any] = {}
    else:
        baseline_rows = _read_track_rows(baseline_path)
        candidate_rows = _read_track_rows(candidate_path)
        window = _target_window(target_window, baseline_rows["rows"], candidate_rows["rows"])
        baseline_audit = _load_json(baseline_audit_path)
        candidate_audit = _load_json(candidate_audit_path)
        metrics = _comparison_metrics(
            baseline_rows["rows_by_frame"],
            candidate_rows["rows_by_frame"],
            window,
            baseline_audit=baseline_audit,
            candidate_audit=candidate_audit,
            min_sustained_run_frames=max(1, int(min_sustained_run_frames)),
        )
        provenance = _strategy_provenance(strategy_provenance, approval, window)
        checks = _comparison_checks(
            metrics,
            approval,
            candidate_id=candidate_id,
            strategy_provenance=provenance,
            candidate_audit=candidate_audit,
            require_candidate_audit=require_candidate_audit,
        )
        metrics["strategy_provenance"] = provenance

    payload = build_candidate_comparison(
        problem_type="noise",
        baseline={"path": str(baseline_path), "metrics": metrics.get("baseline", {})},
        candidate={"id": candidate_id, "path": str(candidate_path), "metrics": metrics.get("candidate", {})},
        approval=approval,
        checks=checks,
    )
    payload["metrics"] = metrics
    payload["promotion_eligible"] = payload["summary"]["promotion_eligible"]
    payload["requires_human_confirmation"] = payload["summary"]["requires_human_confirmation"]
    payload["candidate_id"] = candidate_id
    payload["approval_id"] = _approval_id(approval)
    payload["comparison_report"] = comparison_report
    payload["comparison_status"] = payload["summary"]["status"]
    payload["promotion_status"] = "not_promoted"
    payload["consumed_approval_ids"] = _consumed_approval_ids(approval)
    payload["candidate_dir"] = candidate_dir or _parent_dir(candidate_path)
    payload["candidate_artifacts"] = candidate_artifacts or [str(candidate_path)]
    if approval and isinstance(approval.get("false_positive_class"), str):
        payload["false_positive_class"] = approval["false_positive_class"].strip()
    return payload


def write_noise_candidate_comparison(
    output_dir: Path,
    baseline_track_path: Path,
    candidate_track_path: Path,
    *,
    candidate_id: str,
    approval: dict[str, Any] | None = None,
    target_window: dict[str, Any] | None = None,
    baseline_audit_path: Path | None = None,
    candidate_audit_path: Path | None = None,
    strategy_provenance: dict[str, Any] | None = None,
    min_sustained_run_frames: int = MIN_SUSTAINED_RUN_FRAMES,
    require_candidate_audit: bool = False,
    candidate_dir: str | None = None,
    comparison_report: str = NOISE_CANDIDATE_COMPARISON_NAME,
    candidate_artifacts: list[str] | None = None,
) -> Path:
    payload = build_noise_candidate_comparison(
        baseline_track_path,
        candidate_track_path,
        candidate_id=candidate_id,
        approval=approval,
        target_window=target_window,
        baseline_audit_path=baseline_audit_path,
        candidate_audit_path=candidate_audit_path,
        strategy_provenance=strategy_provenance,
        min_sustained_run_frames=min_sustained_run_frames,
        require_candidate_audit=require_candidate_audit,
        candidate_dir=candidate_dir,
        comparison_report=comparison_report,
        candidate_artifacts=candidate_artifacts,
    )
    return write_candidate_comparison_report(Path(output_dir), payload, name=NOISE_CANDIDATE_COMPARISON_NAME)


def execute_noise_cleanup_candidate(
    output_dir: Path,
    approval: dict[str, Any],
    *,
    min_sustained_run_frames: int = MIN_SUSTAINED_RUN_FRAMES,
) -> dict[str, Any]:
    output_dir = Path(output_dir)
    baseline_track = _preferred_track_path(output_dir)
    if not baseline_track.exists():
        raise FileNotFoundError(f"baseline track artifact missing: {baseline_track}")
    normalized_approval = _normalized_noise_approval(approval)
    candidate_id = normalized_approval["candidate_id"]

    baseline_rows = _read_track_rows(baseline_track)
    window = _target_window(normalized_approval, baseline_rows["rows"], baseline_rows["rows"])
    if not _has_traceable_noise_evidence(output_dir, normalized_approval, window):
        raise ValueError("noise approval requires traceable packet or visual evidence")
    strategy = _strategy_provenance_from_approval(normalized_approval, window)
    provenance_check = _strategy_provenance_check(strategy, normalized_approval)
    if provenance_check["status"] == "fail":
        raise ValueError(str(provenance_check["reason"]))
    existing_records = _existing_registry_records_for_update(output_dir, candidate_id)

    candidate_dir = output_dir / "ai_candidates" / "noise" / candidate_id
    candidate_dir.mkdir(parents=True, exist_ok=True)
    baseline_islands = _false_positive_islands(
        baseline_rows["rows_by_frame"],
        window,
        audit=_load_json(output_dir / "ball_audit.json"),
        min_sustained_run_frames=min_sustained_run_frames,
    )
    candidate_rows = deepcopy(baseline_rows["rows"])
    removed_ranges = _remove_islands(candidate_rows, baseline_islands)
    candidate_cleaned_path = candidate_dir / "ball_track.cleaned.csv"
    _write_track_rows(candidate_cleaned_path, baseline_rows["fieldnames"], candidate_rows)
    _write_track_rows(candidate_dir / "ball_track.csv", baseline_rows["fieldnames"], candidate_rows)

    cleanup_report = _cleanup_report(
        normalized_approval,
        candidate_id=candidate_id,
        target_window=window,
        removed_ranges=removed_ranges,
        strategy_provenance=strategy,
    )
    _write_json(candidate_dir / "cleanup_report.json", cleanup_report)
    write_ball_audit_report(candidate_dir)

    relative_candidate_dir = _relative_path(candidate_dir, output_dir)
    relative_comparison = f"{relative_candidate_dir}/{NOISE_CANDIDATE_COMPARISON_NAME}"
    candidate_artifacts = [f"{relative_candidate_dir}/{name}" for name in ARTIFACT_NAMES]
    comparison = build_noise_candidate_comparison(
        baseline_track,
        candidate_cleaned_path,
        candidate_id=candidate_id,
        approval=normalized_approval,
        target_window={"start_frame": window[0], "end_frame": window[1]},
        baseline_audit_path=output_dir / "ball_audit.json",
        candidate_audit_path=candidate_dir / "ball_audit.json",
        strategy_provenance=strategy,
        min_sustained_run_frames=min_sustained_run_frames,
        require_candidate_audit=True,
        candidate_dir=relative_candidate_dir,
        comparison_report=relative_comparison,
        candidate_artifacts=candidate_artifacts,
    )
    write_candidate_comparison_report(candidate_dir, comparison, name=NOISE_CANDIDATE_COMPARISON_NAME)
    manifest = _candidate_manifest(
        normalized_approval,
        candidate_id=candidate_id,
        candidate_dir=relative_candidate_dir,
        candidate_artifacts=candidate_artifacts,
        comparison=comparison,
    )
    _write_json(candidate_dir / "candidate_manifest.json", manifest)
    _write_candidate_registry(output_dir, comparison, existing_records=existing_records)
    return comparison


def _comparison_checks(
    metrics: dict[str, Any],
    approval: dict[str, Any] | None,
    *,
    candidate_id: str,
    strategy_provenance: dict[str, Any],
    candidate_audit: dict[str, Any] | None,
    require_candidate_audit: bool,
) -> list[dict[str, Any]]:
    return [
        _false_positive_reduction_check(metrics),
        _lost_frame_budget_check(metrics),
        _sustained_coverage_check(metrics),
        _approval_linkage_check(approval, candidate_id=candidate_id),
        _strategy_provenance_check(strategy_provenance, approval),
        _candidate_reaudit_check(candidate_audit, require_candidate_audit=require_candidate_audit),
    ]


def _comparison_metrics(
    baseline_rows: dict[int, dict[str, Any]],
    candidate_rows: dict[int, dict[str, Any]],
    window: tuple[int, int],
    *,
    baseline_audit: dict[str, Any] | None,
    candidate_audit: dict[str, Any] | None,
    min_sustained_run_frames: int,
) -> dict[str, Any]:
    start, end = window
    window_frame_count = end - start + 1
    baseline_islands = _false_positive_islands(
        baseline_rows,
        window,
        audit=baseline_audit,
        min_sustained_run_frames=min_sustained_run_frames,
    )
    candidate_islands = _false_positive_islands(
        candidate_rows,
        window,
        audit=candidate_audit,
        min_sustained_run_frames=min_sustained_run_frames,
    )
    baseline_lost = _lost_frames(baseline_rows, window)
    candidate_lost = _lost_frames(candidate_rows, window)
    baseline_sustained = _sustained_detected_frames(
        baseline_rows,
        window,
        min_sustained_run_frames=min_sustained_run_frames,
    )
    candidate_sustained = _sustained_detected_frames(
        candidate_rows,
        window,
        min_sustained_run_frames=min_sustained_run_frames,
    )
    removed_islands = [
        island
        for island in baseline_islands
        if not any(_is_detected(candidate_rows.get(frame)) for frame in range(island["start_frame"], island["end_frame"] + 1))
    ]
    baseline_count = len(baseline_islands)
    candidate_count = len(candidate_islands)
    island_delta = baseline_count - candidate_count
    decrease_ratio = (island_delta / baseline_count) if baseline_count > 0 else 0.0
    increase_ratio = ((candidate_count - baseline_count) / baseline_count) if baseline_count > 0 else (1.0 if candidate_count > 0 else 0.0)
    baseline_coverage = baseline_sustained / window_frame_count if window_frame_count > 0 else 0.0
    candidate_coverage = candidate_sustained / window_frame_count if window_frame_count > 0 else 0.0
    lost_tolerance = max(math.ceil(window_frame_count * 0.01), 15)
    return {
        "target_window": {"start_frame": start, "end_frame": end, "frame_count": window_frame_count},
        "min_sustained_run_frames": min_sustained_run_frames,
        "baseline_false_positive_islands": baseline_count,
        "candidate_false_positive_islands": candidate_count,
        "false_positive_island_delta": island_delta,
        "false_positive_island_decrease_ratio": round(decrease_ratio, 6),
        "false_positive_island_increase_ratio": round(increase_ratio, 6),
        "baseline_false_positive_island_ranges": baseline_islands,
        "candidate_false_positive_island_ranges": candidate_islands,
        "removed_false_positive_island_ranges": removed_islands,
        "baseline_lost_frames": len(baseline_lost),
        "candidate_lost_frames": len(candidate_lost),
        "lost_frame_increase": len(candidate_lost) - len(baseline_lost),
        "lost_frame_tolerance": lost_tolerance,
        "baseline_sustained_detected_frames": baseline_sustained,
        "candidate_sustained_detected_frames": candidate_sustained,
        "baseline_sustained_detected_coverage": round(baseline_coverage, 6),
        "candidate_sustained_detected_coverage": round(candidate_coverage, 6),
        "sustained_coverage_decrease": round(baseline_coverage - candidate_coverage, 6),
        "sustained_coverage_tolerance": SUSTAINED_COVERAGE_TOLERANCE,
        "baseline": {
            "false_positive_islands": baseline_count,
            "lost_frames": len(baseline_lost),
            "sustained_detected_coverage": round(baseline_coverage, 6),
        },
        "candidate": {
            "false_positive_islands": candidate_count,
            "lost_frames": len(candidate_lost),
            "sustained_detected_coverage": round(candidate_coverage, 6),
        },
    }


def _false_positive_reduction_check(metrics: dict[str, Any]) -> dict[str, Any]:
    baseline = int(metrics["baseline_false_positive_islands"])
    candidate = int(metrics["candidate_false_positive_islands"])
    decrease = int(metrics["false_positive_island_delta"])
    decrease_ratio = float(metrics["false_positive_island_decrease_ratio"])
    increase_ratio = float(metrics["false_positive_island_increase_ratio"])
    if candidate > baseline and increase_ratio > FALSE_POSITIVE_FAIL_INCREASE_RATIO:
        status = "fail"
        reason = "false-positive islands increased by more than 10 percent"
    elif candidate > baseline:
        status = "fail"
        reason = "false-positive islands increased"
    elif decrease >= 2 or decrease_ratio >= FALSE_POSITIVE_PASS_MIN_RATIO:
        status = "pass"
        reason = "false-positive islands decreased enough for PR3 pass threshold"
    elif decrease > 0 and decrease_ratio >= FALSE_POSITIVE_WARN_MIN_RATIO:
        status = "warn"
        reason = "false-positive islands decreased by at least 5 percent, but below the PR3 pass threshold"
    else:
        status = "fail"
        reason = (
            "false-positive island decrease is below the 5 percent warn threshold"
            if decrease > 0
            else "candidate does not reduce false-positive islands"
        )
    return {
        "name": "false_positive_island_reduction",
        "status": status,
        "baseline_value": baseline,
        "candidate_value": candidate,
        "absolute_decrease": decrease,
        "decrease_ratio": decrease_ratio,
        "increase_ratio": increase_ratio,
        "reason": reason,
    }


def _lost_frame_budget_check(metrics: dict[str, Any]) -> dict[str, Any]:
    increase = int(metrics["lost_frame_increase"])
    tolerance = int(metrics["lost_frame_tolerance"])
    status = "pass" if increase <= tolerance else "fail"
    return {
        "name": "lost_frame_budget_preserved",
        "status": status,
        "baseline_value": int(metrics["baseline_lost_frames"]),
        "candidate_value": int(metrics["candidate_lost_frames"]),
        "lost_frame_increase": increase,
        "maximum_allowed_increase": tolerance,
        "reason": (
            "lost-frame increase stays within PR3 tolerance"
            if status == "pass"
            else "lost-frame increase exceeds PR3 tolerance"
        ),
    }


def _sustained_coverage_check(metrics: dict[str, Any]) -> dict[str, Any]:
    decrease = float(metrics["sustained_coverage_decrease"])
    status = "pass" if decrease <= SUSTAINED_COVERAGE_TOLERANCE else "fail"
    return {
        "name": "sustained_detected_coverage_preserved",
        "status": status,
        "baseline_value": float(metrics["baseline_sustained_detected_coverage"]),
        "candidate_value": float(metrics["candidate_sustained_detected_coverage"]),
        "coverage_decrease": decrease,
        "maximum_allowed_decrease": SUSTAINED_COVERAGE_TOLERANCE,
        "reason": (
            "sustained detected coverage stays within PR3 tolerance"
            if status == "pass"
            else "sustained detected coverage drops beyond PR3 tolerance"
        ),
    }


def _approval_linkage_check(approval: dict[str, Any] | None, *, candidate_id: str) -> dict[str, Any]:
    if not isinstance(approval, dict):
        return {"name": "approval_linkage", "status": "unavailable", "reason": "approval provenance is absent"}
    approval_id = _approval_id(approval)
    if approval_id is None:
        return {"name": "approval_linkage", "status": "fail", "reason": "approval_id is required"}
    action = str(approval.get("approved_action") or "")
    if action not in NOISE_APPROVAL_ACTIONS:
        return {
            "name": "approval_linkage",
            "status": "fail",
            "approval_id": approval_id,
            "approved_action": action,
            "reason": "approved_action is not a noise cleanup action",
        }
    approval_candidate_id = approval.get("candidate_id")
    if isinstance(approval_candidate_id, str) and approval_candidate_id.strip() and approval_candidate_id.strip() != candidate_id:
        return {
            "name": "approval_linkage",
            "status": "fail",
            "approval_id": approval_id,
            "candidate_id": candidate_id,
            "approval_candidate_id": approval_candidate_id,
            "reason": "approval candidate_id does not match comparison candidate",
        }
    if not isinstance(approval.get("false_positive_class"), str) or not approval["false_positive_class"].strip():
        return {"name": "approval_linkage", "status": "fail", "approval_id": approval_id, "reason": "false_positive_class is required"}
    if _window_from_mapping(approval) is None:
        return {"name": "approval_linkage", "status": "fail", "approval_id": approval_id, "reason": "bounded frame window is required"}
    if _approval_evidence_ids(approval):
        return {
            "name": "approval_linkage",
            "status": "pass",
            "approval_id": approval_id,
            "candidate_id": candidate_id,
            "false_positive_class": approval["false_positive_class"],
            "reason": "approval is bounded and tied to packet or visual evidence",
        }
    return {
        "name": "approval_linkage",
        "status": "fail",
        "approval_id": approval_id,
        "reason": "noise cleanup approval lacks packet or visual evidence",
    }


def _strategy_provenance_check(strategy_provenance: dict[str, Any], approval: dict[str, Any] | None) -> dict[str, Any]:
    strategy = str(strategy_provenance.get("strategy") or "").casefold()
    full_video_sahi = strategy_provenance.get("full_video_sahi") is True
    full_video_spatial = strategy_provenance.get("full_video_spatial_split") is True
    name_implies_full_video = "full_video_sahi" in strategy or "full_video_spatial_split" in strategy
    unbounded_strategy = strategy in {
        "full_video_sahi",
        "full_video_spatial_split",
        "broad_spatial_split",
        "unbounded_full_video_spatial",
        "unbounded_sahi",
    }
    bounded_window = _window_from_mapping(strategy_provenance) or (
        _window_from_mapping(approval) if isinstance(approval, dict) else None
    )
    if full_video_sahi or full_video_spatial or name_implies_full_video or unbounded_strategy:
        return {
            "name": "bounded_strategy_provenance",
            "status": "fail",
            "strategy": strategy_provenance,
            "reason": "unbounded full-video spatial/SAHI cleanup provenance is not allowed",
        }
    if "sahi" in strategy and bounded_window is None:
        return {
            "name": "bounded_strategy_provenance",
            "status": "fail",
            "strategy": strategy_provenance,
            "reason": "SAHI/ROI cleanup requires an explicit bounded window",
        }
    return {
        "name": "bounded_strategy_provenance",
        "status": "pass",
        "strategy": strategy_provenance,
        "reason": "cleanup strategy is temporal or bounded to explicit approved evidence",
    }


def _candidate_reaudit_check(audit: dict[str, Any] | None, *, require_candidate_audit: bool) -> dict[str, Any]:
    if audit is None:
        return {
            "name": "candidate_reaudit",
            "status": "unavailable" if require_candidate_audit else "pass",
            "reason": "candidate ball_audit.json is missing" if require_candidate_audit else "candidate re-audit was not required",
        }
    summary = audit.get("summary") if isinstance(audit.get("summary"), dict) else {}
    return {
        "name": "candidate_reaudit",
        "status": "pass",
        "review_event_count": summary.get("review_event_count"),
        "reason": "candidate ball_audit.json was regenerated from candidate tracks",
    }


def _false_positive_islands(
    rows_by_frame: dict[int, dict[str, Any]],
    window: tuple[int, int],
    *,
    audit: dict[str, Any] | None,
    min_sustained_run_frames: int,
) -> list[dict[str, int]]:
    start, end = window
    detected_frames = [frame for frame in range(start, end + 1) if _is_detected(rows_by_frame.get(frame))]
    runs = _runs_with_gap(detected_frames, max_gap=SHORT_ISLAND_MERGE_GAP_FRAMES)
    islands = [_range(run) for run in runs if len(run) < min_sustained_run_frames]
    islands.extend(_audit_false_positive_ranges(audit, window))
    return _merge_ranges(islands)


def _audit_false_positive_ranges(audit: dict[str, Any] | None, window: tuple[int, int]) -> list[dict[str, int]]:
    if not isinstance(audit, dict):
        return []
    events = audit.get("review_events")
    if not isinstance(events, list):
        return []
    ranges: list[dict[str, int]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        if _event_is_candidate_cleanup_provenance(event):
            continue
        if not _event_marks_false_positive(event):
            continue
        event_window = _window_from_mapping(event)
        if event_window is None or not _windows_overlap(event_window, window):
            continue
        ranges.append(
            {
                "start_frame": max(window[0], event_window[0]),
                "end_frame": min(window[1], event_window[1]),
                "frame_count": min(window[1], event_window[1]) - max(window[0], event_window[0]) + 1,
            }
        )
    return ranges


def _event_is_candidate_cleanup_provenance(event: dict[str, Any]) -> bool:
    if str(event.get("type") or "").casefold() != "postprocess_action":
        return False
    evidence = event.get("evidence") if isinstance(event.get("evidence"), dict) else {}
    action = evidence.get("action") if isinstance(evidence, dict) else None
    text = json.dumps(action or event, ensure_ascii=False).casefold()
    return "bounded_noise_cleanup_candidate" in text or "remove_short_false_positive_island" in text


def _event_marks_false_positive(event: dict[str, Any]) -> bool:
    event_type = str(event.get("type") or "").casefold()
    if event_type in {"short_tracklet", "postprocess_action", "dense_noise_cluster"}:
        return True
    text = json.dumps(event, ensure_ascii=False).casefold()
    return any(token in text for token in ("false_positive", "reject_noise", "dense_noise", "short detected island"))


def _lost_frames(rows_by_frame: dict[int, dict[str, Any]], window: tuple[int, int]) -> list[int]:
    start, end = window
    return [frame for frame in range(start, end + 1) if _is_lost(rows_by_frame.get(frame))]


def _sustained_detected_frames(
    rows_by_frame: dict[int, dict[str, Any]],
    window: tuple[int, int],
    *,
    min_sustained_run_frames: int,
) -> int:
    start, end = window
    detected_frames = [frame for frame in range(start, end + 1) if _is_detected(rows_by_frame.get(frame))]
    return sum(len(run) for run in _contiguous_runs(detected_frames) if len(run) >= min_sustained_run_frames)


def _remove_islands(rows: list[dict[str, Any]], islands: list[dict[str, int]]) -> list[dict[str, Any]]:
    ranges = {(item["start_frame"], item["end_frame"]) for item in islands}
    removed: list[dict[str, Any]] = []
    for row in rows:
        frame = _parse_int(row.get("Frame"))
        if frame is None:
            continue
        for start, end in ranges:
            if start <= frame <= end and _is_detected(_row_for_metrics(row)):
                _clear_detection(row)
                break
    for start, end in sorted(ranges):
        removed.append({"start_frame": start, "end_frame": end, "frame_count": end - start + 1})
    return removed


def _read_track_rows(path: Path) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    rows_by_frame: dict[int, dict[str, Any]] = {}
    with Path(path).open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        for raw_row in reader:
            row = dict(raw_row)
            rows.append(row)
            frame = _parse_int(row.get("Frame"))
            if frame is not None:
                rows_by_frame[frame] = _row_for_metrics(row)
    return {"fieldnames": fieldnames or ["Frame", "X", "Y", "Confidence", "Status"], "rows": rows, "rows_by_frame": rows_by_frame}


def _write_track_rows(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized_fieldnames = list(fieldnames)
    for required in ("Frame", "X", "Y", "Confidence", "Status"):
        if required not in normalized_fieldnames:
            normalized_fieldnames.append(required)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=normalized_fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _row_for_metrics(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": str(row.get("Status") or row.get("status") or "").strip(),
        "x": _parse_float(row.get("X") if "X" in row else row.get("x")),
        "y": _parse_float(row.get("Y") if "Y" in row else row.get("y")),
        "confidence": _parse_float(row.get("Confidence") if "Confidence" in row else row.get("confidence")),
    }


def _clear_detection(row: dict[str, Any]) -> None:
    for key in ("X", "Y", "x", "y"):
        if key in row:
            row[key] = ""
    for key in ("Confidence", "confidence"):
        if key in row:
            row[key] = "0.00"
    if "Status" in row:
        row["Status"] = "Lost"
    elif "status" in row:
        row["status"] = "Lost"
    else:
        row["Status"] = "Lost"


def _artifact_check(baseline_path: Path, candidate_path: Path) -> dict[str, Any] | None:
    missing = [str(path) for path in (baseline_path, candidate_path) if not path.exists()]
    if not missing:
        return None
    return {
        "name": "required_artifacts_available",
        "status": "unavailable",
        "reason": "required track artifacts are absent",
        "missing_paths": missing,
    }


def _target_window(
    target_window: dict[str, Any] | None,
    baseline_rows: list[dict[str, Any]],
    candidate_rows: list[dict[str, Any]],
) -> tuple[int, int]:
    if isinstance(target_window, dict):
        parsed = _window_from_mapping(target_window)
        if parsed is not None:
            return parsed
    frames = [
        frame
        for row in [*baseline_rows, *candidate_rows]
        if (frame := _parse_int(row.get("Frame"))) is not None
    ]
    if not frames:
        return 0, 0
    return min(frames), max(frames)


def _preferred_track_path(output_dir: Path) -> Path:
    cleaned = output_dir / "ball_track.cleaned.csv"
    return cleaned if cleaned.exists() else output_dir / "ball_track.csv"


def _normalized_noise_approval(approval: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(approval, dict):
        raise TypeError("noise approval must be a mapping")
    action = str(approval.get("approved_action") or "")
    if action not in NOISE_APPROVAL_ACTIONS:
        raise ValueError("approved_action must be a noise cleanup action")
    approval_id = _approval_id(approval)
    if approval_id is None:
        raise ValueError("noise approval requires approval_id")
    window = _window_from_mapping(approval)
    if window is None:
        raise ValueError("noise approval requires bounded start_frame and end_frame")
    false_positive_class = approval.get("false_positive_class")
    if not isinstance(false_positive_class, str) or not false_positive_class.strip():
        raise ValueError("noise approval requires false_positive_class")
    candidate_id = approval.get("candidate_id")
    if not isinstance(candidate_id, str) or not candidate_id.strip():
        raise ValueError("noise approval requires explicit candidate_id")
    normalized = dict(approval)
    normalized["candidate_id"] = _safe_candidate_id(candidate_id)
    normalized["start_frame"] = window[0]
    normalized["end_frame"] = window[1]
    return normalized


def _strategy_provenance_from_approval(approval: dict[str, Any], window: tuple[int, int]) -> dict[str, Any]:
    for key in ("strategy_provenance", "inference_strategy_provenance"):
        value = approval.get(key)
        if isinstance(value, dict):
            return _strategy_provenance(value, approval, window)
    return _strategy_provenance(None, approval, window)


def _strategy_provenance(
    strategy_provenance: dict[str, Any] | None,
    approval: dict[str, Any] | None,
    window: tuple[int, int],
) -> dict[str, Any]:
    result = dict(DEFAULT_STRATEGY_PROVENANCE)
    if isinstance(strategy_provenance, dict):
        result.update(strategy_provenance)
    result.setdefault("target_window", {"start_frame": window[0], "end_frame": window[1]})
    if "start_frame" not in result:
        result["start_frame"] = window[0]
    if "end_frame" not in result:
        result["end_frame"] = window[1]
    if isinstance(approval, dict):
        evidence_ids = _approval_evidence_ids(approval)
        if evidence_ids:
            result["evidence_ids"] = evidence_ids
    return result


def _cleanup_report(
    approval: dict[str, Any],
    *,
    candidate_id: str,
    target_window: tuple[int, int],
    removed_ranges: list[dict[str, Any]],
    strategy_provenance: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "generated_at": _utc_now_iso(),
        "candidate_id": candidate_id,
        "approval_id": _approval_id(approval),
        "problem_type": "noise",
        "approved_action": approval.get("approved_action"),
        "false_positive_class": approval.get("false_positive_class"),
        "target_window": {"start_frame": target_window[0], "end_frame": target_window[1]},
        "strategy_provenance": strategy_provenance,
        "actions": [
            {
                "action": "remove_short_false_positive_island",
                "start_frame": item["start_frame"],
                "end_frame": item["end_frame"],
                "island_length": item["frame_count"],
                "reason": "bounded_noise_cleanup_candidate",
            }
            for item in removed_ranges
        ],
        "summary": {
            "removed_island_count": len(removed_ranges),
            "removed_frame_count": sum(int(item.get("frame_count") or 0) for item in removed_ranges),
        },
    }


def _candidate_manifest(
    approval: dict[str, Any],
    *,
    candidate_id: str,
    candidate_dir: str,
    candidate_artifacts: list[str],
    comparison: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "generated_at": _utc_now_iso(),
        "candidate_id": candidate_id,
        "approval_id": _approval_id(approval),
        "problem_type": "noise",
        "candidate_dir": candidate_dir,
        "candidate_artifacts": candidate_artifacts,
        "comparison_report": comparison.get("comparison_report"),
        "comparison_status": comparison.get("comparison_status"),
        "promotion_status": "not_promoted",
        "false_positive_class": approval.get("false_positive_class"),
        "consumed_approval_ids": _consumed_approval_ids(approval),
    }


def _existing_registry_records_for_update(output_dir: Path, candidate_id: str) -> list[dict[str, Any]]:
    loaded = load_candidate_registry(output_dir)
    artifact_status = loaded.get("artifact_status")
    if artifact_status not in {"loaded", "missing"}:
        raise ValueError(f"Cannot update ai_candidate_registry.json with artifact_status={artifact_status}")
    existing_records: list[dict[str, Any]] = []
    if loaded.get("artifact_status") == "loaded" and isinstance(loaded.get("candidates"), list):
        for item in loaded["candidates"]:
            if not isinstance(item, dict):
                continue
            if item.get("candidate_id") == candidate_id:
                continue
            existing_records.append(item)
    return existing_records


def _write_candidate_registry(output_dir: Path, comparison: dict[str, Any], *, existing_records: list[dict[str, Any]]) -> None:
    write_candidate_registry(output_dir, records=existing_records, comparison_reports=[comparison])


def _has_traceable_noise_evidence(output_dir: Path, approval: dict[str, Any], window: tuple[int, int]) -> bool:
    evidence_ids = set(_approval_evidence_ids(approval))
    if not evidence_ids:
        return False
    review_packets = _load_json(output_dir / "review_packets.json")
    packets = review_packets.get("packets") if isinstance(review_packets, dict) else None
    if isinstance(packets, list):
        for packet in packets:
            if not isinstance(packet, dict):
                continue
            packet_ids = {
                str(packet.get(key)).strip()
                for key in ("packet_id", "id", "source_packet_id")
                if isinstance(packet.get(key), str) and str(packet.get(key)).strip()
            }
            packet_window = _window_from_mapping(packet)
            if evidence_ids & packet_ids and packet_window is not None and _windows_overlap(packet_window, window):
                return True
    visual_review = _load_json(output_dir / "ai_visual_review.json")
    reviews = visual_review.get("reviews") if isinstance(visual_review, dict) else None
    if isinstance(reviews, list):
        for review in reviews:
            if not isinstance(review, dict):
                continue
            visual_ids = {
                str(review.get(key)).strip()
                for key in ("visual_review_id", "id", "packet_id", "source_packet_id")
                if isinstance(review.get(key), str) and str(review.get(key)).strip()
            }
            review_window = _window_from_mapping(review)
            if evidence_ids & visual_ids and (review_window is None or _windows_overlap(review_window, window)):
                return True
    return False


def _is_detected(row: dict[str, Any] | None) -> bool:
    if not isinstance(row, dict):
        return False
    status = str(row.get("status") or "").strip()
    return status == "Detected" and row.get("x") is not None and row.get("y") is not None


def _is_lost(row: dict[str, Any] | None) -> bool:
    if not isinstance(row, dict):
        return True
    status = str(row.get("status") or "").strip().casefold()
    if status in {"lost", "missing", ""}:
        return True
    return row.get("x") is None or row.get("y") is None


def _runs_with_gap(frames: list[int], *, max_gap: int) -> list[list[int]]:
    if not frames:
        return []
    runs: list[list[int]] = []
    current = [frames[0]]
    for frame in frames[1:]:
        if frame - current[-1] <= max_gap + 1:
            current.append(frame)
        else:
            runs.append(current)
            current = [frame]
    runs.append(current)
    return runs


def _contiguous_runs(frames: list[int]) -> list[list[int]]:
    return _runs_with_gap(frames, max_gap=0)


def _range(run: list[int]) -> dict[str, int]:
    return {"start_frame": run[0], "end_frame": run[-1], "frame_count": len(run)}


def _merge_ranges(ranges: list[dict[str, int]]) -> list[dict[str, int]]:
    valid = sorted(
        (
            {"start_frame": item["start_frame"], "end_frame": item["end_frame"]}
            for item in ranges
            if item.get("start_frame") is not None and item.get("end_frame") is not None
        ),
        key=lambda item: (item["start_frame"], item["end_frame"]),
    )
    merged: list[dict[str, int]] = []
    for item in valid:
        start = int(item["start_frame"])
        end = int(item["end_frame"])
        if end < start:
            start, end = end, start
        if not merged or start > merged[-1]["end_frame"] + SHORT_ISLAND_MERGE_GAP_FRAMES + 1:
            merged.append({"start_frame": start, "end_frame": end, "frame_count": end - start + 1})
            continue
        merged[-1]["end_frame"] = max(merged[-1]["end_frame"], end)
        merged[-1]["frame_count"] = merged[-1]["end_frame"] - merged[-1]["start_frame"] + 1
    return merged


def _windows_overlap(left: tuple[int, int], right: tuple[int, int]) -> bool:
    return left[0] <= right[1] and left[1] >= right[0]


def _window_from_mapping(value: dict[str, Any] | None) -> tuple[int, int] | None:
    if not isinstance(value, dict):
        return None
    source = value.get("window") if isinstance(value.get("window"), dict) else value
    start = _parse_int(source.get("start_frame"))
    end = _parse_int(source.get("end_frame"))
    if start is None or end is None:
        return None
    if end < start:
        return None
    return start, end


def _approval_evidence_ids(approval: dict[str, Any] | None) -> list[str]:
    if not isinstance(approval, dict):
        return []
    result: list[str] = []
    for key in ("source_packet_id", "visual_review_id"):
        value = approval.get(key)
        if isinstance(value, str) and value.strip() and value.strip() not in result:
            result.append(value.strip())
    evidence = approval.get("evidence")
    if isinstance(evidence, list):
        for item in evidence:
            if not isinstance(item, dict):
                continue
            for key in ("source_packet_id", "visual_review_id", "packet_id"):
                value = item.get(key)
                if isinstance(value, str) and value.strip() and value.strip() not in result:
                    result.append(value.strip())
    return result


def _approval_id(approval: dict[str, Any] | None) -> str | None:
    if not isinstance(approval, dict):
        return None
    value = approval.get("approval_id")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _consumed_approval_ids(approval: dict[str, Any] | None) -> list[str]:
    approval_id = _approval_id(approval)
    return [approval_id] if approval_id is not None else []


def _parent_dir(path: Path) -> str | None:
    parent = Path(path).parent
    return None if str(parent) == "." else str(parent)


def _safe_candidate_id(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("candidate_id must be a non-empty string")
    candidate_id = value.strip()
    path = Path(candidate_id)
    if (
        candidate_id != value
        or candidate_id in {".", ".."}
        or path.name != candidate_id
        or any(separator in candidate_id for separator in ("/", "\\"))
        or ":" in candidate_id
        or ".." in candidate_id
        or candidate_id.rstrip(" .") != candidate_id
        or any(ord(character) < 32 for character in candidate_id)
    ):
        raise ValueError("candidate_id must be a safe identifier")
    return candidate_id


def _relative_path(path: Path, root: Path) -> str:
    return Path(path).resolve().relative_to(Path(root).resolve()).as_posix()


def _load_json(path: Path | None) -> dict[str, Any] | None:
    if path is None or not Path(path).exists():
        return None
    try:
        loaded = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _parse_int(value: Any) -> int | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return None


def _parse_float(value: Any) -> float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
