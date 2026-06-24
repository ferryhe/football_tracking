from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from football_tracking.highlight_candidate_comparison import write_highlight_candidate_comparison


class HighlightCandidateComparisonTests(unittest.TestCase):
    def test_tail_safe_candidate_passes_and_records_window_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            baseline = root / "baseline"
            candidate = root / "candidate"
            self.write_event_candidates(baseline, source_frames=100)
            self.write_highlight_report(candidate, start=35, end=85, frame_count=51, fps=10.0)

            report_path = write_highlight_candidate_comparison(
                candidate,
                baseline_dir=baseline,
                candidate_id="highlight-candidate-1",
                approval=self.approval(start=35, end=85),
            )
            payload = self.read_json(report_path)

        self.assertEqual("pass", payload["comparison_status"])
        self.assertEqual("event-1", payload["event_candidate_id"])
        self.assertEqual({"start_frame": 40, "end_frame": 50}, payload["core_window"])
        self.assertEqual({"start_frame": 30, "end_frame": 80}, payload["baseline_render_window"])
        self.assertEqual({"start_frame": 35, "end_frame": 85}, payload["suggested_window"])
        self.assertEqual({"start_frame": 35, "end_frame": 85}, payload["render_window"])
        self.assertEqual(10, payload["default_pre_buffer_frames"])
        self.assertEqual(30, payload["default_post_buffer_frames"])
        self.assertEqual(-5, payload["pre_frame_delta"])
        self.assertEqual(5, payload["post_frame_delta"])
        self.assertFalse(payload["source_end_clamp"])
        self.assertEqual("preserved", payload["tail_status"])
        self.assertEqual(51, payload["frame_count"])
        self.assertEqual(5.1, payload["duration_seconds"])

    def test_invalid_highlight_windows_fail_named_checks(self) -> None:
        cases = [
            (
                "missing_event_id",
                {"candidate_id": "highlight-candidate-1", "approved_action": "render_suggested_highlight"},
                35,
                85,
                51,
                "event_candidate_linkage",
            ),
            (
                "invalid_window",
                self.approval(start=86, end=85),
                86,
                85,
                0,
                "suggested_window_valid",
            ),
            (
                "cut_core",
                self.approval(start=45, end=85),
                45,
                85,
                41,
                "core_window_preserved",
            ),
            (
                "cut_available_tail",
                self.approval(start=35, end=70),
                35,
                70,
                36,
                "tail_preserved",
            ),
            (
                "frame_count_mismatch",
                self.approval(start=35, end=85),
                35,
                85,
                50,
                "frame_count_match",
            ),
        ]
        for name, approval, start, end, frame_count, failed_check in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp_name:
                root = Path(temp_name)
                baseline = root / "baseline"
                candidate = root / "candidate"
                self.write_event_candidates(baseline, source_frames=100)
                self.write_highlight_report(candidate, start=start, end=end, frame_count=frame_count, fps=10.0)

                report_path = write_highlight_candidate_comparison(
                    candidate,
                    baseline_dir=baseline,
                    candidate_id="highlight-candidate-1",
                    approval=approval,
                )
                payload = self.read_json(report_path)

            self.assertEqual("fail", payload["comparison_status"])
            self.assertEqual("fail", self.check(payload, failed_check)["status"])

    def test_source_end_clamp_allows_tail_that_reaches_last_source_frame(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            baseline = root / "baseline"
            candidate = root / "candidate"
            self.write_event_candidates(baseline, source_frames=75)
            self.write_highlight_report(candidate, start=35, end=74, frame_count=40, fps=10.0)

            report_path = write_highlight_candidate_comparison(
                candidate,
                baseline_dir=baseline,
                candidate_id="highlight-candidate-1",
                approval=self.approval(start=35, end=90),
            )
            payload = self.read_json(report_path)

        self.assertEqual("pass", payload["comparison_status"])
        self.assertTrue(payload["source_end_clamp"])
        self.assertEqual({"start_frame": 35, "end_frame": 74}, payload["render_window"])
        self.assertEqual("source_end_clamped", payload["tail_status"])
        self.assertEqual(40, payload["frame_count"])

    def approval(self, *, start: int, end: int) -> dict[str, object]:
        return {
            "approval_id": "highlight_1",
            "candidate_id": "highlight-candidate-1",
            "event_candidate_id": "event-1",
            "approved_action": "render_suggested_highlight",
            "suggested_window": {"start_frame": start, "end_frame": end},
        }

    def write_event_candidates(self, output_dir: Path, *, source_frames: int) -> None:
        self.write_json(
            output_dir / "event_candidates.json",
            {
                "summary": {"total_source_frames": source_frames},
                "candidates": [
                    {
                        "id": "event-1",
                        "core_window": {"start_frame": 40, "end_frame": 50},
                        "render_window": {"start_frame": 30, "end_frame": 80},
                        "buffer_policy": {
                            "pre_buffer_frames": 10,
                            "post_buffer_frames": 30,
                            "min_tail_frames": 30,
                        },
                    }
                ],
            },
        )

    def write_highlight_report(self, output_dir: Path, *, start: int, end: int, frame_count: int, fps: float) -> None:
        self.write_json(
            output_dir / "highlight_report.json",
            {
                "schema_version": "1.0",
                "candidate_id": "highlight-candidate-1",
                "event_candidate_id": "event-1",
                "window": {"start_frame": start, "end_frame": end},
                "renderer": {"frame_count": frame_count, "fps": fps},
            },
        )

    def check(self, payload: dict[str, object], name: str) -> dict[str, object]:
        checks = payload["checks"]
        self.assertIsInstance(checks, list)
        for check in checks:
            if isinstance(check, dict) and check.get("name") == name:
                return check
        raise AssertionError(f"missing check {name}")

    def read_json(self, path: Path) -> dict[str, object]:
        return json.loads(path.read_text(encoding="utf-8"))

    def write_json(self, path: Path, payload: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
