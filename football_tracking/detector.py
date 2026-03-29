from __future__ import annotations

import logging
from typing import Iterable, Sequence

from football_tracking.config import DetectorConfig, MockConfig, SahiConfig
from football_tracking.types import Candidate

LOGGER = logging.getLogger(__name__)


class YOLOSahiBallDetector:
    """Use batched sliced YOLO inference for high-resolution small-object detection."""

    def __init__(self, detector_config: DetectorConfig, sahi_config: SahiConfig) -> None:
        self.detector_config = detector_config
        self.sahi_config = sahi_config
        self.allow_all_labels = len(self.detector_config.allowed_labels) == 0
        self.allowed_labels = {label.lower() for label in self.detector_config.allowed_labels}
        self.use_half = self.detector_config.use_half and self.detector_config.device.startswith("cuda")
        self.model = self._build_model()
        self.postprocess = self._build_postprocess()

    def _build_model(self):
        """Initialize one Ultralytics model instance and reuse it across predict paths."""
        from ultralytics import YOLO

        model = YOLO(str(self.detector_config.model_path))

        backend = getattr(model, "model", None)
        if backend is not None and hasattr(backend, "eval"):
            backend.eval()

        if self.use_half and backend is not None and hasattr(backend, "half"):
            try:
                backend.half()
            except Exception as exc:  # pragma: no cover - backend specific
                LOGGER.warning("Failed to enable half precision inference, falling back to default precision: %s", exc)

        return model

    def _build_postprocess(self):
        """Construct the SAHI-compatible postprocess stage once."""
        from sahi.predict import POSTPROCESS_NAME_TO_CLASS

        postprocess_type = str(self.sahi_config.postprocess_type).upper()
        postprocess_constructor = POSTPROCESS_NAME_TO_CLASS.get(postprocess_type)
        if postprocess_constructor is None:
            supported_types = ", ".join(sorted(POSTPROCESS_NAME_TO_CLASS))
            raise ValueError(f"Unsupported SAHI postprocess type: {postprocess_type}. Expected one of: {supported_types}")

        return postprocess_constructor(
            match_threshold=self.sahi_config.postprocess_match_threshold,
            match_metric=self.sahi_config.postprocess_match_metric,
            class_agnostic=True,
        )

    def _get_direct_model(self):
        return self.model

    def detect(self, frame, frame_index: int) -> list[Candidate]:
        """Detect candidates in the current frame."""
        prediction_list = self._predict_sliced(frame)

        if self.sahi_config.perform_standard_pred:
            prediction_list.extend(self._predict_full_frame(frame))

        if self.postprocess is not None and len(prediction_list) > 1:
            prediction_list = self.postprocess(prediction_list)

        return list(self._to_candidates(prediction_list, frame_index))

    def detect_direct(
        self,
        frame,
        frame_index: int,
        confidence_threshold: float | None = None,
        image_size: int | None = None,
    ) -> list[Candidate]:
        """Run direct YOLO inference on a full frame or crop without slicing."""
        prediction_results = self._predict_images(
            frame,
            confidence_threshold=confidence_threshold,
            image_size=image_size,
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
        """Run a more aggressive second-pass direct detect on a local ROI."""
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

    def _predict_sliced(self, frame) -> list:
        """Slice the frame, batch the slices, and merge predictions back into full-image coordinates."""
        from sahi.slicing import slice_image

        frame_height, frame_width = frame.shape[:2]
        slice_result = slice_image(
            image=frame,
            slice_height=self.sahi_config.slice_height,
            slice_width=self.sahi_config.slice_width,
            overlap_height_ratio=self.sahi_config.overlap_height_ratio,
            overlap_width_ratio=self.sahi_config.overlap_width_ratio,
            auto_slice_resolution=False,
            verbose=bool(self.sahi_config.verbose),
        )

        if not slice_result.images:
            return []

        object_predictions: list = []
        batch_size = max(1, self.sahi_config.batch_size)
        for start_index in range(0, len(slice_result.images), batch_size):
            image_batch = slice_result.images[start_index : start_index + batch_size]
            shift_batch = slice_result.starting_pixels[start_index : start_index + batch_size]
            prediction_results = self._predict_images(image_batch)
            object_predictions.extend(
                self._to_object_predictions_batch(
                    prediction_results,
                    shift_batch=shift_batch,
                    full_shape=(frame_height, frame_width),
                )
            )

        return object_predictions

    def _predict_full_frame(self, frame) -> list:
        """Mirror SAHI's optional standard prediction path using the same YOLO backend."""
        frame_height, frame_width = frame.shape[:2]
        prediction_results = self._predict_images(frame)
        return self._to_object_predictions_batch(
            prediction_results,
            shift_batch=[[0, 0]],
            full_shape=(frame_height, frame_width),
        )

    def _predict_images(
        self,
        images,
        confidence_threshold: float | None = None,
        image_size: int | None = None,
    ):
        model = self._get_direct_model()
        return model.predict(
            images,
            conf=confidence_threshold if confidence_threshold is not None else self.detector_config.confidence_threshold,
            imgsz=image_size if image_size is not None else self.detector_config.image_size,
            device=self.detector_config.device,
            verbose=False,
            half=self.use_half,
        )

    def _to_object_predictions_batch(
        self,
        prediction_results,
        shift_batch: Sequence[Sequence[int]],
        full_shape: tuple[int, int],
    ) -> list:
        object_predictions: list = []
        for prediction_result, shift_amount in zip(prediction_results, shift_batch):
            object_predictions.extend(
                self._to_object_predictions(
                    prediction_result,
                    shift_amount=shift_amount,
                    full_shape=full_shape,
                )
            )
        return object_predictions

    def _to_object_predictions(
        self,
        prediction_result,
        shift_amount: Sequence[int],
        full_shape: tuple[int, int],
    ) -> list:
        from sahi.prediction import ObjectPrediction

        boxes = getattr(prediction_result, "boxes", None)
        if boxes is None:
            return []

        names = getattr(prediction_result, "names", {})
        shift_x, shift_y = int(shift_amount[0]), int(shift_amount[1])
        full_height, full_width = int(full_shape[0]), int(full_shape[1])

        xyxy = boxes.xyxy.cpu().tolist()
        confidences = boxes.conf.cpu().tolist()
        class_ids = boxes.cls.cpu().tolist()

        object_predictions: list[ObjectPrediction] = []
        for bbox, score, class_id in zip(xyxy, confidences, class_ids):
            class_index = int(class_id)
            label = self._resolve_label(names, class_index)
            object_predictions.append(
                ObjectPrediction(
                    bbox=[
                        float(bbox[0]) + shift_x,
                        float(bbox[1]) + shift_y,
                        float(bbox[2]) + shift_x,
                        float(bbox[3]) + shift_y,
                    ],
                    category_id=class_index,
                    category_name=label,
                    score=float(score),
                    shift_amount=[0, 0],
                    full_shape=[full_height, full_width],
                )
            )
        return object_predictions

    def _resolve_label(self, names, class_index: int) -> str:
        if isinstance(names, dict):
            return str(names.get(class_index, "ball"))
        return str(names[class_index])

    def _to_candidates(self, predictions: Iterable, frame_index: int) -> Iterable[Candidate]:
        """Convert SAHI-style predictions into the unified candidate structure."""
        for prediction in predictions:
            category = getattr(prediction, "category", None)
            label = getattr(category, "name", "ball") or "ball"
            if not self.allow_all_labels and label.lower() not in self.allowed_labels:
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
        boxes = getattr(prediction_result, "boxes", None)
        if boxes is None:
            return

        names = getattr(prediction_result, "names", {})
        xyxy = boxes.xyxy.cpu().tolist()
        confidences = boxes.conf.cpu().tolist()
        class_ids = boxes.cls.cpu().tolist()

        for bbox, score, class_id in zip(xyxy, confidences, class_ids):
            class_index = int(class_id)
            label = self._resolve_label(names, class_index)
            if not self.allow_all_labels and label.lower() not in self.allowed_labels:
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
    """A mock detector used during integration without loading YOLO or SAHI."""

    def __init__(self, config: MockConfig) -> None:
        self.config = config
        self.scenario = config.scenario.upper()
        self.ball_box_size = max(4, config.ball_box_size)
        self.scenario_frames = self._build_scenario_frames()

    def detect(self, frame, frame_index: int) -> list[Candidate]:
        """Emit pre-defined candidates for the current frame."""
        centers = self.scenario_frames.get(frame_index, [])
        return [self._make_candidate(frame_index, center_x, center_y) for center_x, center_y in centers]

    def _build_scenario_frames(self) -> dict[int, list[tuple[float, float]]]:
        """Build three fixed scenarios that cover Detected / Predicted / Lost states."""
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

        raise ValueError(f"Unsupported mock scenario: {self.scenario}")

    def _make_candidate(self, frame_index: int, center_x: float, center_y: float) -> Candidate:
        """Convert the pre-defined center point into a candidate box."""
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
