from __future__ import annotations

import csv
import json
import math
import os
import re
import shutil
import subprocess
import time
import uuid
from concurrent.futures import CancelledError
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

import cv2

from football_tracking.camera_motion_audit import write_camera_motion_audit_report
from football_tracking.config import AppConfig
from football_tracking.types import OutputStatus

_FFMPEG_FINALIZE_TIMEOUT_SECONDS = 30.0
_FFMPEG_PROBE_FALLBACK_TIMEOUT_SECONDS = 600.0
_FFMPEG_PROBE_MINIMUM_TIMEOUT_SECONDS = 60.0
_FFMPEG_PROBE_DURATION_MULTIPLIER = 1.25
_FFMPEG_PROBE_POLL_SECONDS = 0.1
_FFMPEG_PROBE_STOP_TIMEOUT_SECONDS = 2.0


class _FrameWriter(Protocol):
    def write(self, frame: Any) -> None: ...

    def release(self) -> None: ...


class _ImageioH264Writer:
    def __init__(self, writer: Any, output_path: Path) -> None:
        self._writer = writer
        self._output_path = output_path
        self._released = False

    def write(self, frame: Any) -> None:
        if self._released:
            raise RuntimeError("Follow-cam H.264 writer is already closed")
        try:
            self._writer.send(frame.tobytes())
        except (OSError, RuntimeError) as exc:
            raise RuntimeError("Follow-cam H.264 encoding failed; the bundled ffmpeg must provide libx264") from exc

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            self._writer.close()
        except (OSError, RuntimeError) as exc:
            raise RuntimeError(f"Unable to finalize browser-compatible follow-cam video: {self._output_path}") from exc


@dataclass(slots=True)
class FollowCamFrame:
    frame_index: int
    x: float | None
    y: float | None
    confidence: float
    status: OutputStatus


@dataclass(slots=True)
class CameraPathEntry:
    frame_index: int
    center_x: float
    center_y: float
    crop_x1: int
    crop_y1: int
    crop_x2: int
    crop_y2: int
    crop_width: int
    crop_height: int
    source_status: str
    track_x: float | None
    track_y: float | None
    confidence: float
    speed: float
    zoom_out_ratio: float
    pan_mode: str
    profile: str
    action_center_enabled: bool
    action_center_x: float | None
    action_center_y: float | None
    action_center_source: str
    action_center_player_count: int


@dataclass(slots=True)
class ActionCenterPoint:
    x: float | None
    y: float | None
    source: str
    player_count: int = 0


class FollowCamGenerator:
    def __init__(self, app_config: AppConfig) -> None:
        self.app_config = app_config
        self.config = app_config.follow_cam

    def run(
        self,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> None:
        if not self.config.enabled:
            return

        output_dir = self.app_config.output_dir
        output_path = output_dir / self.config.output_video_name
        if output_path.suffix.lower() != ".mp4":
            raise RuntimeError(f"Follow-cam browser output must use an .mp4 container: {output_path}")

        track_csv_path, track_source = self._resolve_track_csv(output_dir)
        frames = self._load_frames(track_csv_path)
        if not frames:
            raise RuntimeError(f"Follow-cam track source is empty: {track_csv_path}")

        capture_backend = getattr(cv2, self.app_config.runtime.capture_backend, cv2.CAP_ANY)
        capture = cv2.VideoCapture(str(self.app_config.input_video), capture_backend)
        if not capture.isOpened():
            raise RuntimeError(f"Unable to reopen input video for follow-cam: {self.app_config.input_video}")

        transaction_id = uuid.uuid4().hex
        pending_output_path = output_path.with_name(f".{output_path.name}.{transaction_id}.pending.mp4")
        sidecar_staging_dir = output_dir / f".follow_cam.{transaction_id}.pending"

        try:
            start_frame = frames[0].frame_index
            self._seek_to_frame(capture, start_frame)

            fps = capture.get(cv2.CAP_PROP_FPS) or 20.0
            source_width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
            source_height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
            writer = self._open_writer(pending_output_path, fps)
            try:
                path_entries = self._render_follow_cam(
                    capture=capture,
                    writer=writer,
                    frames=frames,
                    source_width=source_width,
                    source_height=source_height,
                    progress_callback=progress_callback,
                    should_cancel=should_cancel,
                )
            finally:
                writer.release()

            if len(path_entries) != len(frames):
                raise RuntimeError(
                    "Follow-cam render ended before all input frames were written: "
                    f"expected {len(frames)}, got {len(path_entries)}"
                )
            self._raise_if_cancelled(should_cancel)
            sidecar_staging_dir.mkdir(parents=True, exist_ok=False)
            staged_camera_path = sidecar_staging_dir / self.config.camera_path_name
            staged_camera_path.parent.mkdir(parents=True, exist_ok=True)
            self._write_camera_path(staged_camera_path, path_entries)
            camera_motion_audit = write_camera_motion_audit_report(
                sidecar_staging_dir,
                target_width=self.config.target_width,
                target_height=self.config.target_height,
                camera_path_name=self.config.camera_path_name,
            )
            staged_report = sidecar_staging_dir / self.config.report_name
            staged_report.parent.mkdir(parents=True, exist_ok=True)
            self._write_report(
                staged_report,
                track_csv_path,
                track_source,
                path_entries,
                camera_motion_audit=camera_motion_audit,
            )

            self._raise_if_cancelled(should_cancel)
            self._publish_delivery_bundle(
                pending_output_path,
                output_path,
                sidecar_staging_dir=sidecar_staging_dir,
                output_dir=output_dir,
                expected_frame_count=len(frames),
                expected_fps=fps,
                should_cancel=should_cancel,
            )
        finally:
            capture.release()
            pending_output_path.unlink(missing_ok=True)
            shutil.rmtree(sidecar_staging_dir, ignore_errors=True)

    def _resolve_track_csv(self, output_dir: Path) -> tuple[Path, str]:
        cleaned_csv = output_dir / self.app_config.postprocess.cleaned_csv_name
        raw_csv = output_dir / self.app_config.output.csv_name
        if self.config.prefer_cleaned_track and cleaned_csv.exists():
            return cleaned_csv, "cleaned"
        if raw_csv.exists():
            return raw_csv, "raw"
        raise FileNotFoundError("No track CSV available for follow-cam generation.")

    def _load_frames(self, track_csv_path: Path) -> list[FollowCamFrame]:
        frames: list[FollowCamFrame] = []
        with track_csv_path.open("r", newline="", encoding="utf-8-sig") as csv_file:
            reader = csv.DictReader(csv_file)
            for row in reader:
                x_raw = row.get("X", "")
                y_raw = row.get("Y", "")
                frames.append(
                    FollowCamFrame(
                        frame_index=int(row["Frame"]),
                        x=None if x_raw == "" else float(x_raw),
                        y=None if y_raw == "" else float(y_raw),
                        confidence=float(row.get("Confidence") or 0.0),
                        status=OutputStatus(row["Status"]),
                    )
                )
        return frames

    def _load_action_center_player_tracks(self) -> dict[str, Any] | None:
        path = self.config.action_center_player_tracks_path
        if path is None:
            return None
        if not path.exists():
            raise FileNotFoundError(f"Action-center player tracks not found: {path}")
        with path.open("r", encoding="utf-8") as handle:
            report = json.load(handle)
        if not isinstance(report, dict):
            raise ValueError(f"Action-center player tracks must be a JSON object: {path}")
        return report

    def _player_points_by_frame(self, report: dict[str, Any] | None) -> dict[int, list[tuple[float, float]]]:
        if not report:
            return {}
        points_by_frame: dict[int, list[tuple[float, float]]] = {}
        raw_tracks = report.get("tracks", [])
        if not isinstance(raw_tracks, list):
            return points_by_frame
        for track in raw_tracks:
            if not isinstance(track, dict):
                continue
            samples = track.get("samples", [])
            if not isinstance(samples, list):
                continue
            for sample in samples:
                parsed = self._parse_player_sample(sample)
                if parsed is None:
                    continue
                frame_index, point = parsed
                points_by_frame.setdefault(frame_index, []).append(point)
        return points_by_frame

    def _parse_player_sample(self, sample: Any) -> tuple[int, tuple[float, float]] | None:
        if not isinstance(sample, dict):
            return None
        try:
            raw_frame = float(sample["frame"])
        except (KeyError, TypeError, ValueError):
            return None
        if not math.isfinite(raw_frame):
            return None
        frame_index = int(raw_frame)

        foot_point = sample.get("foot_point")
        if isinstance(foot_point, dict):
            try:
                point = (float(foot_point["x"]), float(foot_point["y"]))
            except (KeyError, TypeError, ValueError):
                return None
            return (frame_index, point) if self._is_finite_point(point) else None
        if isinstance(foot_point, (list, tuple)) and len(foot_point) == 2:
            try:
                point = (float(foot_point[0]), float(foot_point[1]))
            except (TypeError, ValueError):
                return None
            return (frame_index, point) if self._is_finite_point(point) else None
        return None

    def _is_finite_point(self, point: tuple[float, float]) -> bool:
        return math.isfinite(point[0]) and math.isfinite(point[1])

    def _resolve_action_center(
        self,
        frame_info: FollowCamFrame,
        has_track_point: bool,
        player_points: list[tuple[float, float]],
    ) -> ActionCenterPoint:
        if not has_track_point or frame_info.x is None or frame_info.y is None:
            return ActionCenterPoint(None, None, "missing_track")
        ball_point = (float(frame_info.x), float(frame_info.y))
        if not self.config.action_center_enabled:
            return ActionCenterPoint(ball_point[0], ball_point[1], "raw_track")
        if not player_points:
            return ActionCenterPoint(ball_point[0], ball_point[1], "ball_track")

        player_center = (
            sum(point[0] for point in player_points) / len(player_points),
            sum(point[1] for point in player_points) / len(player_points),
        )
        weight = self.config.action_center_player_weight
        return ActionCenterPoint(
            self._lerp(ball_point[0], player_center[0], weight),
            self._lerp(ball_point[1], player_center[1], weight),
            "ball_players",
            len(player_points),
        )

    def _open_writer(self, output_path: Path, fps: float) -> _FrameWriter:
        if output_path.suffix.lower() != ".mp4":
            raise RuntimeError(f"Follow-cam browser output must use an .mp4 container: {output_path}")
        if self.config.target_width % 2 != 0 or self.config.target_height % 2 != 0:
            raise RuntimeError("Follow-cam H.264 output dimensions must both be even")
        if not math.isfinite(fps) or fps <= 0:
            raise RuntimeError(f"Follow-cam output FPS must be positive: {fps}")
        try:
            import imageio_ffmpeg  # pyright: ignore[reportMissingImports]

            writer = imageio_ffmpeg.write_frames(
                output_path,
                (self.config.target_width, self.config.target_height),
                pix_fmt_in="bgr24",
                pix_fmt_out="yuv420p",
                fps=fps,
                quality=6,
                codec="libx264",
                macro_block_size=1,
                ffmpeg_log_level="error",
                ffmpeg_timeout=_FFMPEG_FINALIZE_TIMEOUT_SECONDS,
                output_params=["-movflags", "+faststart", "-tag:v", "avc1"],
            )
            writer.send(None)
        except (ImportError, OSError, RuntimeError) as exc:
            raise RuntimeError(
                "Unable to start browser-compatible follow-cam encoding; bundled ffmpeg with libx264 is required"
            ) from exc
        return _ImageioH264Writer(writer, output_path)

    def _publish_browser_video(
        self,
        pending_path: Path,
        output_path: Path,
        *,
        expected_frame_count: int,
        expected_fps: float | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> None:
        if output_path.suffix.lower() != ".mp4":
            raise RuntimeError(f"Follow-cam browser output must use an .mp4 container: {output_path}")
        if pending_path.parent.resolve() != output_path.parent.resolve():
            raise RuntimeError("Follow-cam video must be atomically published within one output directory")
        self._validate_browser_video(
            pending_path,
            expected_frame_count=expected_frame_count,
            expected_fps=expected_fps,
            should_cancel=should_cancel,
        )
        self._raise_if_cancelled(should_cancel)
        self._replace_artifact_bundle([(pending_path, output_path)])

    def _publish_delivery_bundle(
        self,
        pending_video_path: Path,
        output_video_path: Path,
        *,
        sidecar_staging_dir: Path,
        output_dir: Path,
        expected_frame_count: int,
        expected_fps: float,
        should_cancel: Callable[[], bool] | None,
    ) -> None:
        if output_video_path.suffix.lower() != ".mp4":
            raise RuntimeError(f"Follow-cam browser output must use an .mp4 container: {output_video_path}")
        if pending_video_path.parent.resolve() != output_video_path.parent.resolve():
            raise RuntimeError("Follow-cam video must be atomically published within one output directory")

        sidecar_names = (
            self.config.camera_path_name,
            "camera_motion_audit.json",
            self.config.report_name,
        )
        artifacts = [
            (sidecar_staging_dir / sidecar_name, output_dir / sidecar_name)
            for sidecar_name in sidecar_names
        ]
        artifacts.append((pending_video_path, output_video_path))
        targets = [target.resolve() for _source, target in artifacts]
        if len(targets) != len(set(targets)):
            raise RuntimeError("Follow-cam delivery artifact names must be unique")
        for source, target in artifacts:
            if not source.is_file():
                raise RuntimeError(f"Follow-cam delivery artifact was not staged: {source}")
            target.parent.mkdir(parents=True, exist_ok=True)

        self._validate_browser_video(
            pending_video_path,
            expected_frame_count=expected_frame_count,
            expected_fps=expected_fps,
            should_cancel=should_cancel,
        )
        self._raise_if_cancelled(should_cancel)
        self._replace_artifact_bundle(artifacts)

    @staticmethod
    def _replace_artifact_bundle(artifacts: list[tuple[Path, Path]]) -> None:
        backup_id = uuid.uuid4().hex
        backups: list[tuple[Path, Path]] = []
        published_targets: list[Path] = []
        try:
            for _source, target in artifacts:
                if not target.exists():
                    continue
                backup = target.with_name(f".{target.name}.{backup_id}.backup")
                os.replace(target, backup)
                backups.append((target, backup))
            for source, target in artifacts:
                os.replace(source, target)
                published_targets.append(target)
        except BaseException as exc:
            rollback_errors: list[str] = []
            for target in reversed(published_targets):
                try:
                    target.unlink(missing_ok=True)
                except OSError as rollback_exc:
                    rollback_errors.append(f"remove {target}: {rollback_exc}")
            for target, backup in reversed(backups):
                try:
                    os.replace(backup, target)
                except OSError as rollback_exc:
                    rollback_errors.append(f"restore {target}: {rollback_exc}")
            if rollback_errors:
                detail = "; ".join(rollback_errors)
                raise RuntimeError(f"Follow-cam delivery failed and rollback was incomplete: {detail}") from exc
            raise
        else:
            for _target, backup in backups:
                try:
                    backup.unlink(missing_ok=True)
                except OSError:
                    pass

    @staticmethod
    def _raise_if_cancelled(should_cancel: Callable[[], bool] | None) -> None:
        if should_cancel and should_cancel():
            raise CancelledError("Run cancelled by user.")

    def _validate_browser_video(
        self,
        path: Path,
        *,
        expected_frame_count: int,
        expected_fps: float | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> None:
        if expected_frame_count <= 0:
            raise RuntimeError("Follow-cam output must contain at least one rendered frame")
        try:
            metadata = self._probe_browser_video(
                path,
                expected_frame_count=expected_frame_count,
                expected_fps=expected_fps,
                should_cancel=should_cancel,
            )
        except (ImportError, OSError, ValueError) as exc:
            raise RuntimeError(f"Follow-cam H.264 output is not decodable: {path}") from exc

        actual_frame_count = metadata["frame_count"]
        if actual_frame_count != expected_frame_count:
            raise RuntimeError(
                "Follow-cam output frame count does not match rendered frames: "
                f"expected {expected_frame_count}, got {actual_frame_count}"
            )

        expected_size = (self.config.target_width, self.config.target_height)
        if metadata.get("codec") != "h264":
            raise RuntimeError(f"Follow-cam output is not H.264: {metadata.get('codec')}")
        if not str(metadata.get("pix_fmt", "")).startswith("yuv420p"):
            raise RuntimeError(f"Follow-cam output is not yuv420p: {metadata.get('pix_fmt')}")
        if tuple(metadata.get("size", ())) != expected_size:
            raise RuntimeError(f"Follow-cam output size changed during encoding: {metadata.get('size')}")

        box_types = self._mp4_top_level_box_types(path)
        if b"moov" not in box_types or b"mdat" not in box_types:
            raise RuntimeError("Follow-cam output is missing required MP4 media boxes")
        if box_types.index(b"moov") > box_types.index(b"mdat"):
            raise RuntimeError("Follow-cam MP4 metadata is not faststart-compatible")
        with path.open("rb") as handle:
            prefix = handle.read(1024 * 1024)
        if b"avc1" not in prefix:
            raise RuntimeError("Follow-cam MP4 does not advertise an avc1 browser codec")

    def _probe_browser_video(
        self,
        path: Path,
        *,
        expected_frame_count: int,
        expected_fps: float | None,
        should_cancel: Callable[[], bool] | None,
    ) -> dict[str, Any]:
        import imageio_ffmpeg  # pyright: ignore[reportMissingImports]

        command = [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-hide_banner",
            "-nostdin",
            "-progress",
            "pipe:1",
            "-nostats",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-an",
            "-vf",
            "null",
            "-f",
            "null",
            "-",
        ]
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        stdout = ""
        stderr = ""
        probe_timeout_seconds = self._probe_timeout_seconds(expected_frame_count, expected_fps)
        deadline = time.monotonic() + probe_timeout_seconds
        try:
            while True:
                self._raise_if_cancelled(should_cancel)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError(
                        f"Follow-cam ffmpeg probe timed out after {probe_timeout_seconds:.1f} seconds"
                    )
                try:
                    stdout, stderr = process.communicate(
                        timeout=min(_FFMPEG_PROBE_POLL_SECONDS, remaining)
                    )
                    break
                except subprocess.TimeoutExpired:
                    continue
        except BaseException:
            self._stop_probe_process(process)
            raise
        finally:
            self._close_probe_streams(process)

        if process.returncode != 0:
            raise RuntimeError(f"ffmpeg probe exited with code {process.returncode}: {stderr[-2000:]}")
        if "progress=end" not in stdout:
            raise RuntimeError("ffmpeg probe did not report a completed decode")

        frame_count = 0
        for line in stdout.splitlines():
            if line.startswith("frame="):
                frame_count = int(line.split("=", 1)[1].strip())
        video_line = next((line for line in stderr.splitlines() if "Video:" in line), None)
        if video_line is None:
            raise RuntimeError("ffmpeg probe did not report a video stream")
        video_fields = [field.strip() for field in video_line.split("Video:", 1)[1].split(",")]
        if len(video_fields) < 3:
            raise RuntimeError("ffmpeg probe returned an incomplete video stream description")
        codec = video_fields[0].split()[0]
        pix_fmt = video_fields[1].split("(", 1)[0]
        size_match = next(
            (match for field in video_fields[2:] if (match := re.search(r"\b(\d{2,6})x(\d{2,6})\b", field))),
            None,
        )
        if size_match is None:
            raise RuntimeError("ffmpeg probe did not report video dimensions")
        return {
            "codec": codec,
            "pix_fmt": pix_fmt,
            "size": (int(size_match.group(1)), int(size_match.group(2))),
            "frame_count": frame_count,
        }

    @staticmethod
    def _probe_timeout_seconds(expected_frame_count: int, expected_fps: float | None) -> float:
        if expected_fps is None or not math.isfinite(expected_fps) or expected_fps <= 0:
            return _FFMPEG_PROBE_FALLBACK_TIMEOUT_SECONDS
        expected_duration = expected_frame_count / expected_fps
        return max(
            _FFMPEG_PROBE_MINIMUM_TIMEOUT_SECONDS,
            expected_duration * _FFMPEG_PROBE_DURATION_MULTIPLIER
            + _FFMPEG_PROBE_MINIMUM_TIMEOUT_SECONDS,
        )

    @staticmethod
    def _stop_probe_process(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=_FFMPEG_PROBE_STOP_TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            finally:
                process.wait()

    @staticmethod
    def _close_probe_streams(process: subprocess.Popen[str]) -> None:
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()

    @staticmethod
    def _mp4_top_level_box_types(path: Path) -> list[bytes]:
        file_size = path.stat().st_size
        offset = 0
        box_types: list[bytes] = []
        with path.open("rb") as handle:
            while offset + 8 <= file_size:
                handle.seek(offset)
                header = handle.read(8)
                if len(header) != 8:
                    break
                box_size = int.from_bytes(header[:4], "big")
                header_size = 8
                if box_size == 1:
                    extended_size = handle.read(8)
                    if len(extended_size) != 8:
                        raise RuntimeError("Follow-cam MP4 has a truncated extended box")
                    box_size = int.from_bytes(extended_size, "big")
                    header_size = 16
                elif box_size == 0:
                    box_size = file_size - offset
                if box_size < header_size or offset + box_size > file_size:
                    raise RuntimeError("Follow-cam MP4 has an invalid top-level box")
                box_types.append(header[4:8])
                offset += box_size
        if offset != file_size:
            raise RuntimeError("Follow-cam MP4 has trailing or truncated bytes")
        return box_types

    def _seek_to_frame(self, capture, frame_index: int) -> None:
        if frame_index <= 0:
            return
        seek_ok = capture.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
        actual_frame = int(capture.get(cv2.CAP_PROP_POS_FRAMES))
        if seek_ok and abs(actual_frame - frame_index) <= 1:
            return

        capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
        skipped = 0
        while skipped < frame_index:
            ok, _ = capture.read()
            if not ok:
                break
            skipped += 1
        if skipped != frame_index:
            raise RuntimeError(f"Unable to seek to follow-cam start frame: {frame_index}")

    def _render_follow_cam(
        self,
        capture,
        writer: _FrameWriter,
        frames: list[FollowCamFrame],
        source_width: int,
        source_height: int,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> list[CameraPathEntry]:
        cfg = self.config
        aspect = cfg.target_width / cfg.target_height
        home_center = (
            source_width * cfg.home_center_x_ratio,
            source_height * cfg.home_center_y_ratio,
        )
        current_center = home_center
        current_crop_height = float(max(1, min(cfg.max_crop_height, source_height)))
        current_crop_width = current_crop_height * aspect
        committed_crop_height = current_crop_height
        zoom_candidate_height = current_crop_height
        zoom_candidate_streak = 0
        zoom_hold_frames_remaining = 0
        last_zoom_commit_status = OutputStatus.LOST
        last_zoom_direction = 0
        last_point: tuple[float, float] | None = None
        last_point_frame_index: int | None = None
        smoothed_velocity = (0.0, 0.0)
        lost_streak = 0
        pan_mode = "glide"
        last_action_center: tuple[float, float] | None = None
        last_action_center_frame_index: int | None = None
        last_action_center_player_count = 0
        last_camera_target: tuple[float, float] | None = None
        path_entries: list[CameraPathEntry] = []
        total_frames = len(frames)
        last_progress_at = 0.0
        player_points_by_frame: dict[int, list[tuple[float, float]]] = {}
        if cfg.action_center_enabled:
            player_points_by_frame = self._player_points_by_frame(self._load_action_center_player_tracks())

        def emit_progress(current_frame: int, *, force: bool = False) -> None:
            nonlocal last_progress_at
            if progress_callback is None:
                return
            now = time.monotonic()
            if not force and now - last_progress_at < 1.0:
                return
            last_progress_at = now
            progress_callback(
                {
                    "stage": "render",
                    "current_frame": current_frame,
                    "total_frames": total_frames,
                }
            )

        def raise_if_cancelled() -> None:
            if should_cancel and should_cancel():
                raise CancelledError("Run cancelled by user.")

        emit_progress(0, force=True)
        for index, frame_info in enumerate(frames, start=1):
            raise_if_cancelled()
            ok, frame = capture.read()
            if not ok:
                break

            camera_frame_info = frame_info
            has_track_point = frame_info.x is not None and frame_info.y is not None
            if frame_info.status == OutputStatus.LOST:
                has_track_point = False
            elif has_track_point and self._is_unreliable_predicted_edge_point(
                frame_info=frame_info,
                source_width=source_width,
                source_height=source_height,
            ):
                has_track_point = False
                camera_frame_info = FollowCamFrame(
                    frame_index=frame_info.frame_index,
                    x=None,
                    y=None,
                    confidence=0.0,
                    status=OutputStatus.LOST,
                )

            if has_track_point:
                assert frame_info.x is not None and frame_info.y is not None
                lost_streak = 0
                current_point = (float(frame_info.x), float(frame_info.y))
                smoothed_velocity = self._update_velocity(
                    current_point=current_point,
                    current_frame_index=frame_info.frame_index,
                    last_point=last_point,
                    last_frame_index=last_point_frame_index,
                    previous_velocity=smoothed_velocity,
                )
                last_point = current_point
                last_point_frame_index = frame_info.frame_index
            else:
                lost_streak += 1
                smoothed_velocity = (
                    smoothed_velocity[0] * 0.90,
                    smoothed_velocity[1] * 0.90,
                )

            speed = math.hypot(smoothed_velocity[0], smoothed_velocity[1])
            desired_crop_height, zoom_out_ratio = self._desired_crop_height(
                frame_info=camera_frame_info,
                speed=speed,
                source_height=source_height,
            )
            (
                committed_crop_height,
                zoom_candidate_height,
                zoom_candidate_streak,
                zoom_hold_frames_remaining,
                last_zoom_commit_status,
                last_zoom_direction,
            ) = self._update_zoom_commit_state(
                committed_crop_height=committed_crop_height,
                desired_crop_height=desired_crop_height,
                zoom_candidate_height=zoom_candidate_height,
                zoom_candidate_streak=zoom_candidate_streak,
                zoom_hold_frames_remaining=zoom_hold_frames_remaining,
                frame_status=frame_info.status,
                last_zoom_commit_status=last_zoom_commit_status,
                last_zoom_direction=last_zoom_direction,
            )
            current_crop_height = self._update_crop_height(
                current_crop_height=current_crop_height,
                desired_crop_height=committed_crop_height,
            )
            current_crop_width, current_crop_height = self._crop_size_for_height(
                current_crop_height,
                aspect,
                source_width,
                source_height,
            )

            desired_center = home_center
            action_center = self._resolve_action_center(
                frame_info=frame_info,
                has_track_point=has_track_point,
                player_points=player_points_by_frame.get(frame_info.frame_index, []),
            )
            if has_track_point:
                if action_center.x is not None and action_center.y is not None:
                    camera_target = (action_center.x, action_center.y)
                else:
                    assert frame_info.x is not None and frame_info.y is not None
                    camera_target = (frame_info.x, frame_info.y)
                anchor_x, anchor_y = self._apply_look_ahead(
                    current_point=camera_target,
                    velocity=smoothed_velocity,
                )
                desired_center = (
                    anchor_x - (cfg.ball_screen_x_ratio - 0.5) * current_crop_width,
                    anchor_y - (cfg.ball_screen_y_ratio - 0.5) * current_crop_height,
                )
                if self._can_seed_lost_action_hold(
                    frame_info=frame_info,
                    action_center=camera_target,
                    source_width=source_width,
                    source_height=source_height,
                ):
                    last_action_center = (float(camera_target[0]), float(camera_target[1]))
                    last_action_center_frame_index = frame_info.frame_index
                    last_action_center_player_count = action_center.player_count
                    last_camera_target = desired_center
                elif self._should_clear_lost_action_hold_seed(
                    frame_info=frame_info,
                    action_center=(float(camera_target[0]), float(camera_target[1])),
                    source_width=source_width,
                ):
                    last_action_center = None
                    last_action_center_frame_index = None
                    last_action_center_player_count = 0
                    last_camera_target = None
                current_center, pan_mode = self._move_camera_towards(
                    current_center=current_center,
                    desired_center=desired_center,
                    crop_width=current_crop_width,
                    crop_height=current_crop_height,
                    status=frame_info.status,
                    current_pan_mode=pan_mode,
                )
            else:
                if (
                    last_camera_target is not None
                    and last_action_center is not None
                    and self._should_hold_lost_action(
                        frame_index=frame_info.frame_index,
                        last_action_center=last_action_center,
                        last_action_center_frame_index=last_action_center_frame_index,
                        source_width=source_width,
                        source_height=source_height,
                    )
                ):
                    current_center = self._move_towards_action_hold(current_center, last_camera_target)
                    pan_mode = "action_hold"
                    action_center = ActionCenterPoint(
                        last_action_center[0],
                        last_action_center[1],
                        "lost_action_hold",
                        last_action_center_player_count,
                    )
                elif lost_streak > cfg.lost_recenter_frames:
                    pan_mode = "hold"
                    current_center = self._move_towards_home(current_center, home_center, cfg.recenter_smoothing)
                else:
                    pan_mode = "hold"

            current_center = self._clamp_center(
                center=current_center,
                crop_width=current_crop_width,
                crop_height=current_crop_height,
                source_width=source_width,
                source_height=source_height,
            )
            crop_box = self._crop_box_for_center(
                center=current_center,
                crop_width=current_crop_width,
                crop_height=current_crop_height,
                source_width=source_width,
                source_height=source_height,
            )
            crop = frame[crop_box[1] : crop_box[3], crop_box[0] : crop_box[2]]
            resized = cv2.resize(crop, (cfg.target_width, cfg.target_height), interpolation=cv2.INTER_LINEAR)
            self._draw_overlay(
                image=resized,
                frame_info=camera_frame_info,
                crop_box=crop_box,
            )
            writer.write(resized)

            path_entries.append(
                CameraPathEntry(
                    frame_index=frame_info.frame_index,
                    center_x=current_center[0],
                    center_y=current_center[1],
                    crop_x1=crop_box[0],
                    crop_y1=crop_box[1],
                    crop_x2=crop_box[2],
                    crop_y2=crop_box[3],
                    crop_width=crop_box[2] - crop_box[0],
                    crop_height=crop_box[3] - crop_box[1],
                    source_status=frame_info.status.value,
                    track_x=frame_info.x,
                    track_y=frame_info.y,
                    confidence=frame_info.confidence,
                    speed=speed,
                    zoom_out_ratio=zoom_out_ratio,
                    pan_mode=pan_mode,
                    profile=cfg.profile,
                    action_center_enabled=cfg.action_center_enabled,
                    action_center_x=action_center.x,
                    action_center_y=action_center.y,
                    action_center_source=action_center.source,
                    action_center_player_count=action_center.player_count,
                )
            )
            emit_progress(index, force=index == total_frames)

        emit_progress(len(path_entries), force=True)
        return path_entries

    def _desired_crop_height(
        self,
        frame_info: FollowCamFrame,
        speed: float,
        source_height: int,
    ) -> tuple[float, float]:
        cfg = self.config
        speed_ratio = self._normalize(speed, cfg.speed_zoom_out_start, cfg.speed_zoom_out_end)
        confidence_ratio = 0.0
        if frame_info.status != OutputStatus.LOST:
            confidence_ratio = 1.0 - self._normalize(
                frame_info.confidence,
                cfg.low_confidence_zoom_out_end,
                cfg.low_confidence_zoom_out_start,
            )
        desired_ratio = max(0.0, speed_ratio, confidence_ratio)
        if frame_info.status == OutputStatus.PREDICTED:
            desired_ratio = max(desired_ratio, min(1.0, desired_ratio + cfg.predicted_zoom_out_bonus))
        elif frame_info.status == OutputStatus.LOST:
            desired_ratio = max(desired_ratio, cfg.lost_zoom_out_bonus)

        min_crop_height = max(1, min(cfg.min_crop_height, source_height))
        max_crop_height = max(min_crop_height, min(cfg.max_crop_height, source_height))
        desired_crop_height = min_crop_height + (max_crop_height - min_crop_height) * desired_ratio
        return desired_crop_height, desired_ratio

    def _update_crop_height(
        self,
        current_crop_height: float,
        desired_crop_height: float,
    ) -> float:
        cfg = self.config
        if desired_crop_height >= current_crop_height:
            alpha = cfg.zoom_out_smoothing
            max_delta = cfg.max_zoom_out_per_frame
        else:
            alpha = cfg.zoom_in_smoothing
            max_delta = cfg.max_zoom_in_per_frame

        smoothed_target = self._lerp(current_crop_height, desired_crop_height, alpha)
        delta = smoothed_target - current_crop_height
        delta = max(-max_delta, min(max_delta, delta))
        return current_crop_height + delta

    def _update_zoom_commit_state(
        self,
        committed_crop_height: float,
        desired_crop_height: float,
        zoom_candidate_height: float,
        zoom_candidate_streak: int,
        zoom_hold_frames_remaining: int,
        frame_status: OutputStatus,
        last_zoom_commit_status: OutputStatus,
        last_zoom_direction: int,
    ) -> tuple[float, float, int, int, OutputStatus, int]:
        cfg = self.config
        if zoom_hold_frames_remaining > 0:
            return (
                committed_crop_height,
                committed_crop_height,
                0,
                zoom_hold_frames_remaining - 1,
                last_zoom_commit_status,
                last_zoom_direction,
            )

        if abs(desired_crop_height - committed_crop_height) <= cfg.zoom_deadband_height:
            return committed_crop_height, committed_crop_height, 0, 0, last_zoom_commit_status, last_zoom_direction

        direction_changed = (desired_crop_height - committed_crop_height) * (zoom_candidate_height - committed_crop_height) < 0
        if direction_changed or abs(desired_crop_height - zoom_candidate_height) > cfg.zoom_deadband_height:
            zoom_candidate_height = desired_crop_height
            zoom_candidate_streak = 1
        else:
            zoom_candidate_height = self._lerp(zoom_candidate_height, desired_crop_height, 0.35)
            zoom_candidate_streak += 1

        confirm_frames = cfg.zoom_out_confirm_frames
        candidate_direction = 1
        if zoom_candidate_height < committed_crop_height:
            candidate_direction = -1
            confirm_frames = cfg.zoom_in_confirm_frames
        if (
            last_zoom_direction != 0
            and candidate_direction != last_zoom_direction
            and frame_status == last_zoom_commit_status
        ):
            confirm_frames = max(confirm_frames, cfg.zoom_reverse_confirm_frames)

        if zoom_candidate_streak >= confirm_frames:
            return (
                zoom_candidate_height,
                zoom_candidate_height,
                0,
                cfg.zoom_hold_frames_after_change,
                frame_status,
                candidate_direction,
            )
        return (
            committed_crop_height,
            zoom_candidate_height,
            zoom_candidate_streak,
            0,
            last_zoom_commit_status,
            last_zoom_direction,
        )

    def _update_velocity(
        self,
        current_point: tuple[float, float],
        current_frame_index: int,
        last_point: tuple[float, float] | None,
        last_frame_index: int | None,
        previous_velocity: tuple[float, float],
    ) -> tuple[float, float]:
        if last_point is None or last_frame_index is None:
            return previous_velocity
        delta_frames = max(1, current_frame_index - last_frame_index)
        measured_velocity = (
            (current_point[0] - last_point[0]) / delta_frames,
            (current_point[1] - last_point[1]) / delta_frames,
        )
        alpha = self.config.velocity_smoothing
        return (
            self._lerp(previous_velocity[0], measured_velocity[0], alpha),
            self._lerp(previous_velocity[1], measured_velocity[1], alpha),
        )

    def _apply_look_ahead(
        self,
        current_point: tuple[float, float],
        velocity: tuple[float, float],
    ) -> tuple[float, float]:
        gain = self.config.look_ahead_gain
        max_px = self.config.look_ahead_max_px
        offset_x = max(-max_px, min(max_px, velocity[0] * gain))
        offset_y = max(-max_px, min(max_px, velocity[1] * gain))
        return current_point[0] + offset_x, current_point[1] + offset_y

    def _move_camera_towards(
        self,
        current_center: tuple[float, float],
        desired_center: tuple[float, float],
        crop_width: float,
        crop_height: float,
        status: OutputStatus,
        current_pan_mode: str,
    ) -> tuple[tuple[float, float], str]:
        cfg = self.config
        dead_x = crop_width * cfg.dead_zone_ratio_x
        dead_y = crop_height * cfg.dead_zone_ratio_y
        pan_decay = 1.0 if status == OutputStatus.DETECTED else cfg.predicted_pan_decay
        offset_x = abs(desired_center[0] - current_center[0])
        offset_y = abs(desired_center[1] - current_center[1])
        trigger_x = crop_width * cfg.catch_up_trigger_ratio_x
        trigger_y = crop_height * cfg.catch_up_trigger_ratio_y
        release_x = crop_width * cfg.catch_up_release_ratio_x
        release_y = crop_height * cfg.catch_up_release_ratio_y

        catch_up_active = current_pan_mode == "catch_up"
        if catch_up_active:
            catch_up_active = offset_x >= release_x or offset_y >= release_y
        else:
            catch_up_active = offset_x >= trigger_x or offset_y >= trigger_y

        if catch_up_active:
            smoothing = cfg.catch_up_pan_smoothing
            max_step_x = cfg.catch_up_max_pan_per_frame_x
            max_step_y = cfg.catch_up_max_pan_per_frame_y
            next_pan_mode = "catch_up"
        else:
            smoothing = cfg.glide_pan_smoothing
            max_step_x = cfg.glide_max_pan_per_frame_x
            max_step_y = cfg.glide_max_pan_per_frame_y
            next_pan_mode = "glide"

        move_x = self._axis_move(
            current=current_center[0],
            desired=desired_center[0],
            dead_zone=dead_x,
            smoothing=smoothing * pan_decay,
            max_step=max_step_x * pan_decay,
        )
        move_y = self._axis_move(
            current=current_center[1],
            desired=desired_center[1],
            dead_zone=dead_y,
            smoothing=smoothing * pan_decay,
            max_step=max_step_y * pan_decay,
        )
        return (current_center[0] + move_x, current_center[1] + move_y), next_pan_mode

    def _axis_move(
        self,
        current: float,
        desired: float,
        dead_zone: float,
        smoothing: float,
        max_step: float,
    ) -> float:
        delta = desired - current
        if abs(delta) <= dead_zone:
            return 0.0
        delta_outside = math.copysign(abs(delta) - dead_zone, delta)
        step = delta_outside * smoothing
        return max(-max_step, min(max_step, step))

    def _move_towards_home(
        self,
        current_center: tuple[float, float],
        home_center: tuple[float, float],
        smoothing: float,
    ) -> tuple[float, float]:
        return (
            self._lerp(current_center[0], home_center[0], smoothing),
            self._lerp(current_center[1], home_center[1], smoothing),
        )

    def _move_towards_action_hold(
        self,
        current_center: tuple[float, float],
        desired_center: tuple[float, float],
    ) -> tuple[float, float]:
        cfg = self.config
        return (
            current_center[0]
            + self._axis_move(
                current=current_center[0],
                desired=desired_center[0],
                dead_zone=0.0,
                smoothing=cfg.lost_action_hold_smoothing,
                max_step=cfg.glide_max_pan_per_frame_x,
            ),
            current_center[1]
            + self._axis_move(
                current=current_center[1],
                desired=desired_center[1],
                dead_zone=0.0,
                smoothing=cfg.lost_action_hold_smoothing,
                max_step=cfg.glide_max_pan_per_frame_y,
            ),
        )

    def _should_hold_lost_action(
        self,
        frame_index: int,
        last_action_center: tuple[float, float],
        last_action_center_frame_index: int | None,
        source_width: int,
        source_height: int,
    ) -> bool:
        cfg = self.config
        if not cfg.lost_action_hold_enabled or last_action_center_frame_index is None:
            return False
        frame_delta = frame_index - last_action_center_frame_index
        if frame_delta < 0 or frame_delta > cfg.lost_action_hold_frames:
            return False
        return self._is_edge_action_center(last_action_center, source_width, source_height)

    def _can_seed_lost_action_hold(
        self,
        frame_info: FollowCamFrame,
        action_center: tuple[float, float],
        source_width: int,
        source_height: int,
    ) -> bool:
        if frame_info.status != OutputStatus.DETECTED:
            return False
        if frame_info.confidence < self.config.lost_action_hold_min_confidence:
            return False
        return self._is_edge_action_center(action_center, source_width, source_height)

    def _is_unreliable_predicted_edge_point(
        self,
        frame_info: FollowCamFrame,
        source_width: int,
        source_height: int,
    ) -> bool:
        if not self.config.lost_action_hold_enabled or frame_info.status != OutputStatus.PREDICTED:
            return False
        if frame_info.confidence >= self.config.lost_action_hold_min_confidence:
            return False
        if frame_info.x is None or frame_info.y is None:
            return False
        return self._is_edge_action_center((float(frame_info.x), float(frame_info.y)), source_width, source_height)

    def _should_clear_lost_action_hold_seed(
        self,
        frame_info: FollowCamFrame,
        action_center: tuple[float, float],
        source_width: int,
    ) -> bool:
        if frame_info.status != OutputStatus.DETECTED:
            return False
        if frame_info.confidence < self.config.lost_action_hold_min_confidence:
            return False
        return self._is_central_action_center(action_center, source_width)

    def _is_central_action_center(
        self,
        point: tuple[float, float],
        source_width: int,
    ) -> bool:
        if source_width <= 0:
            return False
        x, _ = point
        if not math.isfinite(x):
            return False
        return source_width * 0.35 <= x <= source_width * 0.65

    def _is_edge_action_center(
        self,
        point: tuple[float, float],
        source_width: int,
        source_height: int,
    ) -> bool:
        if source_width <= 0 or source_height <= 0:
            return False
        x, y = point
        if not (math.isfinite(x) and math.isfinite(y)):
            return False
        margin_x = source_width * self.config.lost_action_hold_edge_margin_ratio
        return x <= margin_x or x >= source_width - margin_x

    def _crop_size_for_height(
        self,
        desired_height: float,
        aspect: float,
        source_width: int,
        source_height: int,
    ) -> tuple[float, float]:
        crop_height = max(1.0, min(float(source_height), desired_height))
        crop_width = crop_height * aspect
        if crop_width > source_width:
            crop_width = float(source_width)
            crop_height = crop_width / aspect
        return crop_width, crop_height

    def _clamp_center(
        self,
        center: tuple[float, float],
        crop_width: float,
        crop_height: float,
        source_width: int,
        source_height: int,
    ) -> tuple[float, float]:
        half_width = crop_width / 2.0
        half_height = crop_height / 2.0
        return (
            max(half_width, min(source_width - half_width, center[0])),
            max(half_height, min(source_height - half_height, center[1])),
        )

    def _crop_box_for_center(
        self,
        center: tuple[float, float],
        crop_width: float,
        crop_height: float,
        source_width: int,
        source_height: int,
    ) -> tuple[int, int, int, int]:
        half_width = crop_width / 2.0
        half_height = crop_height / 2.0
        left = int(round(center[0] - half_width))
        top = int(round(center[1] - half_height))
        right = int(round(center[0] + half_width))
        bottom = int(round(center[1] + half_height))

        left = max(0, left)
        top = max(0, top)
        right = min(source_width, right)
        bottom = min(source_height, bottom)
        if right <= left:
            right = min(source_width, left + 1)
        if bottom <= top:
            bottom = min(source_height, top + 1)
        return (left, top, right, bottom)

    def _draw_overlay(
        self,
        image,
        frame_info: FollowCamFrame,
        crop_box: tuple[int, int, int, int],
    ) -> None:
        if self.config.draw_ball_marker and frame_info.x is not None and frame_info.y is not None:
            left, top, right, bottom = crop_box
            if left <= frame_info.x <= right and top <= frame_info.y <= bottom:
                scale_x = self.config.target_width / max(1.0, right - left)
                scale_y = self.config.target_height / max(1.0, bottom - top)
                marker_x = int(round((frame_info.x - left) * scale_x))
                marker_y = int(round((frame_info.y - top) * scale_y))
                cv2.circle(image, (marker_x, marker_y), 16, (0, 255, 255), 3, lineType=cv2.LINE_AA)

        if self.config.draw_frame_text:
            text = (
                f"Frame: {frame_info.frame_index} | "
                f"Status: {frame_info.status.value} | "
                f"Conf: {frame_info.confidence:.3f}"
            )
            self._draw_text(image, text, (24, 40))

    def _draw_text(self, image, text: str, origin: tuple[int, int]) -> None:
        cv2.putText(
            image,
            text,
            (origin[0] + 2, origin[1] + 2),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.9,
            (0, 0, 0),
            3,
            cv2.LINE_AA,
        )
        cv2.putText(
            image,
            text,
            origin,
            cv2.FONT_HERSHEY_SIMPLEX,
            0.9,
            (255, 255, 255),
            2,
            cv2.LINE_AA,
        )

    def _write_camera_path(self, path: Path, path_entries: list[CameraPathEntry]) -> None:
        with path.open("w", newline="", encoding="utf-8-sig") as csv_file:
            writer = csv.writer(csv_file)
            writer.writerow(
                [
                    "Frame",
                    "Profile",
                    "CenterX",
                    "CenterY",
                    "CropX1",
                    "CropY1",
                    "CropX2",
                    "CropY2",
                    "CropWidth",
                    "CropHeight",
                    "Status",
                    "TrackX",
                    "TrackY",
                    "ActionCenterEnabled",
                    "ActionCenterX",
                    "ActionCenterY",
                    "ActionCenterSource",
                    "ActionCenterPlayerCount",
                    "Confidence",
                    "Speed",
                    "ZoomOutRatio",
                    "PanMode",
                ]
            )
            for entry in path_entries:
                writer.writerow(
                    [
                        entry.frame_index,
                        entry.profile,
                        f"{entry.center_x:.2f}",
                        f"{entry.center_y:.2f}",
                        entry.crop_x1,
                        entry.crop_y1,
                        entry.crop_x2,
                        entry.crop_y2,
                        entry.crop_width,
                        entry.crop_height,
                        entry.source_status,
                        "" if entry.track_x is None else f"{entry.track_x:.2f}",
                        "" if entry.track_y is None else f"{entry.track_y:.2f}",
                        "1" if entry.action_center_enabled else "0",
                        "" if entry.action_center_x is None else f"{entry.action_center_x:.2f}",
                        "" if entry.action_center_y is None else f"{entry.action_center_y:.2f}",
                        entry.action_center_source,
                        entry.action_center_player_count,
                        f"{entry.confidence:.4f}",
                        f"{entry.speed:.2f}",
                        f"{entry.zoom_out_ratio:.4f}",
                        entry.pan_mode,
                    ]
                )

    def _write_report(
        self,
        path: Path,
        track_csv_path: Path,
        track_source: str,
        path_entries: list[CameraPathEntry],
        camera_motion_audit: dict[str, Any] | None = None,
    ) -> None:
        if not path_entries:
            payload = {
                "track_source": track_source,
                "track_csv": track_csv_path.name,
                "frame_count": 0,
                "profile": self.config.profile,
                "action_center": self._action_center_report_payload(path_entries),
            }
        else:
            crop_heights = [entry.crop_height for entry in path_entries]
            payload = {
                "track_source": track_source,
                "track_csv": track_csv_path.name,
                "frame_count": len(path_entries),
                "profile": self.config.profile,
                "target_resolution": [self.config.target_width, self.config.target_height],
                "min_crop_height": min(crop_heights),
                "max_crop_height": max(crop_heights),
                "mean_crop_height": round(sum(crop_heights) / len(crop_heights), 2),
                "action_center": self._action_center_report_payload(path_entries),
                "status_counts": {
                    status.value: sum(1 for entry in path_entries if entry.source_status == status.value)
                    for status in OutputStatus
                },
            }
        if camera_motion_audit is not None:
            payload["camera_motion_audit"] = {
                "report": "camera_motion_audit.json",
                "summary": camera_motion_audit.get("summary", {}),
            }
        with path.open("w", encoding="utf-8") as report_file:
            json.dump(payload, report_file, ensure_ascii=False, indent=2)

    def _action_center_report_payload(self, path_entries: list[CameraPathEntry]) -> dict[str, Any]:
        sources = sorted({entry.action_center_source for entry in path_entries})
        return {
            "enabled": self.config.action_center_enabled,
            "player_tracks_path": (
                None
                if self.config.action_center_player_tracks_path is None
                else str(self.config.action_center_player_tracks_path)
            ),
            "frames_with_player_context": sum(
                1 for entry in path_entries if entry.action_center_player_count > 0
            ),
            "sources": sources,
        }

    def _normalize(self, value: float, start: float, end: float) -> float:
        if end <= start:
            return 0.0
        return max(0.0, min(1.0, (value - start) / (end - start)))

    def _lerp(self, current: float, target: float, alpha: float) -> float:
        alpha = max(0.0, min(1.0, alpha))
        return current + (target - current) * alpha
