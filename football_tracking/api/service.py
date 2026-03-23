from __future__ import annotations

import base64
import csv
import json
import mimetypes
import threading
import unicodedata
from copy import deepcopy
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import cv2
import yaml

from football_tracking.api.ai_provider import OpenAIResponsesClient, load_provider_settings
from football_tracking.config import AppConfig, load_config
from football_tracking.pipeline import BallTrackingPipeline


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return {key: _jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def _flatten_patch_lines(patch: dict[str, Any], prefix: str = "") -> list[str]:
    lines: list[str] = []
    for key, value in patch.items():
        current_key = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            lines.extend(_flatten_patch_lines(value, current_key))
        else:
            lines.append(f"{current_key}: {value}")
    return lines


def _normalize_ai_language(language: str | None) -> str:
    return "zh" if language == "zh" else "en"


def _localized_text(language: str, *, en: str, zh: str) -> str:
    return zh if language == "zh" else en


def _localized_run_status(language: str, status: str) -> str:
    labels = {
        "en": {
            "queued": "queued",
            "running": "running",
            "completed": "completed",
            "failed": "failed",
        },
        "zh": {
            "queued": "\u6392\u961f\u4e2d",
            "running": "\u8fd0\u884c\u4e2d",
            "completed": "\u5df2\u5b8c\u6210",
            "failed": "\u5931\u8d25",
        },
    }
    return labels[language].get(status, status)


class ApiService:
    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root
        self.config_dir = repo_root / "config"
        self.outputs_dir = repo_root / "outputs"
        self.data_dir = repo_root / "data"
        self.registry_path = repo_root / "data" / "run_registry.json"
        self.generated_config_dir = self.config_dir / "generated"
        self._lock = threading.Lock()
        self._active_threads: dict[str, threading.Thread] = {}
        self.provider_settings = load_provider_settings(repo_root)
        self.ai_client = OpenAIResponsesClient(self.provider_settings)
        self._ensure_registry_file()

    def health_summary(self) -> dict[str, Any]:
        runs = self.list_runs()
        active_run = next((run["run_id"] for run in runs if run["status"] in {"queued", "running"}), None)
        return {
            "status": "ok",
            "active_run_id": active_run,
            "config_count": len(self.list_configs()),
            "run_count": len(runs),
        }

    def list_configs(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for config_path in sorted(self.config_dir.rglob("*.yaml")):
            if config_path.is_file():
                relative_name = config_path.relative_to(self.config_dir).as_posix()
                items.append(self._build_config_summary(config_path, relative_name))
        return items

    def list_input_videos(self) -> dict[str, Any]:
        supported_suffixes = {".mp4", ".mov", ".mkv", ".avi", ".m4v"}
        videos: list[dict[str, Any]] = []
        if self.data_dir.exists():
            for video_path in sorted(self.data_dir.rglob("*"), key=lambda item: item.name.lower()):
                if not video_path.is_file():
                    continue
                if video_path.suffix.lower() not in supported_suffixes:
                    continue
                stat = video_path.stat()
                videos.append(
                    {
                        "name": video_path.relative_to(self.data_dir).as_posix(),
                        "path": str(video_path.resolve()),
                        "size_bytes": stat.st_size,
                        "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                    }
                )
        return {
            "root_dir": str(self.data_dir.resolve()),
            "videos": videos,
        }

    def suggest_field_setup(self, input_video: str, config_name: str | None = None) -> dict[str, Any]:
        video_path = self._resolve_input_video_path(input_video)
        samples = self._sample_video_frames(video_path)
        if not samples:
            raise RuntimeError(f"Unable to read preview frames from input video: {video_path}")

        best_sample: dict[str, Any] | None = None
        config_shape: dict[str, Any] | None = None

        if config_name:
            config_shape = self._load_field_setup_from_config(
                config_name=config_name,
                frame_width=samples[len(samples) // 2]["frame_width"],
                frame_height=samples[len(samples) // 2]["frame_height"],
            )
            if config_shape is not None:
                sample = samples[len(samples) // 2]
                preview_bounds = self._build_preview_bounds(
                    expanded_polygon=config_shape["expanded_polygon"],
                    content_bounds=self._detect_content_bounds(sample["frame"]),
                    frame_width=sample["frame_width"],
                    frame_height=sample["frame_height"],
                )
                best_sample = {
                    **sample,
                    **config_shape,
                    "coverage": 1.0,
                    "confidence": "config",
                    "source": f"config:{config_name}",
                    "preview_bounds": preview_bounds,
                }

        if best_sample is None:
            for sample in samples:
                content_bounds = self._detect_content_bounds(sample["frame"])
                field_polygon, coverage, detected = self._detect_field_polygon(sample["frame"], content_bounds)
                expanded_polygon = self._expand_polygon(
                    field_polygon,
                    frame_width=sample["frame_width"],
                    frame_height=sample["frame_height"],
                    scale_x=1.08,
                    scale_y=1.10,
                )
                candidate = {
                    **sample,
                    "field_polygon": field_polygon,
                    "expanded_polygon": expanded_polygon,
                    "field_roi": self._polygon_bounds(field_polygon),
                    "expanded_roi": self._polygon_bounds(expanded_polygon),
                    "coverage": round(coverage, 4),
                    "confidence": "detected" if detected else "fallback",
                    "source": "field-green-heuristic" if detected else "safe-trapezoid-fallback",
                    "preview_bounds": self._build_preview_bounds(
                        expanded_polygon=expanded_polygon,
                        content_bounds=content_bounds,
                        frame_width=sample["frame_width"],
                        frame_height=sample["frame_height"],
                    ),
                }
                if best_sample is None:
                    best_sample = candidate
                    continue
                if candidate["confidence"] == "detected" and best_sample["confidence"] != "detected":
                    best_sample = candidate
                    continue
                if candidate["coverage"] > best_sample["coverage"]:
                    best_sample = candidate

        if best_sample is None:
            raise RuntimeError(f"Unable to build a field suggestion for input video: {video_path}")

        preview_x1, preview_y1, preview_x2, preview_y2 = best_sample["preview_bounds"]
        return {
            "input_video": str(video_path),
            "preview_data_url": self._encode_frame_data_url(best_sample["frame"][preview_y1:preview_y2, preview_x1:preview_x2]),
            "preview_bounds": best_sample["preview_bounds"],
            "frame_width": best_sample["frame_width"],
            "frame_height": best_sample["frame_height"],
            "frame_time_seconds": round(best_sample["frame_time_seconds"], 2),
            "sample_index": best_sample["sample_index"],
            "sample_count": best_sample["sample_count"],
            "field_polygon": best_sample["field_polygon"],
            "expanded_polygon": best_sample["expanded_polygon"],
            "field_roi": best_sample["field_roi"],
            "expanded_roi": best_sample["expanded_roi"],
            "confidence": best_sample["confidence"],
            "source": best_sample["source"],
            "field_coverage": best_sample["coverage"],
            "config_patch": self._build_field_config_patch(
                field_polygon=best_sample["field_polygon"],
                expanded_polygon=best_sample["expanded_polygon"],
                expanded_roi=best_sample["expanded_roi"],
            ),
        }

    def get_config(self, name: str) -> dict[str, Any]:
        config_path, relative_name = self._resolve_config_path(name)
        raw = self._load_raw_yaml(config_path)
        resolved = load_config(config_path)
        return {
            "name": relative_name,
            "path": str(config_path),
            "raw": raw,
            "resolved": _jsonable(resolved),
            "summary": self._build_config_summary(config_path, relative_name),
        }

    def derive_config(self, base_config_name: str, output_name: str, patch: dict[str, Any]) -> dict[str, Any]:
        base_path, _ = self._resolve_config_path(base_config_name)
        base_raw = self._load_raw_yaml(base_path)
        merged = _deep_merge(base_raw, patch)
        output_stem = Path(output_name).name
        output_file_name = output_stem if output_stem.endswith(".yaml") else f"{output_stem}.yaml"
        self.generated_config_dir.mkdir(parents=True, exist_ok=True)
        output_path = self.generated_config_dir / output_file_name
        with output_path.open("w", encoding="utf-8") as handle:
            yaml.safe_dump(merged, handle, sort_keys=False, allow_unicode=False)
        return self.get_config(output_path.relative_to(self.config_dir).as_posix())

    def list_runs(self) -> list[dict[str, Any]]:
        with self._lock:
            registry = self._read_registry()
            self._refresh_discovered_runs_locked(registry)
            self._write_registry(registry)
            runs = sorted(registry["runs"], key=lambda item: item.get("created_at", ""), reverse=True)
        return runs

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            registry = self._read_registry()
            self._refresh_discovered_runs_locked(registry)
            self._write_registry(registry)
            for run in registry["runs"]:
                if run["run_id"] == run_id:
                    return run
        raise KeyError(run_id)

    def list_artifacts(self, run_id: str) -> list[dict[str, Any]]:
        return self.get_run(run_id).get("artifacts", [])

    def get_artifact_path(self, run_id: str, artifact_name: str) -> Path:
        run = self.get_run(run_id)
        output_dir = Path(run["output_dir"]).resolve()
        candidate = (output_dir / artifact_name).resolve()
        if not candidate.exists():
            raise FileNotFoundError(artifact_name)
        if output_dir not in candidate.parents and candidate != output_dir:
            raise FileNotFoundError(artifact_name)
        return candidate

    def get_cleanup_report(self, run_id: str) -> dict[str, Any]:
        return self._load_optional_json_artifact(run_id, "cleanup_report.json")

    def get_follow_cam_report(self, run_id: str) -> dict[str, Any]:
        return self._load_optional_json_artifact(run_id, "follow_cam_report.json")

    def get_camera_path(self, run_id: str, offset: int, limit: int) -> dict[str, Any]:
        camera_path = self.get_artifact_path(run_id, "camera_path.csv")
        with camera_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            rows = list(reader)
            columns = reader.fieldnames or []
        return {
            "columns": columns,
            "offset": offset,
            "limit": limit,
            "total_rows": len(rows),
            "rows": rows[offset : offset + limit],
        }

    def ai_explain(
        self,
        run_id: str | None,
        config_name: str | None,
        focus: str | None,
        language: str | None = None,
    ) -> dict[str, Any]:
        resolved_language = _normalize_ai_language(language)
        if self.ai_client.is_enabled():
            try:
                return self._ai_explain_with_model(
                    run_id=run_id,
                    config_name=config_name,
                    focus=focus,
                    language=resolved_language,
                )
            except Exception:
                pass
        return self._ai_explain_heuristic(
            run_id=run_id,
            config_name=config_name,
            focus=focus,
            language=resolved_language,
        )

    def ai_recommend(self, run_id: str, objective: str | None, language: str | None = None) -> dict[str, Any]:
        resolved_language = _normalize_ai_language(language)
        if self.ai_client.is_enabled():
            try:
                return self._ai_recommend_with_model(
                    run_id=run_id,
                    objective=objective,
                    language=resolved_language,
                )
            except Exception:
                pass
        return self._ai_recommend_heuristic(run_id=run_id, objective=objective, language=resolved_language)

    def _ai_explain_heuristic(
        self,
        run_id: str | None,
        config_name: str | None,
        focus: str | None,
        language: str,
    ) -> dict[str, Any]:
        evidence: list[str] = []
        summary_parts: list[str] = []

        if run_id:
            run = self.get_run(run_id)
            raw_stats = run.get("stats", {}).get("raw", {})
            cleaned_stats = run.get("stats", {}).get("cleaned", {})
            summary_parts.append(
                _localized_text(
                    language,
                    en=(
                        f"Run {run_id} is {_localized_run_status(language, run['status'])} with cleaned detected ratio "
                        f"{float(cleaned_stats.get('detected_ratio', raw_stats.get('detected_ratio', 0.0))) * 100:.1f}%."
                    ),
                    zh=(
                        f"\u8fd0\u884c {run_id} \u5f53\u524d\u4e3a{_localized_run_status(language, run['status'])}"
                        f"\uff0c\u6e05\u6d17\u540e\u68c0\u6d4b\u7387\u4e3a "
                        f"{float(cleaned_stats.get('detected_ratio', raw_stats.get('detected_ratio', 0.0))) * 100:.1f}%\u3002"
                    ),
                )
            )
            evidence.extend(
                [
                    _localized_text(language, en=f"Run status={run['status']}", zh=f"\u8fd0\u884c\u72b6\u6001={run['status']}"),
                    _localized_text(
                        language,
                        en=f"Run config={run.get('config_name')}",
                        zh=f"\u8fd0\u884c\u914d\u7f6e={run.get('config_name')}",
                    ),
                    _localized_text(
                        language,
                        en=f"Raw detected={raw_stats.get('detected')}",
                        zh=f"\u539f\u59cb\u68c0\u6d4b={raw_stats.get('detected')}",
                    ),
                    _localized_text(language, en=f"Raw lost={raw_stats.get('lost')}", zh=f"\u539f\u59cb\u4e22\u5931={raw_stats.get('lost')}"),
                    _localized_text(
                        language,
                        en=f"Cleaned detected={cleaned_stats.get('detected')}",
                        zh=f"\u6e05\u6d17\u540e\u68c0\u6d4b={cleaned_stats.get('detected')}",
                    ),
                    _localized_text(
                        language,
                        en=f"Cleaned lost={cleaned_stats.get('lost')}",
                        zh=f"\u6e05\u6d17\u540e\u4e22\u5931={cleaned_stats.get('lost')}",
                    ),
                ]
            )

        if config_name:
            config = self.get_config(config_name)
            resolved = config["resolved"]
            summary_parts.append(
                _localized_text(
                    language,
                    en=(
                        f"Config {config_name} has postprocess={resolved.get('postprocess', {}).get('enabled')} "
                        f"and follow_cam={resolved.get('follow_cam', {}).get('enabled')}."
                    ),
                    zh=(
                        f"\u914d\u7f6e {config_name} \u4e2d postprocess="
                        f"{resolved.get('postprocess', {}).get('enabled')} \uff0cfollow_cam="
                        f"{resolved.get('follow_cam', {}).get('enabled')}\u3002"
                    ),
                )
            )
            evidence.extend(
                [
                    _localized_text(
                        language,
                        en=f"Config output={config['summary']['output_dir']}",
                        zh=f"\u914d\u7f6e\u8f93\u51fa\u76ee\u5f55={config['summary']['output_dir']}",
                    ),
                    _localized_text(
                        language,
                        en=f"Config input={config['summary']['input_video']}",
                        zh=f"\u914d\u7f6e\u8f93\u5165\u89c6\u9891={config['summary']['input_video']}",
                    ),
                ]
            )

        if focus:
            summary_parts.append(
                _localized_text(
                    language,
                    en=f"Requested focus: {focus}.",
                    zh=f"\u5f53\u524d\u76ee\u6807\uff1a{focus}\u3002",
                )
            )

        if not summary_parts:
            summary_parts.append(
                _localized_text(
                    language,
                    en="No run or config was provided, so AI explanation has no grounded evidence yet.",
                    zh="\u8fd8\u6ca1\u6709\u63d0\u4f9b run \u6216\u914d\u7f6e\uff0c\u6240\u4ee5\u73b0\u5728\u8fd8\u6ca1\u6709\u53ef\u843d\u5730\u7684\u8bc1\u636e\u6458\u8981\u3002",
                )
            )

        return {
            "summary": " ".join(summary_parts),
            "evidence": evidence,
        }

    def _ai_recommend_heuristic(self, run_id: str, objective: str | None, language: str) -> dict[str, Any]:
        run = self.get_run(run_id)
        config_name = run.get("config_name")
        if not config_name:
            raise FileNotFoundError(f"Run {run_id} is not linked to a config.")

        config = self.get_config(config_name)
        raw_stats = run.get("stats", {}).get("raw", {})
        cleaned_stats = run.get("stats", {}).get("cleaned", {})
        follow_cam_stats = run.get("stats", {}).get("follow_cam", {})
        objective_text = (objective or "").strip().lower()

        lost_ratio = float(cleaned_stats.get("lost_ratio", raw_stats.get("lost_ratio", 0.0)) or 0.0)
        detected_ratio = float(cleaned_stats.get("detected_ratio", raw_stats.get("detected_ratio", 0.0)) or 0.0)
        mean_crop_height = float(follow_cam_stats.get("mean_crop_height", 0.0) or 0.0)

        patch: dict[str, Any] = {}
        output_slug = "grounded_recommendation"
        title = _localized_text(
            language,
            en="Grounded Recommendation",
            zh="\u57fa\u4e8e\u8bc1\u636e\u7684\u5efa\u8bae",
        )
        diagnosis = _localized_text(
            language,
            en=(
                f"Detected ratio is {detected_ratio * 100:.1f}% and lost ratio is {lost_ratio * 100:.1f}% "
                f"for run {run_id}."
            ),
            zh=(
                f"\u8fd0\u884c {run_id} \u7684\u68c0\u6d4b\u7387\u4e3a {detected_ratio * 100:.1f}%"
                f"\uff0c\u4e22\u5931\u7387\u4e3a {lost_ratio * 100:.1f}%\u3002"
            ),
        )
        recommendation = _localized_text(
            language,
            en="Stay on the current baseline and make only targeted adjustments.",
            zh="\u5148\u7559\u5728\u5f53\u524d\u57fa\u7ebf\u4e0a\uff0c\u53ea\u505a\u6709\u9488\u5bf9\u6027\u7684\u5c0f\u8c03\u6574\u3002",
        )
        expected_tradeoff = _localized_text(
            language,
            en="Conservative changes keep current gains and avoid reintroducing noisy regressions.",
            zh="\u4fdd\u5b88\u6539\u52a8\u80fd\u5c3d\u91cf\u4fdd\u4f4f\u73b0\u5728\u7684\u6536\u76ca\uff0c\u907f\u514d\u518d\u6b21\u5f15\u5165\u660e\u663e\u566a\u58f0\u56de\u9000\u3002",
        )
        evidence = [
            _localized_text(language, en=f"Run ID={run_id}", zh=f"\u8fd0\u884c ID={run_id}"),
            _localized_text(language, en=f"Config={config_name}", zh=f"\u914d\u7f6e={config_name}"),
            _localized_text(
                language,
                en=f"Cleaned detected ratio={detected_ratio:.4f}",
                zh=f"\u6e05\u6d17\u540e\u68c0\u6d4b\u7387={detected_ratio:.4f}",
            ),
            _localized_text(
                language,
                en=f"Cleaned lost ratio={lost_ratio:.4f}",
                zh=f"\u6e05\u6d17\u540e\u4e22\u5931\u7387={lost_ratio:.4f}",
            ),
        ]

        if any(token in objective_text for token in ["camera", "follow", "zoom", "pan", "\u955c\u5934", "\u8ddf\u968f", "\u8ddf\u62cd", "\u5e73\u79fb", "\u7f29\u653e", "\u76f8\u673a"]):
            current_follow = config["resolved"].get("follow_cam", {})
            patch = {
                "follow_cam": {
                    "glide_pan_smoothing": round(max(0.06, float(current_follow.get("glide_pan_smoothing", 0.10)) - 0.02), 2),
                    "catch_up_pan_smoothing": round(max(0.16, float(current_follow.get("catch_up_pan_smoothing", 0.22)) - 0.02), 2),
                    "zoom_out_confirm_frames": int(current_follow.get("zoom_out_confirm_frames", 6)) + 2,
                    "zoom_in_confirm_frames": int(current_follow.get("zoom_in_confirm_frames", 12)) + 2,
                    "zoom_hold_frames_after_change": int(current_follow.get("zoom_hold_frames_after_change", 16)) + 4,
                }
            }
            output_slug = "follow_cam_stabilization"
            title = _localized_text(language, en="Follow-Cam Stabilization", zh="\u8ddf\u968f\u955c\u5934\u7a33\u5b9a\u5316")
            diagnosis = _localized_text(
                language,
                en=f"Mean crop height is {mean_crop_height:.1f}px. The fastest win is to make pan and zoom slower to react.",
                zh=(
                    f"\u5e73\u5747\u88c1\u5207\u9ad8\u5ea6\u4e3a {mean_crop_height:.1f}px\u3002"
                    "\u6700\u76f4\u63a5\u7684\u6539\u8fdb\u662f\u5148\u653e\u6162\u5e73\u79fb\u548c\u7f29\u653e\u7684\u53cd\u5e94\u901f\u5ea6\u3002"
                ),
            )
            recommendation = _localized_text(
                language,
                en="Slow pan response first and require longer zoom confirmation before changing crop depth.",
                zh="\u5148\u653e\u6162\u5e73\u79fb\u54cd\u5e94\uff0c\u5e76\u63d0\u9ad8\u7f29\u653e\u786e\u8ba4\u65f6\u95f4\uff0c\u518d\u6539\u53d8\u753b\u9762\u6df1\u5ea6\u3002",
            )
            expected_tradeoff = _localized_text(
                language,
                en="The camera will feel steadier, but fast breaks may take slightly longer to catch up.",
                zh="\u955c\u5934\u4f1a\u66f4\u7a33\uff0c\u4f46\u5feb\u901f\u653b\u9632\u8f6c\u6362\u65f6\u53ef\u80fd\u4f1a\u7a0d\u6162\u4e00\u70b9\u8ddf\u4e0a\u3002",
            )
            evidence.extend(
                [
                    _localized_text(
                        language,
                        en=f"Follow-cam mean crop height={mean_crop_height:.2f}",
                        zh=f"\u8ddf\u968f\u955c\u5934\u5e73\u5747\u88c1\u5207\u9ad8\u5ea6={mean_crop_height:.2f}",
                    ),
                    _localized_text(
                        language,
                        en=f"Follow-cam enabled={run.get('modules_enabled', {}).get('follow_cam')}",
                        zh=f"\u8ddf\u968f\u955c\u5934\u5df2\u542f\u7528={run.get('modules_enabled', {}).get('follow_cam')}",
                    ),
                ]
            )
        elif lost_ratio > 0.18:
            current_dynamic = (
                config["resolved"]
                .get("scene_bias", {})
                .get("dynamic_air_recovery", {})
            )
            patch = {
                "scene_bias": {
                    "dynamic_air_recovery": {
                        "tentative_reacquire_confidence_threshold": round(
                            min(0.36, float(current_dynamic.get("tentative_reacquire_confidence_threshold", 0.30)) + 0.02),
                            2,
                        ),
                        "tentative_reacquire_score_threshold": round(
                            min(0.45, float(current_dynamic.get("tentative_reacquire_score_threshold", 0.38)) + 0.02),
                            2,
                        ),
                    }
                }
            }
            output_slug = "reacquire_tightening"
            title = _localized_text(language, en="Reacquire Tightening", zh="\u91cd\u65b0\u6355\u83b7\u6536\u7d27")
            diagnosis = _localized_text(
                language,
                en="Lost ratio is still material, but global detector loosening is riskier than targeted reacquire tightening.",
                zh="\u4e22\u5931\u7387\u4ecd\u7136\u504f\u9ad8\uff0c\u4f46\u76f4\u63a5\u5168\u5c40\u653e\u5bbd detector \u98ce\u9669\u66f4\u5927\uff0c\u5148\u6536\u7d27\u91cd\u65b0\u6355\u83b7\u4f1a\u66f4\u7a33\u3002",
            )
            recommendation = _localized_text(
                language,
                en="Tighten tentative reacquire acceptance before changing detector sensitivity.",
                zh="\u5148\u6536\u7d27 tentative reacquire \u7684\u63a5\u53d7\u9608\u503c\uff0c\u518d\u8003\u8651\u52a8 detector \u7075\u654f\u5ea6\u3002",
            )
            expected_tradeoff = _localized_text(
                language,
                en="This should suppress noisy far-jump recoveries, but may delay a few true long-gap reacquires.",
                zh="\u8fd9\u4f1a\u538b\u6389\u4e00\u4e9b\u566a\u58f0\u6027\u7684\u8fdc\u8df3\u6062\u590d\uff0c\u4f46\u4e5f\u53ef\u80fd\u8ba9\u5c11\u6570\u771f\u5b9e\u7684\u957f\u95f4\u9694\u91cd\u6355\u7a0d\u5fae\u6162\u4e00\u70b9\u3002",
            )
        else:
            current_post = config["resolved"].get("postprocess", {})
            patch = {
                "postprocess": {
                    "max_detected_island_length": max(1, int(current_post.get("max_detected_island_length", 2))),
                    "low_confidence_threshold": round(
                        min(0.5, float(current_post.get("low_confidence_threshold", 0.40)) + 0.02),
                        2,
                    ),
                }
            }
            output_slug = "post_cleanup_tightening"
            title = _localized_text(language, en="Post-Cleanup Tightening", zh="\u6e05\u6d17\u9636\u6bb5\u6536\u7d27")
            diagnosis = _localized_text(
                language,
                en="Tracking is already strong enough that cleanup is a safer place to shave visible noise.",
                zh="\u8ddf\u8e2a\u4e3b\u4f53\u5df2\u7ecf\u8db3\u591f\u7a33\uff0c\u5148\u5728 cleanup \u73af\u8282\u53bb\u6389\u53ef\u89c1\u566a\u58f0\u4f1a\u66f4\u5b89\u5168\u3002",
            )
            recommendation = _localized_text(
                language,
                en="Prefer small cleanup threshold changes before touching detector or tracker behavior.",
                zh="\u5148\u8c03\u5c0f cleanup \u9608\u503c\uff0c\u5c3d\u91cf\u4e0d\u8981\u5148\u52a8 detector \u6216 tracker \u884c\u4e3a\u3002",
            )
            expected_tradeoff = _localized_text(
                language,
                en="A stricter cleanup pass may hide a few borderline true detections along with short noise islands.",
                zh="\u66f4\u4e25\u7684 cleanup \u53ef\u80fd\u4f1a\u5728\u538b\u6389\u77ed\u566a\u58f0\u6bb5\u7684\u540c\u65f6\uff0c\u4e5f\u85cf\u6389\u5c11\u91cf\u8fb9\u7f18\u771f\u5b9e\u68c0\u6d4b\u3002",
            )

        output_name_suggestion = f"{Path(config_name).stem}_{output_slug}"

        return {
            "title": title,
            "diagnosis": diagnosis,
            "recommendation": recommendation,
            "expected_tradeoff": expected_tradeoff,
            "patch": patch,
            "patch_preview": _flatten_patch_lines(patch),
            "evidence": evidence,
            "output_name_suggestion": output_name_suggestion,
        }

    def _ai_explain_with_model(
        self,
        run_id: str | None,
        config_name: str | None,
        focus: str | None,
        language: str,
    ) -> dict[str, Any]:
        payload = self._build_ai_context(run_id=run_id, config_name=config_name, focus=focus, language=language)
        prompt = json.dumps(payload, ensure_ascii=False, indent=2)
        language_instruction = _localized_text(
            language,
            en="Write all human-readable output in English.",
            zh="Write all human-readable output in Simplified Chinese.",
        )
        instructions = (
            "You are helping operate a football tracking system. "
            "Return strict JSON with keys: summary (string), evidence (array of short strings). "
            "Ground every sentence in the provided evidence. "
            "Do not invent artifacts, files, or metrics. "
            f"{language_instruction}"
        )
        response = self.ai_client.create_json_response(
            instructions=instructions,
            prompt=prompt,
            temperature=0.1,
        )
        return {
            "summary": str(response.get("summary", "")),
            "evidence": [str(item) for item in response.get("evidence", []) if str(item).strip()],
        }

    def _ai_recommend_with_model(self, run_id: str, objective: str | None, language: str) -> dict[str, Any]:
        run = self.get_run(run_id)
        config_name = run.get("config_name")
        if not config_name:
            raise FileNotFoundError(f"Run {run_id} is not linked to a config.")

        payload = self._build_ai_context(run_id=run_id, config_name=config_name, focus=objective, language=language)
        prompt = json.dumps(payload, ensure_ascii=False, indent=2)
        language_instruction = _localized_text(
            language,
            en="Write all human-readable fields in English. Keep patch keys and patch_preview paths in code-style English.",
            zh="Write all human-readable fields in Simplified Chinese. Keep patch keys and patch_preview paths in code-style English.",
        )
        instructions = (
            "You are recommending the next config adjustment for a football tracking pipeline. "
            "Return strict JSON with keys: title, diagnosis, recommendation, expected_tradeoff, patch, patch_preview, evidence, output_name_suggestion. "
            "The patch must be a nested object suitable for YAML merge. "
            "Only touch conservative operator-facing parameters in follow_cam, postprocess, scene_bias.dynamic_air_recovery, selection, or tracking. "
            "Do not suggest destructive changes. "
            "Patch preview must be a flat array of 'path: value' strings matching the patch object. "
            "output_name_suggestion must be a short lowercase ASCII slug. "
            f"{language_instruction}"
        )
        response = self.ai_client.create_json_response(
            instructions=instructions,
            prompt=prompt,
            temperature=0.2,
        )
        patch = response.get("patch", {})
        if not isinstance(patch, dict):
            patch = {}
        patch_preview = response.get("patch_preview", [])
        if not isinstance(patch_preview, list) or not patch_preview:
            patch_preview = _flatten_patch_lines(patch)
        output_name_suggestion = str(
            response.get("output_name_suggestion") or f"{Path(config_name).stem}_{self._slugify(objective or 'ai_update')}"
        )
        return {
            "title": str(response.get("title", "Model Recommendation")),
            "diagnosis": str(response.get("diagnosis", "")),
            "recommendation": str(response.get("recommendation", "")),
            "expected_tradeoff": str(response.get("expected_tradeoff", "")),
            "patch": patch,
            "patch_preview": [str(item) for item in patch_preview],
            "evidence": [str(item) for item in response.get("evidence", []) if str(item).strip()],
            "output_name_suggestion": output_name_suggestion,
        }

    def ai_config_diff(self, base_config_name: str, patch: dict[str, Any], output_name: str | None = None) -> dict[str, Any]:
        resolved_output_name = output_name or f"{Path(base_config_name).stem}_ai_patch"
        return {
            "base_config_name": base_config_name,
            "output_name": resolved_output_name,
            "patch": patch,
            "patch_preview": _flatten_patch_lines(patch),
        }

    def _build_ai_context(
        self,
        run_id: str | None,
        config_name: str | None,
        focus: str | None,
        language: str,
    ) -> dict[str, Any]:
        context: dict[str, Any] = {"focus": focus, "response_language": language}

        if config_name:
            config = self.get_config(config_name)
            context["config"] = {
                "name": config["name"],
                "summary": config["summary"],
                "resolved": {
                    "postprocess": config["resolved"].get("postprocess", {}),
                    "follow_cam": config["resolved"].get("follow_cam", {}),
                    "scene_bias": config["resolved"].get("scene_bias", {}),
                    "selection": config["resolved"].get("selection", {}),
                    "tracking": config["resolved"].get("tracking", {}),
                },
            }

        if run_id:
            run = self.get_run(run_id)
            cleanup = run.get("stats", {}).get("cleanup", {}) or {}
            follow_cam = run.get("stats", {}).get("follow_cam", {}) or {}
            context["run"] = {
                "run_id": run["run_id"],
                "status": run["status"],
                "config_name": run.get("config_name"),
                "modules_enabled": run.get("modules_enabled", {}),
                "raw_stats": run.get("stats", {}).get("raw", {}),
                "cleaned_stats": run.get("stats", {}).get("cleaned", {}),
                "cleanup_summary": {
                    "scrubbed_frame_count": cleanup.get("scrubbed_frame_count"),
                    "scrubbed_segment_count": cleanup.get("scrubbed_segment_count"),
                    "actions_preview": (cleanup.get("actions") or [])[:5],
                },
                "follow_cam_summary": {
                    "track_source": follow_cam.get("track_source"),
                    "target_resolution": follow_cam.get("target_resolution"),
                    "mean_crop_height": follow_cam.get("mean_crop_height"),
                    "min_crop_height": follow_cam.get("min_crop_height"),
                    "max_crop_height": follow_cam.get("max_crop_height"),
                    "status_counts": follow_cam.get("status_counts"),
                },
            }
        return context

    def create_run(self, request: dict[str, Any]) -> dict[str, Any]:
        requested_output_name = request.get("output_dir_name")
        run_id = Path(requested_output_name).name if requested_output_name else ""
        if not run_id:
            run_id = f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"

        config_path, relative_name = self._resolve_config_path(request["config_name"])
        config_patch = request.get("config_patch") or {}
        if config_patch:
            config_path, relative_name = self._materialize_run_config(
                base_config_path=config_path,
                base_config_name=relative_name,
                run_id=run_id,
                patch=config_patch,
                suffix="field_setup",
            )
        config = load_config(config_path)

        if request.get("input_video"):
            config.input_video = Path(request["input_video"]).resolve()
        if request.get("enable_postprocess") is not None:
            config.postprocess.enabled = bool(request["enable_postprocess"])
        if request.get("enable_follow_cam") is not None:
            config.follow_cam.enabled = bool(request["enable_follow_cam"])
        if request.get("start_frame") is not None:
            config.runtime.start_frame = int(request["start_frame"])
        if request.get("max_frames") is not None:
            config.runtime.max_frames = int(request["max_frames"])

        config.output_dir = (self.outputs_dir / "api_runs" / run_id).resolve()
        if config.output_dir.exists() and any(config.output_dir.iterdir()):
            raise FileExistsError(str(config.output_dir))
        config.output_dir.mkdir(parents=True, exist_ok=True)

        run_record = {
            "run_id": run_id,
            "source": "api",
            "status": "queued",
            "created_at": _utc_now_iso(),
            "started_at": None,
            "completed_at": None,
            "config_name": relative_name,
            "config_path": str(config_path),
            "input_video": str(config.input_video),
            "output_dir": str(config.output_dir),
            "modules_enabled": {
                "postprocess": bool(config.postprocess.enabled),
                "follow_cam": bool(config.follow_cam.enabled),
            },
            "artifacts": [],
            "stats": {},
            "notes": request.get("notes"),
            "error": None,
        }

        with self._lock:
            self._assert_no_active_run_locked()
            registry = self._read_registry()
            registry["runs"] = [run for run in registry["runs"] if run["run_id"] != run_id]
            registry["runs"].append(run_record)
            self._write_registry(registry)
            thread = threading.Thread(
                target=self._execute_run,
                args=(run_id, config),
                name=f"football-tracking-run-{run_id}",
                daemon=True,
            )
            self._active_threads[run_id] = thread
            thread.start()
        return run_record

    def _execute_run(self, run_id: str, config: AppConfig) -> None:
        self._update_run(run_id, {"status": "running", "started_at": _utc_now_iso(), "error": None})
        try:
            BallTrackingPipeline(config).run()
            existing = self.get_run(run_id)
            updated = self._build_run_snapshot(
                run_id=run_id,
                source="api",
                status="completed",
                created_at=existing["created_at"],
                config_name=existing.get("config_name"),
                config_path=existing.get("config_path"),
                input_video=str(config.input_video),
                output_dir=config.output_dir,
                modules_enabled={
                    "postprocess": bool(config.postprocess.enabled),
                    "follow_cam": bool(config.follow_cam.enabled),
                },
                notes=existing.get("notes"),
                started_at=existing.get("started_at"),
                completed_at=_utc_now_iso(),
            )
            self._replace_run(run_id, updated)
        except Exception as exc:
            self._update_run(
                run_id,
                {
                    "status": "failed",
                    "completed_at": _utc_now_iso(),
                    "error": str(exc),
                    "artifacts": self._collect_artifacts(config.output_dir),
                    "stats": self._collect_stats(config.output_dir),
                },
            )
        finally:
            with self._lock:
                self._active_threads.pop(run_id, None)

    def _assert_no_active_run_locked(self) -> None:
        running = [run_id for run_id, thread in self._active_threads.items() if thread.is_alive()]
        if running:
            raise RuntimeError(f"Another run is already active: {running[0]}")

    def _update_run(self, run_id: str, patch: dict[str, Any]) -> None:
        with self._lock:
            registry = self._read_registry()
            for run in registry["runs"]:
                if run["run_id"] == run_id:
                    run.update(patch)
                    self._write_registry(registry)
                    return
        raise KeyError(run_id)

    def _replace_run(self, run_id: str, replacement: dict[str, Any]) -> None:
        with self._lock:
            registry = self._read_registry()
            for index, run in enumerate(registry["runs"]):
                if run["run_id"] == run_id:
                    registry["runs"][index] = replacement
                    self._write_registry(registry)
                    return
        raise KeyError(run_id)

    def _ensure_registry_file(self) -> None:
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.registry_path.exists():
            self._write_registry({"runs": []})

    def _read_registry(self) -> dict[str, Any]:
        if not self.registry_path.exists():
            return {"runs": []}
        with self.registry_path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
        if not isinstance(raw, dict) or "runs" not in raw:
            return {"runs": []}
        if not isinstance(raw["runs"], list):
            raw["runs"] = []
        return raw

    def _write_registry(self, registry: dict[str, Any]) -> None:
        with self.registry_path.open("w", encoding="utf-8") as handle:
            json.dump(registry, handle, ensure_ascii=False, indent=2)

    def _refresh_discovered_runs_locked(self, registry: dict[str, Any]) -> None:
        known_by_output = {Path(run["output_dir"]).resolve(): run for run in registry["runs"] if run.get("output_dir")}
        config_index = self._build_config_output_index()
        for output_dir in self._iter_output_run_dirs():
            if not output_dir.is_dir():
                continue
            if not any(output_dir.iterdir()):
                continue
            resolved_output_dir = output_dir.resolve()
            if resolved_output_dir in known_by_output:
                run = known_by_output[resolved_output_dir]
                run["artifacts"] = self._collect_artifacts(output_dir)
                run["stats"] = self._collect_stats(output_dir)
                continue

            config_meta = config_index.get(resolved_output_dir)
            registry["runs"].append(
                self._build_run_snapshot(
                    run_id=f"scan_{output_dir.name}",
                    source="filesystem_scan",
                    status="completed",
                    created_at=datetime.fromtimestamp(output_dir.stat().st_mtime, tz=timezone.utc).isoformat(),
                    config_name=None if config_meta is None else config_meta["name"],
                    config_path=None if config_meta is None else str(config_meta["path"]),
                    input_video=None if config_meta is None else config_meta["input_video"],
                    output_dir=output_dir,
                    modules_enabled=self._collect_module_flags(output_dir, config_meta),
                    notes=None,
                )
            )

    def _build_run_snapshot(
        self,
        *,
        run_id: str,
        source: str,
        status: str,
        created_at: str,
        config_name: str | None,
        config_path: str | None,
        input_video: str | None,
        output_dir: Path,
        modules_enabled: dict[str, bool],
        notes: str | None,
        started_at: str | None = None,
        completed_at: str | None = None,
    ) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "source": source,
            "status": status,
            "created_at": created_at,
            "started_at": started_at,
            "completed_at": completed_at,
            "config_name": config_name,
            "config_path": config_path,
            "input_video": input_video,
            "output_dir": str(output_dir.resolve()),
            "modules_enabled": modules_enabled,
            "artifacts": self._collect_artifacts(output_dir),
            "stats": self._collect_stats(output_dir),
            "notes": notes,
            "error": None,
        }

    def _build_config_output_index(self) -> dict[Path, dict[str, Any]]:
        index: dict[Path, dict[str, Any]] = {}
        for config_path in self.config_dir.rglob("*.yaml"):
            try:
                config = load_config(config_path)
            except Exception:
                continue
            index[config.output_dir.resolve()] = {
                "name": config_path.relative_to(self.config_dir).as_posix(),
                "path": config_path,
                "input_video": str(config.input_video),
                "postprocess_enabled": bool(config.postprocess.enabled),
                "follow_cam_enabled": bool(config.follow_cam.enabled),
            }
        return index

    def _build_config_summary(self, config_path: Path, relative_name: str) -> dict[str, Any]:
        raw = self._load_raw_yaml(config_path)
        try:
            config = load_config(config_path)
            input_video = str(config.input_video)
            output_dir = str(config.output_dir)
            detector_model_path = str(config.detector.model_path)
            postprocess_enabled = bool(config.postprocess.enabled)
            follow_cam_enabled = bool(config.follow_cam.enabled)
            exists = {
                "input_video": config.input_video.exists(),
                "output_dir": config.output_dir.exists(),
                "detector_model_path": config.detector.model_path.exists(),
            }
        except Exception:
            input_video = str(raw.get("input_video", "")) or None
            output_dir = str(raw.get("output_dir", "")) or None
            detector_model_path = str((raw.get("detector") or {}).get("model_path", "")) or None
            postprocess_enabled = bool((raw.get("postprocess") or {}).get("enabled", False))
            follow_cam_enabled = bool((raw.get("follow_cam") or {}).get("enabled", False))
            exists = {"input_video": False, "output_dir": False, "detector_model_path": False}
        return {
            "name": relative_name,
            "path": str(config_path),
            "input_video": input_video,
            "output_dir": output_dir,
            "detector_model_path": detector_model_path,
            "postprocess_enabled": postprocess_enabled,
            "follow_cam_enabled": follow_cam_enabled,
            "exists": exists,
        }

    def _resolve_config_path(self, name: str) -> tuple[Path, str]:
        candidate = (self.config_dir / name).resolve()
        if self.config_dir.resolve() in candidate.parents and candidate.exists() and candidate.is_file():
            return candidate, candidate.relative_to(self.config_dir).as_posix()
        if not name.endswith(".yaml"):
            candidate = (self.config_dir / f"{name}.yaml").resolve()
            if self.config_dir.resolve() in candidate.parents and candidate.exists() and candidate.is_file():
                return candidate, candidate.relative_to(self.config_dir).as_posix()
        raise FileNotFoundError(name)

    def _resolve_input_video_path(self, input_video: str) -> Path:
        candidate = Path(input_video).resolve()
        data_root = self.data_dir.resolve()
        if candidate != data_root and data_root not in candidate.parents:
            raise FileNotFoundError(f"Input video must live under {data_root}: {input_video}")
        if not candidate.exists() or not candidate.is_file():
            raise FileNotFoundError(f"Input video not found: {input_video}")
        return candidate

    def _sample_video_frames(self, video_path: Path) -> list[dict[str, Any]]:
        capture = cv2.VideoCapture(str(video_path))
        if not capture.isOpened():
            return []

        fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
        frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        fractions = [0.18, 0.5, 0.82] if frame_count > 3 else [0.5]
        samples: list[dict[str, Any]] = []

        for index, fraction in enumerate(fractions, start=1):
            frame_index = max(0, int(round((max(frame_count, 1) - 1) * fraction)))
            capture.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
            ok, frame = capture.read()
            if not ok or frame is None:
                continue
            frame_height, frame_width = frame.shape[:2]
            samples.append(
                {
                    "frame": frame,
                    "frame_index": frame_index,
                    "frame_time_seconds": frame_index / fps if fps > 0 else 0.0,
                    "frame_width": int(frame_width),
                    "frame_height": int(frame_height),
                    "sample_index": index,
                    "sample_count": len(fractions),
                }
            )

        if not samples:
            capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = capture.read()
            if ok and frame is not None:
                frame_height, frame_width = frame.shape[:2]
                samples.append(
                    {
                        "frame": frame,
                        "frame_index": 0,
                        "frame_time_seconds": 0.0,
                        "frame_width": int(frame_width),
                        "frame_height": int(frame_height),
                        "sample_index": 1,
                        "sample_count": 1,
                    }
                )

        capture.release()
        return samples

    def _detect_field_polygon(
        self,
        frame: Any,
        content_bounds: tuple[int, int, int, int],
    ) -> tuple[list[tuple[int, int]], float, bool]:
        frame_height, frame_width = frame.shape[:2]
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, (28, 28, 20), (96, 255, 255))
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15))
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)

        content_x1, content_y1, content_x2, content_y2 = content_bounds
        content_width = max(1, content_x2 - content_x1)
        content_height = max(1, content_y2 - content_y1)
        content_mask = mask[content_y1:content_y2, content_x1:content_x2]
        coverage = float(cv2.countNonZero(content_mask)) / float(content_width * content_height)

        band_height = max(6, int(round(content_height * 0.03)))
        top_span = self._mask_row_span(mask, content_x1, content_x2, int(round(content_y1 + content_height * 0.18)), band_height)
        bottom_span = self._mask_row_span(mask, content_x1, content_x2, int(round(content_y1 + content_height * 0.92)), band_height)

        if coverage >= 0.08 and top_span and bottom_span:
            polygon = self._clip_polygon(
                [
                    (top_span[0], top_span[2]),
                    (top_span[1], top_span[2]),
                    (bottom_span[1], bottom_span[2]),
                    (bottom_span[0], bottom_span[2]),
                ],
                frame_width=frame_width,
                frame_height=frame_height,
            )
            return polygon, coverage, True

        return self._default_field_polygon(content_bounds), 0.0, False

    def _detect_content_bounds(self, frame: Any) -> tuple[int, int, int, int]:
        frame_height, frame_width = frame.shape[:2]
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        _, threshold = cv2.threshold(gray, 10, 255, cv2.THRESH_BINARY)
        points = cv2.findNonZero(threshold)
        if points is None:
            return (0, 0, frame_width, frame_height)
        x, y, width, height = cv2.boundingRect(points)
        return (x, y, x + width, y + height)

    def _mask_row_span(
        self,
        mask: Any,
        x1: int,
        x2: int,
        y_center: int,
        band_height: int,
    ) -> tuple[int, int, int] | None:
        y1 = max(0, y_center - band_height)
        y2 = min(mask.shape[0], y_center + band_height)
        if y2 <= y1 or x2 <= x1:
            return None
        band = mask[y1:y2, x1:x2]
        points = cv2.findNonZero(band)
        if points is None:
            return None
        xs = points[:, 0, 0]
        return (x1 + int(xs.min()), x1 + int(xs.max()), int(round((y1 + y2) / 2.0)))

    def _default_field_polygon(self, content_bounds: tuple[int, int, int, int]) -> list[tuple[int, int]]:
        x1, y1, x2, y2 = content_bounds
        width = max(1, x2 - x1)
        height = max(1, y2 - y1)
        if width / float(height) >= 2.6:
            return [
                (int(round(x1 + width * 0.18)), int(round(y1 + height * 0.18))),
                (int(round(x1 + width * 0.82)), int(round(y1 + height * 0.18))),
                (int(round(x1 + width * 0.98)), int(round(y1 + height * 0.96))),
                (int(round(x1 + width * 0.02)), int(round(y1 + height * 0.96))),
            ]
        return self._roi_to_polygon(
            (
                int(round(x1 + width * 0.08)),
                int(round(y1 + height * 0.10)),
                int(round(x2 - width * 0.08)),
                int(round(y2 - height * 0.06)),
            )
        )

    def _roi_to_polygon(self, roi: tuple[int, int, int, int]) -> list[tuple[int, int]]:
        x1, y1, x2, y2 = roi
        return [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]

    def _polygon_bounds(self, polygon: list[tuple[int, int]]) -> tuple[int, int, int, int]:
        xs = [point[0] for point in polygon]
        ys = [point[1] for point in polygon]
        return (min(xs), min(ys), max(xs), max(ys))

    def _clip_polygon(
        self,
        polygon: list[tuple[int, int]],
        *,
        frame_width: int,
        frame_height: int,
    ) -> list[tuple[int, int]]:
        return [
            (
                max(0, min(frame_width, int(round(x)))),
                max(0, min(frame_height, int(round(y)))),
            )
            for x, y in polygon
        ]

    def _expand_polygon(
        self,
        polygon: list[tuple[int, int]],
        *,
        frame_width: int,
        frame_height: int,
        scale_x: float,
        scale_y: float,
    ) -> list[tuple[int, int]]:
        bounds = self._polygon_bounds(polygon)
        center_x = (bounds[0] + bounds[2]) / 2.0
        center_y = (bounds[1] + bounds[3]) / 2.0
        expanded: list[tuple[int, int]] = []
        for x, y in polygon:
            expanded.append(
                (
                    int(round(center_x + (x - center_x) * scale_x)),
                    int(round(center_y + (y - center_y) * scale_y)),
                )
            )
        return self._clip_polygon(expanded, frame_width=frame_width, frame_height=frame_height)

    def _build_preview_bounds(
        self,
        *,
        expanded_polygon: list[tuple[int, int]],
        content_bounds: tuple[int, int, int, int],
        frame_width: int,
        frame_height: int,
    ) -> tuple[int, int, int, int]:
        content_x1, content_y1, content_x2, content_y2 = content_bounds
        box_x1, box_y1, box_x2, box_y2 = self._polygon_bounds(expanded_polygon)
        pad_x = max(12, int(round((content_x2 - content_x1) * 0.04)))
        pad_y = max(12, int(round((content_y2 - content_y1) * 0.04)))
        return (
            max(0, max(content_x1, box_x1 - pad_x)),
            max(0, max(content_y1, box_y1 - pad_y)),
            min(frame_width, min(content_x2, box_x2 + pad_x)),
            min(frame_height, min(content_y2, box_y2 + pad_y)),
        )

    def _normalize_points(self, raw_points: Any) -> list[tuple[int, int]]:
        if not isinstance(raw_points, list):
            return []
        points: list[tuple[int, int]] = []
        for raw_point in raw_points:
            if not isinstance(raw_point, list) or len(raw_point) != 2:
                return []
            points.append((int(raw_point[0]), int(raw_point[1])))
        return points

    def _load_field_setup_from_config(
        self,
        *,
        config_name: str,
        frame_width: int,
        frame_height: int,
    ) -> dict[str, Any] | None:
        config_path, _ = self._resolve_config_path(config_name)
        raw = self._load_raw_yaml(config_path)
        filtering_raw = raw.get("filtering") or {}
        scene_bias_raw = raw.get("scene_bias") or {}
        ground_zones = scene_bias_raw.get("ground_zones") or []
        positive_rois = scene_bias_raw.get("positive_rois") or []

        field_polygon: list[tuple[int, int]] = []
        expanded_polygon: list[tuple[int, int]] = []

        for zone in ground_zones:
            if not isinstance(zone, dict):
                continue
            field_polygon = self._normalize_points(zone.get("points"))
            if field_polygon:
                break
            roi = zone.get("roi")
            if isinstance(roi, list) and len(roi) == 4:
                field_polygon = self._roi_to_polygon(tuple(int(value) for value in roi))
                break

        for zone in positive_rois:
            if not isinstance(zone, dict):
                continue
            expanded_polygon = self._normalize_points(zone.get("points"))
            if expanded_polygon:
                break
            roi = zone.get("roi")
            if isinstance(roi, list) and len(roi) == 4:
                expanded_polygon = self._roi_to_polygon(tuple(int(value) for value in roi))
                break

        if not expanded_polygon:
            roi = filtering_raw.get("roi")
            if isinstance(roi, list) and len(roi) == 4:
                expanded_polygon = self._roi_to_polygon(tuple(int(value) for value in roi))

        if not field_polygon:
            roi = filtering_raw.get("roi")
            if isinstance(roi, list) and len(roi) == 4:
                field_polygon = self._roi_to_polygon(tuple(int(value) for value in roi))

        if not field_polygon:
            return None

        field_polygon = self._clip_polygon(field_polygon, frame_width=frame_width, frame_height=frame_height)
        if not expanded_polygon:
            expanded_polygon = self._expand_polygon(
                field_polygon,
                frame_width=frame_width,
                frame_height=frame_height,
                scale_x=1.08,
                scale_y=1.10,
            )
        else:
            expanded_polygon = self._clip_polygon(expanded_polygon, frame_width=frame_width, frame_height=frame_height)

        return {
            "field_polygon": field_polygon,
            "expanded_polygon": expanded_polygon,
            "field_roi": self._polygon_bounds(field_polygon),
            "expanded_roi": self._polygon_bounds(expanded_polygon),
        }

    def _pad_roi(
        self,
        roi: tuple[int, int, int, int],
        frame_width: int,
        frame_height: int,
        pad_x_ratio: float,
        pad_y_ratio: float,
    ) -> tuple[int, int, int, int]:
        x1, y1, x2, y2 = roi
        pad_x = int(round(frame_width * pad_x_ratio))
        pad_y = int(round(frame_height * pad_y_ratio))
        return (
            max(0, x1 - pad_x),
            max(0, y1 - pad_y),
            min(frame_width, x2 + pad_x),
            min(frame_height, y2 + pad_y),
        )

    def _expand_roi(
        self,
        roi: tuple[int, int, int, int],
        *,
        frame_width: int,
        frame_height: int,
        padding_x_ratio: float,
        padding_y_ratio: float,
    ) -> tuple[int, int, int, int]:
        return self._pad_roi(
            roi,
            frame_width=frame_width,
            frame_height=frame_height,
            pad_x_ratio=padding_x_ratio,
            pad_y_ratio=padding_y_ratio,
        )

    def _encode_frame_data_url(self, frame: Any) -> str:
        ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
        if not ok:
            raise RuntimeError("Unable to encode preview frame.")
        payload = base64.b64encode(encoded.tobytes()).decode("ascii")
        return f"data:image/jpeg;base64,{payload}"

    def _build_field_config_patch(
        self,
        *,
        field_polygon: list[tuple[int, int]],
        expanded_polygon: list[tuple[int, int]],
        expanded_roi: tuple[int, int, int, int],
    ) -> dict[str, Any]:
        expanded_width = max(1, expanded_roi[2] - expanded_roi[0])
        expanded_height = max(1, expanded_roi[3] - expanded_roi[1])
        return {
            "filtering": {
                "roi": list(expanded_roi),
            },
            "scene_bias": {
                "enabled": True,
                "ground_zones": [
                    {
                        "name": "field_core",
                        "points": [list(point) for point in field_polygon],
                    }
                ],
                "positive_rois": [
                    {
                        "name": "field_buffer",
                        "points": [list(point) for point in expanded_polygon],
                    }
                ],
                "dynamic_air_recovery": {
                    "enabled": True,
                    "edge_reentry_expand_x": float(expanded_width),
                    "edge_reentry_expand_y": float(expanded_height),
                },
            },
        }

    def _materialize_run_config(
        self,
        *,
        base_config_path: Path,
        base_config_name: str,
        run_id: str,
        patch: dict[str, Any],
        suffix: str,
    ) -> tuple[Path, str]:
        merged = _deep_merge(self._load_raw_yaml(base_config_path), patch)
        output_name = f"{Path(base_config_name).stem}_{suffix}_{run_id}.yaml"
        self.generated_config_dir.mkdir(parents=True, exist_ok=True)
        output_path = self.generated_config_dir / output_name
        with output_path.open("w", encoding="utf-8") as handle:
            yaml.safe_dump(merged, handle, sort_keys=False, allow_unicode=False)
        return output_path, output_path.relative_to(self.config_dir).as_posix()

    def _iter_output_run_dirs(self) -> list[Path]:
        if not self.outputs_dir.exists():
            return []
        discovered: list[Path] = []
        for child in sorted(self.outputs_dir.iterdir(), key=lambda item: item.name):
            if not child.is_dir():
                continue
            if child.name == "api_runs":
                discovered.extend(sorted((item for item in child.iterdir() if item.is_dir()), key=lambda item: item.name))
                continue
            discovered.append(child)
        return discovered

    def _load_raw_yaml(self, config_path: Path) -> dict[str, Any]:
        with config_path.open("r", encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"Invalid config root in {config_path}")
        return loaded

    def _collect_module_flags(self, output_dir: Path, config_meta: dict[str, Any] | None) -> dict[str, bool]:
        if config_meta is not None:
            return {
                "postprocess": bool(config_meta.get("postprocess_enabled", False)),
                "follow_cam": bool(config_meta.get("follow_cam_enabled", False)),
            }
        return {
            "postprocess": (output_dir / "cleanup_report.json").exists(),
            "follow_cam": (output_dir / "follow_cam_report.json").exists(),
        }

    def _collect_artifacts(self, output_dir: Path) -> list[dict[str, Any]]:
        if not output_dir.exists():
            return []
        artifacts: list[dict[str, Any]] = []
        for artifact_path in sorted(output_dir.iterdir(), key=lambda item: item.name):
            if not artifact_path.is_file():
                continue
            content_type, _ = mimetypes.guess_type(str(artifact_path))
            suffix = artifact_path.suffix.lower()
            if suffix == ".mp4":
                kind = "video"
            elif suffix == ".csv":
                kind = "csv"
            elif suffix == ".jsonl":
                kind = "jsonl"
            elif suffix == ".json":
                kind = "json"
            else:
                kind = "file"
            artifacts.append(
                {
                    "name": artifact_path.name,
                    "path": str(artifact_path.resolve()),
                    "kind": kind,
                    "exists": artifact_path.exists(),
                    "size_bytes": artifact_path.stat().st_size,
                    "content_type": content_type,
                }
            )
        return artifacts

    def _collect_stats(self, output_dir: Path) -> dict[str, Any]:
        raw_summary = self._summarize_track_csv(output_dir / "ball_track.csv")
        cleaned_summary = self._summarize_track_csv(output_dir / "ball_track.cleaned.csv")
        cleanup_report = self._read_optional_json(output_dir / "cleanup_report.json")
        follow_cam_report = self._read_optional_json(output_dir / "follow_cam_report.json")
        stats: dict[str, Any] = {}
        if raw_summary is not None:
            stats["raw"] = raw_summary
        if cleaned_summary is not None:
            stats["cleaned"] = cleaned_summary
        if cleanup_report is not None:
            stats["cleanup"] = cleanup_report
        if follow_cam_report is not None:
            stats["follow_cam"] = follow_cam_report
        return stats

    def _summarize_track_csv(self, csv_path: Path) -> dict[str, Any] | None:
        if not csv_path.exists():
            return None
        frame_count = 0
        status_counts = {"Detected": 0, "Predicted": 0, "Lost": 0}
        with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                frame_count += 1
                status = row.get("Status", "")
                if status in status_counts:
                    status_counts[status] += 1
        return {
            "frame_count": frame_count,
            "detected": status_counts["Detected"],
            "predicted": status_counts["Predicted"],
            "lost": status_counts["Lost"],
            "detected_ratio": 0.0 if frame_count == 0 else round(status_counts["Detected"] / frame_count, 4),
            "predicted_ratio": 0.0 if frame_count == 0 else round(status_counts["Predicted"] / frame_count, 4),
            "lost_ratio": 0.0 if frame_count == 0 else round(status_counts["Lost"] / frame_count, 4),
        }

    def _load_optional_json_artifact(self, run_id: str, name: str) -> dict[str, Any]:
        artifact_path = self.get_artifact_path(run_id, name)
        with artifact_path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def _read_optional_json(self, path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def _slugify(self, text: str) -> str:
        normalized = unicodedata.normalize("NFKD", text.strip()).encode("ascii", "ignore").decode("ascii")
        cleaned = "".join(char.lower() if char.isalnum() else "_" for char in normalized)
        collapsed = "_".join(filter(None, cleaned.split("_")))
        return collapsed[:48] or "ai_update"
