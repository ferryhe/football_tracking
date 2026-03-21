from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path

import cv2

from football_tracking.config import AppConfig, PostprocessConfig, SceneZoneConfig
from football_tracking.renderer import FrameRenderer
from football_tracking.types import OutputStatus, TrackPoint, TrackResult, TrackState


@dataclass(slots=True)
class CleanedFrame:
    frame_index: int
    x: float | None
    y: float | None
    confidence: float
    status: OutputStatus
    reason: str
    raw_debug: dict


@dataclass(slots=True)
class CleanupAction:
    start_frame: int
    end_frame: int
    action: str
    reason: str
    island_length: int
    mean_confidence: float
    prev_jump_distance: float | None
    next_jump_distance: float | None
    nuisance_zone: str | None

    def to_dict(self) -> dict:
        return {
            "start_frame": self.start_frame,
            "end_frame": self.end_frame,
            "action": self.action,
            "reason": self.reason,
            "island_length": self.island_length,
            "mean_confidence": round(self.mean_confidence, 4),
            "prev_jump_distance": None if self.prev_jump_distance is None else round(self.prev_jump_distance, 2),
            "next_jump_distance": None if self.next_jump_distance is None else round(self.next_jump_distance, 2),
            "nuisance_zone": self.nuisance_zone,
        }


class TrackPostprocessor:
    def __init__(self, app_config: AppConfig) -> None:
        self.app_config = app_config
        self.config = app_config.postprocess
        self.renderer = FrameRenderer(app_config.output)

    def run(self) -> None:
        if not self.config.enabled:
            return

        output_dir = self.app_config.output_dir
        raw_csv_path = output_dir / self.app_config.output.csv_name
        raw_debug_path = output_dir / self.app_config.output.debug_jsonl_name
        if not raw_csv_path.exists() or not raw_debug_path.exists():
            raise FileNotFoundError("Postprocess requires raw CSV and debug JSONL outputs.")

        frames = self._load_frames(raw_csv_path, raw_debug_path)
        actions = self._apply_cleanup(frames)
        self._write_cleaned_csv(output_dir / self.config.cleaned_csv_name, frames)
        self._write_cleaned_debug(output_dir / self.config.cleaned_debug_jsonl_name, frames)
        self._write_report(output_dir / self.config.cleanup_report_name, frames, actions)
        if self.config.save_cleaned_video:
            self._write_cleaned_video(output_dir / self.config.cleaned_video_name, frames)

    def _load_frames(self, raw_csv_path: Path, raw_debug_path: Path) -> list[CleanedFrame]:
        csv_rows: dict[int, dict[str, str]] = {}
        with raw_csv_path.open("r", newline="", encoding="utf-8-sig") as csv_file:
            reader = csv.DictReader(csv_file)
            for row in reader:
                csv_rows[int(row["Frame"])] = row

        frames: list[CleanedFrame] = []
        with raw_debug_path.open("r", encoding="utf-8") as debug_file:
            for line in debug_file:
                if not line.strip():
                    continue
                item = json.loads(line)
                frame_index = int(item["frame"])
                csv_row = csv_rows[frame_index]
                x_raw = csv_row.get("X", "")
                y_raw = csv_row.get("Y", "")
                confidence = float(csv_row.get("Confidence") or 0.0)
                status = OutputStatus(csv_row["Status"])
                frames.append(
                    CleanedFrame(
                        frame_index=frame_index,
                        x=None if x_raw == "" else float(x_raw),
                        y=None if y_raw == "" else float(y_raw),
                        confidence=confidence,
                        status=status,
                        reason=str(item.get("reason", "")),
                        raw_debug=item,
                    )
                )
        return frames

    def _apply_cleanup(self, frames: list[CleanedFrame]) -> list[CleanupAction]:
        actions: list[CleanupAction] = []
        segments = self._find_detected_segments(frames)
        stable_min = max(1, self.config.stable_segment_min_length)
        max_island = max(1, self.config.max_detected_island_length)

        for segment_index, (start_idx, end_idx) in enumerate(segments):
            island_length = end_idx - start_idx + 1
            if island_length > max_island:
                continue
            if self._segment_intersects_protected_range(frames[start_idx].frame_index, frames[end_idx].frame_index):
                continue

            prev_segment = self._find_neighbor_segment(segments, segment_index, direction=-1, min_length=stable_min)
            next_segment = self._find_neighbor_segment(segments, segment_index, direction=1, min_length=stable_min)
            if prev_segment is None or next_segment is None:
                continue

            prev_end_idx = prev_segment[1]
            next_start_idx = next_segment[0]
            left_gap = start_idx - prev_end_idx - 1
            right_gap = next_start_idx - end_idx - 1
            if left_gap < 3 or right_gap < 3:
                continue

            island_frames = frames[start_idx : end_idx + 1]
            mean_confidence = sum(frame.confidence for frame in island_frames) / island_length
            prev_anchor = frames[prev_end_idx]
            next_anchor = frames[next_start_idx]
            prev_jump = self._distance_frames(prev_anchor, island_frames[0])
            next_jump = self._distance_frames(island_frames[-1], next_anchor)
            nuisance_zone = self._match_nuisance_zone(island_frames)
            jump_threshold = self.config.min_jump_distance
            if nuisance_zone is not None:
                jump_threshold = self.config.nuisance_zone_jump_distance

            if mean_confidence > self.config.low_confidence_threshold:
                continue
            if prev_jump < jump_threshold or next_jump < jump_threshold:
                continue

            action = CleanupAction(
                start_frame=island_frames[0].frame_index,
                end_frame=island_frames[-1].frame_index,
                action="replace_with_predicted" if self._can_interpolate(prev_anchor, next_anchor) else "replace_with_lost",
                reason="postprocess_short_detected_island",
                island_length=island_length,
                mean_confidence=mean_confidence,
                prev_jump_distance=prev_jump,
                next_jump_distance=next_jump,
                nuisance_zone=nuisance_zone,
            )
            self._scrub_segment(frames, start_idx, end_idx, prev_anchor, next_anchor, action)
            actions.append(action)
        return actions

    def _find_detected_segments(self, frames: list[CleanedFrame]) -> list[tuple[int, int]]:
        segments: list[tuple[int, int]] = []
        start_idx: int | None = None
        for idx, frame in enumerate(frames):
            if frame.status == OutputStatus.DETECTED:
                if start_idx is None:
                    start_idx = idx
                continue
            if start_idx is not None:
                segments.append((start_idx, idx - 1))
                start_idx = None
        if start_idx is not None:
            segments.append((start_idx, len(frames) - 1))
        return segments

    def _find_neighbor_segment(
        self,
        segments: list[tuple[int, int]],
        current_index: int,
        direction: int,
        min_length: int,
    ) -> tuple[int, int] | None:
        cursor = current_index + direction
        while 0 <= cursor < len(segments):
            segment = segments[cursor]
            if segment[1] - segment[0] + 1 >= min_length:
                return segment
            cursor += direction
        return None

    def _segment_intersects_protected_range(self, start_frame: int, end_frame: int) -> bool:
        for protected_start, protected_end in self.config.protected_ranges:
            if protected_start <= end_frame and start_frame <= protected_end:
                return True
        return False

    def _match_nuisance_zone(self, island_frames: list[CleanedFrame]) -> str | None:
        for zone in self.config.nuisance_zones:
            for frame in island_frames:
                if frame.x is None or frame.y is None:
                    continue
                if zone.frame_range is not None:
                    zone_start, zone_end = zone.frame_range
                    if not (zone_start <= frame.frame_index <= zone_end):
                        continue
                if self._point_in_zone(frame.x, frame.y, zone):
                    return zone.name
        return None

    def _scrub_segment(
        self,
        frames: list[CleanedFrame],
        start_idx: int,
        end_idx: int,
        prev_anchor: CleanedFrame,
        next_anchor: CleanedFrame,
        action: CleanupAction,
    ) -> None:
        can_interpolate = self._can_interpolate(prev_anchor, next_anchor)
        total_gap = max(1, next_anchor.frame_index - prev_anchor.frame_index)
        for idx in range(start_idx, end_idx + 1):
            frame = frames[idx]
            frame.raw_debug.setdefault("postprocess", {})
            frame.raw_debug["postprocess"].update(
                {
                    "applied": True,
                    "action": action.action,
                    "reason": action.reason,
                    "nuisance_zone": action.nuisance_zone,
                }
            )
            frame.raw_debug["postprocess_original"] = {
                "status": frame.status.value,
                "point": None if frame.x is None or frame.y is None else {"x": round(frame.x, 2), "y": round(frame.y, 2)},
                "confidence": round(frame.confidence, 4),
                "reason": frame.reason,
            }
            if can_interpolate:
                alpha = (frame.frame_index - prev_anchor.frame_index) / total_gap
                frame.x = prev_anchor.x + (next_anchor.x - prev_anchor.x) * alpha
                frame.y = prev_anchor.y + (next_anchor.y - prev_anchor.y) * alpha
                frame.confidence = min(prev_anchor.confidence, next_anchor.confidence) * 0.5
                frame.status = OutputStatus.PREDICTED
                frame.reason = action.reason
            else:
                frame.x = None
                frame.y = None
                frame.confidence = 0.0
                frame.status = OutputStatus.LOST
                frame.reason = action.reason

    def _can_interpolate(self, prev_anchor: CleanedFrame, next_anchor: CleanedFrame) -> bool:
        return (
            prev_anchor.x is not None
            and prev_anchor.y is not None
            and next_anchor.x is not None
            and next_anchor.y is not None
            and next_anchor.frame_index > prev_anchor.frame_index
        )

    def _write_cleaned_csv(self, path: Path, frames: list[CleanedFrame]) -> None:
        if not self.config.save_cleaned_csv:
            return
        with path.open("w", newline="", encoding="utf-8-sig") as csv_file:
            writer = csv.writer(csv_file)
            writer.writerow(["Frame", "X", "Y", "Confidence", "Status"])
            for frame in frames:
                if frame.x is None or frame.y is None:
                    writer.writerow([frame.frame_index, "", "", f"{frame.confidence:.4f}", frame.status.value])
                else:
                    writer.writerow(
                        [
                            frame.frame_index,
                            f"{frame.x:.2f}",
                            f"{frame.y:.2f}",
                            f"{frame.confidence:.4f}",
                            frame.status.value,
                        ]
                    )

    def _write_cleaned_debug(self, path: Path, frames: list[CleanedFrame]) -> None:
        if not self.config.save_cleaned_debug_jsonl:
            return
        with path.open("w", encoding="utf-8") as debug_file:
            for frame in frames:
                item = dict(frame.raw_debug)
                item["status"] = frame.status.value
                item["confidence"] = round(frame.confidence, 4)
                item["reason"] = frame.reason
                item["point"] = (
                    None
                    if frame.x is None or frame.y is None
                    else {
                        "x": round(frame.x, 2),
                        "y": round(frame.y, 2),
                        "confidence": round(frame.confidence, 4),
                    }
                )
                debug_file.write(json.dumps(item, ensure_ascii=False) + "\n")

    def _write_report(self, path: Path, frames: list[CleanedFrame], actions: list[CleanupAction]) -> None:
        report = {
            "input_csv": self.app_config.output.csv_name,
            "input_debug_jsonl": self.app_config.output.debug_jsonl_name,
            "cleaned_csv": self.config.cleaned_csv_name,
            "cleaned_debug_jsonl": self.config.cleaned_debug_jsonl_name,
            "cleaned_video": self.config.cleaned_video_name if self.config.save_cleaned_video else None,
            "scrubbed_segment_count": len(actions),
            "scrubbed_frame_count": sum(action.island_length for action in actions),
            "actions": [action.to_dict() for action in actions],
        }
        with path.open("w", encoding="utf-8") as report_file:
            json.dump(report, report_file, ensure_ascii=False, indent=2)

    def _write_cleaned_video(self, path: Path, frames: list[CleanedFrame]) -> None:
        input_video = self.app_config.input_video
        capture_backend = getattr(cv2, self.app_config.runtime.capture_backend, cv2.CAP_ANY)
        capture = cv2.VideoCapture(str(input_video), capture_backend)
        if not capture.isOpened():
            raise RuntimeError(f"Unable to reopen input video for postprocess rendering: {input_video}")

        start_frame = self.app_config.runtime.start_frame
        if start_frame > 0:
            capture.set(cv2.CAP_PROP_POS_FRAMES, start_frame)

        fps = capture.get(cv2.CAP_PROP_FPS) or 20.0
        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fourcc = cv2.VideoWriter_fourcc(*self.app_config.output.video_codec)
        writer = cv2.VideoWriter(str(path), fourcc, fps, (width, height))
        if not writer.isOpened():
            capture.release()
            raise RuntimeError(f"Unable to open cleaned video writer: {path}")

        try:
            for frame in frames:
                ok, image = capture.read()
                if not ok:
                    break
                writer.write(self.renderer.render(image, self._to_track_result(frame)))
        finally:
            writer.release()
            capture.release()

    def _to_track_result(self, frame: CleanedFrame) -> TrackResult:
        state = TrackState.LOST
        if frame.status == OutputStatus.DETECTED:
            state = TrackState.TRACKING
        elif frame.status == OutputStatus.PREDICTED:
            state = TrackState.PREDICTING

        point = None
        if frame.x is not None and frame.y is not None:
            point = TrackPoint(
                frame_index=frame.frame_index,
                x=frame.x,
                y=frame.y,
                confidence=frame.confidence,
                status=frame.status,
            )
        return TrackResult(
            frame_index=frame.frame_index,
            output_status=frame.status,
            state=state,
            point=point,
            confidence=frame.confidence,
            reason=frame.reason,
            lost_frames=0,
            raw_candidate_count=int(frame.raw_debug.get("raw_candidate_count", 0)),
            filtered_candidate_count=int(frame.raw_debug.get("filtered_candidate_count", 0)),
        )

    def _distance_frames(self, frame_a: CleanedFrame, frame_b: CleanedFrame) -> float:
        assert frame_a.x is not None and frame_a.y is not None and frame_b.x is not None and frame_b.y is not None
        return ((frame_a.x - frame_b.x) ** 2 + (frame_a.y - frame_b.y) ** 2) ** 0.5

    def _point_in_zone(self, x: float, y: float, zone: SceneZoneConfig) -> bool:
        if zone.points:
            return self._point_in_polygon(x, y, list(zone.points))
        if zone.roi is None:
            return False
        left, top, right, bottom = zone.roi
        return left <= x <= right and top <= y <= bottom

    def _point_in_polygon(self, x: float, y: float, polygon: list[tuple[int, int]]) -> bool:
        inside = False
        polygon_count = len(polygon)
        if polygon_count < 3:
            return False
        j = polygon_count - 1
        for i in range(polygon_count):
            xi, yi = polygon[i]
            xj, yj = polygon[j]
            intersects = ((yi > y) != (yj > y)) and (
                x < (xj - xi) * (y - yi) / max(1e-6, (yj - yi)) + xi
            )
            if intersects:
                inside = not inside
            j = i
        return inside
