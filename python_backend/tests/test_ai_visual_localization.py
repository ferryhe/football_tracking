from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from football_tracking.ai_visual_localization import (
    AI_VISUAL_LOCALIZATION_RESPONSE_SCHEMA,
    OpenAIVisualLocalizationClient,
    _sample_frames,
    write_ai_visual_localization_report,
)


class _FakeLocalizationClient:
    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    def localize_window(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        return self.response


class _LegacyLocalizationClient:
    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    def localize_window(
        self,
        *,
        metadata: dict[str, object],
        contact_sheet_data_url: str,
        crop_sheet_data_url: str,
        model: str,
    ) -> dict[str, object]:
        self.calls.append(
            {
                "metadata": metadata,
                "contact_sheet_data_url": contact_sheet_data_url,
                "crop_sheet_data_url": crop_sheet_data_url,
                "model": model,
            }
        )
        return self.response


class _CapturingResponsesClient:
    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    def create_json_vision_response(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        return self.response


class AiVisualLocalizationTests(unittest.TestCase):
    def test_missing_video_writes_unavailable_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            report = write_ai_visual_localization_report(
                output_dir,
                output_dir / "missing_5760x1440.mp4",
                ["10:20:right_corner"],
                dry_run=True,
            )
            written = json.loads((output_dir / "ai_visual_localization.json").read_text(encoding="utf-8"))

        self.assertEqual("unavailable", report["summary"]["status"])
        self.assertEqual("unavailable", report["requests"][0]["status"])
        self.assertEqual(0, report["video_width"])
        self.assertEqual(report["summary"], written["summary"])
        self.assertEqual("visual_localization:10_20_right_corner", report["requests"][0]["visual_localization_id"])

    def test_invalid_window_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            with self.assertRaisesRegex(ValueError, "start:end:label"):
                write_ai_visual_localization_report(output_dir, output_dir / "video.mp4", ["10-20"], dry_run=True)

    def test_decoded_opencv_dimensions_win_over_filename_dimensions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            video_path = output_dir / "match_5760x1440.mp4"
            _write_tiny_video(video_path, width=64, height=36, frame_count=24)

            report = write_ai_visual_localization_report(
                output_dir,
                video_path,
                ["5:15:right_corner"],
                dry_run=True,
            )
            request = report["requests"][0]
            contact_sheet_exists = (output_dir / request["media"]["contact_sheet"]).exists()
            crop_sheet_exists = (output_dir / request["media"]["crop_sheet"]).exists()
            zoom_sheet_exists = (output_dir / request["media"]["zoom_sheet"]).exists()

        self.assertEqual(64, report["source_video"]["width"])
        self.assertEqual(36, report["source_video"]["height"])
        self.assertEqual("opencv", report["source_video"]["dimension_source"])
        self.assertEqual(64, report["video_width"])
        self.assertEqual(36, report["video_height"])
        self.assertEqual("planned", request["status"])
        self.assertLessEqual(request["crop"]["x"] + request["crop"]["width"], 64)
        self.assertLessEqual(request["crop"]["y"] + request["crop"]["height"], 36)
        self.assertTrue(contact_sheet_exists)
        self.assertTrue(crop_sheet_exists)
        self.assertTrue(zoom_sheet_exists)
        self.assertIn("ai_visual_localization/visual_localization_5_15_right_corner/zoom_sheet.jpg", request["media"]["zoom_sheet"])
        self.assertRegex(request["media"]["zoom_sheet_sha256"], r"^[0-9a-f]{64}$")
        self.assertIn("zoom_crop", request)
        self.assertLessEqual(request["zoom_crop"]["x"] + request["zoom_crop"]["width"], 64)
        self.assertLessEqual(request["zoom_crop"]["y"] + request["zoom_crop"]["height"], 36)

    def test_video_must_decode_first_frame_before_dimensions_are_available(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            video_path = output_dir / "container_metadata_only_5760x1440.mp4"
            video_path.write_bytes(b"not a decodable test video")

            capture = _UnreadableCapture(width=5760, height=1440)
            with patch("cv2.VideoCapture", return_value=capture):
                report = write_ai_visual_localization_report(
                    output_dir,
                    video_path,
                    ["5:15:right_corner"],
                    dry_run=True,
                )

        self.assertEqual("unavailable", report["summary"]["status"])
        self.assertTrue(capture.released)
        self.assertEqual("unavailable", report["source_video"]["status"])
        self.assertEqual(0, report["video_width"])
        self.assertEqual(0, report["video_height"])
        self.assertIn("first frame", report["requests"][0]["reason"])

    def test_long_window_sampling_keeps_early_recovery_frames(self) -> None:
        self.assertEqual([2049, 2079, 2109, 2296, 2544], _sample_frames(2049, 2544, 5))

    def test_provider_response_records_traceable_roi_and_uncovered_subwindows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            video_path = output_dir / "source.mp4"
            _write_tiny_video(video_path, width=80, height=40, frame_count=30)
            client = _FakeLocalizationClient(
                {
                    "reason": "The ball is visible near the right corner in the sampled crop.",
                    "frames": [
                        {
                            "frame": 12,
                            "ball_visible": True,
                            "confidence": 0.91,
                            "visual_evidence": ["white ball appears near the touchline"],
                            "local_search_roi": {
                                "coordinate_space": "original",
                                "frame": 12,
                                "x": 62,
                                "y": 20,
                                "width": 12,
                                "height": 10,
                                "confidence": 0.88,
                            },
                        }
                    ],
                    "coverage": {"covered_subwindows": [{"start_frame": 12, "end_frame": 15, "status": "localized"}]},
                }
            )

            report = write_ai_visual_localization_report(
                output_dir,
                video_path,
                ["10:20:right_corner"],
                client=client,
                model="vision-test",
            )

        self.assertEqual(1, len(client.calls))
        self.assertEqual(80, client.calls[0]["metadata"]["source_video"]["width"])
        self.assertIn("zoom_crop", client.calls[0]["metadata"])
        self.assertIn("zoom_sheet", client.calls[0]["metadata"]["media"])
        self.assertRegex(client.calls[0]["metadata"]["media"]["zoom_sheet_sha256"], r"^[0-9a-f]{64}$")
        request = report["requests"][0]
        self.assertEqual("localized", request["status"])
        self.assertEqual("visual_localization:10_20_right_corner", request["visual_localization_id"])
        roi = request["frames"][0]["local_search_roi"]
        self.assertEqual("image", roi["coordinate_space"])
        self.assertEqual(
            [{"start_frame": 10, "end_frame": 11, "status": "needs_review"}, {"start_frame": 16, "end_frame": 20, "status": "needs_review"}],
            request["coverage"]["uncovered_subwindows"],
        )
        self.assertEqual("localize_ball_roi", request["suggestions"][0]["recommended_action"])
        self.assertEqual(request["visual_localization_id"], request["suggestions"][0]["visual_localization_id"])
        self.assertNotIn("source_packet_id", request["suggestions"][0])
        self.assertEqual("warn", report["summary"]["status"])
        self.assertEqual(2, report["summary"]["uncovered_subwindow_count"])

    def test_legacy_localization_client_without_zoom_argument_still_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            video_path = output_dir / "source.mp4"
            _write_tiny_video(video_path, width=80, height=40, frame_count=30)
            client = _LegacyLocalizationClient(
                {
                    "reason": "Needs review.",
                    "frames": [],
                    "coverage": {"covered_subwindows": []},
                }
            )

            report = write_ai_visual_localization_report(
                output_dir,
                video_path,
                ["10:20:right_corner"],
                client=client,
                model="vision-test",
            )

        self.assertEqual(1, len(client.calls))
        self.assertIn("zoom_crop", client.calls[0]["metadata"])
        self.assertNotIn("zoom_sheet_data_url", client.calls[0])
        self.assertEqual("needs_review", report["requests"][0]["status"])

    def test_out_of_bounds_roi_is_rejected_without_silent_clamping(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            video_path = output_dir / "source.mp4"
            _write_tiny_video(video_path, width=64, height=36, frame_count=20)
            client = _FakeLocalizationClient(
                {
                    "reason": "The crop hints at a ball, but the proposed ROI is outside the decoded frame.",
                    "frames": [
                        {
                            "frame": 8,
                            "ball_visible": True,
                            "confidence": 0.80,
                            "visual_evidence": ["candidate near right edge"],
                            "local_search_roi": {
                                "coordinate_space": "image",
                                "frame": 8,
                                "x": 60,
                                "y": 10,
                                "width": 12,
                                "height": 8,
                                "confidence": 0.80,
                            },
                        }
                    ],
                    "coverage": {"covered_subwindows": []},
                }
            )
            client.response["coverage"] = {"covered_subwindows": [{"start_frame": 8, "end_frame": 10, "status": "localized"}]}

            report = write_ai_visual_localization_report(
                output_dir,
                video_path,
                ["5:12:right_corner"],
                client=client,
                model="vision-test",
            )

        request = report["requests"][0]
        self.assertEqual("needs_review", request["status"])
        self.assertEqual(1, request["invalid_roi_count"])
        self.assertEqual("rejected", request["frames"][0]["roi_status"])
        self.assertIsNone(request["frames"][0]["local_search_roi"])
        self.assertEqual([], request["coverage"]["covered_subwindows"])
        self.assertEqual([{"start_frame": 5, "end_frame": 12, "status": "needs_review"}], request["coverage"]["uncovered_subwindows"])
        self.assertNotIn("suggestions", request)
        self.assertEqual("request_targeted_localization", request["requested_action"]["recommended_action"])
        self.assertNotIn("source_packet_id", request["requested_action"])
        self.assertEqual("warn", report["summary"]["status"])

    def test_openai_visual_localization_client_sends_schema_and_images(self) -> None:
        response_client = _CapturingResponsesClient(
            {
                "reason": "visible",
                "frames": [],
                "coverage": {"covered_subwindows": []},
            }
        )
        client = OpenAIVisualLocalizationClient(response_client)

        client.localize_window(
            metadata={"visual_localization_id": "visual_localization:1_2_right"},
            contact_sheet_data_url="data:image/jpeg;base64,contact",
            crop_sheet_data_url="data:image/jpeg;base64,crop",
            zoom_sheet_data_url="data:image/jpeg;base64,zoom",
            model="vision-test",
        )

        call = response_client.calls[0]
        self.assertEqual("vision-test", call["model"])
        self.assertEqual(AI_VISUAL_LOCALIZATION_RESPONSE_SCHEMA, call["json_schema"])
        self.assertEqual(["contact_sheet", "crop_sheet", "zoom_sheet"], [image["label"] for image in call["images"]])
        self.assertIn("zoom", f"{call['instructions']} {call['prompt']}")
        self.assertIn("local_search_roi", f"{call['instructions']} {call['prompt']}")


def _write_tiny_video(path: Path, *, width: int, height: int, frame_count: int) -> None:
    import cv2
    import numpy as np

    writer = cv2.VideoWriter(str(path), cv2.VideoWriter.fourcc(*"mp4v"), 10.0, (width, height))
    if not writer.isOpened():
        raise RuntimeError("OpenCV VideoWriter could not create a tiny test video.")
    for index in range(frame_count):
        frame = np.zeros((height, width, 3), dtype=np.uint8)
        frame[:, :] = (index * 5 % 255, 30, 80)
        writer.write(frame)
    writer.release()


class _UnreadableCapture:
    def __init__(self, *, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self.released = False

    def isOpened(self) -> bool:
        return True

    def read(self) -> tuple[bool, object | None]:
        return False, None

    def get(self, prop: int) -> float:
        import cv2

        if prop == cv2.CAP_PROP_FRAME_WIDTH:
            return float(self.width)
        if prop == cv2.CAP_PROP_FRAME_HEIGHT:
            return float(self.height)
        return 0.0

    def release(self) -> None:
        self.released = True


if __name__ == "__main__":
    unittest.main()
