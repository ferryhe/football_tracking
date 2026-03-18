from __future__ import annotations

import logging

import cv2
import torch

from football_tracking.config import AppConfig
from football_tracking.detector import MockBallDetector, YOLOSahiBallDetector
from football_tracking.exporter import TrackingExporter
from football_tracking.filtering import CandidateFilter
from football_tracking.mock_mode import MockFrameSource
from football_tracking.renderer import FrameRenderer
from football_tracking.selector import UniqueBallSelector
from football_tracking.tracker import BallTracker
from football_tracking.types import SelectionDecision


class BallTrackingPipeline:
    """主流程编排层：把五层架构串联起来。"""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self._configure_runtime()
        self.logger = self._build_logger()
        self.detector = self._build_detector()
        self.candidate_filter = CandidateFilter(config.filtering)
        self.selector = UniqueBallSelector(config.selection, config.tracking)
        self.tracker = BallTracker(config.tracking)
        self.renderer = FrameRenderer(config.output)

    def _configure_runtime(self) -> None:
        """集中处理 OpenCV 与 CUDA 相关优化。"""
        cv2.setUseOptimized(True)
        cv2.setNumThreads(self.config.runtime.opencv_threads)

        cuda_available = torch.cuda.is_available()
        if not self.config.runtime.use_gpu_if_available or not cuda_available:
            # 当用户禁用 GPU 或 CUDA 不可用时，主动回退到 CPU，避免检测器仍然强制初始化到 cuda:0。
            self.config.detector.device = "cpu"
            self.config.detector.use_half = False

        if self.config.runtime.enable_cudnn_benchmark and cuda_available:
            torch.backends.cudnn.benchmark = True

        if self.config.runtime.use_gpu_if_available and cuda_available:
            torch.set_float32_matmul_precision("high")

    def _build_detector(self):
        """根据配置选择真实 detector 或 mock detector。"""
        if self.config.mock.enabled:
            return MockBallDetector(self.config.mock)
        return YOLOSahiBallDetector(self.config.detector, self.config.sahi)

    def _build_logger(self) -> logging.Logger:
        logging.basicConfig(
            level=getattr(logging, self.config.logging.level.upper(), logging.INFO),
            format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        )
        return logging.getLogger("football_tracking")

    def run(self) -> None:
        """执行完整视频处理流程。"""
        capture, width, height, fps = self._open_frame_source()

        exporter = TrackingExporter(
            output_dir=self.config.output_dir,
            config=self.config.output,
            logging_config=self.config.logging,
            frame_size=(width, height),
            fps=fps,
        )

        if self.config.mock.enabled:
            self.logger.info("开始处理 mock 场景: %s", self.config.mock.scenario)
        else:
            self.logger.info("开始处理视频: %s", self.config.input_video)
        self.logger.info("输入参数: width=%s, height=%s, fps=%.2f", width, height, fps)

        frame_index = -1
        try:
            while True:
                ok, frame = capture.read()
                if not ok:
                    break

                frame_index += 1
                track_result = self._process_frame(frame, frame_index)
                try:
                    annotated_frame = self.renderer.render(frame, track_result)
                except Exception as exc:
                    # 渲染异常时退回原始帧，保证单帧失败不会中断整段视频处理。
                    self.logger.exception("第 %s 帧渲染异常，将回退为原始帧输出: %s", frame_index, exc)
                    annotated_frame = frame.copy()

                try:
                    exporter.write(annotated_frame, track_result)
                except Exception as exc:
                    # 输出异常单独捕获，避免一次写盘失败直接终止后续帧。
                    self.logger.exception("第 %s 帧输出异常，将继续处理后续帧: %s", frame_index, exc)
                    continue

                if frame_index % 50 == 0:
                    self.logger.info(
                        "已处理到第 %s 帧 | status=%s | state=%s | lost_frames=%s",
                        frame_index,
                        track_result.output_status.value,
                        track_result.state.value,
                        track_result.lost_frames,
                    )
        finally:
            capture.release()
            exporter.close()
            self.logger.info("处理完成，输出目录: %s", self.config.output_dir)

    def _open_frame_source(self):
        """统一打开真实视频源或 mock 假帧源。"""
        if self.config.mock.enabled:
            mock_source = MockFrameSource(self.config.mock)
            return mock_source, mock_source.width, mock_source.height, mock_source.fps

        if not self.config.input_video.exists():
            raise FileNotFoundError(f"输入视频不存在: {self.config.input_video}")
        if not self.config.detector.model_path.exists():
            raise FileNotFoundError(f"检测模型不存在: {self.config.detector.model_path}")

        capture_backend = getattr(cv2, self.config.runtime.capture_backend, cv2.CAP_ANY)
        capture = cv2.VideoCapture(str(self.config.input_video), capture_backend)
        if not capture.isOpened():
            raise RuntimeError(f"无法打开视频: {self.config.input_video}")

        fps = capture.get(cv2.CAP_PROP_FPS) or 20.0
        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        return capture, width, height, fps

    def _process_frame(self, frame, frame_index: int):
        """处理单帧，任何异常都退化为预测或丢失，不让整体流程中断。"""
        try:
            raw_candidates = self.detector.detect(frame, frame_index=frame_index)
            filtered_candidates = self.candidate_filter.filter(raw_candidates)
            context = self.tracker.build_context()
            decision = self.selector.select(filtered_candidates, context)
            track_result = self.tracker.update(
                frame_index=frame_index,
                decision=decision,
                raw_candidate_count=len(raw_candidates),
                filtered_candidate_count=len(filtered_candidates),
            )
        except Exception as exc:
            self.logger.exception("第 %s 帧处理异常，系统将退化为预测/丢失: %s", frame_index, exc)
            decision = SelectionDecision(
                selected_candidate=None,
                selected_score=0.0,
                selected_reason=f"frame_exception: {exc}",
                candidate_scores=[],
            )
            track_result = self.tracker.update(
                frame_index=frame_index,
                decision=decision,
                raw_candidate_count=0,
                filtered_candidate_count=0,
                missing_reason=f"frame_exception: {exc}",
            )

        self.logger.debug(
            "frame=%s raw_candidates=%s filtered_candidates=%s status=%s state=%s lost_frames=%s reason=%s",
            frame_index,
            track_result.raw_candidate_count,
            track_result.filtered_candidate_count,
            track_result.output_status.value,
            track_result.state.value,
            track_result.lost_frames,
            track_result.reason,
        )
        return track_result
