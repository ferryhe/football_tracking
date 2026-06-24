from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from football_tracking.config import load_config
from football_tracking.high_recall_reconcile import read_track_csv
from football_tracking.missing_ball_candidate_executor import (
    apply_localize_recovery_stitches,
    execute_missing_ball_candidate,
    missing_ball_candidate_output_dir,
    select_missing_ball_recovery_actions,
)


class MissingBallCandidateExecutorTests(unittest.TestCase):
    def test_select_recovery_actions_rejects_unknown_and_duplicate_ids_before_execution(self) -> None:
        artifact = {
            "approved_actions": [
                _approval("approval_1", candidate_id="candidate_1", start=10, end=20),
                _approval("approval_1", candidate_id="candidate_1", start=30, end=40),
            ]
        }

        with self.assertRaisesRegex(ValueError, "Duplicate approved action IDs"):
            select_missing_ball_recovery_actions(artifact, ["approval_1"])

        with self.assertRaisesRegex(ValueError, "Approved action IDs not found"):
            select_missing_ball_recovery_actions({"approved_actions": []}, ["missing"])

    def test_candidate_output_dir_rejects_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)

            with self.assertRaisesRegex(ValueError, "candidate_id"):
                missing_ball_candidate_output_dir(output_dir, "../escape")

    def test_execute_writes_candidate_artifacts_and_keeps_baseline_hash_stable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            config_path = _write_config(root)
            output_dir = root / "outputs" / "baseline"
            output_dir.mkdir(parents=True)
            _write_lost_tracks(output_dir, start=2049, end=2544)
            _write_packet(output_dir, packet_id="packet_2079", start=2049, end=2544)
            approval = _approval("approval_2079", candidate_id="candidate_2079", start=2049, end=2544)
            before_hash = _sha256(output_dir / "ball_track.csv")

            def fake_runner(config, **_: object) -> dict[str, object]:
                rows = ["Frame,X,Y,Confidence,Status"]
                rows.extend(f"{frame},5700,1390,0.9000,Detected" for frame in range(2049, 2545))
                (config.output_dir / "ball_track.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
                (config.output_dir / "ball_track.cleaned.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
                return {
                    "windows": [{"approval_id": "approval_2079", "start_frame": 2049, "end_frame": 2544}],
                    "execution": {"status": "succeeded"},
                }

            report = execute_missing_ball_candidate(
                output_dir,
                {"approved_actions": [approval]},
                config_path=config_path,
                input_video=root / "data" / "input.mp4",
                source_total_frames=3000,
                runner=fake_runner,
            )

            candidate_dir = output_dir / "ai_candidates" / "missing_ball" / "candidate_2079"
            self.assertEqual("candidate_2079", report["candidate_id"])
            for name in (
                "ball_track.csv",
                "ball_track.cleaned.csv",
                "ball_audit.json",
                "metrics_report.json",
                "run_manifest.json",
                "candidate_manifest.json",
                "missing_ball_recovery_comparison.json",
            ):
                self.assertTrue((candidate_dir / name).exists(), name)
            self.assertEqual(before_hash, _sha256(output_dir / "ball_track.csv"))
            registry = json.loads((output_dir / "ai_candidate_registry.json").read_text(encoding="utf-8"))
            self.assertEqual("candidate_2079", registry["candidates"][0]["candidate_id"])

    def test_execute_parent_mutation_after_candidate_tracks_cleans_candidate_without_registry_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            config_path = _write_config(root)
            output_dir = root / "outputs" / "baseline"
            output_dir.mkdir(parents=True)
            _write_lost_tracks(output_dir, start=2049, end=2544)
            _write_packet(output_dir, packet_id="packet_2079", start=2049, end=2544)
            approval = _approval("approval_2079", candidate_id="candidate_2079", start=2049, end=2544)

            def mutating_runner(config, **_: object) -> dict[str, object]:
                rows = ["Frame,X,Y,Confidence,Status"]
                rows.extend(f"{frame},5700,1390,0.9000,Detected" for frame in range(2049, 2545))
                (config.output_dir / "ball_track.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
                (config.output_dir / "ball_track.cleaned.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
                (output_dir / "ball_track.csv").write_text(
                    "Frame,X,Y,Confidence,Status\n2049,1,2,0.1000,Detected\n",
                    encoding="utf-8",
                )
                return {
                    "windows": [{"approval_id": "approval_2079", "start_frame": 2049, "end_frame": 2544}],
                    "execution": {"status": "succeeded"},
                }

            with self.assertRaisesRegex(RuntimeError, "Parent run artifact changed"):
                execute_missing_ball_candidate(
                    output_dir,
                    {"approved_actions": [approval]},
                    config_path=config_path,
                    input_video=root / "data" / "input.mp4",
                    source_total_frames=3000,
                    runner=mutating_runner,
                )

            candidate_dir = output_dir / "ai_candidates" / "missing_ball" / "candidate_2079"
            self.assertFalse(candidate_dir.exists())
            registry_path = output_dir / "ai_candidate_registry.json"
            if registry_path.exists():
                registry = json.loads(registry_path.read_text(encoding="utf-8"))
                self.assertNotIn("candidate_2079", [item.get("candidate_id") for item in registry.get("candidates", [])])

    def test_execute_rejects_broad_full_video_localize_before_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            config_path = _write_config(root)
            output_dir = root / "outputs" / "baseline"
            output_dir.mkdir(parents=True)
            _write_lost_tracks(output_dir, start=0, end=2)
            _write_packet(output_dir, packet_id="packet_full", start=0, end=2)
            approval = _approval(
                "approval_full",
                candidate_id="candidate_full",
                start=0,
                end=2,
                approved_action="localize_ball_roi",
            )

            with self.assertRaisesRegex(ValueError, "full-video localize_ball_roi"):
                execute_missing_ball_candidate(
                    output_dir,
                    {"approved_actions": [approval]},
                    config_path=config_path,
                    input_video=root / "data" / "input.mp4",
                    source_total_frames=3,
                    runner=lambda *_args, **_kwargs: {},
                )

            self.assertFalse((output_dir / "ai_candidates" / "missing_ball" / "candidate_full").exists())

    def test_execute_accepts_and_carries_visual_localization_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            config_path = _write_config(root)
            output_dir = root / "outputs" / "baseline"
            output_dir.mkdir(parents=True)
            _write_lost_tracks(output_dir, start=2049, end=2544)
            _write_packet(output_dir, packet_id="packet_2079", start=2049, end=2544)
            _write_json(
                output_dir / "ai_visual_localization.json",
                {
                    "requests": [
                        {
                            "visual_localization_id": "visual_localization:2049_2544_right_corner",
                            "source_packet_id": "packet_2079",
                        }
                    ]
                },
            )
            approval = _approval(
                "approval_2079",
                candidate_id="candidate_2079",
                start=2049,
                end=2544,
                approved_action="localize_ball_roi",
            )
            approval.pop("source_packet_id", None)
            approval["visual_localization_id"] = "visual_localization:2049_2544_right_corner"

            def fake_runner(config, **_: object) -> dict[str, object]:
                rows = ["Frame,X,Y,Confidence,Status"]
                rows.extend(f"{frame},4700,1020,0.9000,Detected" for frame in range(2049, 2545))
                (config.output_dir / "ball_track.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
                (config.output_dir / "ball_track.cleaned.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
                return {
                    "windows": [{"approval_id": "approval_2079", "start_frame": 2049, "end_frame": 2544}],
                    "execution": {"status": "succeeded"},
                }

            execute_missing_ball_candidate(
                output_dir,
                {"approved_actions": [approval]},
                config_path=config_path,
                input_video=root / "data" / "input.mp4",
                source_total_frames=3000,
                runner=fake_runner,
            )

            candidate_dir = output_dir / "ai_candidates" / "missing_ball" / "candidate_2079"
            self.assertTrue((candidate_dir / "ai_visual_localization.json").exists())
            manifest = json.loads((candidate_dir / "candidate_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(
                ["visual_localization:2049_2544_right_corner"],
                manifest["evidence_ids"]["visual_localization_ids"],
            )

    def test_execute_localize_candidate_stitches_roi_child_track_and_records_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            config_path = _write_config(root)
            output_dir = root / "outputs" / "baseline"
            output_dir.mkdir(parents=True)
            _write_parent_track_with_false_point(output_dir, start=2121, end=2132)
            _write_packet(output_dir, packet_id="packet_full", start=2121, end=2132)
            approval = _approval(
                "approval_roi",
                candidate_id="candidate_roi",
                start=2121,
                end=2132,
                approved_action="localize_ball_roi",
            )
            approval["local_search_roi"] = {
                "coordinate_space": "image",
                "frame": 2121,
                "x": 100,
                "y": 200,
                "width": 100,
                "height": 100,
                "confidence": 0.9,
            }
            approval["match_ball_confirmed"] = True

            def fake_runner(config, **_: object) -> dict[str, object]:
                window_dir = config.output_dir / "high_recall_windows" / "window_000"
                window_dir.mkdir(parents=True)
                rows = ["Frame,X,Y,Confidence,Status"]
                rows.extend(
                    f"{frame},{140 + frame - 2121},240,0.9000,Detected"
                    for frame in range(2121, 2133)
                )
                (window_dir / "ball_track.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
                return {
                    "windows": [
                        {
                            "approval_id": "approval_roi",
                            "approved_action": "localize_ball_roi",
                            "start_frame": 2121,
                            "end_frame": 2132,
                            "execution_window_index": 0,
                            "effective_roi": [100, 200, 200, 300],
                        }
                    ],
                    "execution": {"status": "succeeded"},
                }

            report = execute_missing_ball_candidate(
                output_dir,
                {"approved_actions": [approval]},
                config_path=config_path,
                input_video=root / "data" / "input.mp4",
                source_total_frames=3000,
                runner=fake_runner,
            )

            candidate_dir = output_dir / "ai_candidates" / "missing_ball" / "candidate_roi"
            by_frame = {int(row["Frame"]): row for row in read_track_csv(candidate_dir / "ball_track.csv")}
            manifest = json.loads((candidate_dir / "candidate_manifest.json").read_text(encoding="utf-8"))
            stitch_report = json.loads((candidate_dir / "recovery_stitch_report.json").read_text(encoding="utf-8"))

        self.assertEqual("pass", report["comparison_status"])
        self.assertEqual("140.0", by_frame[2121]["X"])
        self.assertEqual("151.0", by_frame[2132]["X"])
        self.assertEqual("pass", stitch_report["summary"]["status"])
        self.assertIn(
            "ai_candidates/missing_ball/candidate_roi/recovery_stitch_report.json",
            report["candidate_artifacts"],
        )
        self.assertEqual(
            "ai_candidates/missing_ball/candidate_roi/recovery_stitch_report.json",
            manifest["stitch_report"],
        )

    def test_execute_localize_candidate_records_failed_stitch_when_child_track_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            config_path = _write_config(root)
            output_dir = root / "outputs" / "baseline"
            output_dir.mkdir(parents=True)
            _write_lost_tracks(output_dir, start=2121, end=2132)
            _write_packet(output_dir, packet_id="packet_full", start=2121, end=2132)
            approval = _approval(
                "approval_roi",
                candidate_id="candidate_roi_missing",
                start=2121,
                end=2132,
                approved_action="localize_ball_roi",
            )
            approval["match_ball_confirmed"] = True

            def fake_runner(_config, **_: object) -> dict[str, object]:
                return {
                    "windows": [
                        {
                            "approval_id": "approval_roi",
                            "approved_action": "localize_ball_roi",
                            "start_frame": 2121,
                            "end_frame": 2132,
                            "execution_window_index": 0,
                            "effective_roi": [10, 10, 30, 30],
                        }
                    ],
                    "execution": {"status": "succeeded"},
                }

            report = execute_missing_ball_candidate(
                output_dir,
                {"approved_actions": [approval]},
                config_path=config_path,
                input_video=root / "data" / "input.mp4",
                source_total_frames=3000,
                runner=fake_runner,
            )

            candidate_dir = output_dir / "ai_candidates" / "missing_ball" / "candidate_roi_missing"
            stitch_report = json.loads((candidate_dir / "recovery_stitch_report.json").read_text(encoding="utf-8"))

        self.assertEqual("fail", report["comparison_status"])
        self.assertEqual("fail", stitch_report["summary"]["status"])
        self.assertIn("child_track_missing", stitch_report["windows"][0]["metrics"]["blocking_reasons"])

    def test_failed_direct_candidate_track_stitch_does_not_overwrite_candidate_csv(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            parent_dir = root / "parent"
            candidate_dir = root / "candidate"
            parent_dir.mkdir()
            candidate_dir.mkdir()
            _write_lost_tracks(parent_dir, start=10, end=21)
            candidate_rows = ["Frame,X,Y,Confidence,Status"]
            candidate_rows.extend(f"{frame},900,900,0.9000,Detected" for frame in range(10, 22))
            candidate_text = "\n".join(candidate_rows) + "\n"
            (candidate_dir / "ball_track.csv").write_text(candidate_text, encoding="utf-8")

            apply_localize_recovery_stitches(
                parent_output_dir=parent_dir,
                candidate_output_dir=candidate_dir,
                selected_artifact={
                    "approved_actions": [
                        {
                            "approval_id": "approval_roi",
                            "improvement_id": "imp_roi",
                            "candidate_id": "candidate_roi",
                            "approved_action": "localize_ball_roi",
                            "source_packet_id": "packet_roi",
                            "start_frame": 10,
                            "end_frame": 21,
                            "local_search_roi": {
                                "coordinate_space": "image",
                                "frame": 10,
                                "x": 10,
                                "y": 10,
                                "width": 20,
                                "height": 20,
                            },
                        }
                    ]
                },
                csv_name="ball_track.csv",
                high_recall_report={
                    "windows": [
                        {
                            "approval_id": "approval_roi",
                            "approved_action": "localize_ball_roi",
                            "start_frame": 10,
                            "end_frame": 21,
                            "effective_roi": [10, 10, 30, 30],
                        }
                    ]
                },
            )

            stitch_report = json.loads((candidate_dir / "recovery_stitch_report.json").read_text(encoding="utf-8"))
            after_text = (candidate_dir / "ball_track.csv").read_text(encoding="utf-8")

        self.assertEqual(candidate_text, after_text)
        self.assertEqual("fail", stitch_report["summary"]["status"])


def _write_config(root: Path) -> Path:
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "weights").mkdir(parents=True, exist_ok=True)
    (root / "data" / "input.mp4").write_text("fake", encoding="utf-8")
    (root / "weights" / "football_ball_yolo.pt").write_text("fake", encoding="utf-8")
    config_path = root / "config.yaml"
    config_path.write_text(
        "\n".join(
            [
                f"input_video: {str(root / 'data' / 'input.mp4')}",
                f"output_dir: {str(root / 'outputs' / 'baseline')}",
                "detector:",
                f"  model_path: {str(root / 'weights' / 'football_ball_yolo.pt')}",
                "postprocess:",
                "  enabled: true",
                "follow_cam:",
                "  enabled: true",
                "temporal_chunks:",
                "  enabled: false",
                "high_recall_windows:",
                "  enabled: false",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    load_config(config_path)
    return config_path


def _approval(
    approval_id: str,
    *,
    candidate_id: str,
    start: int,
    end: int,
    approved_action: str = "targeted_rerun",
) -> dict[str, object]:
    approval: dict[str, object] = {
        "approval_id": approval_id,
        "improvement_id": f"imp_{approval_id}",
        "candidate_id": candidate_id,
        "approved_action": approved_action,
        "source_packet_id": "packet_2079" if start == 2049 else "packet_full",
        "rerun_scope": {"start_frame": start, "end_frame": end},
    }
    if approved_action == "localize_ball_roi":
        approval.update(
            {
                "start_frame": start,
                "end_frame": end,
                "local_search_roi": {
                    "coordinate_space": "image",
                    "frame": start,
                    "x": 10,
                    "y": 10,
                    "width": 20,
                    "height": 20,
                    "confidence": 0.8,
                },
            }
        )
    return approval


def _write_lost_tracks(output_dir: Path, *, start: int, end: int) -> None:
    rows = ["Frame,X,Y,Confidence,Status"]
    rows.extend(f"{frame},,,0.0000,Lost" for frame in range(start, end + 1))
    text = "\n".join(rows) + "\n"
    (output_dir / "ball_track.csv").write_text(text, encoding="utf-8")
    (output_dir / "ball_track.cleaned.csv").write_text(text, encoding="utf-8")


def _write_parent_track_with_false_point(output_dir: Path, *, start: int, end: int) -> None:
    rows = ["Frame,X,Y,Confidence,Status"]
    rows.append(f"{start},5000,1400,0.9000,Detected")
    rows.extend(f"{frame},,,0.0000,Lost" for frame in range(start + 1, end + 1))
    text = "\n".join(rows) + "\n"
    (output_dir / "ball_track.csv").write_text(text, encoding="utf-8")
    (output_dir / "ball_track.cleaned.csv").write_text(text, encoding="utf-8")


def _write_packet(output_dir: Path, *, packet_id: str, start: int, end: int) -> None:
    payload = {
        "packets": [
            {
                "packet_id": packet_id,
                "window": {"start_frame": start, "end_frame": end},
                "decision": {"label": "needs_ai_review"},
            }
        ]
    }
    (output_dir / "review_packets.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


if __name__ == "__main__":
    unittest.main()
