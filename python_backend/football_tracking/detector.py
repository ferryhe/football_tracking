from __future__ import annotations

import logging
from typing import Iterable

from football_tracking.config import DetectorConfig, MockConfig, SahiConfig
from football_tracking.types import Candidate

LOGGER = logging.getLogger(__name__)


class YOLOSahiBallDetector:
    """使用 YOLO + SAHI 对高分辨率视频做小目标候选检测。"""

    def __init__(self, detector_config: DetectorConfig, sahi_config: SahiConfig) -> None:
        self.detector_config = detector_config
        self.sahi_config = sahi_config
        self.model = self._build_model()
        self.get_sliced_prediction = self._load_sahi_predictor()
        self.direct_model = None

    def _build_model(self):
        """只初始化一次检测模型，避免重复占用显存。"""
        from sahi import AutoDetectionModel

        model = AutoDetectionModel.from_pretrained(
            model_type="ultralytics",
            model_path=str(self.detector_config.model_path),
            confidence_threshold=self.detector_config.confidence_threshold,
            device=self.detector_config.device,
            image_size=self.detector_config.image_size,
        )

        # 这里仅做半精度推理优化，不引入任何追踪逻辑，保持检测层纯净。
        if self.detector_config.use_half:
            backend = getattr(model, "model", None)
            if backend is not None and hasattr(backend, "half"):
                try:
                    backend.half()
                except Exception as exc:  # pragma: no cover - 依赖具体后端
                    LOGGER.warning("无法启用半精度推理，将继续使用默认精度: %s", exc)

        return model

    def _load_sahi_predictor(self):
        """延迟导入 SAHI，避免 mock 模式下无意义加载检测依赖。"""
        from sahi.predict import get_sliced_prediction

        return get_sliced_prediction

    def _get_direct_model(self):
        if self.direct_model is None:
            from ultralytics import YOLO

            self.direct_model = YOLO(str(self.detector_config.model_path))
        return self.direct_model

    def detect(self, frame, frame_index: int) -> list[Candidate]:
        """检测当前帧中的候选球，返回原始候选列表。"""
        prediction_result = self.get_sliced_prediction(
            image=frame,
            detection_model=self.model,
            slice_height=self.sahi_config.slice_height,
            slice_width=self.sahi_config.slice_width,
            overlap_height_ratio=self.sahi_config.overlap_height_ratio,
            overlap_width_ratio=self.sahi_config.overlap_width_ratio,
            perform_standard_pred=self.sahi_config.perform_standard_pred,
            postprocess_type=self.sahi_config.postprocess_type,
            postprocess_match_metric=self.sahi_config.postprocess_match_metric,
            postprocess_match_threshold=self.sahi_config.postprocess_match_threshold,
            verbose=self.sahi_config.verbose,
        )

        return list(self._to_candidates(prediction_result.object_prediction_list, frame_index))

    def detect_direct(
        self,
        frame,
        frame_index: int,
        confidence_threshold: float | None = None,
        image_size: int | None = None,
    ) -> list[Candidate]:
        """在整帧或裁剪区域上直接运行 YOLO，不使用 SAHI 切片。"""
        model = self._get_direct_model()
        prediction_results = model.predict(
            frame,
            conf=confidence_threshold if confidence_threshold is not None else self.detector_config.confidence_threshold,
            imgsz=image_size if image_size is not None else self.detector_config.image_size,
            device=self.detector_config.device,
            verbose=False,
            half=self.detector_config.use_half and self.detector_config.device.startswith("cuda"),
        )
        if not prediction_results:
            return []
        return list(self._to_direct_candidates(prediction_results[0], frame_index, source="yolo_direct"))

    def detect_direct_in_roi(
        self,
        frame,
        frame_index: int,
        roi: tuple[int, int, int, int],
        confidence_threshold: float | None = None,
        image_size: int | None = None,
    ) -> list[Candidate]:
        """对局部 ROI 做更激进的二次重检，再映射回全图坐标。"""
        left, top, right, bottom = roi
        crop = frame[top:bottom, left:right]
        if crop.size == 0:
            return []

        local_candidates = self.detect_direct(
            crop,
            frame_index=frame_index,
            confidence_threshold=confidence_threshold,
            image_size=image_size,
        )
        global_candidates: list[Candidate] = []
        for candidate in local_candidates:
            global_candidates.append(
                Candidate(
                    frame_index=frame_index,
                    x1=candidate.x1 + left,
                    y1=candidate.y1 + top,
                    x2=candidate.x2 + left,
                    y2=candidate.y2 + top,
                    confidence=candidate.confidence,
                    label=candidate.label,
                    source=f"{candidate.source}_roi",
                )
            )
        return global_candidates

    def _to_candidates(self, predictions: Iterable, frame_index: int) -> Iterable[Candidate]:
        """将 SAHI 输出转换为统一候选结构。"""
        allow_all = len(self.detector_config.allowed_labels) == 0
        allowed_labels = {label.lower() for label in self.detector_config.allowed_labels}

        for prediction in predictions:
            category = getattr(prediction, "category", None)
            label = getattr(category, "name", "ball") or "ball"
            if not allow_all and label.lower() not in allowed_labels:
                continue

            bbox = prediction.bbox.to_xyxy()
            yield Candidate(
                frame_index=frame_index,
                x1=float(bbox[0]),
                y1=float(bbox[1]),
                x2=float(bbox[2]),
                y2=float(bbox[3]),
                confidence=float(prediction.score.value),
                label=label,
                source="yolo_sahi",
            )

    def _to_direct_candidates(self, prediction_result, frame_index: int, source: str) -> Iterable[Candidate]:
        allow_all = len(self.detector_config.allowed_labels) == 0
        allowed_labels = {label.lower() for label in self.detector_config.allowed_labels}

        boxes = getattr(prediction_result, "boxes", None)
        if boxes is None:
            return

        names = getattr(prediction_result, "names", {})
        xyxy = boxes.xyxy.cpu().tolist()
        confidences = boxes.conf.cpu().tolist()
        class_ids = boxes.cls.cpu().tolist()

        for bbox, score, class_id in zip(xyxy, confidences, class_ids):
            class_index = int(class_id)
            if isinstance(names, dict):
                label = str(names.get(class_index, "ball"))
            else:
                label = str(names[class_index])
            if not allow_all and label.lower() not in allowed_labels:
                continue

            yield Candidate(
                frame_index=frame_index,
                x1=float(bbox[0]),
                y1=float(bbox[1]),
                x2=float(bbox[2]),
                y2=float(bbox[3]),
                confidence=float(score),
                label=label,
                source=source,
            )


class MockBallDetector:
    """联调用假 detector，直接输出预设 Candidate 序列，不加载 YOLO/SAHI。"""

    def __init__(self, config: MockConfig) -> None:
        self.config = config
        self.scenario = config.scenario.upper()
        self.ball_box_size = max(4, config.ball_box_size)
        self.scenario_frames = self._build_scenario_frames()

    def detect(self, frame, frame_index: int) -> list[Candidate]:
        """按预设场景输出当前帧候选列表。"""
        centers = self.scenario_frames.get(frame_index, [])
        return [self._make_candidate(frame_index, center_x, center_y) for center_x, center_y in centers]

    def _build_scenario_frames(self) -> dict[int, list[tuple[float, float]]]:
        """构造三组固定联调场景，确保覆盖 Detected / Predicted / Lost。"""
        if self.scenario == "A":
            return {
                frame_index: [(80.0 + 18.0 * frame_index, self.config.frame_height * 0.50)]
                for frame_index in range(self.config.frame_count)
            }

        if self.scenario == "B":
            missing_frames = {5, 6, 7}
            return {
                frame_index: [(70.0 + 20.0 * frame_index, self.config.frame_height * 0.48)]
                for frame_index in range(self.config.frame_count)
                if frame_index not in missing_frames
            }

        if self.scenario == "C":
            visible_frames = {0, 1, 2, 3}
            return {
                frame_index: [(90.0 + 22.0 * frame_index, self.config.frame_height * 0.52)]
                for frame_index in range(self.config.frame_count)
                if frame_index in visible_frames
            }

        raise ValueError(f"不支持的 mock 场景: {self.scenario}")

    def _make_candidate(self, frame_index: int, center_x: float, center_y: float) -> Candidate:
        """将预设中心点转换为候选框，继续复用后续 filtering / selector / tracker。"""
        half_size = self.ball_box_size / 2.0
        return Candidate(
            frame_index=frame_index,
            x1=center_x - half_size,
            y1=center_y - half_size,
            x2=center_x + half_size,
            y2=center_y + half_size,
            confidence=0.95,
            label="ball",
            source=f"mock_{self.scenario.lower()}",
        )
