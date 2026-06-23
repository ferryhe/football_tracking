from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from football_tracking.ai_candidate_comparison import (
    build_candidate_comparison,
    write_candidate_comparison_report,
)

SUSTAINED_RECOVERY_MIN_FRAMES = 24
SHORT_ISLAND_MAX_FRAMES = 3
ROI_PLAUSIBILITY_PADDING_PX = 32.0


def build_missing_ball_recovery_comparison(
    baseline_track_path: Path,
    candidate_track_path: Path,
    *,
    candidate_id: str,
    approval: dict[str, Any] | None = None,
    target_window: dict[str, Any] | None = None,
    candidate_audit_path: Path | None = None,
    require_candidate_audit: bool = False,
    review_packets_path: Path | None = None,
    require_packet_coverage: bool = False,
) -> dict[str, Any]:
    baseline_path = Path(baseline_track_path)
    candidate_path = Path(candidate_track_path)
    artifact_check = _artifact_check(baseline_path, candidate_path)
    if artifact_check is not None:
        checks = [artifact_check]
        metrics: dict[str, Any] = {}
    else:
        baseline_rows = _read_track_rows(baseline_path)
        candidate_rows = _read_track_rows(candidate_path)
        window = _target_window(target_window, baseline_rows, candidate_rows)
        metrics = _comparison_metrics(baseline_rows, candidate_rows, window)
        candidate_audit = _load_candidate_audit(candidate_audit_path)
        review_packets = _load_review_packets(review_packets_path)
        checks = _comparison_checks(
            metrics,
            approval,
            candidate_id=candidate_id,
            candidate_rows=candidate_rows,
            target_window=window,
            candidate_audit=candidate_audit,
            require_candidate_audit=require_candidate_audit,
            review_packets=review_packets,
            require_packet_coverage=require_packet_coverage,
        )

    payload = build_candidate_comparison(
        problem_type="missing_ball",
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
    payload["comparison_report"] = "missing_ball_recovery_comparison.json"
    payload["comparison_status"] = payload["summary"]["status"]
    payload["promotion_status"] = "not_promoted"
    payload["consumed_approval_ids"] = _consumed_approval_ids(approval)
    payload["candidate_artifacts"] = [str(candidate_path)]
    return payload


def write_missing_ball_recovery_comparison(
    output_dir: Path,
    baseline_track_path: Path,
    candidate_track_path: Path,
    *,
    candidate_id: str,
    approval: dict[str, Any] | None = None,
    target_window: dict[str, Any] | None = None,
    candidate_audit_path: Path | None = None,
    require_candidate_audit: bool = False,
    review_packets_path: Path | None = None,
    require_packet_coverage: bool = False,
) -> Path:
    payload = build_missing_ball_recovery_comparison(
        baseline_track_path,
        candidate_track_path,
        candidate_id=candidate_id,
        approval=approval,
        target_window=target_window,
        candidate_audit_path=candidate_audit_path,
        require_candidate_audit=require_candidate_audit,
        review_packets_path=review_packets_path,
        require_packet_coverage=require_packet_coverage,
    )
    return write_candidate_comparison_report(
        Path(output_dir),
        payload,
        name="missing_ball_recovery_comparison.json",
    )


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


def _comparison_checks(
    metrics: dict[str, Any],
    approval: dict[str, Any] | None,
    *,
    candidate_id: str,
    candidate_rows: dict[int, dict[str, Any]],
    target_window: tuple[int, int],
    candidate_audit: dict[str, Any] | None,
    require_candidate_audit: bool,
    review_packets: dict[str, Any] | None,
    require_packet_coverage: bool,
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    baseline_lost = int(metrics["baseline_lost_frames"])
    candidate_lost = int(metrics["candidate_lost_frames"])
    sustained = int(metrics["sustained_recovered_frames"])
    islands = int(metrics["new_short_false_positive_islands"])
    lost_delta = baseline_lost - candidate_lost

    checks.append(
        {
            "name": "lost_gap_reduced",
            "status": "pass" if lost_delta >= SUSTAINED_RECOVERY_MIN_FRAMES else "fail",
            "baseline_value": baseline_lost,
            "candidate_value": candidate_lost,
            "reason": _lost_gap_reduced_reason(lost_delta),
        }
    )
    checks.append(
        {
            "name": "sustained_recovered_frames",
            "status": "pass" if sustained >= SUSTAINED_RECOVERY_MIN_FRAMES else "fail",
            "candidate_value": sustained,
            "minimum_value": SUSTAINED_RECOVERY_MIN_FRAMES,
            "reason": "candidate recovers a sustained segment" if sustained >= SUSTAINED_RECOVERY_MIN_FRAMES else "only short recovery spans found",
        }
    )
    checks.append(
        {
            "name": "short_false_positive_islands",
            "status": "pass" if islands == 0 else "fail",
            "candidate_value": islands,
            "maximum_value": 0,
            "reason": "candidate adds no short noisy islands" if islands == 0 else "candidate recovery is fragmented into short noisy islands",
        }
    )
    missing_frames = int(metrics["candidate_missing_frames"])
    checks.append(
        {
            "name": "candidate_frame_coverage",
            "status": "pass" if missing_frames == 0 else "fail",
            "candidate_value": missing_frames,
            "maximum_value": 0,
            "reason": (
                "candidate track covers every baseline lost frame in the target window"
                if missing_frames == 0
                else "candidate track is missing frames in the target window"
            ),
        }
    )
    checks.append(_approval_linkage_check(approval, candidate_id=candidate_id))
    checks.append(
        _candidate_reaudit_check(
            candidate_audit,
            target_window=target_window,
            require_candidate_audit=require_candidate_audit,
        )
    )
    checks.append(_localize_roi_plausibility_check(approval, candidate_rows))
    checks.append(_match_ball_confirmation_check(approval))
    checks.append(
        _packet_evidence_coverage_check(
            approval,
            review_packets,
            require_packet_coverage=require_packet_coverage,
        )
    )
    return checks


def _approval_linkage_check(approval: dict[str, Any] | None, *, candidate_id: str) -> dict[str, Any]:
    if not isinstance(approval, dict):
        return {"name": "approval_linkage", "status": "unavailable", "reason": "approval provenance is absent"}
    approval_id = approval.get("approval_id")
    if not isinstance(approval_id, str) or not approval_id.strip():
        return {"name": "approval_linkage", "status": "fail", "reason": "approval_id is required"}
    approved_action = approval.get("approved_action")
    if approved_action not in {"localize_ball_roi", "targeted_rerun"}:
        return {
            "name": "approval_linkage",
            "status": "fail",
            "approval_id": approval_id,
            "approved_action": approved_action,
            "reason": "approved_action is not a missing-ball recovery action",
        }
    approval_candidate_id = approval.get("candidate_id")
    if not isinstance(approval_candidate_id, str) or not approval_candidate_id.strip():
        return {
            "name": "approval_linkage",
            "status": "fail",
            "approval_id": approval_id,
            "approved_action": approved_action,
            "candidate_id": candidate_id,
            "reason": "approval candidate_id is required",
        }
    if isinstance(approval_candidate_id, str) and approval_candidate_id.strip() and approval_candidate_id != candidate_id:
        return {
            "name": "approval_linkage",
            "status": "fail",
            "approval_id": approval_id,
            "approved_action": approved_action,
            "candidate_id": candidate_id,
            "approval_candidate_id": approval_candidate_id,
            "reason": "approval candidate_id does not match comparison candidate",
        }
    if approval.get("source_packet_id") not in (None, "") or approval.get("visual_review_id") not in (None, ""):
        return {
            "name": "approval_linkage",
            "status": "pass",
            "approval_id": approval_id,
            "approved_action": approved_action,
            "candidate_id": candidate_id,
            "reason": "approval is tied to packet or visual review provenance",
        }
    return {
        "name": "approval_linkage",
        "status": "fail",
        "approval_id": approval_id,
        "reason": "approval lacks packet or visual review provenance",
    }


def _comparison_metrics(
    baseline_rows: dict[int, dict[str, Any]],
    candidate_rows: dict[int, dict[str, Any]],
    window: tuple[int, int],
) -> dict[str, Any]:
    start, end = window
    baseline_lost_frames = [frame for frame in range(start, end + 1) if _row_status(baseline_rows.get(frame)) == "Lost"]
    candidate_missing_frames = [frame for frame in baseline_lost_frames if frame not in candidate_rows]
    candidate_lost_frames = [frame for frame in baseline_lost_frames if not _is_detected_status(_row_status(candidate_rows.get(frame)))]
    recovered_frames = [frame for frame in baseline_lost_frames if _is_detected_status(_row_status(candidate_rows.get(frame)))]
    recovered_runs = _contiguous_runs(recovered_frames)
    short_islands = [run for run in recovered_runs if len(run) <= SHORT_ISLAND_MAX_FRAMES]
    sustained_runs = [run for run in recovered_runs if len(run) >= SUSTAINED_RECOVERY_MIN_FRAMES]
    return {
        "target_window": {"start_frame": start, "end_frame": end},
        "baseline_lost_frames": len(baseline_lost_frames),
        "candidate_lost_frames": len(candidate_lost_frames),
        "candidate_missing_frames": len(candidate_missing_frames),
        "candidate_missing_frame_ranges": [_range(run) for run in _contiguous_runs(candidate_missing_frames)],
        "lost_gap_reduction_frames": len(baseline_lost_frames) - len(candidate_lost_frames),
        "sustained_recovered_frames": sum(len(run) for run in sustained_runs),
        "new_short_false_positive_islands": len(short_islands),
        "short_false_positive_island_ranges": [_range(run) for run in short_islands],
        "baseline": {
            "lost_frames": len(baseline_lost_frames),
            "longest_lost_gap": _longest_run(_contiguous_runs(baseline_lost_frames)),
        },
        "candidate": {
            "lost_frames": len(candidate_lost_frames),
            "longest_lost_gap": _longest_run(_contiguous_runs(candidate_lost_frames)),
        },
    }


def _target_window(
    target_window: dict[str, Any] | None,
    baseline_rows: dict[int, dict[str, Any]],
    candidate_rows: dict[int, dict[str, Any]],
) -> tuple[int, int]:
    if isinstance(target_window, dict):
        start = _parse_int(target_window.get("start_frame"))
        end = _parse_int(target_window.get("end_frame"))
        if start is not None and end is not None:
            return (start, end) if start <= end else (end, start)
    frames = sorted(set(baseline_rows) | set(candidate_rows))
    if not frames:
        return 0, 0
    return frames[0], frames[-1]


def _read_track_rows(path: Path) -> dict[int, dict[str, Any]]:
    rows: dict[int, dict[str, Any]] = {}
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            frame = _parse_int(row.get("Frame"))
            if frame is None:
                continue
            rows[frame] = {
                "status": str(row.get("Status") or "").strip() or "Lost",
                "x": _parse_float(row.get("X")),
                "y": _parse_float(row.get("Y")),
            }
    return rows


def _candidate_reaudit_check(
    audit: dict[str, Any] | None,
    *,
    target_window: tuple[int, int],
    require_candidate_audit: bool,
) -> dict[str, Any]:
    if audit is None:
        status = "unavailable" if require_candidate_audit else "pass"
        return {
            "name": "candidate_reaudit",
            "status": status,
            "reason": "candidate ball_audit.json is missing" if require_candidate_audit else "candidate re-audit was not required",
        }
    events = audit.get("review_events")
    if not isinstance(events, list):
        return {"name": "candidate_reaudit", "status": "unavailable", "reason": "candidate audit has no review_events list"}
    overlapping = [event for event in events if isinstance(event, dict) and _event_overlaps(event, target_window)]
    fail_events = [event for event in overlapping if event.get("severity") == "fail"]
    warn_events = [event for event in overlapping if event.get("severity") == "warn"]
    if fail_events:
        return {
            "name": "candidate_reaudit",
            "status": "fail",
            "reason": "candidate audit contains failing events inside the recovery window",
            "event_count": len(fail_events),
            "event_types": sorted({str(event.get("type")) for event in fail_events}),
        }
    if warn_events:
        return {
            "name": "candidate_reaudit",
            "status": "warn",
            "reason": "candidate audit contains warning events inside the recovery window",
            "event_count": len(warn_events),
            "event_types": sorted({str(event.get("type")) for event in warn_events}),
        }
    return {
        "name": "candidate_reaudit",
        "status": "pass",
        "reason": "candidate audit has no warning or failing events inside the recovery window",
        "event_count": len(overlapping),
    }


def _localize_roi_plausibility_check(
    approval: dict[str, Any] | None,
    candidate_rows: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    if not isinstance(approval, dict):
        return {"name": "localize_roi_plausibility", "status": "pass", "reason": "no localize ROI to validate"}
    approvals = _localize_approvals(approval)
    if not approvals:
        return {"name": "localize_roi_plausibility", "status": "pass", "reason": "not a localize approval"}
    results = [_single_localize_roi_check(item, candidate_rows) for item in approvals]
    failed = [item for item in results if item["status"] == "fail"]
    unavailable = [item for item in results if item["status"] == "unavailable"]
    if failed:
        return {
            "name": "localize_roi_plausibility",
            "status": "fail",
            "reason": "one or more localize approvals recover a point outside the ROI",
            "results": results,
        }
    if unavailable:
        return {
            "name": "localize_roi_plausibility",
            "status": "unavailable",
            "reason": "one or more localize approvals could not be checked against the candidate point",
            "results": results,
        }
    return {
        "name": "localize_roi_plausibility",
        "status": "pass",
        "reason": "all localize approval ROI frames match candidate points",
        "results": results,
    }


def _single_localize_roi_check(
    approval: dict[str, Any],
    candidate_rows: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    roi = approval.get("local_search_roi")
    approval_id = approval.get("approval_id")
    if not isinstance(roi, dict):
        return {"approval_id": approval_id, "status": "unavailable", "reason": "localize approval has no ROI"}
    frame = _parse_int(roi.get("frame"))
    if frame is None:
        return {"approval_id": approval_id, "status": "unavailable", "reason": "localize ROI has no frame"}
    row = candidate_rows.get(frame)
    if row is None or not _is_detected_status(_row_status(row)):
        return {
            "approval_id": approval_id,
            "status": "unavailable",
            "reason": "candidate has no detected point on the localize ROI frame",
            "frame": frame,
        }
    x = row.get("x")
    y = row.get("y")
    if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
        return {"approval_id": approval_id, "status": "unavailable", "reason": "candidate ROI frame has no point"}
    bounds = _effective_roi_bounds(approval) or _roi_bounds(roi)
    if bounds is None:
        return {"approval_id": approval_id, "status": "unavailable", "reason": "localize ROI bounds are invalid"}
    left, top, right, bottom = bounds
    inside = left <= float(x) <= right and top <= float(y) <= bottom
    window = _approval_window(approval)
    outside_frames: list[int] = []
    checked_frames = 0
    if window is not None:
        start, end = window
        for candidate_frame in range(start, end + 1):
            candidate_row = candidate_rows.get(candidate_frame)
            if not _is_detected_status(_row_status(candidate_row)):
                continue
            point_x = candidate_row.get("x") if isinstance(candidate_row, dict) else None
            point_y = candidate_row.get("y") if isinstance(candidate_row, dict) else None
            if not isinstance(point_x, (int, float)) or not isinstance(point_y, (int, float)):
                continue
            checked_frames += 1
            if not (left <= float(point_x) <= right and top <= float(point_y) <= bottom):
                outside_frames.append(candidate_frame)
    if inside and outside_frames:
        inside = False
    return {
        "approval_id": approval_id,
        "status": "pass" if inside else "fail",
        "reason": "candidate point lies inside localize ROI" if inside else "candidate point is outside localize ROI",
        "frame": frame,
        "point": {"x": round(float(x), 2), "y": round(float(y), 2)},
        "roi": {"x": left, "y": top, "right": right, "bottom": bottom},
        "checked_frame_count": checked_frames,
        "outside_frame_count": len(outside_frames),
        "outside_frame_ranges": [_range(run) for run in _contiguous_runs(outside_frames)],
    }


def _localize_approvals(approval: dict[str, Any]) -> list[dict[str, Any]]:
    related = approval.get("related_approvals")
    if isinstance(related, list):
        approvals = [item for item in related if isinstance(item, dict)]
    else:
        approvals = [approval]
    return [item for item in approvals if item.get("approved_action") == "localize_ball_roi"]


def _approval_window(approval: dict[str, Any]) -> tuple[int, int] | None:
    for key in ("rerun_scope", "window", "source"):
        nested = approval.get(key)
        if isinstance(nested, dict):
            parsed = _window_from_mapping(nested)
            if parsed is not None:
                return parsed
    return _window_from_mapping(approval)


def _packet_required_window(approval: dict[str, Any]) -> tuple[int, int] | None:
    return _approval_window(approval)


def _match_ball_confirmation_check(approval: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(approval, dict):
        return {"name": "match_ball_confirmation", "status": "pass", "reason": "no localize recovery to confirm"}
    approvals = _localize_approvals(approval)
    if not approvals:
        return {"name": "match_ball_confirmation", "status": "pass", "reason": "not a localize approval"}
    unconfirmed = [item.get("approval_id") for item in approvals if not _is_match_ball_confirmed(item)]
    if unconfirmed:
        return {
            "name": "match_ball_confirmation",
            "status": "warn",
            "reason": "localize recovery is inside ROI but lacks explicit match-ball confirmation",
            "unconfirmed_approval_ids": [item for item in unconfirmed if item not in (None, "")],
        }
    return {
        "name": "match_ball_confirmation",
        "status": "pass",
        "reason": "all localize recoveries have explicit match-ball confirmation",
    }


def _is_match_ball_confirmed(approval: dict[str, Any]) -> bool:
    if approval.get("match_ball_confirmed") is True:
        return True
    roi = approval.get("local_search_roi")
    if isinstance(roi, dict) and roi.get("match_ball_confirmed") is True:
        return True
    verdict = str(approval.get("match_ball_verdict") or "").strip().lower()
    return verdict in {"match_ball", "confirmed_match_ball"}


def _approval_id(approval: dict[str, Any] | None) -> str | None:
    if not isinstance(approval, dict):
        return None
    approval_id = approval.get("approval_id")
    return approval_id if isinstance(approval_id, str) and approval_id.strip() else None


def _consumed_approval_ids(approval: dict[str, Any] | None) -> list[str]:
    if not isinstance(approval, dict):
        return []
    related = approval.get("related_approvals")
    approvals = [item for item in related if isinstance(item, dict)] if isinstance(related, list) else [approval]
    approval_ids: list[str] = []
    for item in approvals:
        approval_id = item.get("approval_id")
        if isinstance(approval_id, str) and approval_id.strip() and approval_id.strip() not in approval_ids:
            approval_ids.append(approval_id.strip())
    return approval_ids


def _window_from_mapping(value: dict[str, Any]) -> tuple[int, int] | None:
    start = _parse_int(value.get("start_frame"))
    end = _parse_int(value.get("end_frame"))
    if start is None or end is None:
        return None
    return (start, end) if start <= end else (end, start)


def _load_candidate_audit(path: Path | None) -> dict[str, Any] | None:
    if path is None or not Path(path).exists():
        return None
    try:
        loaded = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _load_review_packets(path: Path | None) -> dict[str, Any] | None:
    if path is None or not Path(path).exists():
        return None
    try:
        loaded = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _packet_evidence_coverage_check(
    approval: dict[str, Any] | None,
    review_packets: dict[str, Any] | None,
    *,
    require_packet_coverage: bool,
) -> dict[str, Any]:
    if not require_packet_coverage:
        return {"name": "packet_evidence_coverage", "status": "pass", "reason": "packet coverage was not required"}
    if not isinstance(approval, dict):
        return {"name": "packet_evidence_coverage", "status": "unavailable", "reason": "approval provenance is absent"}
    approvals = approval.get("related_approvals")
    approval_items = [item for item in approvals if isinstance(item, dict)] if isinstance(approvals, list) else [approval]
    packets_by_id = _packets_by_id(review_packets)
    results: list[dict[str, Any]] = []
    for item in approval_items:
        packet_id = item.get("source_packet_id")
        if not isinstance(packet_id, str) or not packet_id.strip():
            continue
        approval_window = _packet_required_window(item)
        packet = packets_by_id.get(packet_id.strip())
        if packet is None:
            results.append({"approval_id": item.get("approval_id"), "packet_id": packet_id, "status": "fail", "reason": "packet id not found"})
            continue
        packet_window = _approval_window(packet)
        if packet_window is None:
            results.append(
                {
                    "approval_id": item.get("approval_id"),
                    "packet_id": packet_id,
                    "status": "unavailable",
                    "reason": "packet has no frame window",
                }
            )
            continue
        if approval_window is None:
            results.append(
                {
                    "approval_id": item.get("approval_id"),
                    "packet_id": packet_id,
                    "status": "unavailable",
                    "reason": "approval has no frame window",
                }
            )
            continue
        covers = packet_window[0] <= approval_window[0] and packet_window[1] >= approval_window[1]
        results.append(
            {
                "approval_id": item.get("approval_id"),
                "packet_id": packet_id,
                "status": "pass" if covers else "fail",
                "approval_window": {"start_frame": approval_window[0], "end_frame": approval_window[1]},
                "packet_window": {"start_frame": packet_window[0], "end_frame": packet_window[1]},
                "reason": "packet covers recovery evidence window" if covers else "packet does not cover recovery evidence window",
            }
        )
    if not results:
        return {"name": "packet_evidence_coverage", "status": "unavailable", "reason": "no source_packet_id approvals to check"}
    if any(item["status"] == "fail" for item in results):
        return {
            "name": "packet_evidence_coverage",
            "status": "fail",
            "reason": "one or more approval packets do not cover the recovery window",
            "results": results,
        }
    if any(item["status"] == "unavailable" for item in results):
        return {
            "name": "packet_evidence_coverage",
            "status": "unavailable",
            "reason": "one or more approval packets cannot prove coverage",
            "results": results,
        }
    return {
        "name": "packet_evidence_coverage",
        "status": "pass",
        "reason": "packet evidence fully covers all recovery approvals",
        "results": results,
    }


def _lost_gap_reduced_reason(lost_delta: int) -> str:
    if lost_delta >= SUSTAINED_RECOVERY_MIN_FRAMES:
        return "candidate reduces the sustained lost gap"
    if lost_delta > 0:
        return "candidate reduces lost frames, but not enough for sustained recovery"
    return "candidate does not reduce lost frames"


def _packets_by_id(review_packets: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not isinstance(review_packets, dict):
        return {}
    packets = review_packets.get("packets")
    if not isinstance(packets, list):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for packet in packets:
        if not isinstance(packet, dict):
            continue
        for key in ("packet_id", "id", "source_packet_id"):
            packet_id = packet.get(key)
            if isinstance(packet_id, str) and packet_id.strip():
                result[packet_id.strip()] = packet
    return result


def _event_overlaps(event: dict[str, Any], window: tuple[int, int]) -> bool:
    start, end = window
    event_start = _parse_int(event.get("start_frame"))
    event_end = _parse_int(event.get("end_frame"))
    if event_start is None:
        return False
    if event_end is None:
        event_end = event_start
    if event_end < event_start:
        event_start, event_end = event_end, event_start
    return event_start <= end and event_end >= start


def _roi_bounds(roi: dict[str, Any]) -> tuple[float, float, float, float] | None:
    x = _parse_float(roi.get("x"))
    y = _parse_float(roi.get("y"))
    width = _parse_float(roi.get("width"))
    height = _parse_float(roi.get("height"))
    if x is None or y is None or width is None or height is None or width <= 0 or height <= 0:
        return None
    return (
        x - ROI_PLAUSIBILITY_PADDING_PX,
        y - ROI_PLAUSIBILITY_PADDING_PX,
        x + width + ROI_PLAUSIBILITY_PADDING_PX,
        y + height + ROI_PLAUSIBILITY_PADDING_PX,
    )


def _effective_roi_bounds(approval: dict[str, Any]) -> tuple[float, float, float, float] | None:
    value = approval.get("effective_roi")
    if not isinstance(value, list) or len(value) != 4:
        return None
    parsed = [_parse_float(item) for item in value]
    if any(item is None for item in parsed):
        return None
    left, top, right, bottom = [float(item) for item in parsed if item is not None]
    if right < left or bottom < top:
        return None
    return left, top, right, bottom


def _row_status(row: dict[str, Any] | None) -> str | None:
    if not isinstance(row, dict):
        return None
    status = row.get("status")
    return status if isinstance(status, str) else None


def _contiguous_runs(frames: list[int]) -> list[list[int]]:
    if not frames:
        return []
    runs: list[list[int]] = []
    current = [frames[0]]
    for frame in frames[1:]:
        if frame == current[-1] + 1:
            current.append(frame)
        else:
            runs.append(current)
            current = [frame]
    runs.append(current)
    return runs


def _range(run: list[int]) -> dict[str, int]:
    return {"start_frame": run[0], "end_frame": run[-1], "frame_count": len(run)}


def _longest_run(runs: list[list[int]]) -> int:
    return max((len(run) for run in runs), default=0)


def _is_detected_status(status: str | None) -> bool:
    return status == "Detected"


def _parse_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return None


def _parse_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return None
