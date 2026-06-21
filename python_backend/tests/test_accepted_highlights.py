from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from football_tracking.accepted_highlights import (
    compact_accepted_highlights_summary,
    write_accepted_highlights_report,
)


class AcceptedHighlightsTests(unittest.TestCase):
    def test_accepted_copy_only_copies_qualified_clips_and_skips_missing_clips(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_ai_review(
                output_dir,
                [
                    _review("packet_keep", "accept_highlight", True, "keep_highlight"),
                    _review("packet_not_publishable", "accept_highlight", False, "keep_highlight"),
                    _review("packet_wrong_action", "accept_highlight", True, "send_to_human"),
                    _review("packet_human", "needs_human_review", True, "keep_highlight"),
                    _review("packet_missing", "accept_highlight", True, "keep_highlight"),
                ],
            )
            highlights_dir = output_dir / "highlights"
            highlights_dir.mkdir()
            for packet_id in ["packet_keep", "packet_not_publishable", "packet_wrong_action", "packet_human"]:
                (highlights_dir / f"{packet_id}.mp4").write_bytes(f"clip:{packet_id}".encode("ascii"))

            report = write_accepted_highlights_report(output_dir)

            copied_path = output_dir / "highlights_ai_accepted" / "packet_keep.mp4"
            skipped_ids = {item["packet_id"] for item in report["skipped"]}
            compact = compact_accepted_highlights_summary(report)

            self.assertTrue(copied_path.exists())
            self.assertEqual(b"clip:packet_keep", copied_path.read_bytes())
            self.assertFalse((output_dir / "highlights_ai_accepted" / "packet_not_publishable.mp4").exists())
            self.assertFalse((output_dir / "highlights_ai_accepted" / "packet_wrong_action.mp4").exists())
            self.assertEqual(1, report["summary"]["copied_count"])
            self.assertEqual(1, report["summary"]["skipped_count"])
            self.assertEqual(0, report["summary"]["error_count"])
            self.assertEqual({"packet_missing"}, skipped_ids)
            self.assertEqual(report["summary"]["copied_count"], compact["copied_count"])
            self.assertTrue((output_dir / "highlights_ai_accepted" / "ai_accepted_highlights_report.json").exists())

    def test_accepted_copy_uses_review_packet_media_clip_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            packet_dir = output_dir / "review_packets" / "packet_keep"
            packet_dir.mkdir(parents=True)
            packet_clip = packet_dir / "packet_keep.mp4"
            packet_clip.write_bytes(b"review-packet-clip")
            review = _review("packet_keep", "accept_highlight", True, "keep_highlight")
            review["media"] = {"clip": str(packet_clip)}
            _write_ai_review(output_dir, [review])

            report = write_accepted_highlights_report(output_dir)

            copied_path = output_dir / "highlights_ai_accepted" / "packet_keep.mp4"
            self.assertEqual(1, report["summary"]["copied_count"])
            self.assertEqual(b"review-packet-clip", copied_path.read_bytes())

    def test_accepted_copy_accepts_output_relative_media_clip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            packet_dir = output_dir / "review_packets" / "packet_keep"
            packet_dir.mkdir(parents=True)
            packet_clip = packet_dir / "packet_keep.mp4"
            packet_clip.write_bytes(b"output-relative-clip")
            review = _review("packet_keep", "accept_highlight", True, "keep_highlight")
            review["media"] = {"clip": str(packet_clip.relative_to(output_dir))}
            _write_ai_review(output_dir, [review])

            report = write_accepted_highlights_report(output_dir)

            copied_path = output_dir / "highlights_ai_accepted" / "packet_keep.mp4"
            self.assertEqual(1, report["summary"]["copied_count"])
            self.assertEqual(b"output-relative-clip", copied_path.read_bytes())

    def test_accepted_copy_accepts_repo_root_relative_media_clip(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory(dir=repo_root) as temp_name:
            output_dir = Path(temp_name)
            packet_dir = output_dir / "review_packets" / "packet_keep"
            packet_dir.mkdir(parents=True)
            packet_clip = packet_dir / "packet_keep.mp4"
            packet_clip.write_bytes(b"repo-relative-clip")
            review = _review("packet_keep", "accept_highlight", True, "keep_highlight")
            review["media"] = {"clip": str(packet_clip.resolve().relative_to(repo_root.resolve()))}
            _write_ai_review(output_dir, [review])

            report = write_accepted_highlights_report(output_dir)

            copied_path = output_dir / "highlights_ai_accepted" / "packet_keep.mp4"
            self.assertEqual(1, report["summary"]["copied_count"])
            self.assertEqual(b"repo-relative-clip", copied_path.read_bytes())

    def test_accepted_copy_removes_stale_generated_mp4_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            accepted_dir = output_dir / "highlights_ai_accepted"
            accepted_dir.mkdir()
            stale_clip = accepted_dir / "old_packet.mp4"
            stale_clip.write_bytes(b"old")
            keep_note = accepted_dir / "note.txt"
            keep_note.write_text("keep", encoding="utf-8")
            _write_ai_review(output_dir, [])

            report = write_accepted_highlights_report(output_dir)

            self.assertFalse(stale_clip.exists())
            self.assertTrue(keep_note.exists())
            self.assertEqual(1, report["summary"]["stale_removed_count"])
            self.assertEqual(0, report["summary"]["copied_count"])

    def test_accepted_dir_path_traversal_and_absolute_paths_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_ai_review(output_dir, [])

            with self.assertRaises(ValueError):
                write_accepted_highlights_report(output_dir, accepted_dir_name="../outside")

            with self.assertRaises(ValueError):
                write_accepted_highlights_report(output_dir, accepted_dir_name=".")

            with self.assertRaises(ValueError):
                write_accepted_highlights_report(output_dir, accepted_dir_name=str(output_dir.parent / "outside"))


def _review(
    packet_id: str,
    verdict: str,
    highlight_publishable: bool,
    recommended_action: str,
) -> dict[str, object]:
    return {
        "packet_id": packet_id,
        "packet_label": "highlight_worthy",
        "review": {
            "verdict": verdict,
            "confidence": 0.8,
            "reason": "fixture",
            "match_ball_visible": "yes",
            "marker_alignment": "good",
            "highlight_publishable": highlight_publishable,
            "recommended_action": recommended_action,
        },
    }


def _write_ai_review(output_dir: Path, reviews: list[dict[str, object]]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "ai_visual_review.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "generated_at": "2026-01-01T00:00:00+00:00",
                "summary": {"packet_count": len(reviews)},
                "reviews": reviews,
                "errors": [],
            }
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    unittest.main()
