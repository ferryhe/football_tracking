from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from football_tracking.config import load_config
from football_tracking.missing_ball_candidate_executor import (
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


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


if __name__ == "__main__":
    unittest.main()
