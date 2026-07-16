from __future__ import annotations

import csv
import hashlib
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import cv2
import numpy as np

from football_tracking.detector_candidate_contract import assign_candidate_ids
from football_tracking.global_ball_trajectory import (
    DECISIONS_NAME,
    REPORT_NAME,
    TRACK_NAME,
    GlobalBallTrajectoryError,
    TrajectoryConfig,
    solve_global_ball_trajectory,
)
from football_tracking.tracking_contracts import CLASSIFICATION_LABELS, build_tracking_contract
from football_tracking.types import Candidate

_MODEL_VERSION = hashlib.sha256(b"test-model").hexdigest()
_DATASET_VERSION = hashlib.sha256(b"test-dataset").hexdigest()


class GlobalBallTrajectoryTests(unittest.TestCase):
    def test_short_occlusion_interpolates_and_returns_to_observed_track(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = self._inputs(
                root,
                frame_count=5,
                candidates=[(0, 10.0, 10.0, 0.95), (1, 12.0, 10.0, 0.95), (4, 18.0, 10.0, 0.95)],
            )
            solve_global_ball_trajectory(*inputs, root / "generation", config=TrajectoryConfig(max_interpolation_gap=3))
            rows = self._rows(root / "generation")

        self.assertEqual(["detected", "detected", "interpolated", "interpolated", "detected"], [r["Status"] for r in rows])
        self.assertAlmostEqual(18.0, float(rows[4]["X"]))
        self.assertTrue(all(r["Source"] == "constant_acceleration_kalman" for r in rows[2:4]))

    def test_long_gap_is_unknown_and_trailing_prediction_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = self._inputs(
                root,
                frame_count=10,
                candidates=[(0, 10.0, 10.0, 0.9), (1, 12.0, 10.0, 0.9), (8, 26.0, 10.0, 0.9)],
            )
            solve_global_ball_trajectory(*inputs, root / "generation", config=TrajectoryConfig(max_interpolation_gap=2))
            rows = self._rows(root / "generation")

        self.assertEqual(["unknown"] * 6, [row["Status"] for row in rows[2:8]])
        self.assertEqual("interpolated", rows[9]["Status"])

    def test_second_order_motion_prefers_long_pass_over_jump_noise(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = self._inputs(
                root,
                frame_count=4,
                candidates=[
                    (0, 10.0, 20.0, 0.9),
                    (1, 30.0, 20.0, 0.9),
                    (2, 50.0, 20.0, 0.72),
                    (2, 31.0, 80.0, 0.99),
                    (3, 70.0, 20.0, 0.9),
                ],
                match_probability_by_center={(50.0, 20.0): 0.76, (31.0, 80.0): 0.98},
            )
            solve_global_ball_trajectory(*inputs, root / "generation")
            rows = self._rows(root / "generation")
            decisions = self._decisions(root / "generation")

        self.assertAlmostEqual(50.0, float(rows[2]["X"]))
        selected = [row for row in decisions if row.get("decision") == "selected" and row.get("frame_index") == 2]
        self.assertIn("acceleration", selected[0]["costs"]["edge"])
        self.assertIn("direction", selected[0]["costs"]["edge"])
        rejected = [row for row in decisions if row.get("decision") == "rejected" and row.get("frame_index") == 2]
        self.assertTrue(rejected[0]["costs"]["edge"])
        self.assertIsNotNone(rejected[0]["costs"]["path_total"])
        self.assertIn("counterfactual_delta", rejected[0])

    def test_human_confirmed_noise_is_hard_rejected_and_bare_decision_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = self._inputs(
                root,
                frame_count=2,
                candidates=[(0, 10.0, 10.0, 0.8), (0, 80.0, 80.0, 0.99), (1, 12.0, 10.0, 0.8)],
                classifications={(80.0, 80.0): ("field_line_or_mark", "human_confirmed")},
                decisions={(10.0, 10.0): "reject"},
            )
            solve_global_ball_trajectory(*inputs, root / "generation")
            rows = self._rows(root / "generation")
            decisions = self._decisions(root / "generation")

        self.assertAlmostEqual(10.0, float(rows[0]["X"]))
        self.assertTrue(any(row.get("reason") == "human_confirmed_noise" for row in decisions))
        self.assertTrue(any(row.get("reason") == "unvalidated_selective_decision" for row in decisions))

    def test_legacy_statuses_map_lowercase_and_only_positive_evidence_is_out_of_view(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = self._inputs(root, frame_count=3, candidates=[], frame_statuses=["Lost", "out_of_view", "Predicted"])
            solve_global_ball_trajectory(*inputs, root / "generation")
            rows = self._rows(root / "generation")

        self.assertEqual(["unknown", "out_of_view", "unknown"], [row["Status"] for row in rows])
        self.assertEqual("", rows[1]["X"])
        self.assertEqual("explicit_upstream_out_of_view", rows[1]["Reason"])

    def test_candidate_cap_is_deterministic_and_reports_approximation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            candidates = [(0, float(5 + index * 5), 10.0, 0.5 + index / 100.0) for index in range(10)]
            inputs = self._inputs(root, frame_count=1, candidates=candidates)
            report = solve_global_ball_trajectory(
                *inputs,
                root / "generation",
                config=TrajectoryConfig(candidate_cap_per_frame=3, beam_width=4),
            )
            decisions = self._decisions(root / "generation")

        self.assertTrue(report["algorithm"]["pruned"])
        self.assertEqual("beam_approximation", report["algorithm"]["optimality"])
        self.assertEqual(7, sum(row.get("reason") == "candidate_budget_exceeded" for row in decisions))
        self.assertLessEqual(report["work"]["max_frontier_states"], 4)

    def test_deterministic_rerun_produces_identical_track_and_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = self._inputs(root, frame_count=3, candidates=[(0, 10, 10, 0.8), (1, 12, 10, 0.8)])
            solve_global_ball_trajectory(*inputs, root / "one")
            solve_global_ball_trajectory(*inputs, root / "two")

            for name in (TRACK_NAME, DECISIONS_NAME):
                self.assertEqual((root / "one" / name).read_bytes(), (root / "two" / name).read_bytes())

    def test_prediction_binding_failures_are_closed(self) -> None:
        mutations = {
            "source sha": lambda value: value.__setitem__("source_contract_sha256", "0" * 64),
            "model": lambda value: value["predictions"][0].__setitem__("model_version", "0" * 64),
            "fingerprint": lambda value: value["predictions"][0].__setitem__("candidate_fingerprint", "0" * 64),
            "missing": lambda value: value["predictions"].pop(),
            "duplicate": lambda value: value["predictions"].append(dict(value["predictions"][0])),
            "nan": lambda value: value["predictions"][0]["probabilities"].__setitem__("match_ball", float("nan")),
            "sum": lambda value: value["predictions"][0]["probabilities"].__setitem__("match_ball", 0.4),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp_name:
                root = Path(temp_name)
                source, contract, predictions = self._inputs(root, frame_count=1, candidates=[(0, 10, 10, 0.8)])
                payload = json.loads(predictions.read_text(encoding="utf-8"))
                mutate(payload)
                payload["prediction_count"] = len(payload["predictions"])
                predictions.write_text(json.dumps(payload, allow_nan=True), encoding="utf-8")
                with self.assertRaises(GlobalBallTrajectoryError):
                    solve_global_ball_trajectory(source, contract, predictions, root / "generation")
                self.assertFalse((root / "generation").exists())

    def test_prediction_inference_batch_size_above_128_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source, contract, predictions = self._inputs(root, frame_count=1, candidates=[(0, 10, 10, 0.8)])
            payload = json.loads(predictions.read_text(encoding="utf-8"))
            payload["inference"] = {"device": "cpu", "batch_size": 129}
            predictions.write_text(json.dumps(payload, allow_nan=False), encoding="utf-8")

            with self.assertRaisesRegex(GlobalBallTrajectoryError, "exceeds 128"):
                solve_global_ball_trajectory(source, contract, predictions, root / "generation")
            self.assertFalse((root / "generation").exists())

    def test_json_evidence_rejects_duplicate_keys_and_bounded_record_overflow(self) -> None:
        mutations = {
            "duplicate": (
                lambda text: text.replace('"frame_index":0', '"frame_index":999,"frame_index":0', 1),
                "duplicate JSON key",
            ),
            "large array": (
                lambda text: text.replace('"status":"unknown"', '"status":"unknown","extra":[' + "0," * 5000 + "0]", 1),
                "token bound|item bound",
            ),
            "deep value": (
                lambda text: text.replace(
                    '"status":"unknown"',
                    '"status":"unknown","extra":' + "[" * 20 + "0" + "]" * 20,
                    1,
                ),
                "depth bound",
            ),
            "large string": (
                lambda text: text.replace(
                    '"status":"unknown"',
                    '"status":"unknown","extra":"' + "x" * 70000 + '"',
                    1,
                ),
                "string exceeds length bound",
            ),
        }
        for label, (mutate, error) in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp_name:
                root = Path(temp_name)
                source, contract, predictions = self._inputs(root, frame_count=1, candidates=[])
                contract.write_text(mutate(contract.read_text(encoding="utf-8")), encoding="utf-8")
                self._write_predictions(contract, predictions, [])

                with self.assertRaisesRegex(GlobalBallTrajectoryError, error):
                    solve_global_ball_trajectory(source, contract, predictions, root / "generation")
                self.assertFalse((root / "generation").exists())

    def test_candidate_referencing_absent_frame_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = self._inputs(root, frame_count=2, candidates=[(3, 10, 10, 0.8)], video_frame_count=4)
            with self.assertRaisesRegex(GlobalBallTrajectoryError, "absent frame|cover every source frame"):
                solve_global_ball_trajectory(*inputs, root / "generation")

    def test_unbound_player_tracks_degrade_neutrally_with_audit_reason(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = self._inputs(root, frame_count=1, candidates=[(0, 10, 10, 0.8)])
            player_path = root / "player_tracks.json"
            player_path.write_text(json.dumps({"schema_version": "1.0", "tracks": []}), encoding="utf-8")
            report = solve_global_ball_trajectory(*inputs, root / "generation", player_tracks_path=player_path)

        self.assertEqual("neutral", report["priors"]["player_foot"]["status"])
        self.assertEqual("source_lineage_missing", report["priors"]["player_foot"]["reason"])

    def test_input_change_and_keyboard_interrupt_do_not_publish_or_touch_previous_generation(self) -> None:
        for failure in ("input_change", "keyboard"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temp_name:
                root = Path(temp_name)
                inputs = self._inputs(root, frame_count=2, candidates=[(0, 10, 10, 0.8)])
                output = root / "generation"
                previous = root / "previous-generation"
                previous.mkdir()
                for name in (TRACK_NAME, DECISIONS_NAME, REPORT_NAME):
                    (previous / name).write_text(f"old:{name}", encoding="utf-8")

                if failure == "input_change":
                    patcher = mock.patch(
                        "football_tracking.global_ball_trajectory._verify_source_lease",
                        side_effect=GlobalBallTrajectoryError("source video changed during trajectory solving"),
                    )
                else:
                    patcher = mock.patch(
                        "football_tracking.global_ball_trajectory._publish_generation", side_effect=KeyboardInterrupt
                    )
                with patcher, self.assertRaises((GlobalBallTrajectoryError, KeyboardInterrupt)):
                    solve_global_ball_trajectory(*inputs, output)

                self.assertFalse(output.exists())
                for name in (TRACK_NAME, DECISIONS_NAME, REPORT_NAME):
                    self.assertEqual(f"old:{name}", (previous / name).read_text(encoding="utf-8"))

    def test_synthetic_100k_frames_has_bounded_frontier_and_no_whole_json_loader(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source = root / "source.bin"
            source.write_bytes(b"source")
            frames = [{"frame_index": index, "status": "unknown"} for index in range(100_000)]
            contract_path = root / "tracking_contract.v2.json"
            contract_path.write_text(
                json.dumps(
                    build_tracking_contract(
                        source={
                            "video_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                            "fps": 30.0,
                            "width": 64,
                            "height": 48,
                            "frame_count": 100_000,
                        },
                        frames=frames,
                    ),
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            predictions_path = root / "candidate_predictions.v1.json"
            self._write_predictions(contract_path, predictions_path, [])
            metadata = {"fps": 30.0, "width": 64, "height": 48, "frame_count": 100_000}
            with mock.patch("football_tracking.global_ball_trajectory._probe_video_metadata", return_value=metadata), mock.patch(
                "football_tracking.global_ball_trajectory.json.load", side_effect=AssertionError("whole JSON load forbidden")
            ):
                report = solve_global_ball_trajectory(source, contract_path, predictions_path, root / "generation")

        self.assertEqual(100_000, report["summary"]["row_count"])
        self.assertEqual(0, report["work"]["max_frontier_states"])

    def test_early_noise_can_be_skipped_and_all_positive_path_stays_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = self._inputs(
                root,
                frame_count=3,
                candidates=[(0, 55, 40, 0.99), (1, 10, 10, 0.95), (2, 12, 10, 0.95)],
                match_probability_by_center={(55, 40): 0.02, (10, 10): 0.95, (12, 10): 0.95},
            )
            solve_global_ball_trajectory(*inputs, root / "generation")
            rows = self._rows(root / "generation")

            weak_inputs = self._inputs(
                root / "weak",
                frame_count=2,
                candidates=[(0, 20, 20, 0.9), (1, 22, 20, 0.9)],
                match_probability_by_center={(20, 20): 0.011, (22, 20): 0.011},
            )
            solve_global_ball_trajectory(*weak_inputs, root / "weak-generation")
            weak_rows = self._rows(root / "weak-generation")

        self.assertEqual(["unknown", "detected", "detected"], [row["Status"] for row in rows])
        self.assertEqual(["unknown", "unknown"], [row["Status"] for row in weak_rows])

    def test_second_order_ablation_changes_the_ambiguous_choice(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = self._inputs(
                root,
                frame_count=3,
                candidates=[(0, 10, 20, 0.9), (1, 20, 20, 0.9), (2, 10, 20, 0.9), (2, 30, 20, 0.9)],
            )
            solve_global_ball_trajectory(*inputs, root / "second-order")
            default_rows = self._rows(root / "second-order")
            solve_global_ball_trajectory(
                *inputs,
                root / "first-order",
                config=TrajectoryConfig(acceleration_weight=0.0, direction_weight=0.0),
            )
            ablated_rows = self._rows(root / "first-order")

        self.assertAlmostEqual(30.0, float(default_rows[2]["X"]))
        self.assertAlmostEqual(10.0, float(ablated_rows[2]["X"]))

    def test_restart_boundary_is_not_interpolated(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = self._inputs(
                root,
                frame_count=4,
                candidates=[(0, 10, 20, 0.9), (1, 20, 20, 0.9), (3, 10, 20, 0.9)],
            )
            solve_global_ball_trajectory(
                *inputs,
                root / "generation",
                config=TrajectoryConfig(restart_penalty=0.0, adjacent_restart_penalty=0.0),
            )
            rows = self._rows(root / "generation")
            decisions = self._decisions(root / "generation")

        self.assertEqual("unknown", rows[2]["Status"])
        self.assertEqual("trajectory_segment_restart_boundary", rows[2]["Reason"])
        restarted = [row for row in decisions if row.get("decision") == "selected" and row["frame_index"] == 3]
        self.assertTrue(restarted[0]["restart"])

    def test_trailing_prediction_outside_frame_becomes_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = self._inputs(
                root,
                frame_count=4,
                candidates=[(0, 114, 20, 0.9), (1, 126, 20, 0.9)],
            )
            solve_global_ball_trajectory(*inputs, root / "generation")
            rows = self._rows(root / "generation")

        self.assertEqual(["unknown", "unknown"], [rows[2]["Status"], rows[3]["Status"]])
        self.assertTrue(all(row["Reason"] == "predicted_position_outside_frame" for row in rows[2:]))

    def test_contract_source_envelope_bbox_and_canonical_id_fail_closed(self) -> None:
        mutations = {
            "source": (
                lambda contract: contract["source"].__setitem__("video_sha256", "0" * 64),
                "source video sha256",
            ),
            "unsupported source field": (
                lambda contract: contract["source"].__setitem__("opaque_lineage", "unexpected"),
                "source contains unsupported fields",
            ),
            "missing source frame count": (
                lambda contract: contract["source"].pop("frame_count"),
                "source metadata is missing",
            ),
            "boolean source frame count": (
                lambda contract: contract["source"].__setitem__("frame_count", True),
                "source frame_count must be a non-negative integer",
            ),
            "float source frame count": (
                lambda contract: contract["source"].__setitem__("frame_count", 1.0),
                "source frame_count must be a non-negative integer",
            ),
            "boolean summary frame count": (
                lambda contract: contract["summary"].__setitem__("frame_count", True),
                "summary frame_count must be a non-negative integer",
            ),
            "float summary frame count": (
                lambda contract: contract["summary"].__setitem__("frame_count", 1.0),
                "summary frame_count must be a non-negative integer",
            ),
            "validation errors type": (
                lambda contract: contract.__setitem__("validation_errors", "fatal"),
                "validation_errors must be an array",
            ),
            "candidate collection type": (
                lambda contract: contract.__setitem__("candidates", {}),
                "candidates must be an array",
            ),
            "bbox outside": (
                lambda contract: contract["candidates"][0].__setitem__("bbox", [1, 1, 1000, 1000]),
                "bbox lies outside",
            ),
            "frame width": (
                lambda contract: contract["candidates"][0].__setitem__(
                    "candidate_id",
                    contract["candidates"][0]["candidate_id"].replace("-000000000-", "-0000000000-"),
                ),
                "identity does not match",
            ),
            "occurrence width": (
                lambda contract: contract["candidates"][0].__setitem__(
                    "candidate_id", contract["candidates"][0]["candidate_id"] + "0"
                ),
                "occurrence is not canonically encoded",
            ),
        }
        for label, (mutate, expected_error) in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp_name:
                root = Path(temp_name)
                source, contract_path, predictions_path = self._inputs(
                    root, frame_count=1, candidates=[(0, 10, 10, 0.8)]
                )
                contract = json.loads(contract_path.read_text(encoding="utf-8"))
                mutate(contract)
                contract_path.write_text(json.dumps(contract, allow_nan=False), encoding="utf-8")
                with self.assertRaisesRegex(GlobalBallTrajectoryError, expected_error):
                    solve_global_ball_trajectory(source, contract_path, predictions_path, root / "generation")
                self.assertFalse((root / "generation").exists())

    def test_output_is_immutable_and_cannot_contain_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = self._inputs(root, frame_count=1, candidates=[])
            existing = root / "existing-generation"
            existing.mkdir()
            marker = existing / "marker"
            marker.write_text("keep", encoding="utf-8")
            with self.assertRaisesRegex(GlobalBallTrajectoryError, "already exists"):
                solve_global_ball_trajectory(*inputs, existing)
            self.assertEqual("keep", marker.read_text(encoding="utf-8"))

            with self.assertRaisesRegex(GlobalBallTrajectoryError, "cannot contain"):
                solve_global_ball_trajectory(*inputs, root)
            self.assertTrue(all(path.exists() for path in inputs))

    def test_dense_candidate_graph_persists_only_beam_bounded_states(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            candidates = [
                (frame, 5.0 + float(index % 20) * 5.0, 10.0 + float(index // 20) * 5.0, 0.8)
                for frame in range(30)
                for index in range(24)
            ]
            inputs = self._inputs(root, frame_count=30, candidates=candidates)
            report = solve_global_ball_trajectory(
                *inputs,
                root / "generation",
                config=TrajectoryConfig(candidate_cap_per_frame=24, beam_width=8),
            )

        self.assertLessEqual(report["work"]["persisted_state_count"], 30 * 8)
        self.assertLessEqual(report["work"]["max_frontier_states"], 8)

    def test_source_bound_pitch_and_player_priors_are_audited_and_used(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source, contract, predictions = self._inputs(
                root,
                frame_count=1,
                candidates=[(0, 10, 10, 0.8), (0, 100, 80, 0.8)],
            )
            lineage = {
                "source_video_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "fps": 10.0,
                "width": 128,
                "height": 96,
                "frame_count": 1,
            }
            pitch = root / "pitch.json"
            pitch.write_text(
                json.dumps(
                    {
                        "lineage": lineage,
                        "pitch_polygon": [[0, 0], [64, 0], [64, 48], [0, 48]],
                    }
                ),
                encoding="utf-8",
            )
            players = root / "players.json"
            players.write_text(
                json.dumps(
                    {
                        "lineage": lineage,
                        "tracks": [{"samples": [{"frame": 0, "foot_point": {"x": 10, "y": 10}}]}],
                    }
                ),
                encoding="utf-8",
            )
            report = solve_global_ball_trajectory(
                source,
                contract,
                predictions,
                root / "generation",
                pitch_report_path=pitch,
                player_tracks_path=players,
            )
            rows = self._rows(root / "generation")
            decisions = self._decisions(root / "generation")

        self.assertEqual("loaded", report["priors"]["pitch"]["status"])
        self.assertEqual("loaded", report["priors"]["player_foot"]["status"])
        self.assertAlmostEqual(10.0, float(rows[0]["X"]))
        selected = next(row for row in decisions if row.get("decision") == "selected")
        self.assertIn("pitch", selected["costs"]["node"])
        self.assertIn("player_foot", selected["costs"]["node"])

    def test_concurrent_solver_is_rejected_without_disturbing_owner(self) -> None:
        import football_tracking.global_ball_trajectory as trajectory_module

        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = self._inputs(root, frame_count=2, candidates=[(0, 10, 10, 0.8)])
            output = root / "generation"
            entered = threading.Event()
            release = threading.Event()
            failures: list[BaseException] = []
            original_acquire = trajectory_module._acquire_source_lease

            def blocking_acquire(path):
                if threading.current_thread().name == "trajectory-owner":
                    entered.set()
                    release.wait(timeout=10)
                return original_acquire(path)

            def run_owner() -> None:
                try:
                    solve_global_ball_trajectory(*inputs, output)
                except BaseException as exc:  # pragma: no cover - asserted below
                    failures.append(exc)

            with mock.patch(
                "football_tracking.global_ball_trajectory._acquire_source_lease",
                side_effect=blocking_acquire,
            ):
                owner = threading.Thread(target=run_owner, name="trajectory-owner")
                owner.start()
                self.assertTrue(entered.wait(timeout=10))
                with self.assertRaisesRegex(GlobalBallTrajectoryError, "already locked"):
                    solve_global_ball_trajectory(*inputs, output)
                release.set()
                owner.join(timeout=20)

            self.assertFalse(owner.is_alive())
            self.assertEqual([], failures)
            self.assertTrue((output / REPORT_NAME).is_file())

    def test_publish_fsync_failure_leaves_no_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = self._inputs(root, frame_count=1, candidates=[])
            calls = 0

            def fail_parent_fsync(path):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected parent fsync failure")

            with mock.patch(
                "football_tracking.global_ball_trajectory._fsync_directory",
                side_effect=fail_parent_fsync,
            ), self.assertRaises(GlobalBallTrajectoryError):
                solve_global_ball_trajectory(*inputs, root / "generation")

            self.assertFalse((root / "generation").exists())

    def test_windows_fdopen_failure_releases_the_source_lease_handle(self) -> None:
        if os.name != "nt":
            self.skipTest("Windows handle ownership regression")
        import football_tracking.global_ball_trajectory as trajectory_module

        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source = root / "source.avi"
            self._write_video(source, 1)

            with mock.patch(
                "football_tracking.global_ball_trajectory.os.fdopen",
                side_effect=OSError("injected fdopen failure"),
            ), self.assertRaisesRegex(OSError, "injected fdopen failure"):
                trajectory_module._open_source_lease_handle(source)

            with source.open("r+b") as handle:
                handle.seek(0, os.SEEK_END)
            renamed = root / "renamed.avi"
            source.replace(renamed)
            renamed.unlink()

    def test_missing_leased_probe_alias_fails_closed_without_copying_or_leaking(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source, contract, predictions = self._inputs(root, frame_count=1, candidates=[])

            with mock.patch(
                "football_tracking.global_ball_trajectory._leased_probe_path",
                return_value=None,
            ), self.assertRaisesRegex(GlobalBallTrajectoryError, "cannot expose the leased source"):
                solve_global_ball_trajectory(source, contract, predictions, root / "generation")

            self.assertFalse((root / "generation").exists())
            self.assertEqual([], list(root.glob("source_video.snapshot*")))
            with source.open("r+b") as handle:
                handle.seek(0, os.SEEK_END)

    def test_keyboard_interrupt_during_source_hash_releases_the_lease(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source, contract, predictions = self._inputs(root, frame_count=1, candidates=[])

            with mock.patch(
                "football_tracking.global_ball_trajectory._hash_source_handle",
                side_effect=KeyboardInterrupt,
            ), self.assertRaises(KeyboardInterrupt):
                solve_global_ball_trajectory(source, contract, predictions, root / "generation")

            self.assertFalse((root / "generation").exists())
            renamed = root / "source-after-interrupt.avi"
            source.replace(renamed)
            renamed.replace(source)

    def test_source_change_inside_publish_window_is_quarantined(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source, contract, predictions = self._inputs(root, frame_count=1, candidates=[])

            with mock.patch(
                "football_tracking.global_ball_trajectory._verify_source_lease",
                side_effect=GlobalBallTrajectoryError("source video changed during trajectory solving"),
            ), self.assertRaisesRegex(GlobalBallTrajectoryError, "source video changed"):
                solve_global_ball_trajectory(source, contract, predictions, root / "generation")

            self.assertFalse((root / "generation").exists())

    def test_metadata_probe_cannot_use_aba_swapped_source_bytes(self) -> None:
        import football_tracking.global_ball_trajectory as trajectory_module

        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source, contract, predictions = self._inputs(
                root,
                frame_count=2,
                video_frame_count=1,
                candidates=[],
            )
            contract_payload = json.loads(contract.read_text(encoding="utf-8"))
            contract_payload["source"]["frame_count"] = 2
            contract.write_text(json.dumps(contract_payload, separators=(",", ":")), encoding="utf-8")
            self._write_predictions(contract, predictions, [])
            alternate = root / "alternate-two-frames.avi"
            self._write_video(alternate, 2)
            original_bytes = source.read_bytes()
            alternate_bytes = alternate.read_bytes()
            original_stat = source.stat()
            original_probe = trajectory_module._probe_video_metadata
            mutation_succeeded: list[bool] = []

            def aba_probe(probe_path):
                replaced = False
                try:
                    source.write_bytes(alternate_bytes)
                    replaced = True
                except PermissionError:
                    pass
                mutation_succeeded.append(replaced)
                try:
                    return original_probe(probe_path)
                finally:
                    if replaced:
                        source.write_bytes(original_bytes)
                        os.utime(
                            source,
                            ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
                        )

            with mock.patch(
                "football_tracking.global_ball_trajectory._probe_video_metadata",
                side_effect=aba_probe,
            ), self.assertRaises(GlobalBallTrajectoryError):
                solve_global_ball_trajectory(source, contract, predictions, root / "generation")

            self.assertEqual(original_bytes, source.read_bytes())
            self.assertEqual(1, original_probe(source)["frame_count"])
            if os.name == "nt":
                self.assertEqual([False], mutation_succeeded)
            self.assertFalse((root / "generation").exists())

    def test_success_report_is_hidden_until_final_source_verification(self) -> None:
        import football_tracking.global_ball_trajectory as trajectory_module

        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source, contract, predictions = self._inputs(root, frame_count=1, candidates=[])
            output = root / "generation"
            entered = threading.Event()
            release = threading.Event()
            failures: list[BaseException] = []
            original_verify = trajectory_module._verify_source_lease

            def block_final_verify(lease):
                entered.set()
                if not release.wait(timeout=10):
                    raise RuntimeError("test timed out waiting to release final verification")
                return original_verify(lease)

            def run_solver() -> None:
                try:
                    solve_global_ball_trajectory(source, contract, predictions, output)
                except BaseException as exc:  # pragma: no cover - asserted below
                    failures.append(exc)

            with mock.patch(
                "football_tracking.global_ball_trajectory._verify_source_lease",
                side_effect=block_final_verify,
            ):
                worker = threading.Thread(target=run_solver, name="trajectory-final-verification")
                worker.start()
                self.assertTrue(entered.wait(timeout=10))
                self.assertTrue((output / TRACK_NAME).is_file())
                self.assertFalse((output / REPORT_NAME).exists())
                release.set()
                worker.join(timeout=20)

            self.assertFalse(worker.is_alive())
            self.assertEqual([], failures)
            self.assertTrue((output / REPORT_NAME).is_file())

    def test_large_inputs_are_not_rehashed_repeatedly(self) -> None:
        import football_tracking.global_ball_trajectory as trajectory_module

        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = self._inputs(root, frame_count=1, candidates=[])
            counts: dict[str, int] = {}
            original_capture = trajectory_module._capture_snapshot
            original_source_hash = trajectory_module._hash_source_handle

            def counted_capture(path, label, *, copy_path=None):
                counts[label] = counts.get(label, 0) + 1
                return original_capture(path, label, copy_path=copy_path)

            def counted_source_hash(handle):
                counts["source video"] = counts.get("source video", 0) + 1
                return original_source_hash(handle)

            with mock.patch(
                "football_tracking.global_ball_trajectory._capture_snapshot",
                side_effect=counted_capture,
            ), mock.patch(
                "football_tracking.global_ball_trajectory._hash_source_handle",
                side_effect=counted_source_hash,
            ):
                solve_global_ball_trajectory(*inputs, root / "generation")

        self.assertEqual(2, counts["source video"])
        self.assertEqual(1, counts["source tracking contract"])
        self.assertEqual(1, counts["candidate predictions"])

    def test_report_hashes_and_frame_candidate_audit_counts_are_exact(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = self._inputs(
                root,
                frame_count=3,
                candidates=[(0, 10, 10, 0.8), (2, 14, 10, 0.8)],
            )
            report = solve_global_ball_trajectory(*inputs, root / "generation")
            decisions = self._decisions(root / "generation")

            for name in (TRACK_NAME, DECISIONS_NAME):
                artifact = root / "generation" / name
                self.assertEqual(hashlib.sha256(artifact.read_bytes()).hexdigest(), report["artifacts"][name]["sha256"])
                self.assertEqual(artifact.stat().st_size, report["artifacts"][name]["size"])

        self.assertEqual(3, sum(row["record_type"] == "frame" for row in decisions))
        self.assertEqual(2, sum(row["record_type"] == "candidate" for row in decisions))

    def _inputs(
        self,
        root: Path,
        *,
        frame_count: int,
        candidates: list[tuple[int, float, float, float]],
        video_frame_count: int | None = None,
        frame_statuses: list[str] | None = None,
        match_probability_by_center: dict[tuple[float, float], float] | None = None,
        classifications: dict[tuple[float, float], tuple[str, str]] | None = None,
        decisions: dict[tuple[float, float], str] | None = None,
    ) -> tuple[Path, Path, Path]:
        root.mkdir(parents=True, exist_ok=True)
        source = root / "source.avi"
        source_frame_count = video_frame_count or max(
            frame_count, max((item[0] + 1 for item in candidates), default=1)
        )
        self._write_video(source, source_frame_count)
        source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
        runtime = [
            Candidate(frame, x - 2, y - 2, x + 2, y + 2, confidence, source="detector")
            for frame, x, y, confidence in candidates
        ]
        assign_candidate_ids(runtime, source_sha)
        candidate_rows = [
            {
                "candidate_id": item.candidate_id,
                "frame_index": item.frame_index,
                "bbox": [item.x1, item.y1, item.x2, item.y2],
                "confidence": item.confidence,
                "source": item.source,
            }
            for item in runtime
        ]
        classification_rows = []
        for item, (_, x, y, _) in zip(runtime, candidates):
            if classifications and (x, y) in classifications:
                label, origin = classifications[(x, y)]
                classification_rows.append(
                    {"candidate_id": item.candidate_id, "label": label, "label_origin": origin, "confidence": 1.0}
                )
        decision_rows = []
        for item, (_, x, y, _) in zip(runtime, candidates):
            if decisions and (x, y) in decisions:
                decision_rows.append(
                    {"candidate_id": item.candidate_id, "decision": decisions[(x, y)], "confidence": 1.0}
                )
        frames = []
        legacy_map = {"Detected": "detected", "Predicted": "interpolated", "Lost": "unknown"}
        for index in range(frame_count):
            status = frame_statuses[index] if frame_statuses else "unknown"
            frame = {"frame_index": index, "status": legacy_map.get(status, status)}
            if status in legacy_map:
                frame["legacy_status"] = status
            if frame["status"] in {"detected", "interpolated"}:
                frame.update({"x": 1.0, "y": 1.0})
            frames.append(frame)
        contract_path = root / "tracking_contract.v2.json"
        contract_path.write_text(
            json.dumps(
                build_tracking_contract(
                    source={
                        "video_sha256": source_sha,
                        "fps": 10.0,
                        "width": 128,
                        "height": 96,
                        "frame_count": source_frame_count,
                    },
                    frames=frames,
                    candidates=candidate_rows,
                    classifications=classification_rows,
                    decisions=decision_rows,
                ),
                allow_nan=False,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        predictions_path = root / "candidate_predictions.v1.json"
        probabilities = []
        for item, (_, x, y, _) in zip(candidate_rows, candidates):
            match = (match_probability_by_center or {}).get((x, y), 0.85)
            probabilities.append((item, match))
        self._write_predictions(contract_path, predictions_path, probabilities)
        return source, contract_path, predictions_path

    def _write_predictions(self, contract_path: Path, predictions_path: Path, rows: list[tuple[dict, float]]) -> None:
        predictions = []
        for candidate, match in rows:
            remainder = (1.0 - match) / (len(CLASSIFICATION_LABELS) - 1)
            values = {label: (match if label == "match_ball" else remainder) for label in CLASSIFICATION_LABELS}
            identity = {
                "candidate_id": candidate["candidate_id"],
                "frame_index": candidate["frame_index"],
                "bbox": [float(value) for value in candidate["bbox"]],
                "detector_source": candidate["source"],
                "confidence": float(candidate["confidence"]),
            }
            fingerprint = hashlib.sha256(
                json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
            ).hexdigest()
            predicted_label = max(values, key=values.get)
            predictions.append(
                {
                    "candidate_id": candidate["candidate_id"],
                    "candidate_fingerprint": fingerprint,
                    "predicted_label": predicted_label,
                    "confidence": values[predicted_label],
                    "probabilities": values,
                    "model_version": _MODEL_VERSION,
                }
            )
        payload = {
            "schema_version": "1.0",
            "artifact_type": "candidate_predictions",
            "model_version": _MODEL_VERSION,
            "dataset_version": _DATASET_VERSION,
            "source_contract_sha256": hashlib.sha256(contract_path.read_bytes()).hexdigest(),
            "class_order": list(CLASSIFICATION_LABELS),
            "temperature": 1.0,
            "prediction_count": len(predictions),
            "predictions": predictions,
        }
        predictions_path.write_text(json.dumps(payload, allow_nan=False, separators=(",", ":")), encoding="utf-8")

    @staticmethod
    def _write_video(path: Path, frame_count: int) -> None:
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 10.0, (128, 96))
        if not writer.isOpened():
            raise RuntimeError("test video writer unavailable")
        for index in range(frame_count):
            frame = np.full((96, 128, 3), index % 255, dtype=np.uint8)
            writer.write(frame)
        writer.release()

    @staticmethod
    def _rows(output_dir: Path) -> list[dict[str, str]]:
        with (output_dir / TRACK_NAME).open("r", encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))

    @staticmethod
    def _decisions(output_dir: Path) -> list[dict]:
        return [json.loads(line) for line in (output_dir / DECISIONS_NAME).read_text(encoding="utf-8").splitlines()]


if __name__ == "__main__":
    unittest.main()
