from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0"
DEFAULT_ACCEPTED_DIR_NAME = "highlights_ai_accepted"
REPORT_FILE_NAME = "ai_accepted_highlights_report.json"


def write_accepted_highlights_report(
    output_dir: Path,
    *,
    ai_review_path: Path | None = None,
    accepted_dir_name: str = DEFAULT_ACCEPTED_DIR_NAME,
    dry_run: bool = False,
) -> dict[str, Any]:
    output_dir = Path(output_dir)
    accepted_dir = _accepted_dir(output_dir, accepted_dir_name)
    review_path = ai_review_path if ai_review_path is not None else output_dir / "ai_visual_review.json"
    ai_review = _read_json(Path(review_path))
    reviews = ai_review.get("reviews") if isinstance(ai_review, dict) else None
    review_items = [item for item in reviews if isinstance(item, dict)] if isinstance(reviews, list) else []
    qualified = [item for item in review_items if _is_qualified_review(item)]

    copied: list[dict[str, Any]] = []
    planned: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    stale_removed = [] if dry_run else _remove_stale_accepted_clips(accepted_dir)
    for item in qualified:
        packet_id = item.get("packet_id")
        if not isinstance(packet_id, str) or not packet_id.strip():
            errors.append({"packet_id": str(packet_id), "error": "Missing packet_id."})
            continue
        try:
            source_clip = _source_clip_path(output_dir, item, packet_id)
            target_clip = _safe_target_path(accepted_dir, packet_id)
        except ValueError as exc:
            errors.append({"packet_id": packet_id, "error": str(exc)})
            continue

        if not source_clip.exists() or not source_clip.is_file():
            skipped.append(
                {
                    "packet_id": packet_id,
                    "reason": "source_clip_missing",
                    "source": str(source_clip),
                }
            )
            continue

        if dry_run:
            planned.append(
                {
                    "packet_id": packet_id,
                    "source": str(source_clip),
                    "target": str(target_clip),
                }
            )
            continue

        try:
            accepted_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_clip, target_clip)
        except OSError as exc:
            errors.append({"packet_id": packet_id, "error": str(exc)})
            continue
        copied.append(
            {
                "packet_id": packet_id,
                "source": str(source_clip),
                "target": str(target_clip),
            }
        )

    report = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now_iso(),
        "output_dir": str(output_dir.resolve()),
        "ai_review_path": str(Path(review_path).resolve()),
        "accepted_dir": str(accepted_dir),
        "dry_run": bool(dry_run),
        "summary": {
            "qualified_count": len(qualified),
            "copied_count": len(copied),
            "planned_count": len(planned),
            "skipped_count": len(skipped),
            "error_count": len(errors),
            "stale_removed_count": len(stale_removed),
        },
        "copied": copied,
        "planned": planned,
        "skipped": skipped,
        "errors": errors,
        "stale_removed": stale_removed,
    }
    accepted_dir.mkdir(parents=True, exist_ok=True)
    _write_json(accepted_dir / REPORT_FILE_NAME, report)
    return report


def compact_accepted_highlights_summary(report: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(report, dict):
        return None
    summary = report.get("summary")
    if not isinstance(summary, dict):
        return None
    return {
        "schema_version": report.get("schema_version"),
        "generated_at": report.get("generated_at"),
        "qualified_count": _safe_int(summary.get("qualified_count")),
        "copied_count": _safe_int(summary.get("copied_count")),
        "planned_count": _safe_int(summary.get("planned_count")),
        "skipped_count": _safe_int(summary.get("skipped_count")),
        "error_count": _safe_int(summary.get("error_count")),
        "stale_removed_count": _safe_int(summary.get("stale_removed_count")),
    }


def _is_qualified_review(item: dict[str, Any]) -> bool:
    review = item.get("review")
    if not isinstance(review, dict):
        return False
    return (
        review.get("verdict") == "accept_highlight"
        and review.get("highlight_publishable") is True
        and review.get("recommended_action") == "keep_highlight"
    )


def _remove_stale_accepted_clips(accepted_dir: Path) -> list[dict[str, Any]]:
    removed: list[dict[str, Any]] = []
    if not accepted_dir.exists():
        return removed
    for path in sorted(accepted_dir.glob("*.mp4"), key=lambda item: item.name):
        resolved = path.resolve()
        if not _is_relative_to(resolved, accepted_dir):
            continue
        try:
            path.unlink()
        except OSError as exc:
            removed.append({"path": str(resolved), "removed": False, "error": str(exc)})
            continue
        removed.append({"path": str(resolved), "removed": True})
    return removed


def _accepted_dir(output_dir: Path, accepted_dir_name: str) -> Path:
    if not accepted_dir_name or not str(accepted_dir_name).strip():
        raise ValueError("accepted_dir_name must not be empty.")
    raw_name = Path(accepted_dir_name)
    if raw_name.is_absolute():
        raise ValueError("accepted_dir_name must be relative to output_dir.")
    output_root = output_dir.resolve()
    target = (output_dir / raw_name).resolve()
    if target == output_root or output_root not in target.parents:
        raise ValueError("accepted_dir must resolve inside output_dir.")
    return target


def _source_clip_path(output_dir: Path, item: dict[str, Any], packet_id: str) -> Path:
    media = item.get("media")
    if isinstance(media, dict):
        raw_clip = media.get("clip")
        if isinstance(raw_clip, str) and raw_clip.strip():
            return _resolve_clip_reference(output_dir, Path(raw_clip))
    return _fallback_highlight_clip_path(output_dir, packet_id)


def _resolve_clip_reference(output_dir: Path, raw_path: Path) -> Path:
    output_root = output_dir.resolve()
    candidates = [raw_path] if raw_path.is_absolute() else [output_dir / raw_path, _repo_root() / raw_path]
    fallback: Path | None = None
    for candidate in candidates:
        resolved = candidate.resolve()
        if not _is_relative_to(resolved, output_root):
            continue
        if fallback is None:
            fallback = resolved
        if resolved.exists() and resolved.is_file():
            return resolved
    if fallback is None:
        raise ValueError("source clip must resolve inside output_dir.")
    return fallback


def _fallback_highlight_clip_path(output_dir: Path, packet_id: str) -> Path:
    highlights_dir = (output_dir / "highlights").resolve()
    if "/" in packet_id or "\\" in packet_id:
        raise ValueError("packet_id must not contain path separators.")
    clip_path = (highlights_dir / f"{packet_id}.mp4").resolve()
    if not _is_relative_to(clip_path, highlights_dir):
        raise ValueError("source clip must resolve inside highlights directory.")
    return clip_path


def _safe_target_path(accepted_dir: Path, packet_id: str) -> Path:
    if "/" in packet_id or "\\" in packet_id:
        raise ValueError("packet_id must not contain path separators.")
    target_path = (accepted_dir / f"{packet_id}.mp4").resolve()
    if not _is_relative_to(target_path, accepted_dir):
        raise ValueError("target clip must resolve inside accepted directory.")
    return target_path


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
