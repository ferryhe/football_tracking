from __future__ import annotations

from dataclasses import dataclass
import logging
import math

import cv2
import torch

from football_tracking.config import AppConfig
from football_tracking.detector import MockBallDetector, YOLOSahiBallDetector
from football_tracking.exporter import TrackingExporter
from football_tracking.filtering import CandidateFilter
from football_tracking.mock_mode import MockFrameSource
from football_tracking.postprocess import TrackPostprocessor
from football_tracking.renderer import FrameRenderer
from football_tracking.scene_bias import SceneBiasResolver
from football_tracking.selector import UniqueBallSelector
from football_tracking.tracker import BallTracker
from football_tracking.types import OutputStatus, SelectionDecision, TrackState, TrackerContext


@dataclass(slots=True)
class TentativeReacquireState:
    frame_index: int
    center: tuple[float, float]
    confidence: float
    score: float
    scene_zone: str | None
    kind: str = "tentative"


class BallTrackingPipeline:
    """主流程编排层：把五层架构串联起来。"""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self._configure_runtime()
        self.logger = self._build_logger()
        self.detector = self._build_detector()
        self.scene_bias = SceneBiasResolver(config.scene_bias)
        self.candidate_filter = CandidateFilter(config.filtering, self.scene_bias)
        self.selector = UniqueBallSelector(config.selection, config.tracking, self.scene_bias)
        self.tracker = BallTracker(config.tracking)
        self.renderer = FrameRenderer(config.output)
        self.air_burst_frames_remaining = 0
        self.tentative_reacquire: TentativeReacquireState | None = None
        self.true_out_of_view_active = False
        self.true_out_of_view_empty_frames = 0
        self.true_out_of_view_tentative: TentativeReacquireState | None = None

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

        frame_index = self.config.runtime.start_frame - 1
        processed_frames = 0
        start_frame = self.config.runtime.start_frame
        max_frames = self.config.runtime.max_frames
        if start_frame > 0:
            self.logger.info("Starting from frame=%s", start_frame)
        try:
            while True:
                if max_frames is not None and processed_frames >= max_frames:
                    self.logger.info("Reached max_frames=%s, stopping early.", max_frames)
                    break

                ok, frame = capture.read()
                if not ok:
                    break

                frame_index += 1
                processed_frames += 1
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
            if not self.config.mock.enabled and self.config.postprocess.enabled:
                self.logger.info("Starting postprocess cleanup...")
                TrackPostprocessor(self.config).run()
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
        self._seek_to_start_frame(capture)
        return capture, width, height, fps

    def _seek_to_start_frame(self, capture) -> None:
        start_frame = self.config.runtime.start_frame
        if start_frame <= 0:
            return

        seek_ok = capture.set(cv2.CAP_PROP_POS_FRAMES, start_frame)
        actual_frame = int(capture.get(cv2.CAP_PROP_POS_FRAMES))
        if seek_ok and abs(actual_frame - start_frame) <= 1:
            return

        capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
        skipped = 0
        while skipped < start_frame:
            ok, _ = capture.read()
            if not ok:
                break
            skipped += 1
        if skipped != start_frame:
            raise RuntimeError(f"无法跳转到起始帧: start_frame={start_frame}, skipped={skipped}")

    def _process_frame(self, frame, frame_index: int):
        """处理单帧，任何异常都退化为预测或丢失，不让整体流程中断。"""
        try:
            raw_candidates = self.detector.detect(frame, frame_index=frame_index)
            context = self.tracker.build_context()
            filtered_candidates, filter_rejections, filter_rejection_counts = self.candidate_filter.filter(
                raw_candidates,
                context,
                frame_index,
            )
            decision = self.selector.select(filtered_candidates, context, frame_index)
            reacquire_attempted = False
            reacquire_candidates = []
            reacquire_window = None
            tentative_reacquire_active = False
            tentative_reacquire_confirmed = False
            edge_reentry_active = False
            edge_reentry_window = None
            isolated_far_jump_active = False
            isolated_far_jump_confirmed = False
            true_out_of_view_active = self.true_out_of_view_active
            true_out_of_view_confirmed = False

            (
                raw_candidates,
                filtered_candidates,
                filter_rejections,
                filter_rejection_counts,
                decision,
                reacquire_attempted,
                reacquire_candidates,
                reacquire_window,
                burst_active,
            ) = self._maybe_run_dynamic_reacquire(
                frame=frame,
                frame_index=frame_index,
                context=context,
                raw_candidates=raw_candidates,
                filtered_candidates=filtered_candidates,
                filter_rejections=filter_rejections,
                filter_rejection_counts=filter_rejection_counts,
                decision=decision,
            )
            decision = self._maybe_reject_low_quality_reacquire(
                decision=decision,
                context=context,
            )
            (
                decision,
                tentative_reacquire_active,
                tentative_reacquire_confirmed,
                edge_reentry_active,
                edge_reentry_window,
            ) = self._maybe_apply_tentative_reacquire(
                decision=decision,
                context=context,
                frame_index=frame_index,
                frame_shape=frame.shape[:2],
            )
            (
                decision,
                isolated_far_jump_active,
                isolated_far_jump_confirmed,
            ) = self._maybe_apply_isolated_far_jump_scrub(
                decision=decision,
                context=context,
                frame_index=frame_index,
            )
            (
                decision,
                true_out_of_view_active,
                true_out_of_view_confirmed,
            ) = self._maybe_apply_true_out_of_view_tentative(
                decision=decision,
                context=context,
                frame_index=frame_index,
                frame_shape=frame.shape[:2],
            )
            force_lost = self._should_force_ground_exit_lost(
                context=context,
                frame_index=frame_index,
                decision=decision,
            )
            track_result = self.tracker.update(
                frame_index=frame_index,
                decision=decision,
                raw_candidate_count=len(raw_candidates),
                filtered_candidate_count=len(filtered_candidates),
                frame_size=(frame.shape[1], frame.shape[0]),
                force_lost=force_lost,
            )
            track_result.filter_rejections = filter_rejections
            track_result.filter_rejection_counts = filter_rejection_counts
            track_result.reacquire_attempted = reacquire_attempted
            track_result.reacquire_candidate_count = len(reacquire_candidates)
            track_result.reacquire_window = None if reacquire_window is None else list(reacquire_window)
            track_result.tentative_reacquire_active = tentative_reacquire_active
            track_result.tentative_reacquire_confirmed = tentative_reacquire_confirmed
            track_result.edge_reentry_active = edge_reentry_active
            track_result.edge_reentry_window = None if edge_reentry_window is None else list(edge_reentry_window)
            track_result.isolated_far_jump_active = isolated_far_jump_active
            track_result.isolated_far_jump_confirmed = isolated_far_jump_confirmed
            self._update_true_out_of_view_state(
                context=context,
                track_result=track_result,
                frame_index=frame_index,
                frame_shape=frame.shape[:2],
                filtered_candidate_count=len(filtered_candidates),
            )
            track_result.true_out_of_view_active = self.true_out_of_view_active
            track_result.true_out_of_view_confirmed = true_out_of_view_confirmed
            track_result.true_out_of_view_empty_frames = self.true_out_of_view_empty_frames
            self._advance_air_burst_state(decision)
            track_result.air_burst_active = burst_active
            track_result.air_burst_frames_remaining = self.air_burst_frames_remaining
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
                frame_size=(frame.shape[1], frame.shape[0]),
            )
            track_result.filter_rejections = []
            track_result.filter_rejection_counts = {}
            track_result.reacquire_attempted = False
            track_result.reacquire_candidate_count = 0
            track_result.reacquire_window = None
            track_result.tentative_reacquire_active = False
            track_result.tentative_reacquire_confirmed = False
            track_result.edge_reentry_active = False
            track_result.edge_reentry_window = None
            track_result.isolated_far_jump_active = False
            track_result.isolated_far_jump_confirmed = False
            track_result.true_out_of_view_active = self.true_out_of_view_active
            track_result.true_out_of_view_confirmed = False
            track_result.true_out_of_view_empty_frames = self.true_out_of_view_empty_frames
            self._advance_air_burst_state(None)
            track_result.air_burst_active = False
            track_result.air_burst_frames_remaining = self.air_burst_frames_remaining

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

    def _maybe_run_dynamic_reacquire(
        self,
        frame,
        frame_index: int,
        context,
        raw_candidates,
        filtered_candidates,
        filter_rejections,
        filter_rejection_counts,
        decision: SelectionDecision,
    ):
        dynamic_config = self.config.scene_bias.dynamic_air_recovery
        burst_active = dynamic_config.burst_enabled and self.air_burst_frames_remaining > 0
        if decision.selected_candidate is not None:
            return (
                raw_candidates,
                filtered_candidates,
                filter_rejections,
                filter_rejection_counts,
                decision,
                False,
                [],
                None,
                burst_active,
            )
        if not dynamic_config.enabled or not dynamic_config.reacquire_enabled:
            return (
                raw_candidates,
                filtered_candidates,
                filter_rejections,
                filter_rejection_counts,
                decision,
                False,
                [],
                None,
                burst_active,
            )
        if not burst_active and not self.scene_bias.is_dynamic_air_recovery_active(context):
            return (
                raw_candidates,
                filtered_candidates,
                filter_rejections,
                filter_rejection_counts,
                decision,
                False,
                [],
                None,
                burst_active,
            )
        if not hasattr(self.detector, "detect_direct_in_roi"):
            return (
                raw_candidates,
                filtered_candidates,
                filter_rejections,
                filter_rejection_counts,
                decision,
                False,
                [],
                None,
                burst_active,
            )

        reacquire_window = self.scene_bias.get_dynamic_air_window(context, frame.shape[:2], force=burst_active)
        if reacquire_window is None:
            return (
                raw_candidates,
                filtered_candidates,
                filter_rejections,
                filter_rejection_counts,
                decision,
                False,
                [],
                None,
                burst_active,
            )
        if burst_active:
            reacquire_window = self._scale_roi(
                reacquire_window,
                frame.shape[:2],
                dynamic_config.burst_window_scale,
            )
        reacquire_context = self._build_reacquire_context(context, burst_active)

        confidence_threshold = dynamic_config.reacquire_confidence_threshold
        image_size = dynamic_config.reacquire_image_size
        if burst_active:
            confidence_threshold = dynamic_config.burst_confidence_threshold
            image_size = dynamic_config.burst_image_size

        reacquire_candidates = self.detector.detect_direct_in_roi(
            frame,
            frame_index=frame_index,
            roi=reacquire_window,
            confidence_threshold=confidence_threshold,
            image_size=image_size,
        )
        if not reacquire_candidates:
            return (
                raw_candidates,
                filtered_candidates,
                filter_rejections,
                filter_rejection_counts,
                decision,
                True,
                [],
                reacquire_window,
                burst_active,
            )

        combined_raw_candidates = [*raw_candidates, *reacquire_candidates]
        combined_filtered_candidates, combined_filter_rejections, combined_filter_rejection_counts = self.candidate_filter.filter(
            combined_raw_candidates,
            reacquire_context,
            frame_index,
        )
        combined_decision = self.selector.select(combined_filtered_candidates, reacquire_context, frame_index)
        return (
            combined_raw_candidates,
            combined_filtered_candidates,
            combined_filter_rejections,
            combined_filter_rejection_counts,
            combined_decision,
            True,
            reacquire_candidates,
            reacquire_window,
            burst_active,
        )

    def _build_reacquire_context(self, context: TrackerContext, burst_active: bool) -> TrackerContext:
        if not burst_active or context.state != TrackState.TRACKING:
            return context
        return TrackerContext(
            state=TrackState.PREDICTING,
            last_position=context.last_position,
            predicted_position=context.predicted_position,
            last_detected_position=context.last_detected_position,
            gating_radius=context.gating_radius,
            velocity=context.velocity,
            acceleration=context.acceleration,
            history_length=context.history_length,
            lost_frames=context.lost_frames,
        )

    def _scale_roi(
        self,
        roi: tuple[int, int, int, int],
        frame_shape: tuple[int, int],
        scale: float,
    ) -> tuple[int, int, int, int]:
        if scale <= 1.0:
            return roi

        frame_height, frame_width = frame_shape
        left, top, right, bottom = roi
        center_x = (left + right) / 2.0
        center_y = (top + bottom) / 2.0
        width = (right - left) * scale
        height = (bottom - top) * scale
        scaled_left = max(0, int(round(center_x - width / 2.0)))
        scaled_top = max(0, int(round(center_y - height / 2.0)))
        scaled_right = min(frame_width, int(round(center_x + width / 2.0)))
        scaled_bottom = min(frame_height, int(round(center_y + height / 2.0)))
        scaled_right = max(scaled_left + 1, scaled_right)
        scaled_bottom = max(scaled_top + 1, scaled_bottom)
        return (scaled_left, scaled_top, scaled_right, scaled_bottom)

    def _maybe_reject_low_quality_reacquire(
        self,
        decision: SelectionDecision,
        context,
    ) -> SelectionDecision:
        dynamic_config = self.config.scene_bias.dynamic_air_recovery
        candidate = decision.selected_candidate
        if candidate is None or not dynamic_config.low_quality_reject_enabled:
            return decision
        if candidate.source != "yolo_direct_roi":
            return decision
        if context.lost_frames < dynamic_config.low_quality_reject_min_lost_frames:
            return decision
        if not decision.candidate_scores:
            return decision

        best_score = decision.candidate_scores[0]
        if best_score.scene_zone != "dynamic_air_recovery":
            return decision
        if candidate.confidence >= dynamic_config.low_quality_reject_confidence:
            return decision
        if decision.selected_score >= dynamic_config.low_quality_reject_score:
            return decision

        return SelectionDecision(
            selected_candidate=None,
            selected_score=decision.selected_score,
            selected_reason="low_quality_reacquire_rejected",
            candidate_scores=decision.candidate_scores,
        )

    def _maybe_apply_tentative_reacquire(
        self,
        decision: SelectionDecision,
        context,
        frame_index: int,
        frame_shape: tuple[int, int],
    ) -> tuple[SelectionDecision, bool, bool, bool, tuple[int, int, int, int] | None]:
        dynamic_config = self.config.scene_bias.dynamic_air_recovery
        self._expire_tentative_reacquire(frame_index, dynamic_config.tentative_reacquire_max_age)
        pending_state = self.tentative_reacquire
        isolated_pending = pending_state is not None and pending_state.kind == "isolated_far_jump"
        candidate = decision.selected_candidate
        if candidate is None or not dynamic_config.tentative_reacquire_enabled:
            return decision, self.tentative_reacquire is not None, False, False, None
        if candidate.source != "yolo_direct_roi":
            if isolated_pending:
                return decision, True, False, False, None
            self.tentative_reacquire = None
            return decision, False, False, False, None
        if context.lost_frames < dynamic_config.tentative_reacquire_min_lost_frames:
            if isolated_pending:
                return decision, True, False, False, None
            self.tentative_reacquire = None
            return decision, False, False, False, None
        if not decision.candidate_scores:
            if isolated_pending:
                return decision, True, False, False, None
            self.tentative_reacquire = None
            return decision, False, False, False, None

        best_score = decision.candidate_scores[0]
        if best_score.scene_zone != "dynamic_air_recovery":
            if isolated_pending:
                return decision, True, False, False, None
            self.tentative_reacquire = None
            return decision, False, False, False, None
        jump_mode, jump_distance = self._evaluate_gap_aware_jump_mode(candidate, context)
        if jump_mode == "reject":
            self.tentative_reacquire = None
            rejected_decision = SelectionDecision(
                selected_candidate=None,
                selected_score=decision.selected_score,
                selected_reason="gap_aware_jump_rejected",
                candidate_scores=decision.candidate_scores,
            )
            return rejected_decision, False, False, False, None

        edge_reentry_window = self.scene_bias.get_edge_reentry_window(context, frame_shape)
        edge_reentry_tentative = False
        if (
            edge_reentry_window is not None
            and candidate.confidence < dynamic_config.edge_reentry_high_confidence_bypass
        ):
            center_x, center_y = candidate.center
            left, top, right, bottom = edge_reentry_window
            if not (left <= center_x <= right and top <= center_y <= bottom):
                edge_reentry_tentative = True

        confidence_or_score_low = (
            candidate.confidence < dynamic_config.tentative_reacquire_confidence_threshold
            or decision.selected_score < dynamic_config.tentative_reacquire_score_threshold
        )
        far_jump_for_tentative = jump_distance >= dynamic_config.gap_aware_short_jump_distance
        needs_tentative = (
            edge_reentry_tentative
            or jump_mode == "tentative"
            or (confidence_or_score_low and far_jump_for_tentative)
        )

        if not needs_tentative:
            self.tentative_reacquire = None
            return decision, False, False, edge_reentry_window is not None, edge_reentry_window

        if self.tentative_reacquire is not None:
            if (
                frame_index == self.tentative_reacquire.frame_index + 1
                and self._distance(candidate.center, self.tentative_reacquire.center)
                <= dynamic_config.tentative_reacquire_confirmation_radius
            ):
                confirmed_decision = SelectionDecision(
                    selected_candidate=candidate,
                    selected_score=decision.selected_score,
                    selected_reason="tentative_reacquire_confirmed",
                    candidate_scores=decision.candidate_scores,
                )
                self.tentative_reacquire = None
                return confirmed_decision, False, True, edge_reentry_window is not None, edge_reentry_window

        self.tentative_reacquire = TentativeReacquireState(
            frame_index=frame_index,
            center=candidate.center,
            confidence=candidate.confidence,
            score=decision.selected_score,
            scene_zone=best_score.scene_zone,
            kind="edge_reentry" if edge_reentry_tentative else "dynamic_air",
        )
        pending_decision = SelectionDecision(
            selected_candidate=None,
            selected_score=decision.selected_score,
            selected_reason=(
                "edge_reentry_tentative"
                if edge_reentry_tentative
                else ("tentative_reacquire_pending" if jump_mode != "tentative" else "gap_aware_jump_tentative")
            ),
            candidate_scores=decision.candidate_scores,
        )
        return pending_decision, True, False, edge_reentry_window is not None, edge_reentry_window

    def _maybe_apply_isolated_far_jump_scrub(
        self,
        decision: SelectionDecision,
        context,
        frame_index: int,
    ) -> tuple[SelectionDecision, bool, bool]:
        dynamic_config = self.config.scene_bias.dynamic_air_recovery
        candidate = decision.selected_candidate
        if not dynamic_config.isolated_far_jump_enabled or candidate is None:
            return decision, False, False

        pending_state = self.tentative_reacquire
        if pending_state is not None and pending_state.kind == "isolated_far_jump":
            if (
                frame_index == pending_state.frame_index + 1
                and self._distance(candidate.center, pending_state.center)
                <= dynamic_config.isolated_far_jump_confirmation_radius
            ):
                confirmed_decision = SelectionDecision(
                    selected_candidate=candidate,
                    selected_score=decision.selected_score,
                    selected_reason="isolated_far_jump_confirmed",
                    candidate_scores=decision.candidate_scores,
                )
                self.tentative_reacquire = None
                return confirmed_decision, False, True

        if context.lost_frames < dynamic_config.isolated_far_jump_min_lost_frames:
            return decision, False, False
        if candidate.confidence >= dynamic_config.isolated_far_jump_high_confidence_bypass:
            return decision, False, False

        anchor_position = context.predicted_position or context.last_position or context.last_detected_position
        if anchor_position is None:
            return decision, False, False

        jump_distance = self._distance(candidate.center, anchor_position)
        if jump_distance < dynamic_config.isolated_far_jump_distance:
            return decision, False, False

        self.tentative_reacquire = TentativeReacquireState(
            frame_index=frame_index,
            center=candidate.center,
            confidence=candidate.confidence,
            score=decision.selected_score,
            scene_zone=decision.candidate_scores[0].scene_zone if decision.candidate_scores else None,
            kind="isolated_far_jump",
        )
        pending_decision = SelectionDecision(
            selected_candidate=None,
            selected_score=decision.selected_score,
            selected_reason="isolated_far_jump_tentative",
            candidate_scores=decision.candidate_scores,
        )
        return pending_decision, True, False

    def _maybe_apply_true_out_of_view_tentative(
        self,
        decision: SelectionDecision,
        context,
        frame_index: int,
        frame_shape: tuple[int, int],
    ) -> tuple[SelectionDecision, bool, bool]:
        dynamic_config = self.config.scene_bias.dynamic_air_recovery
        if not dynamic_config.true_out_of_view_enabled:
            self.true_out_of_view_tentative = None
            return decision, False, False

        self._expire_true_out_of_view_tentative(frame_index)
        candidate = decision.selected_candidate
        if candidate is None:
            return decision, self.true_out_of_view_active, False
        if not self.true_out_of_view_active or context.lost_frames < dynamic_config.true_out_of_view_min_lost_frames:
            self.true_out_of_view_tentative = None
            return decision, self.true_out_of_view_active, False

        pending_state = self.true_out_of_view_tentative
        if pending_state is not None:
            if (
                frame_index == pending_state.frame_index + 1
                and self._distance(candidate.center, pending_state.center)
                <= dynamic_config.true_out_of_view_confirmation_radius
            ):
                confirmed_decision = SelectionDecision(
                    selected_candidate=candidate,
                    selected_score=decision.selected_score,
                    selected_reason="true_out_of_view_confirmed",
                    candidate_scores=decision.candidate_scores,
                )
                self.true_out_of_view_tentative = None
                return confirmed_decision, True, True

        in_ground_zone = self.scene_bias.is_point_in_ground_zone(candidate.center, context, frame_index)
        anchor_position = context.last_detected_position or context.last_position or context.predicted_position
        jump_distance = 0.0 if anchor_position is None else self._distance(candidate.center, anchor_position)
        if (
            candidate.confidence >= dynamic_config.true_out_of_view_high_confidence_bypass
            and in_ground_zone
            and jump_distance < dynamic_config.true_out_of_view_jump_distance
        ):
            self.true_out_of_view_tentative = None
            return decision, True, False

        self.true_out_of_view_tentative = TentativeReacquireState(
            frame_index=frame_index,
            center=candidate.center,
            confidence=candidate.confidence,
            score=decision.selected_score,
            scene_zone=decision.candidate_scores[0].scene_zone if decision.candidate_scores else None,
            kind="true_out_of_view",
        )
        pending_decision = SelectionDecision(
            selected_candidate=None,
            selected_score=decision.selected_score,
            selected_reason="true_out_of_view_tentative",
            candidate_scores=decision.candidate_scores,
        )
        return pending_decision, True, False

    def _expire_true_out_of_view_tentative(self, frame_index: int) -> None:
        if self.true_out_of_view_tentative is None:
            return
        if frame_index - self.true_out_of_view_tentative.frame_index > 1:
            self.true_out_of_view_tentative = None

    def _update_true_out_of_view_state(
        self,
        context,
        track_result,
        frame_index: int,
        frame_shape: tuple[int, int],
        filtered_candidate_count: int,
    ) -> None:
        dynamic_config = self.config.scene_bias.dynamic_air_recovery
        if not dynamic_config.true_out_of_view_enabled:
            self.true_out_of_view_active = False
            self.true_out_of_view_empty_frames = 0
            self.true_out_of_view_tentative = None
            return

        if track_result.output_status == OutputStatus.DETECTED:
            self.true_out_of_view_active = False
            self.true_out_of_view_empty_frames = 0
            self.true_out_of_view_tentative = None
            return

        current_lost_frames = track_result.lost_frames
        if current_lost_frames < dynamic_config.true_out_of_view_min_lost_frames:
            self.true_out_of_view_active = False
            self.true_out_of_view_empty_frames = 0
            self.true_out_of_view_tentative = None
            return

        anchor_position = None
        if track_result.point is not None:
            anchor_position = (track_result.point.x, track_result.point.y)
        if anchor_position is None:
            anchor_position = context.predicted_position or context.last_position or context.last_detected_position
        if anchor_position is None:
            self.true_out_of_view_active = False
            self.true_out_of_view_empty_frames = 0
            return

        leaving_monitored_area = (
            not self.scene_bias.is_point_in_ground_zone(anchor_position, context, frame_index)
            or self._is_near_frame_edge(anchor_position, frame_shape)
        )
        if leaving_monitored_area and filtered_candidate_count == 0:
            self.true_out_of_view_empty_frames += 1
        elif track_result.output_status != OutputStatus.DETECTED:
            self.true_out_of_view_empty_frames = 0

        if self.true_out_of_view_active:
            return
        if self.true_out_of_view_empty_frames >= dynamic_config.true_out_of_view_min_empty_frames:
            self.true_out_of_view_active = True

    def _is_near_frame_edge(
        self,
        position: tuple[float, float],
        frame_shape: tuple[int, int],
    ) -> bool:
        frame_height, frame_width = frame_shape
        margin_x = frame_width * self.config.scene_bias.dynamic_air_recovery.true_out_of_view_edge_margin_x_ratio
        margin_y = frame_height * self.config.scene_bias.dynamic_air_recovery.true_out_of_view_edge_margin_y_ratio
        x, y = position
        return (
            x <= margin_x
            or x >= frame_width - 1.0 - margin_x
            or y <= margin_y
            or y >= frame_height - 1.0 - margin_y
        )

    def _expire_tentative_reacquire(self, frame_index: int, max_age: int) -> None:
        if self.tentative_reacquire is None:
            return
        if max_age < 0:
            max_age = 0
        if frame_index - self.tentative_reacquire.frame_index > max_age:
            self.tentative_reacquire = None

    def _distance(self, point_a: tuple[float, float], point_b: tuple[float, float]) -> float:
        return math.hypot(point_a[0] - point_b[0], point_a[1] - point_b[1])

    def _evaluate_gap_aware_jump_mode(
        self,
        candidate,
        context,
    ) -> tuple[str, float]:
        dynamic_config = self.config.scene_bias.dynamic_air_recovery
        if not dynamic_config.gap_aware_jump_gate_enabled:
            return "accept", 0.0

        anchor_position = context.predicted_position or context.last_position or context.last_detected_position
        if anchor_position is None:
            return "accept", 0.0

        jump_distance = self._distance(candidate.center, anchor_position)
        if context.lost_frames <= 0:
            return "accept", jump_distance
        if candidate.confidence >= dynamic_config.gap_aware_high_confidence_bypass:
            return "accept", jump_distance

        if (
            context.lost_frames <= dynamic_config.gap_aware_short_lost_frames
            and jump_distance >= dynamic_config.gap_aware_short_jump_distance
        ):
            return "reject", jump_distance
        if jump_distance >= dynamic_config.gap_aware_long_jump_distance:
            return "tentative", jump_distance
        return "accept", jump_distance

    def _should_force_ground_exit_lost(
        self,
        context,
        frame_index: int,
        decision: SelectionDecision,
    ) -> bool:
        dynamic_config = self.config.scene_bias.dynamic_air_recovery
        if decision.selected_candidate is not None or not dynamic_config.ground_exit_enabled:
            return False
        if context.lost_frames < dynamic_config.ground_exit_min_lost_frames:
            return False
        if self.air_burst_frames_remaining > 0:
            return False
        if context.predicted_position is None:
            return False
        return not self.scene_bias.is_point_in_ground_zone(
            context.predicted_position,
            context,
            frame_index,
        )

    def _advance_air_burst_state(self, decision: SelectionDecision | None) -> None:
        dynamic_config = self.config.scene_bias.dynamic_air_recovery
        if decision is not None and decision.selected_candidate is not None and decision.candidate_scores:
            best_score = decision.candidate_scores[0]
            if (
                dynamic_config.burst_enabled
                and best_score.scene_zone == "dynamic_air_recovery"
                and decision.selected_candidate.source == "yolo_direct_roi"
            ):
                self.air_burst_frames_remaining = max(0, dynamic_config.burst_frames)
                return

        if self.air_burst_frames_remaining > 0:
            self.air_burst_frames_remaining -= 1
