from __future__ import annotations

import importlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any


def _module():
    try:
        return importlib.import_module("football_tracking.ai_review_triggers")
    except ModuleNotFoundError as exc:
        raise AssertionError("football_tracking.ai_review_triggers module is missing") from exc


def _write_ball_audit(output_dir: Path, payload: dict[str, Any]) -> None:
    (output_dir / "ball_audit.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _audit_report(
    *,
    summary: dict[str, Any] | None = None,
    review_events: list[dict[str, Any]] | None = None,
    tracklets: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "generated_at": "2026-01-01T00:00:00+00:00",
        "summary": {
            "frame_count": 100,
            "source_count": 1,
            "tracklet_count": len(tracklets or []),
            "suspicious_tracklet_count": sum(1 for item in tracklets or [] if item.get("suspicion_score", 0) >= 0.35),
            "review_event_count": len(review_events or []),
            "lost_gap_count": sum(1 for item in review_events or [] if item.get("type") == "lost_gap"),
            "max_step_px": 250.0,
            **(summary or {}),
        },
        "sources": [{"name": "raw", "path": "ball_track.csv", "row_count": 100, "tracklet_count": len(tracklets or [])}],
        "tracklets": tracklets or [],
        "review_events": review_events or [],
    }


def _event(
    event_type: str,
    start_frame: int,
    end_frame: int,
    frame_count: int | None = None,
    *,
    source: str = "raw",
    reason: str | None = None,
) -> dict[str, Any]:
    return {
        "source": source,
        "type": event_type,
        "severity": "warn",
        "start_frame": start_frame,
        "end_frame": end_frame,
        "frame_count": frame_count if frame_count is not None else end_frame - start_frame + 1,
        "reason": reason or f"{event_type} requires review",
        "evidence": {"event_type": event_type},
    }


def _tracklet(tracklet_id: str, start_frame: int, end_frame: int, suspicion_score: float) -> dict[str, Any]:
    return {
        "id": tracklet_id,
        "source": "raw",
        "start_frame": start_frame,
        "end_frame": end_frame,
        "length": end_frame - start_frame + 1,
        "status_counts": {"Detected": end_frame - start_frame + 1},
        "mean_confidence": 0.4,
        "start_point": {"x": 10.0, "y": 20.0},
        "end_point": {"x": 30.0, "y": 40.0},
        "max_step_px": 10.0,
        "flags": ["low_confidence"],
        "suspicion_score": suspicion_score,
    }


class AIReviewTriggerTests(unittest.TestCase):
    def test_missing_ball_audit_returns_stable_no_review_report(self) -> None:
        module = _module()
        with tempfile.TemporaryDirectory() as temp_name:
            report = module.build_ai_review_trigger_report(Path(temp_name))

        self.assertFalse(report["decision"]["needs_ai_review"])
        self.assertEqual("none", report["decision"]["priority"])
        self.assertEqual("ball_audit_missing", report["decision"]["reason"])
        self.assertEqual([], report["triggers"])
        self.assertEqual("none", report["summary"]["max_trigger_priority"])

    def test_invalid_ball_audit_returns_stable_no_review_report(self) -> None:
        module = _module()
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            (output_dir / "ball_audit.json").write_text("{", encoding="utf-8")

            report = module.build_ai_review_trigger_report(output_dir)

        self.assertFalse(report["decision"]["needs_ai_review"])
        self.assertEqual("ball_audit_invalid", report["decision"]["reason"])
        self.assertEqual(0, report["decision"]["trigger_count"])

    def test_review_event_rules_assign_expected_priorities(self) -> None:
        module = _module()
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_ball_audit(
                output_dir,
                _audit_report(
                    review_events=[
                        _event("large_jump", 10, 12),
                        _event("candidate_ambiguity", 20, 20),
                        _event("postprocess_action", 30, 32),
                        _event("lost_gap", 40, 43, 4),
                        _event("lost_gap", 50, 59, 10),
                    ],
                ),
            )

            report = module.build_ai_review_trigger_report(output_dir)

        priorities_by_type = {trigger["type"]: trigger["priority"] for trigger in report["triggers"]}
        self.assertEqual("high", priorities_by_type["large_jump"])
        self.assertEqual("medium", priorities_by_type["candidate_ambiguity"])
        self.assertEqual("medium", priorities_by_type["postprocess_action"])
        self.assertIn("low", [trigger["priority"] for trigger in report["triggers"] if trigger["type"] == "lost_gap"])
        self.assertIn("medium", [trigger["priority"] for trigger in report["triggers"] if trigger["type"] == "lost_gap"])
        self.assertTrue(report["decision"]["needs_ai_review"])
        self.assertEqual("high", report["decision"]["priority"])

    def test_suspicious_tracklet_rules_and_exact_duplicate_suppression(self) -> None:
        module = _module()
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_ball_audit(
                output_dir,
                _audit_report(
                    review_events=[_event("large_jump", 10, 12)],
                    tracklets=[
                        _tracklet("raw:10-12", 10, 12, 0.8),
                        _tracklet("raw:20-24", 20, 24, 0.5),
                        _tracklet("raw:40-44", 40, 44, 0.8),
                        _tracklet("raw:60-62", 60, 62, 0.2),
                    ],
                ),
            )

            report = module.build_ai_review_trigger_report(output_dir)

        suspicious = [trigger for trigger in report["triggers"] if trigger["type"] == "suspicious_tracklet"]
        self.assertEqual(
            [(20, 24, "low"), (40, 44, "high")],
            [(trigger["start_frame"], trigger["end_frame"], trigger["priority"]) for trigger in suspicious],
        )
        self.assertEqual(
            1,
            sum(1 for trigger in report["triggers"] if trigger["start_frame"] == 10 and trigger["end_frame"] == 12),
        )

    def test_high_suspicion_tracklet_can_raise_priority_for_same_window_event(self) -> None:
        module = _module()
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_ball_audit(
                output_dir,
                _audit_report(
                    review_events=[_event("candidate_ambiguity", 10, 12)],
                    tracklets=[_tracklet("raw:10-12", 10, 12, 0.8)],
                ),
            )

            report = module.build_ai_review_trigger_report(output_dir)

        self.assertEqual("high", report["decision"]["priority"])
        self.assertTrue(
            any(
                trigger["type"] == "suspicious_tracklet"
                and trigger["priority"] == "high"
                and trigger["start_frame"] == 10
                and trigger["end_frame"] == 12
                for trigger in report["triggers"]
            )
        )

    def test_dense_noise_cluster_and_adjacent_windows_are_merged(self) -> None:
        module = _module()
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_ball_audit(
                output_dir,
                _audit_report(
                    review_events=[
                        _event("candidate_ambiguity", 10, 10),
                        _event("postprocess_action", 12, 13),
                        _event("lost_gap", 16, 18, 3),
                        _event("candidate_ambiguity", 40, 40),
                        _event("postprocess_action", 50, 50),
                    ],
                ),
            )

            report = module.build_ai_review_trigger_report(output_dir)

        self.assertTrue(any(trigger["type"] == "dense_noise_cluster" for trigger in report["triggers"]))
        self.assertEqual("high", report["summary"]["max_trigger_priority"])
        self.assertEqual("high", report["decision"]["priority"])
        self.assertEqual(10, report["decision"]["recommended_review_windows"][0]["start_frame"])
        self.assertEqual(50, report["decision"]["recommended_review_windows"][0]["end_frame"])
        self.assertIn("candidate_ambiguity", report["decision"]["recommended_review_windows"][0]["reason"])
        self.assertIn("dense_noise_cluster", report["decision"]["recommended_review_windows"][0]["reason"])

    def test_short_detected_island_event_creates_noise_review_trigger(self) -> None:
        module = _module()
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_ball_audit(
                output_dir,
                _audit_report(
                    review_events=[
                        _event(
                            "short_tracklet",
                            70,
                            72,
                            3,
                            reason="Short detected island is likely a false-positive ball.",
                        )
                    ],
                ),
            )

            report = module.build_ai_review_trigger_report(output_dir)

        short_triggers = [trigger for trigger in report["triggers"] if trigger["type"] == "short_tracklet"]
        self.assertEqual(1, len(short_triggers))
        self.assertEqual("medium", short_triggers[0]["priority"])
        self.assertTrue(report["decision"]["needs_ai_review"])

    def test_write_report_persists_json_and_compact_summary(self) -> None:
        module = _module()
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_ball_audit(output_dir, _audit_report(review_events=[_event("candidate_ambiguity", 2, 2)]))

            report = module.write_ai_review_trigger_report(output_dir)
            compact = module.compact_ai_review_trigger_summary(report)

            self.assertTrue((output_dir / "ai_review_triggers.json").exists())

        self.assertTrue(compact["needs_ai_review"])
        self.assertEqual("medium", compact["priority"])
        self.assertEqual(1, compact["trigger_count"])
        self.assertEqual(1, compact["recommended_window_count"])


if __name__ == "__main__":
    unittest.main()
