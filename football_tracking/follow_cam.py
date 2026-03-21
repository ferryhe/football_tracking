from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass
from pathlib import Path

import cv2

from football_tracking.config import AppConfig, FollowCamConfig
from football_tracking.types import OutputStatus


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


class FollowCamGenerator:
    def __init__(self, app_config: AppConfig) -> None:
        self.app_config = app_config
        self.config = app_config.follow_cam

    def run(self) -> None:
        if not self.config.enabled:
            return

        output_dir = self.app_config.output_dir
        track_csv_path, track_source = self._resolve_track_csv(output_dir)
        frames = self._load_frames(track_csv_path)
        if not frames:
            raise RuntimeError(f"Follow-cam track source is empty: {track_csv_path}")

        capture_backend = getattr(cv2, self.app_config.runtime.capture_backend, cv2.CAP_ANY)
        capture = cv2.VideoCapture(str(self.app_config.input_video), capture_backend)
        if not capture.isOpened():
            raise RuntimeError(f"Unable to reopen input video for follow-cam: {self.app_config.input_video}")

        start_frame = frames[0].frame_index
        self._seek_to_frame(capture, start_frame)

        fps = capture.get(cv2.CAP_PROP_FPS) or 20.0
        source_width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        source_height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        writer = self._open_writer(output_dir, fps)

        try:
            path_entries = self._render_follow_cam(
                capture=capture,
                writer=writer,
                frames=frames,
                source_width=source_width,
                source_height=source_height,
            )
        finally:
            writer.release()
            capture.release()

        self._write_camera_path(output_dir / self.config.camera_path_name, path_entries)
        self._write_report(output_dir / self.config.report_name, track_csv_path, track_source, path_entries)

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

    def _open_writer(self, output_dir: Path, fps: float) -> cv2.VideoWriter:
        output_path = output_dir / self.config.output_video_name
        fourcc = cv2.VideoWriter_fourcc(*self.app_config.output.video_codec)
        writer = cv2.VideoWriter(
            str(output_path),
            fourcc,
            fps,
            (self.config.target_width, self.config.target_height),
        )
        if not writer.isOpened():
            raise RuntimeError(f"Unable to open follow-cam writer: {output_path}")
        return writer

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
        writer: cv2.VideoWriter,
        frames: list[FollowCamFrame],
        source_width: int,
        source_height: int,
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
        path_entries: list[CameraPathEntry] = []

        for frame_info in frames:
            ok, frame = capture.read()
            if not ok:
                break

            has_track_point = frame_info.x is not None and frame_info.y is not None
            if frame_info.status == OutputStatus.LOST:
                has_track_point = False

            if has_track_point:
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
                frame_info=frame_info,
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
            if has_track_point:
                anchor_x, anchor_y = self._apply_look_ahead(
                    current_point=(float(frame_info.x), float(frame_info.y)),
                    velocity=smoothed_velocity,
                )
                desired_center = (
                    anchor_x - (cfg.ball_screen_x_ratio - 0.5) * current_crop_width,
                    anchor_y - (cfg.ball_screen_y_ratio - 0.5) * current_crop_height,
                )
                current_center, pan_mode = self._move_camera_towards(
                    current_center=current_center,
                    desired_center=desired_center,
                    crop_width=current_crop_width,
                    crop_height=current_crop_height,
                    status=frame_info.status,
                    current_pan_mode=pan_mode,
                )
            else:
                pan_mode = "hold"
                if lost_streak > cfg.lost_recenter_frames:
                    current_center = self._move_towards_home(current_center, home_center, cfg.recenter_smoothing)

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
                frame_info=frame_info,
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
                )
            )

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
    ) -> None:
        if not path_entries:
            payload = {
                "track_source": track_source,
                "track_csv": track_csv_path.name,
                "frame_count": 0,
            }
        else:
            crop_heights = [entry.crop_height for entry in path_entries]
            payload = {
                "track_source": track_source,
                "track_csv": track_csv_path.name,
                "frame_count": len(path_entries),
                "target_resolution": [self.config.target_width, self.config.target_height],
                "min_crop_height": min(crop_heights),
                "max_crop_height": max(crop_heights),
                "mean_crop_height": round(sum(crop_heights) / len(crop_heights), 2),
                "status_counts": {
                    status.value: sum(1 for entry in path_entries if entry.source_status == status.value)
                    for status in OutputStatus
                },
            }
        with path.open("w", encoding="utf-8") as report_file:
            json.dump(payload, report_file, ensure_ascii=False, indent=2)

    def _normalize(self, value: float, start: float, end: float) -> float:
        if end <= start:
            return 0.0
        return max(0.0, min(1.0, (value - start) / (end - start)))

    def _lerp(self, current: float, target: float, alpha: float) -> float:
        alpha = max(0.0, min(1.0, alpha))
        return current + (target - current) * alpha
