from __future__ import annotations

import csv
import json
import mimetypes
import threading
from copy import deepcopy
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

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

    def ai_explain(self, run_id: str | None, config_name: str | None, focus: str | None) -> dict[str, Any]:
        if self.ai_client.is_enabled():
            try:
                return self._ai_explain_with_model(run_id=run_id, config_name=config_name, focus=focus)
            except Exception:
                pass
        return self._ai_explain_heuristic(run_id=run_id, config_name=config_name, focus=focus)

    def ai_recommend(self, run_id: str, objective: str | None) -> dict[str, Any]:
        if self.ai_client.is_enabled():
            try:
                return self._ai_recommend_with_model(run_id=run_id, objective=objective)
            except Exception:
                pass
        return self._ai_recommend_heuristic(run_id=run_id, objective=objective)

    def _ai_explain_heuristic(self, run_id: str | None, config_name: str | None, focus: str | None) -> dict[str, Any]:
        evidence: list[str] = []
        summary_parts: list[str] = []

        if run_id:
            run = self.get_run(run_id)
            raw_stats = run.get("stats", {}).get("raw", {})
            cleaned_stats = run.get("stats", {}).get("cleaned", {})
            summary_parts.append(
                f"Run {run_id} is {run['status']} with cleaned detected ratio "
                f"{float(cleaned_stats.get('detected_ratio', raw_stats.get('detected_ratio', 0.0))) * 100:.1f}%."
            )
            evidence.extend(
                [
                    f"run.status={run['status']}",
                    f"run.config={run.get('config_name')}",
                    f"raw.detected={raw_stats.get('detected')}",
                    f"raw.lost={raw_stats.get('lost')}",
                    f"cleaned.detected={cleaned_stats.get('detected')}",
                    f"cleaned.lost={cleaned_stats.get('lost')}",
                ]
            )

        if config_name:
            config = self.get_config(config_name)
            resolved = config["resolved"]
            summary_parts.append(
                f"Config {config_name} has postprocess={resolved.get('postprocess', {}).get('enabled')} "
                f"and follow_cam={resolved.get('follow_cam', {}).get('enabled')}."
            )
            evidence.extend(
                [
                    f"config.output_dir={config['summary']['output_dir']}",
                    f"config.input_video={config['summary']['input_video']}",
                ]
            )

        if focus:
            summary_parts.append(f"Requested focus: {focus}.")

        if not summary_parts:
            summary_parts.append("No run or config was provided, so AI explanation has no grounded evidence yet.")

        return {
            "summary": " ".join(summary_parts),
            "evidence": evidence,
        }

    def _ai_recommend_heuristic(self, run_id: str, objective: str | None) -> dict[str, Any]:
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
        title = "Grounded Recommendation"
        diagnosis = (
            f"Detected ratio is {detected_ratio * 100:.1f}% and lost ratio is {lost_ratio * 100:.1f}% "
            f"for run {run_id}."
        )
        recommendation = "Stay on the current baseline and make only targeted adjustments."
        expected_tradeoff = "Conservative changes keep current gains and avoid reintroducing noisy regressions."
        evidence = [
            f"run_id={run_id}",
            f"config={config_name}",
            f"cleaned.detected_ratio={detected_ratio:.4f}",
            f"cleaned.lost_ratio={lost_ratio:.4f}",
        ]

        if any(token in objective_text for token in ["camera", "follow", "zoom", "pan"]):
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
            title = "Follow-Cam Stabilization"
            diagnosis = (
                f"Mean crop height is {mean_crop_height:.1f}px. The fastest win is to make pan and zoom slower to react."
            )
            recommendation = "Slow pan response first and require longer zoom confirmation before changing crop depth."
            expected_tradeoff = "The camera will feel steadier, but fast breaks may take slightly longer to catch up."
            evidence.extend(
                [
                    f"follow_cam.mean_crop_height={mean_crop_height:.2f}",
                    f"follow_cam.enabled={run.get('modules_enabled', {}).get('follow_cam')}",
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
            title = "Reacquire Tightening"
            diagnosis = "Lost ratio is still material, but global detector loosening is riskier than targeted reacquire tightening."
            recommendation = "Tighten tentative reacquire acceptance before changing detector sensitivity."
            expected_tradeoff = "This should suppress noisy far-jump recoveries, but may delay a few true long-gap reacquires."
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
            title = "Post-Cleanup Tightening"
            diagnosis = "Tracking is already strong enough that cleanup is a safer place to shave visible noise."
            recommendation = "Prefer small cleanup threshold changes before touching detector or tracker behavior."
            expected_tradeoff = "A stricter cleanup pass may hide a few borderline true detections along with short noise islands."

        slug = self._slugify(objective or title)
        output_name_suggestion = f"{Path(config_name).stem}_{slug}"

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

    def _ai_explain_with_model(self, run_id: str | None, config_name: str | None, focus: str | None) -> dict[str, Any]:
        payload = self._build_ai_context(run_id=run_id, config_name=config_name, focus=focus)
        prompt = json.dumps(payload, ensure_ascii=False, indent=2)
        instructions = (
            "You are helping operate a football tracking system. "
            "Return strict JSON with keys: summary (string), evidence (array of short strings). "
            "Ground every sentence in the provided evidence. "
            "Do not invent artifacts, files, or metrics."
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

    def _ai_recommend_with_model(self, run_id: str, objective: str | None) -> dict[str, Any]:
        run = self.get_run(run_id)
        config_name = run.get("config_name")
        if not config_name:
            raise FileNotFoundError(f"Run {run_id} is not linked to a config.")

        payload = self._build_ai_context(run_id=run_id, config_name=config_name, focus=objective)
        prompt = json.dumps(payload, ensure_ascii=False, indent=2)
        instructions = (
            "You are recommending the next config adjustment for a football tracking pipeline. "
            "Return strict JSON with keys: title, diagnosis, recommendation, expected_tradeoff, patch, patch_preview, evidence, output_name_suggestion. "
            "The patch must be a nested object suitable for YAML merge. "
            "Only touch conservative operator-facing parameters in follow_cam, postprocess, scene_bias.dynamic_air_recovery, selection, or tracking. "
            "Do not suggest destructive changes. "
            "Patch preview must be a flat array of 'path: value' strings matching the patch object."
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

    def _build_ai_context(self, run_id: str | None, config_name: str | None, focus: str | None) -> dict[str, Any]:
        context: dict[str, Any] = {"focus": focus}

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
        config_path, relative_name = self._resolve_config_path(request["config_name"])
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

        requested_output_name = request.get("output_dir_name")
        run_id = Path(requested_output_name).name if requested_output_name else ""
        if not run_id:
            run_id = f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"
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
        cleaned = "".join(char.lower() if char.isalnum() else "_" for char in text.strip())
        collapsed = "_".join(filter(None, cleaned.split("_")))
        return collapsed[:48] or "ai_update"
