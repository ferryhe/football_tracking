from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from football_tracking.ai_candidate_registry import REGISTRY_REPORT_NAME, load_candidate_registry
from football_tracking.ai_candidate_comparison import CANDIDATE_STATUSES, comparison_payload_status
from football_tracking.final_artifact_manifest import FINAL_ARTIFACT_MANIFEST_NAME

SCHEMA_VERSION = "1.0"
QUALITY_GATE_REPORT_NAME = "ai_improvement_quality_gate.json"
HASH_SNAPSHOT_REPORT_NAME = "ai_improvement_hash_snapshots.json"
TRACK_FILES = ("ball_track.csv", "ball_track.cleaned.csv")
LONG_LOST_GAP_THRESHOLD_FRAMES = 120
CAMERA_REGRESSION_TOLERANCE = 1.05
CANDIDATE_COMPARISON_STATUSES = CANDIDATE_STATUSES
CANDIDATE_STATUS_RANK = {"pass": 0, "warn": 1, "unavailable": 2, "fail": 3}

CHECK_NAMES = (
    "track_hash_unchanged",
    "approved_actions_explicitly_consumed",
    "long_lost_gap_improvement_coverage",
    "missing_ball_roi_or_not_visible_present",
    "noise_failure_tags_present",
    "camera_regression",
    "highlight_tail_ok",
    "model_routing_recorded",
    "candidate_comparisons_ok",
)

FAILURE_TAG_ALIASES = {
    "advertising": "advertising_board",
    "advertising_board": "advertising_board",
    "background_drift": "wall_background_drift",
    "extra_ball": "extra_ball",
    "foot": "foot_confusion",
    "foot_confusion": "foot_confusion",
    "player_head": "player_head",
    "shoe": "shoe_confusion",
    "shoe_confusion": "shoe_confusion",
    "sideline": "sideline_confusion",
    "sideline_confusion": "sideline_confusion",
    "unknown": "unknown_false_positive",
    "unknown_false_positive": "unknown_false_positive",
    "wall_background_drift": "wall_background_drift",
}
ACCEPTED_FALSE_POSITIVE_TAGS = set(FAILURE_TAG_ALIASES.values())
APPROVAL_ACTIONS_THAT_CAN_COVER_MISSING_BALL = {"targeted_rerun", "localize_ball_roi"}


def build_track_hash_snapshot(output_dir: Path, stage_name: str) -> dict[str, Any]:
    output_dir = Path(output_dir)
    files: dict[str, dict[str, Any]] = {}
    for file_name in TRACK_FILES:
        path = output_dir / file_name
        if not path.exists():
            files[file_name] = {"status": "missing", "sha256": None, "size_bytes": 0}
            continue
        content = path.read_bytes()
        files[file_name] = {
            "status": "available",
            "sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now_iso(),
        "stage_name": stage_name,
        "files": files,
    }


def write_track_hash_snapshot(
    output_dir: Path,
    stage_name: str,
    report_name: str = HASH_SNAPSHOT_REPORT_NAME,
) -> dict[str, Any]:
    output_dir = Path(output_dir)
    snapshot = build_track_hash_snapshot(output_dir, stage_name)
    path = output_dir / report_name
    report = _read_json(path)
    if not isinstance(report, dict):
        report = {"schema_version": SCHEMA_VERSION, "snapshots": []}
    snapshots = report.get("snapshots")
    if not isinstance(snapshots, list):
        snapshots = []
    snapshots.append(snapshot)
    report["schema_version"] = SCHEMA_VERSION
    report["snapshots"] = snapshots
    _write_json(path, report)
    return snapshot


def build_ai_improvement_quality_gate(
    output_dir: Path,
    *,
    report_name: str = QUALITY_GATE_REPORT_NAME,
    mode: str = "artifact-only",
    approved_actions_path: Path | None = None,
    approved_actions_payload: dict[str, Any] | None = None,
    candidate_output_dir: Path | None = None,
    pre_review_stage: str = "before_review",
    post_review_stage: str = "after_ai_improvement",
) -> dict[str, Any]:
    if mode not in {"dry-run", "artifact-only", "real"}:
        raise ValueError("mode must be one of dry-run, artifact-only, real")
    if approved_actions_path is not None and approved_actions_payload is not None:
        raise ValueError("Pass either approved_actions_path or approved_actions_payload, not both")

    output_dir = Path(output_dir)
    candidate_dir = Path(candidate_output_dir) if candidate_output_dir is not None else None
    artifacts = {
        "ball_audit": _load_artifact(output_dir / "ball_audit.json"),
        "review_packets": _load_artifact(output_dir / "review_packets.json"),
        "ai_improvement_report": _load_artifact(output_dir / "ai_improvement_report.json"),
        "ai_improvement_hash_snapshots": _load_artifact(output_dir / HASH_SNAPSHOT_REPORT_NAME),
        "ai_review_triggers": _load_artifact(output_dir / "ai_review_triggers.json"),
        "camera_motion_audit": _load_artifact(output_dir / "camera_motion_audit.json"),
        "event_candidates": _load_artifact(output_dir / "event_candidates.json"),
        "ai_visual_review": _load_artifact(output_dir / "ai_visual_review.json"),
        "final_artifact_manifest": _load_artifact(output_dir / FINAL_ARTIFACT_MANIFEST_NAME),
    }
    candidate_artifacts = {
        "camera_motion_audit": _load_artifact(candidate_dir / "camera_motion_audit.json") if candidate_dir else None
    }
    approved_actions = _load_explicit_approved_actions(
        approved_actions_path,
        approved_actions_payload=approved_actions_payload,
    )

    checks = {
        "track_hash_unchanged": _check_track_hash_unchanged(
            artifacts["ai_improvement_hash_snapshots"],
            mode=mode,
            pre_review_stage=pre_review_stage,
            post_review_stage=post_review_stage,
        ),
        "approved_actions_explicitly_consumed": _check_approved_actions_explicitly_consumed(
            output_dir=output_dir,
            approved_actions_path=approved_actions_path,
            approved_actions=approved_actions,
        ),
        "camera_regression": _check_camera_regression(
            artifacts["camera_motion_audit"],
            candidate_artifacts["camera_motion_audit"],
            mode=mode,
            candidate_output_dir=candidate_dir,
        ),
        "highlight_tail_ok": _check_highlight_tail(
            artifacts["event_candidates"],
            artifacts["ai_improvement_report"],
            mode=mode,
        ),
        "model_routing_recorded": _check_model_routing(
            artifacts["ai_improvement_report"],
            artifacts["ai_visual_review"],
            mode=mode,
        ),
        "candidate_comparisons_ok": _check_candidate_comparisons(
            output_dir,
            artifacts["final_artifact_manifest"],
        ),
    }
    long_gap_check, missing_ball_check = _check_long_lost_gap_coverage(
        artifacts["ball_audit"],
        artifacts["review_packets"],
        artifacts["ai_improvement_report"],
        artifacts["ai_visual_review"],
        approved_actions,
        mode=mode,
        approved_actions_path=approved_actions_path,
    )
    checks["long_lost_gap_improvement_coverage"] = long_gap_check
    checks["missing_ball_roi_or_not_visible_present"] = missing_ball_check
    checks["noise_failure_tags_present"] = _check_noise_failure_tags(
        artifacts["ai_review_triggers"],
        artifacts["ball_audit"],
        artifacts["review_packets"],
        artifacts["ai_improvement_report"],
        mode=mode,
    )
    checks = {name: checks[name] for name in CHECK_NAMES}
    summary = _summary(checks)
    summary["candidate_comparisons"] = _candidate_comparisons_summary(checks["candidate_comparisons_ok"])
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now_iso(),
        "mode": mode,
        "summary": summary,
        "checks": checks,
        "artifacts": {
            "source_output_dir": str(output_dir),
            "candidate_output_dir": str(candidate_dir) if candidate_dir is not None else None,
            "report_name": report_name,
        },
    }


def write_ai_improvement_quality_gate(
    output_dir: Path,
    *,
    report_name: str = QUALITY_GATE_REPORT_NAME,
    mode: str = "artifact-only",
    approved_actions_path: Path | None = None,
    approved_actions_payload: dict[str, Any] | None = None,
    candidate_output_dir: Path | None = None,
    pre_review_stage: str = "before_review",
    post_review_stage: str = "after_ai_improvement",
) -> dict[str, Any]:
    payload = build_ai_improvement_quality_gate(
        output_dir,
        report_name=report_name,
        mode=mode,
        approved_actions_path=approved_actions_path,
        approved_actions_payload=approved_actions_payload,
        candidate_output_dir=candidate_output_dir,
        pre_review_stage=pre_review_stage,
        post_review_stage=post_review_stage,
    )
    _write_json(Path(output_dir) / report_name, payload)
    return payload


def _check_track_hash_unchanged(
    snapshot_artifact: dict[str, Any],
    *,
    mode: str,
    pre_review_stage: str,
    post_review_stage: str,
) -> dict[str, Any]:
    if snapshot_artifact["status"] != "loaded":
        return _check(
            "fail" if mode == "real" else "unavailable",
            reason="ai_improvement_hash_snapshots.json missing or unreadable",
        )
    snapshots = snapshot_artifact["payload"].get("snapshots")
    if not isinstance(snapshots, list):
        return _check("fail", reason="ai_improvement_hash_snapshots.json does not contain snapshots")
    before = _snapshot_for_stage(snapshots, pre_review_stage)
    after = _snapshot_for_stage(snapshots, post_review_stage)
    if before is None or after is None:
        return _check(
            "fail" if mode == "real" else "unavailable",
            reason=f"Missing hash snapshot stages: {pre_review_stage}, {post_review_stage}",
        )

    changed_files: list[str] = []
    file_details: dict[str, Any] = {}
    before_files = before.get("files") if isinstance(before.get("files"), dict) else {}
    after_files = after.get("files") if isinstance(after.get("files"), dict) else {}
    for file_name in TRACK_FILES:
        before_file = before_files.get(file_name) if isinstance(before_files.get(file_name), dict) else {}
        after_file = after_files.get(file_name) if isinstance(after_files.get(file_name), dict) else {}
        file_details[file_name] = {"before": before_file, "after": after_file}
        if before_file.get("sha256") != after_file.get("sha256") or before_file.get("status") != after_file.get("status"):
            changed_files.append(file_name)
    if changed_files:
        return _check("fail", changed_files=changed_files, files=file_details)
    return _check("pass", files=file_details)


def _check_approved_actions_explicitly_consumed(
    *,
    output_dir: Path,
    approved_actions_path: Path | None,
    approved_actions: dict[str, Any],
) -> dict[str, Any]:
    implicit_path = output_dir / "ai_improvement_approved_actions.json"
    if approved_actions["status"] == "missing":
        if approved_actions.get("source") == "path":
            return _check("fail", reason="Explicit approved actions path could not be loaded")
        if implicit_path.exists():
            return _check(
                "fail",
                reason="ai_improvement_approved_actions.json exists but was not passed explicitly",
                consumed=False,
            )
        return _check("unavailable", reason="No explicit approved actions path supplied", consumed=False)
    if approved_actions["status"] != "loaded":
        return _check("fail", reason=f"Explicit approved actions path could not be loaded: {approved_actions_path}")
    actions = approved_actions["payload"].get("approved_actions")
    if not isinstance(actions, list):
        return _check("fail", reason="Explicit approved actions artifact invalid: approved_actions must be a list")
    invalid_indexes = [index for index, action in enumerate(actions) if not isinstance(action, dict)]
    if invalid_indexes:
        return _check(
            "fail",
            reason="Explicit approved actions artifact invalid: approved_actions entries must be objects",
            invalid_indexes=invalid_indexes,
        )
    return _check(
        "pass",
        consumed=True,
        approved_actions_path=str(approved_actions_path) if approved_actions_path is not None else None,
        approved_actions_source=approved_actions.get("source"),
        approved_action_count=len(actions),
    )


def _check_long_lost_gap_coverage(
    ball_audit: dict[str, Any],
    review_packets: dict[str, Any],
    ai_report: dict[str, Any],
    ai_visual_review: dict[str, Any],
    approved_actions: dict[str, Any],
    *,
    mode: str,
    approved_actions_path: Path | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if ball_audit["status"] != "loaded":
        status = "fail" if mode == "real" else "unavailable"
        check = _check(status, reason="ball_audit.json missing or unreadable")
        return check, check.copy()
    if mode == "real" and review_packets["status"] != "loaded":
        check = _check("fail", reason="review_packets.json missing or unreadable in real mode")
        return check, check.copy()
    if mode == "real" and ai_report["status"] != "loaded":
        check = _check("fail", reason="ai_improvement_report.json missing or unreadable in real mode")
        return check, check.copy()

    gaps = _long_lost_gaps(ball_audit["payload"])
    if not gaps:
        check = _check("pass", long_gap_count=0)
        return check, _check("pass", long_gap_count=0)

    packet_windows = _packet_windows(review_packets["payload"]) if review_packets["status"] == "loaded" else []
    improvements = _improvements(ai_report["payload"]) if ai_report["status"] == "loaded" else []
    approval_items = _approval_items(approved_actions["payload"]) if approved_actions["status"] == "loaded" else []
    explicit_approvals = approved_actions["status"] == "loaded" and approved_actions.get("source") != "missing"
    packet_ids = _packet_ids(review_packets["payload"]) if review_packets["status"] == "loaded" else set()
    visual_review_ids = _visual_review_ids(ai_visual_review["payload"]) if ai_visual_review["status"] == "loaded" else set()

    reasons: list[str] = []
    uncovered_ranges: list[dict[str, int]] = []
    saw_not_visible = False
    saw_roi_or_approval = False
    warning_only = False
    gap_details: list[dict[str, Any]] = []

    for gap in gaps:
        start = gap["start_frame"]
        end = gap["end_frame"]
        detail: dict[str, Any] = {"gap": gap}
        overlapping_packets = [packet for packet in packet_windows if _overlaps(packet, start, end)]
        detail["packet_count"] = len(overlapping_packets)
        if not overlapping_packets:
            reasons.append(f"Long lost gap {start}-{end} has no overlapping review packet coverage")

        ai_coverages = [_coverage_window(item) for item in improvements if _is_missing_ball_item(item)]
        ai_coverages = [coverage for coverage in ai_coverages if coverage is not None and _overlaps(coverage, start, end)]
        if not _has_full_coverage(ai_coverages, start, end):
            reasons.append(f"Long lost gap {start}-{end} lacks full AI improvement coverage")
            uncovered_ranges.extend(_uncovered_ranges(ai_coverages, start, end))

        relevant_approvals = [
            item
            for item in approval_items
            if str(item.get("approved_action") or "") in APPROVAL_ACTIONS_THAT_CAN_COVER_MISSING_BALL
        ]
        approval_coverages = [
            coverage
            for item in relevant_approvals
            if _has_traceable_packet_or_visual_provenance(item, packet_ids=packet_ids, visual_review_ids=visual_review_ids)
            for coverage in [_coverage_window(item)]
            if coverage is not None and _overlaps(coverage, start, end)
        ]
        unprovenanced_approval_overlap = any(
            not _has_traceable_packet_or_visual_provenance(item, packet_ids=packet_ids, visual_review_ids=visual_review_ids)
            and (coverage := _coverage_window(item)) is not None
            and _overlaps(coverage, start, end)
            for item in relevant_approvals
        )
        not_visible_coverages = [
            _coverage_window(item)
            for item in improvements
            if _is_evidence_backed_not_visible(item, packet_ids=packet_ids, visual_review_ids=visual_review_ids)
            and _not_visible_status(item) != "unavailable"
        ]
        not_visible_coverages = [
            coverage for coverage in not_visible_coverages if coverage is not None and _overlaps(coverage, start, end)
        ]
        unavailable_not_visible = any(
            _is_evidence_backed_not_visible(item, packet_ids=packet_ids, visual_review_ids=visual_review_ids)
            and _not_visible_status(item) == "unavailable"
            and (coverage := _coverage_window(item)) is not None
            and _overlaps(coverage, start, end)
            for item in improvements
        )
        if explicit_approvals and _has_full_coverage(approval_coverages, start, end):
            saw_roi_or_approval = True
            detail["approved_coverage"] = "full"
        elif _has_full_coverage(not_visible_coverages, start, end):
            saw_not_visible = True
            warning_only = True
            detail["not_visible_coverage"] = "full"
        else:
            if unavailable_not_visible and mode == "real":
                reasons.append(f"Long lost gap {start}-{end} uses unavailable not_visible evidence in real mode")
            elif unprovenanced_approval_overlap:
                reasons.append(f"Long lost gap {start}-{end} approval coverage lacks packet or visual provenance")
            elif not explicit_approvals and approval_coverages:
                reasons.append(f"Long lost gap {start}-{end} has approvals only from an implicit file")
            else:
                reasons.append(f"Long lost gap {start}-{end} lacks explicit approval or evidence-backed not_visible coverage")
        gap_details.append(detail)

    if reasons:
        status = "fail"
    elif warning_only:
        status = "warn"
    else:
        status = "pass"

    long_gap_check = _check(
        status,
        long_gap_count=len(gaps),
        reasons=reasons,
        uncovered_ranges=uncovered_ranges,
        gaps=gap_details,
    )
    missing_ball_status = "warn" if saw_not_visible and status != "fail" else status
    if saw_roi_or_approval and status == "pass":
        missing_ball_status = "pass"
    missing_ball_check = _check(
        missing_ball_status,
        reason="Missing-ball gaps require explicit approval or evidence-backed not_visible",
        not_visible_evidence=saw_not_visible,
        explicit_roi_or_approval=saw_roi_or_approval,
    )
    return long_gap_check, missing_ball_check


def _check_noise_failure_tags(
    ai_review_triggers: dict[str, Any],
    ball_audit: dict[str, Any],
    review_packets: dict[str, Any],
    ai_report: dict[str, Any],
    *,
    mode: str,
) -> dict[str, Any]:
    windows = _noise_windows(ai_review_triggers, ball_audit, review_packets)
    if not windows:
        loaded_any = any(artifact["status"] == "loaded" for artifact in (ai_review_triggers, ball_audit, review_packets))
        if loaded_any:
            return _check("pass", noise_window_count=0)
        return _check("fail" if mode == "real" else "unavailable", reason="No deterministic noise artifacts available")
    improvements = _improvements(ai_report["payload"]) if ai_report["status"] == "loaded" else []
    covered_windows: list[dict[str, Any]] = []
    missing_windows: list[dict[str, Any]] = []
    for window in windows:
        matching = [
            item
            for item in improvements
            if _coverage_window(item) is not None
            and _overlaps(_coverage_window(item) or {}, window["start_frame"], window["end_frame"])
            and _has_false_positive_tag(item)
        ]
        if matching:
            covered_windows.append(window)
        else:
            missing_windows.append(window)
    if missing_windows:
        return _check(
            "fail",
            noise_window_count=len(windows),
            missing_window_count=len(missing_windows),
            missing_windows=missing_windows,
        )
    return _check("pass", noise_window_count=len(windows), covered_window_count=len(covered_windows))


def _check_camera_regression(
    source_artifact: dict[str, Any],
    candidate_artifact: dict[str, Any] | None,
    *,
    mode: str,
    candidate_output_dir: Path | None,
) -> dict[str, Any]:
    if candidate_output_dir is None:
        return _check("unavailable", reason="No candidate output directory supplied")
    if source_artifact["status"] != "loaded" or candidate_artifact is None or candidate_artifact["status"] != "loaded":
        return _check("fail" if mode == "real" else "unavailable", reason="camera_motion_audit.json missing for comparison")
    source_summary = source_artifact["payload"].get("summary") if isinstance(source_artifact["payload"].get("summary"), dict) else {}
    candidate_summary = (
        candidate_artifact["payload"].get("summary") if isinstance(candidate_artifact["payload"].get("summary"), dict) else {}
    )
    regressions: list[str] = []
    comparisons: dict[str, dict[str, float]] = {}
    for key in ("review_event_count", "max_pan_step_px", "p95_pan_step_px"):
        source_value = _number(source_summary.get(key), default=0.0)
        candidate_value = _number(candidate_summary.get(key), default=0.0)
        comparisons[key] = {"source": source_value, "candidate": candidate_value}
        threshold = source_value * CAMERA_REGRESSION_TOLERANCE
        if source_value <= 0.0:
            if candidate_value > 0.0:
                regressions.append(key)
        elif candidate_value > threshold:
            regressions.append(key)
    if regressions:
        return _check("fail", regressions=regressions, comparisons=comparisons)
    return _check("pass", comparisons=comparisons)


def _check_highlight_tail(event_candidates: dict[str, Any], ai_report: dict[str, Any], *, mode: str) -> dict[str, Any]:
    ai_payload = ai_report["payload"] if ai_report["status"] == "loaded" else {}
    highlight_requested = _has_highlight_validation_request(ai_payload)
    if event_candidates["status"] != "loaded":
        return _check(
            "fail" if highlight_requested else "unavailable",
            reason="event_candidates.json missing or unreadable",
        )
    candidates = event_candidates["payload"].get("candidates")
    if not isinstance(candidates, list):
        return _check("fail" if highlight_requested else "unavailable", reason="event_candidates.json has no candidates")
    candidates_by_id = {str(item.get("id")): item for item in candidates if isinstance(item, dict) and item.get("id")}
    source_frame_count = _source_frame_count(event_candidates["payload"])
    windows = _highlight_windows(candidates_by_id, ai_payload)
    if not windows:
        windows = [
            {"candidate_id": candidate_id, "window": candidate.get("render_window"), "source": "render_window"}
            for candidate_id, candidate in candidates_by_id.items()
        ]
    failures: list[dict[str, Any]] = []
    checked = 0
    for item in windows:
        candidate = candidates_by_id.get(str(item.get("candidate_id") or ""))
        window = item.get("window")
        if not isinstance(candidate, dict) or not isinstance(window, dict):
            continue
        core = candidate.get("core_window") if isinstance(candidate.get("core_window"), dict) else {}
        core_end = _optional_int(core.get("end_frame"))
        window_end = _optional_int(window.get("end_frame"))
        if core_end is None or window_end is None:
            continue
        min_tail = _min_tail_frames(candidate)
        required_end = core_end + min_tail
        candidate_source_count = _source_frame_count(candidate) or source_frame_count
        if candidate_source_count is not None:
            required_end = min(required_end, max(0, candidate_source_count - 1))
        checked += 1
        if window_end < required_end:
            failures.append(
                {
                    "candidate_id": item.get("candidate_id"),
                    "source": item.get("source"),
                    "end_frame": window_end,
                    "required_end_frame": required_end,
                }
            )
    if failures:
        return _check("fail", checked_window_count=checked, failures=failures)
    return _check("pass", checked_window_count=checked)


def _check_model_routing(ai_report: dict[str, Any], ai_visual_review: dict[str, Any], *, mode: str) -> dict[str, Any]:
    report_payload = ai_report["payload"] if ai_report["status"] == "loaded" else {}
    visual_payload = ai_visual_review["payload"] if ai_visual_review["status"] == "loaded" else {}
    report_model = _selected_model(report_payload)
    visual_model = _selected_model(visual_payload)
    if mode == "dry-run":
        if report_model or visual_model:
            return _check("pass", mode=mode, ai_improvement_model=report_model, ai_visual_review_model=visual_model)
        return _check("warn", mode=mode, reason="Provider/model unavailable in dry-run mode")
    if mode == "real":
        if _provider_artifact_is_dry_or_unavailable(report_payload):
            return _check("fail", reason="Real mode requires non-dry-run ai_improvement_report model provenance")
        if ai_visual_review["status"] == "loaded" and _provider_artifact_is_dry_or_unavailable(visual_payload):
            return _check("fail", reason="Real mode requires non-dry-run ai_visual_review model provenance")
        if not report_model:
            return _check("fail", reason="Real mode requires selected ai_improvement_report model provenance")
        if ai_visual_review["status"] == "loaded" and not visual_model:
            return _check("fail", reason="Real mode requires selected ai_visual_review model provenance")
    if report_model or visual_model:
        return _check("pass", ai_improvement_model=report_model, ai_visual_review_model=visual_model)
    return _check("unavailable", reason="No selected model provenance found")


def _long_lost_gaps(ball_audit: dict[str, Any]) -> list[dict[str, int]]:
    events = ball_audit.get("review_events")
    if not isinstance(events, list):
        events = ball_audit.get("events") if isinstance(ball_audit.get("events"), list) else []
    gaps: list[dict[str, int]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        text = " ".join(str(event.get(key) or "") for key in ("type", "label", "reason")).casefold()
        if "lost" not in text or "gap" not in text:
            continue
        window = _coverage_window(event)
        if window is None:
            continue
        frame_count = _optional_int(event.get("frame_count")) or (window["end_frame"] - window["start_frame"] + 1)
        if frame_count >= LONG_LOST_GAP_THRESHOLD_FRAMES:
            gaps.append(
                {
                    "start_frame": window["start_frame"],
                    "end_frame": window["end_frame"],
                    "frame_count": frame_count,
                }
            )
    return gaps


def _packet_windows(review_packets: dict[str, Any]) -> list[dict[str, Any]]:
    packets = review_packets.get("packets")
    if not isinstance(packets, list):
        return []
    windows: list[dict[str, Any]] = []
    for packet in packets:
        if not isinstance(packet, dict):
            continue
        window = _coverage_window(packet)
        if window is None:
            continue
        windows.append({**window, "packet_id": packet.get("packet_id") or packet.get("id")})
    return windows


def _improvements(ai_report: dict[str, Any]) -> list[dict[str, Any]]:
    improvements = ai_report.get("improvements")
    return [item for item in improvements if isinstance(item, dict)] if isinstance(improvements, list) else []


def _approval_items(approved_actions: dict[str, Any]) -> list[dict[str, Any]]:
    actions = approved_actions.get("approved_actions")
    return [item for item in actions if isinstance(item, dict)] if isinstance(actions, list) else []


def _is_missing_ball_item(item: dict[str, Any]) -> bool:
    action = str(item.get("recommended_action") or item.get("approved_action") or "").casefold()
    if action in {"targeted_rerun", "localize_ball_roi"}:
        return True
    tags = _tags(item)
    if tags & {"ball_lost", "missing_ball", "lost_gap", "ball_not_visible", "missed_ball"}:
        return True
    text = " ".join(str(item.get(key) or "") for key in ("area", "diagnosis", "root_cause_module")).casefold()
    return "missing" in text or "lost" in text or "not_visible" in text


def _is_evidence_backed_not_visible(
    item: dict[str, Any],
    *,
    packet_ids: set[str],
    visual_review_ids: set[str],
) -> bool:
    region = item.get("likely_ball_region") if isinstance(item.get("likely_ball_region"), dict) else {}
    description = str(region.get("description") or item.get("description") or "").casefold().replace(" ", "_")
    tags = _tags(item)
    if "not_visible" not in description and "ball_not_visible" not in tags:
        return False
    return _has_traceable_packet_or_visual_provenance(item, packet_ids=packet_ids, visual_review_ids=visual_review_ids)


def _has_traceable_packet_or_visual_provenance(
    item: dict[str, Any],
    *,
    packet_ids: set[str],
    visual_review_ids: set[str],
) -> bool:
    provenance = _provenance_ids(item)
    return bool(provenance["packet_ids"] & packet_ids or provenance["visual_review_ids"] & visual_review_ids)


def _provenance_ids(item: dict[str, Any]) -> dict[str, set[str]]:
    packet_values: set[str] = set()
    visual_values: set[str] = set()

    def collect_from_mapping(mapping: dict[str, Any]) -> None:
        _add_string_value(packet_values, mapping.get("source_packet_id"))
        _add_string_value(packet_values, mapping.get("packet_id"))
        _add_string_values(packet_values, mapping.get("packet_ids"))
        _add_string_value(visual_values, mapping.get("visual_review_id"))
        _add_string_values(visual_values, mapping.get("visual_review_ids"))

    collect_from_mapping(item)
    for key in ("provenance", "evidence_payload"):
        nested = item.get(key)
        if isinstance(nested, dict):
            collect_from_mapping(nested)
            local_roi_provenance = nested.get("local_search_roi_provenance")
            if isinstance(local_roi_provenance, dict):
                collect_from_mapping(local_roi_provenance)
    evidence = item.get("evidence")
    if isinstance(evidence, list):
        for entry in evidence:
            if not isinstance(entry, dict):
                continue
            collect_from_mapping(entry)
            nested_provenance = entry.get("provenance")
            if isinstance(nested_provenance, dict):
                collect_from_mapping(nested_provenance)
    return {"packet_ids": packet_values, "visual_review_ids": visual_values}


def _packet_ids(review_packets: dict[str, Any]) -> set[str]:
    packets = review_packets.get("packets")
    ids: set[str] = set()
    if not isinstance(packets, list):
        return ids
    for packet in packets:
        if not isinstance(packet, dict):
            continue
        for key in ("packet_id", "id", "source_packet_id"):
            _add_string_value(ids, packet.get(key))
    return ids


def _visual_review_ids(ai_visual_review: dict[str, Any]) -> set[str]:
    reviews = ai_visual_review.get("reviews")
    ids: set[str] = set()
    if not isinstance(reviews, list):
        return ids
    for item in reviews:
        if not isinstance(item, dict):
            continue
        for key in ("visual_review_id", "id"):
            _add_string_value(ids, item.get(key))
        review = item.get("review")
        if isinstance(review, dict):
            _add_string_value(ids, review.get("visual_review_id"))
    return ids


def _not_visible_status(item: dict[str, Any]) -> str:
    region = item.get("likely_ball_region") if isinstance(item.get("likely_ball_region"), dict) else {}
    status = region.get("status") if isinstance(region.get("status"), str) else item.get("status")
    return str(status or "ok").casefold()


def _noise_windows(
    ai_review_triggers: dict[str, Any],
    ball_audit: dict[str, Any],
    review_packets: dict[str, Any],
) -> list[dict[str, Any]]:
    windows: list[dict[str, Any]] = []
    if ai_review_triggers["status"] == "loaded":
        triggers = ai_review_triggers["payload"].get("triggers")
        if isinstance(triggers, list):
            for trigger in triggers:
                if isinstance(trigger, dict) and _looks_like_noise(trigger):
                    window = _coverage_window(trigger)
                    if window is not None:
                        windows.append({**window, "source": "ai_review_triggers"})
    if ball_audit["status"] == "loaded":
        events = ball_audit["payload"].get("review_events")
        if isinstance(events, list):
            for event in events:
                if isinstance(event, dict) and (_looks_like_noise(event) or _looks_like_short_detected_island(event)):
                    window = _coverage_window(event)
                    if window is not None:
                        windows.append({**window, "source": "ball_audit"})
    if review_packets["status"] == "loaded":
        packets = review_packets["payload"].get("packets")
        if isinstance(packets, list):
            for packet in packets:
                if isinstance(packet, dict) and _looks_like_noise(packet):
                    window = _coverage_window(packet)
                    if window is not None:
                        windows.append({**window, "source": "review_packets"})
    return windows


def _looks_like_noise(item: dict[str, Any]) -> bool:
    text = json.dumps(item, ensure_ascii=False).casefold()
    return any(token in text for token in ("dense_noise", "noise", "reject_noise", "false_positive"))


def _looks_like_short_detected_island(item: dict[str, Any]) -> bool:
    if str(item.get("type") or "").casefold() == "short_tracklet":
        return True
    flags = item.get("flags")
    if isinstance(flags, list) and any(str(flag).casefold() == "short_tracklet" for flag in flags):
        return True
    text = json.dumps(item, ensure_ascii=False).casefold()
    frame_count = _optional_int(item.get("frame_count"))
    return frame_count is not None and frame_count <= 12 and "detected" in text and "island" in text


def _has_false_positive_tag(item: dict[str, Any]) -> bool:
    tags = set()
    tags.update(_tags(item))
    false_positive_class = item.get("false_positive_class")
    if isinstance(false_positive_class, str):
        tags.add(false_positive_class.casefold())
    return any(FAILURE_TAG_ALIASES.get(tag, tag) in ACCEPTED_FALSE_POSITIVE_TAGS for tag in tags)


def _tags(item: dict[str, Any]) -> set[str]:
    tags = item.get("failure_tags")
    result: set[str] = set()
    if isinstance(tags, list):
        result.update(str(tag).casefold() for tag in tags if isinstance(tag, str))
    return result


def _highlight_windows(candidates_by_id: dict[str, dict[str, Any]], ai_report: dict[str, Any]) -> list[dict[str, Any]]:
    windows: list[dict[str, Any]] = []
    for item in _improvements(ai_report):
        candidate_id = str(item.get("candidate_id") or "")
        if candidate_id in candidates_by_id and isinstance(item.get("suggested_window"), dict):
            windows.append({"candidate_id": candidate_id, "window": item["suggested_window"], "source": "ai_improvement"})
    adjustments = ai_report.get("highlight_adjustments")
    if isinstance(adjustments, list):
        for item in adjustments:
            if not isinstance(item, dict):
                continue
            candidate_id = str(item.get("candidate_id") or "")
            if candidate_id in candidates_by_id and isinstance(item.get("suggested_window"), dict):
                windows.append({"candidate_id": candidate_id, "window": item["suggested_window"], "source": "highlight_adjustment"})
    return windows


def _has_highlight_validation_request(ai_report: dict[str, Any]) -> bool:
    for item in _improvements(ai_report):
        if isinstance(item.get("suggested_window"), dict):
            return True
        action = str(item.get("recommended_action") or "")
        if action in {"adjust_highlight_window", "render_suggested_highlight"}:
            return True
    adjustments = ai_report.get("highlight_adjustments")
    return isinstance(adjustments, list) and any(
        isinstance(item, dict) and isinstance(item.get("suggested_window"), dict) for item in adjustments
    )


def _min_tail_frames(candidate: dict[str, Any]) -> int:
    policy = candidate.get("buffer_policy") if isinstance(candidate.get("buffer_policy"), dict) else {}
    for key in ("min_tail_frames", "min_post_event_frames", "post_buffer_frames"):
        parsed = _optional_int(policy.get(key))
        if parsed is not None:
            return max(0, parsed)
    return 0


def _source_frame_count(payload: dict[str, Any]) -> int | None:
    for key in ("total_source_frames", "source_frame_count", "source_total_frames", "video_frame_count"):
        parsed = _optional_int(payload.get(key))
        if parsed is not None:
            return parsed
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    for key in ("total_source_frames", "source_frame_count", "source_total_frames", "video_frame_count"):
        parsed = _optional_int(summary.get(key))
        if parsed is not None:
            return parsed
    return None


def _selected_model(payload: dict[str, Any]) -> str | None:
    for key in ("selected_model", "model", "improvement_model"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    model_selection = payload.get("model_selection") if isinstance(payload.get("model_selection"), dict) else {}
    for key in ("selected_model", "model"):
        value = model_selection.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _provider_artifact_is_dry_or_unavailable(payload: dict[str, Any]) -> bool:
    if payload.get("dry_run") is True:
        return True
    for value in (payload.get("status"),):
        if isinstance(value, str) and value.casefold() in {"unavailable", "error"}:
            return True
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    status = summary.get("status")
    return isinstance(status, str) and status.casefold() in {"unavailable", "error"}


def _coverage_window(item: dict[str, Any]) -> dict[str, int] | None:
    for key in ("rerun_scope", "local_search_roi", "window", "source"):
        nested = item.get(key)
        if isinstance(nested, dict):
            nested_window = _window_from_start_end(nested)
            if nested_window is not None:
                return nested_window
    return _window_from_start_end(item)


def _window_from_start_end(item: dict[str, Any]) -> dict[str, int] | None:
    start = _optional_int(item.get("start_frame"))
    end = _optional_int(item.get("end_frame"))
    if start is None or end is None:
        return None
    if end < start:
        return None
    return {"start_frame": start, "end_frame": end}


def _has_full_coverage(windows: list[dict[str, int]], start: int, end: int) -> bool:
    return not _uncovered_ranges(windows, start, end)


def _uncovered_ranges(windows: list[dict[str, int]], start: int, end: int) -> list[dict[str, int]]:
    clipped = sorted(
        (
            {"start_frame": max(start, window["start_frame"]), "end_frame": min(end, window["end_frame"])}
            for window in windows
            if _overlaps(window, start, end)
        ),
        key=lambda window: (window["start_frame"], window["end_frame"]),
    )
    uncovered: list[dict[str, int]] = []
    cursor = start
    for window in clipped:
        if window["start_frame"] > cursor:
            uncovered.append({"start_frame": cursor, "end_frame": window["start_frame"] - 1})
        cursor = max(cursor, window["end_frame"] + 1)
    if cursor <= end:
        uncovered.append({"start_frame": cursor, "end_frame": end})
    return uncovered


def _overlaps(window: dict[str, Any], start: int, end: int) -> bool:
    window_start = _optional_int(window.get("start_frame"))
    window_end = _optional_int(window.get("end_frame"))
    if window_start is None or window_end is None:
        return False
    return window_start <= end and window_end >= start


def _snapshot_for_stage(snapshots: list[Any], stage_name: str) -> dict[str, Any] | None:
    for item in reversed(snapshots):
        if isinstance(item, dict) and item.get("stage_name") == stage_name:
            return item
    return None


def _load_explicit_approved_actions(
    path: Path | None,
    *,
    approved_actions_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if approved_actions_payload is not None:
        return {"status": "loaded", "payload": approved_actions_payload, "path": None, "source": "inline"}
    if path is None:
        return {"status": "missing", "payload": {}, "source": "missing"}
    loaded = _load_artifact(Path(path))
    loaded["source"] = "path"
    return loaded


def _check_candidate_comparisons(output_dir: Path, final_manifest: dict[str, Any]) -> dict[str, Any]:
    reports = _candidate_comparison_reports(output_dir, final_manifest)
    status_counts = {status: 0 for status in CANDIDATE_COMPARISON_STATUSES}
    for report in reports:
        status = report.get("status")
        if status not in status_counts:
            status = "unavailable"
        status_counts[status] += 1
    if status_counts["fail"]:
        status = "fail"
    elif status_counts["unavailable"]:
        status = "unavailable"
    elif status_counts["warn"]:
        status = "warn"
    elif not reports and _manifest_has_candidate_outputs(final_manifest):
        status = "unavailable"
    else:
        status = "pass"
    return _check(
        status,
        report_count=len(reports),
        status_counts=status_counts,
        reports=reports,
        reason=None if reports else "No candidate comparison reports found",
    )


def _candidate_comparison_reports(output_dir: Path, final_manifest: dict[str, Any]) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    output_root = output_dir.resolve()
    for path in sorted(output_dir.glob("*_comparison.json")):
        loaded = _load_artifact(path)
        reports.append(_comparison_report_summary(loaded, path=path))
        seen_paths.add(str(path.resolve()))

    if final_manifest["status"] == "loaded":
        manifest_reports = final_manifest["payload"].get("comparison_reports")
        if isinstance(manifest_reports, list):
            for item in manifest_reports:
                if not isinstance(item, dict):
                    continue
                path_value = item.get("path") or item.get("report_path")
                if isinstance(path_value, str) and path_value.strip():
                    path = Path(path_value)
                    if not path.is_absolute():
                        path = output_dir / path
                    resolved_path = path.resolve()
                    if not _is_relative_to(resolved_path, output_root):
                        reports.append(
                            {
                                "path": str(path),
                                "problem_type": item.get("problem_type"),
                                "candidate_id": item.get("candidate_id"),
                                "status": "unavailable",
                                "failed_check_count": 0,
                                "warning_count": 0,
                                "unavailable_count": 1,
                                "artifact_status": "path_outside_output_dir",
                            }
                        )
                        continue
                    resolved = str(resolved_path)
                    if resolved in seen_paths:
                        continue
                    loaded = _load_artifact(path)
                    reports.append(_comparison_report_summary(loaded, path=path, manifest_entry=item))
                    seen_paths.add(resolved)
                elif isinstance(item.get("summary"), dict):
                    reports.append(_comparison_payload_summary(item, path=None))
    reports.extend(_registry_candidate_summaries(output_dir, seen_paths))
    return reports


def _comparison_report_summary(
    artifact: dict[str, Any],
    *,
    path: Path,
    manifest_entry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if artifact["status"] == "loaded":
        return _comparison_payload_summary(artifact["payload"], path=path)
    fallback_candidate_id = None
    if isinstance(manifest_entry, dict):
        fallback_candidate_id = manifest_entry.get("candidate_id")
    return {
        "path": str(path),
        "problem_type": None,
        "candidate_id": fallback_candidate_id,
        "status": "unavailable",
        "failed_check_count": 0,
        "warning_count": 0,
        "unavailable_count": 1,
        "artifact_status": artifact["status"],
    }


def _comparison_payload_summary(payload: dict[str, Any], *, path: Path | None) -> dict[str, Any]:
    status_payload = comparison_payload_status(payload)
    candidate = payload.get("candidate") if isinstance(payload.get("candidate"), dict) else {}
    candidate_id = payload.get("candidate_id") or candidate.get("id") or candidate.get("candidate_id")
    return {
        "path": str(path) if path is not None else payload.get("path"),
        "problem_type": payload.get("problem_type"),
        "candidate_id": candidate_id,
        "status": status_payload["status"],
        "failed_check_count": status_payload["failed_check_count"],
        "warning_count": status_payload["warning_count"],
        "unavailable_count": status_payload["unavailable_count"],
        "artifact_status": status_payload["artifact_status"],
    }


def _registry_candidate_summaries(output_dir: Path, seen_paths: set[str]) -> list[dict[str, Any]]:
    registry_path = output_dir / REGISTRY_REPORT_NAME
    if not registry_path.exists():
        return []
    registry = load_candidate_registry(registry_path)
    artifact_status = registry.get("artifact_status")
    if artifact_status in {"corrupt", "invalid"}:
        return [
            {
                "path": str(registry_path),
                "problem_type": None,
                "candidate_id": None,
                "status": "unavailable",
                "failed_check_count": 0,
                "warning_count": 0,
                "unavailable_count": 1,
                "artifact_status": f"registry_{artifact_status}",
            }
        ]
    candidates = registry.get("candidates")
    if not isinstance(candidates, list):
        return []
    reports: list[dict[str, Any]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        comparison_report = candidate.get("comparison_report")
        summary = _registry_candidate_report_summary(output_dir, registry_path, candidate, comparison_report)
        summary_path = summary.get("path")
        if isinstance(summary_path, str) and summary_path:
            resolved_summary_path = str(Path(summary_path).resolve())
            if resolved_summary_path in seen_paths:
                continue
            seen_paths.add(resolved_summary_path)
        reports.append(summary)
    return reports


def _registry_candidate_report_summary(
    output_dir: Path,
    registry_path: Path,
    candidate: dict[str, Any],
    comparison_report: Any,
) -> dict[str, Any]:
    candidate_id = candidate.get("candidate_id")
    registry_status = candidate.get("comparison_status")
    if not isinstance(comparison_report, str) or not comparison_report.strip():
        return _registry_unavailable_report(
            registry_path,
            candidate=candidate,
            artifact_status="registry_missing_comparison_report",
            reason="registry candidate has no comparison_report",
        )
    path = output_dir / comparison_report
    resolved_path = path.resolve()
    output_root = output_dir.resolve()
    if not _is_relative_to(resolved_path, output_root):
        return _registry_unavailable_report(
            path,
            candidate=candidate,
            artifact_status="path_outside_output_dir",
            reason="registry comparison_report path is outside output_dir",
        )
    loaded = _load_artifact(path)
    summary = _comparison_report_summary(loaded, path=path, manifest_entry={"candidate_id": candidate_id})
    if loaded["status"] != "loaded":
        return summary
    if registry_status in CANDIDATE_COMPARISON_STATUSES and registry_status != summary["status"]:
        summary = dict(summary)
        summary["status"] = _worst_candidate_status([registry_status, summary["status"]])
        summary["failed_check_count"] += 1 if summary["status"] == "fail" and summary["failed_check_count"] == 0 else 0
        summary["warning_count"] += 1 if summary["status"] == "warn" and summary["warning_count"] == 0 else 0
        summary["unavailable_count"] += 1 if summary["status"] == "unavailable" and summary["unavailable_count"] == 0 else 0
        summary["artifact_status"] = "registry_status_mismatch"
    return summary


def _registry_unavailable_report(
    path: Path,
    *,
    candidate: dict[str, Any],
    artifact_status: str,
    reason: str,
) -> dict[str, Any]:
    return {
        "path": str(path),
        "problem_type": candidate.get("problem_type"),
        "candidate_id": candidate.get("candidate_id"),
        "status": "unavailable",
        "failed_check_count": 0,
        "warning_count": 0,
        "unavailable_count": 1,
        "artifact_status": artifact_status,
        "reason": reason,
    }


def _worst_candidate_status(statuses: list[Any]) -> str:
    valid = [status for status in statuses if status in CANDIDATE_COMPARISON_STATUSES]
    if not valid:
        return "unavailable"
    return max(valid, key=lambda status: CANDIDATE_STATUS_RANK[status])


def _candidate_comparisons_summary(check: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": check.get("status"),
        "report_count": check.get("report_count", 0),
        "status_counts": check.get("status_counts", {status: 0 for status in CANDIDATE_COMPARISON_STATUSES}),
    }


def _manifest_has_candidate_outputs(final_manifest: dict[str, Any]) -> bool:
    if final_manifest["status"] != "loaded":
        return False
    payload = final_manifest["payload"]
    for key in ("candidate_outputs", "final_selected_artifacts"):
        value = payload.get(key)
        if isinstance(value, list) and value:
            return True
    return False


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


def _summary(checks: dict[str, dict[str, Any]]) -> dict[str, Any]:
    failed_count = sum(1 for check in checks.values() if check.get("status") == "fail")
    warning_count = sum(1 for check in checks.values() if check.get("status") in {"warn", "unavailable"})
    if failed_count:
        status = "fail"
    elif warning_count:
        status = "warn"
    else:
        status = "pass"
    return {
        "status": status,
        "check_count": len(checks),
        "failed_check_count": failed_count,
        "warning_count": warning_count,
    }


def _check(status: str, **kwargs: Any) -> dict[str, Any]:
    return {"status": status, **kwargs}


def _number(value: Any, *, default: float) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


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


def _add_string_value(target: set[str], value: Any) -> None:
    if isinstance(value, str) and value.strip():
        target.add(value.strip())


def _add_string_values(target: set[str], value: Any) -> None:
    if not isinstance(value, list):
        return
    for item in value:
        _add_string_value(target, item)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
