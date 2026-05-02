from __future__ import annotations

import csv
import json
from pathlib import Path

import cv2

from football_tracking.config import LoggingConfig, OutputConfig
from football_tracking.types import TrackResult


class TrackingExporter:
    """输出层：负责视频、逐帧图片、CSV 和调试信息落盘。"""

    def __init__(
        self,
        output_dir: Path,
        config: OutputConfig,
        logging_config: LoggingConfig,
        frame_size: tuple[int, int],
        fps: float,
    ) -> None:
        self.output_dir = output_dir
        self.config = config
        self.logging_config = logging_config
        self.frame_size = frame_size
        self.fps = fps
        self.frames_dir = self.output_dir / self.config.frame_dir
        self.csv_path = self.output_dir / self.config.csv_name
        self.debug_path = self.output_dir / self.config.debug_jsonl_name
        self.video_path = self.output_dir / self.config.video_name
        self.video_writer: cv2.VideoWriter | None = None
        self.csv_file = None
        self.csv_writer = None
        self.debug_file = None

        self._prepare_output_dirs()
        self._init_writers()

    def _prepare_output_dirs(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        if self.config.save_frames:
            self.frames_dir.mkdir(parents=True, exist_ok=True)

    def _init_writers(self) -> None:
        if self.config.save_video:
            fourcc = cv2.VideoWriter_fourcc(*self.config.video_codec)
            self.video_writer = cv2.VideoWriter(
                str(self.video_path),
                fourcc,
                self.fps,
                self.frame_size,
            )
            if not self.video_writer.isOpened():
                raise RuntimeError(f"视频写入器初始化失败: {self.video_path}")

        if self.config.save_csv:
            self.csv_file = self.csv_path.open("w", newline="", encoding="utf-8-sig")
            self.csv_writer = csv.writer(self.csv_file)
            self.csv_writer.writerow(["Frame", "X", "Y", "Confidence", "Status"])

        if self.logging_config.save_debug_jsonl and self.config.save_debug_jsonl:
            self.debug_file = self.debug_path.open("w", encoding="utf-8")

    def write(self, annotated_frame, track_result: TrackResult) -> None:
        """写出当前帧所有结果。"""
        if self.video_writer is not None:
            self.video_writer.write(annotated_frame)

        if self.config.save_frames:
            frame_name = f"frame_{track_result.frame_index:06d}{self.config.frame_image_ext}"
            frame_path = self.frames_dir / frame_name
            self._safe_write_image(frame_path, annotated_frame)

        self._write_csv_row(track_result)
        self._write_debug_row(track_result)

    def _safe_write_image(self, image_path: Path, image) -> None:
        """用 imencode + tofile 兼容 Windows 路径。"""
        success, buffer = cv2.imencode(image_path.suffix, image)
        if not success:
            raise RuntimeError(f"图片编码失败: {image_path}")
        buffer.tofile(str(image_path))

    def _write_csv_row(self, track_result: TrackResult) -> None:
        if self.csv_writer is None:
            return
        if track_result.point is None:
            row = [track_result.frame_index, "", "", f"{track_result.confidence:.4f}", track_result.output_status.value]
        else:
            row = [
                track_result.frame_index,
                f"{track_result.point.x:.2f}",
                f"{track_result.point.y:.2f}",
                f"{track_result.confidence:.4f}",
                track_result.output_status.value,
            ]
        self.csv_writer.writerow(row)

    def _write_debug_row(self, track_result: TrackResult) -> None:
        if self.debug_file is None:
            return
        self.debug_file.write(json.dumps(track_result.to_debug_dict(), ensure_ascii=False) + "\n")

    def close(self) -> None:
        """释放所有文件句柄。"""
        if self.video_writer is not None:
            self.video_writer.release()
        if self.csv_file is not None:
            self.csv_file.close()
        if self.debug_file is not None:
            self.debug_file.close()
