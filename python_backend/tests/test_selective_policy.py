from __future__ import annotations

import hashlib
import io
import json
import math
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from football_tracking.selective_policy import (
    SelectivePolicyConfig,
    SelectivePolicyError,
    _binomial_lower_tail,
    _evaluation_rows,
    _holm_rejections,
    _qualification_evidence_summary,
    _threshold_grid,
    _wilson_upper_bound,
    build_roles_cli_main,
    build_selective_policy_roles,
    fit_cli_main,
    fit_selective_policy,
    validate_selective_decision_semantics,
    validate_selective_decisions_binding,
)
from football_tracking.tracking_contracts import CLASSIFICATION_LABELS, build_tracking_contract


class _PrematureEofReader:
    def __init__(self, handle: object, byte_limit: int) -> None:
        self._handle = handle
        self._remaining = byte_limit

    def __enter__(self) -> _PrematureEofReader:
        return self

    def __exit__(self, *args: object) -> None:
        self._handle.close()

    def fileno(self) -> int:
        return self._handle.fileno()

    def read(self, size: int = -1) -> bytes:
        if self._remaining == 0:
            return b""
        requested = self._remaining if size < 0 else min(size, self._remaining)
        chunk = self._handle.read(requested)
        self._remaining -= len(chunk)
        return chunk


class SelectivePolicyCaptureTests(unittest.TestCase):
    def test_json_capture_hashes_the_exact_bytes_that_are_parsed_after_path_aba(self) -> None:
        from football_tracking import selective_policy

        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            target = root / "artifact.json"
            alternate = root / "alternate.json"
            target_bytes = b'{"marker":"A"}\n'
            alternate_bytes = b'{"marker":"B"}\n'
            target.write_bytes(target_bytes)
            alternate.write_bytes(alternate_bytes)
            original_open = Path.open

            def open_captured_version(path: Path, *args: object, **kwargs: object) -> object:
                captured_path = alternate if path.resolve() == target.resolve() else path
                return original_open(captured_path, *args, **kwargs)

            with patch.object(Path, "open", new=open_captured_version):
                value, snapshot = selective_policy._load_snapshot_json(target, "test artifact")

            self.assertEqual({"marker": "B"}, value)
            self.assertEqual(hashlib.sha256(alternate_bytes).hexdigest(), snapshot.sha256)
            self.assertEqual(len(alternate_bytes), snapshot.size)
            self.assertEqual(target_bytes, target.read_bytes())

    def test_resolved_contract_is_validated_from_the_same_captured_bytes(self) -> None:
        from football_tracking import selective_policy

        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            target = root / "tracking_contract.v2.json"
            alternate = root / "alternate-contract.json"
            contract_a = build_tracking_contract(
                candidates=[
                    {
                        "candidate_id": "candidate-a",
                        "frame_index": 0,
                        "bbox": [0, 0, 1, 1],
                        "confidence": 0.9,
                        "source": "test",
                    }
                ]
            )
            contract_b = build_tracking_contract(
                candidates=[
                    {
                        "candidate_id": "candidate-b",
                        "frame_index": 0,
                        "bbox": [0, 0, 1, 1],
                        "confidence": 0.9,
                        "source": "test",
                    }
                ]
            )
            target.write_text(json.dumps(contract_a), encoding="utf-8")
            alternate_bytes = json.dumps(contract_b).encode("utf-8")
            alternate.write_bytes(alternate_bytes)
            original_open = Path.open

            def open_captured_version(path: Path, *args: object, **kwargs: object) -> object:
                captured_path = alternate if path.resolve() == target.resolve() else path
                return original_open(captured_path, *args, **kwargs)

            with patch.object(Path, "open", new=open_captured_version):
                contract, snapshot = selective_policy._load_snapshot_tracking_contract(
                    target,
                    "resolved tracking contract",
                )

            self.assertEqual("candidate-b", contract["candidates"][0]["candidate_id"])
            self.assertEqual(hashlib.sha256(alternate_bytes).hexdigest(), snapshot.sha256)

    def test_json_capture_rejects_premature_eof_and_invalid_utf8(self) -> None:
        from football_tracking import selective_policy

        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            path = root / "artifact.json"
            payload = b'{"marker":"complete"}\n'
            path.write_bytes(payload)
            original_open = Path.open

            def open_truncated(path_value: Path, *args: object, **kwargs: object) -> object:
                handle = original_open(path_value, *args, **kwargs)
                if path_value.resolve() == path.resolve():
                    return _PrematureEofReader(handle, len(payload) - 2)
                return handle

            with patch.object(Path, "open", new=open_truncated):
                with self.assertRaisesRegex(SelectivePolicyError, "ended early"):
                    selective_policy._load_snapshot_json(path, "test artifact")

            path.write_bytes(b'{"marker":"\xff"}\n')
            with self.assertRaisesRegex(SelectivePolicyError, "invalid test artifact.*utf-8"):
                selective_policy._load_snapshot_json(path, "test artifact")


class SelectivePolicyStatisticsTests(unittest.TestCase):
    def test_exact_binomial_and_holm_are_deterministic_and_small_samples_fail(self) -> None:
        self.assertAlmostEqual((1.0 - 0.01) ** 1000, _binomial_lower_tail(0, 1000, 0.01))
        hypotheses = [("z", 0.04), ("a", 0.001), ("b", 0.02), ("tie", 0.02)]
        first = _holm_rejections(hypotheses, alpha=0.05)
        second = _holm_rejections(list(reversed(hypotheses)), alpha=0.05)
        self.assertEqual(first, second)
        self.assertEqual({"a"}, first)
        self.assertGreater(_wilson_upper_bound(0, 50, alpha=0.05), 0.01)
        self.assertLess(_wilson_upper_bound(0, 400, alpha=0.05), 0.01)
        large_tail = _binomial_lower_tail(20, 100_000, 0.001)
        self.assertTrue(math.isfinite(large_tail))
        self.assertGreater(large_tail, 0.0)
        self.assertLess(large_tail, _binomial_lower_tail(21, 100_000, 0.001))

    def test_required_targets_and_independent_component_floor_cannot_be_weakened(self) -> None:
        for config in (
            SelectivePolicyConfig(accept_precision_target=0.97),
            SelectivePolicyConfig(false_reject_target=0.02),
            SelectivePolicyConfig(min_independent_components=2),
        ):
            with self.subTest(config=config), self.assertRaisesRegex(RuntimeError, "cannot"):
                from football_tracking.selective_policy import _validate_config

                _validate_config(config)

    def test_single_threshold_lane_is_deterministic(self) -> None:
        self.assertEqual([0.9], _threshold_grid([0.1, 0.5, 0.9], 1))

    def test_cli_argument_errors_are_one_json_record_without_usage(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            return_code = fit_cli_main(["--predictions", "only-one-argument.json"])

        lines = stderr.getvalue().splitlines()
        self.assertEqual(2, return_code)
        self.assertEqual(1, len(lines))
        self.assertEqual({"ok": False, "error": "invalid_arguments"}, json.loads(lines[0]))
        self.assertNotIn("usage:", stderr.getvalue().lower())


class SelectivePolicyEndToEndTests(unittest.TestCase):
    def test_policy_version_binds_full_config_qualification_and_recomputable_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = _write_inputs(root, calibration_per_class=1000, audit_per_class=400)

            default_policy = fit_selective_policy(**inputs, output_dir=root / "default")
            alternate_config = SelectivePolicyConfig(max_thresholds_per_lane=32)
            alternate_policy = fit_selective_policy(
                **inputs,
                output_dir=root / "alternate-config",
                config=alternate_config,
            )
            review_only_config = SelectivePolicyConfig(min_audit_accepted=10_000)
            review_only_policy = fit_selective_policy(
                **inputs,
                output_dir=root / "review-only",
                config=review_only_config,
            )
            for output_name in ("default", "alternate-config", "review-only"):
                validate_selective_decisions_binding(
                    root / output_name / "selective_policy.v1.json",
                    root / output_name / "selective_decisions.v1.json",
                )

        self.assertNotEqual(default_policy["policy_version"], alternate_policy["policy_version"])
        self.assertNotEqual(default_policy["policy_version"], review_only_policy["policy_version"])
        self.assertEqual("qualified", default_policy["status"])
        self.assertEqual("review_only", review_only_policy["status"])
        for policy in (default_policy, alternate_policy, review_only_policy):
            self.assertEqual(policy["policy_version"], _canonical_hash(policy["version_inputs"]))
            self.assertEqual(
                policy["status"],
                policy["version_inputs"]["qualification"]["policy_status"],
            )
            self.assertEqual(12, len(policy["version_inputs"]["config"]))
            self.assertEqual(policy["inferential_unit"], policy["version_inputs"]["inferential_unit"])
            self.assertEqual(
                "one_candidate_per_connected_evidence_component_v1",
                policy["version_inputs"]["algorithm_versions"]["inferential_unit"],
            )

    def test_decisions_file_is_content_and_snapshot_bound_to_policy_and_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = _write_inputs(
                root,
                calibration_per_class=1000,
                audit_per_class=400,
                include_application_cases=True,
            )
            output_dir = root / "policy-output"
            policy = fit_selective_policy(**inputs, output_dir=output_dir)
            decisions_path = output_dir / "selective_decisions.v1.json"
            decisions = _read_json(decisions_path)
            acceptance = _read_json(output_dir / "selective_acceptance_report.v1.json")

            content = {key: value for key, value in decisions.items() if key not in {"generated_at", "policy_version"}}
            expected_content_sha = _canonical_hash(content)
            expected_file_sha = _sha256(decisions_path)
            self.assertEqual(expected_content_sha, policy["version_inputs"]["decisions_content_sha256"])
            self.assertEqual(
                {
                    "path": "selective_decisions.v1.json",
                    "sha256": expected_file_sha,
                    "content_sha256": expected_content_sha,
                },
                policy["decisions_artifact"],
            )
            self.assertEqual(policy["version_inputs"], acceptance["version_inputs"])
            self.assertEqual(policy["decisions_artifact"], acceptance["decisions_artifact"])
            policy_path = output_dir / "selective_policy.v1.json"
            self.assertEqual(decisions, validate_selective_decisions_binding(policy_path, decisions_path))

            tampered_policy = _read_json(policy_path)
            tampered_policy["thresholds"]["accept"] = 0.123
            _write_json(policy_path, tampered_policy)
            with self.assertRaisesRegex(RuntimeError, "thresholds.*version_inputs"):
                validate_selective_decisions_binding(policy_path, decisions_path)
            _write_json(policy_path, policy)

            tampered_policy = _read_json(policy_path)
            tampered_policy["version_inputs"]["algorithm_versions"]["inferential_unit"] = "weakened-v0"
            tampered_policy["policy_version"] = _canonical_hash(tampered_policy["version_inputs"])
            _write_json(policy_path, tampered_policy)
            with self.assertRaisesRegex(RuntimeError, "algorithm versions"):
                validate_selective_decisions_binding(policy_path, decisions_path)
            _write_json(policy_path, policy)

            decisions["decisions"][0]["decision"] = "reject"
            _write_json(decisions_path, decisions)
            with self.assertRaisesRegex(RuntimeError, "decisions.*sha256|content"):
                validate_selective_decisions_binding(policy_path, decisions_path)

    def test_resealed_decision_behavior_tampering_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = _write_inputs(
                root,
                calibration_per_class=1000,
                audit_per_class=400,
                include_application_cases=True,
                include_forced_cases=True,
            )
            output_dir = root / "policy-output"
            fit_selective_policy(**inputs, output_dir=output_dir)
            policy_path = output_dir / "selective_policy.v1.json"
            decisions_path = output_dir / "selective_decisions.v1.json"
            valid_policy = _read_json(policy_path)
            valid_decisions = _read_json(decisions_path)

            def row(decisions: dict[str, object], candidate_id: str) -> dict[str, object]:
                return next(item for item in decisions["decisions"] if item["candidate_id"] == candidate_id)

            def extra_field(decisions: dict[str, object]) -> None:
                row(decisions, "application-ball")["forged"] = True

            def missing_field(decisions: dict[str, object]) -> None:
                del row(decisions, "application-ball")["raw_decision"]

            def reject_flipped_to_accept(decisions: dict[str, object]) -> None:
                target = row(decisions, "application-noise")
                target["raw_decision"] = "accept"
                target["decision"] = "accept"

            def accepted_marked_unapplied(decisions: dict[str, object]) -> None:
                row(decisions, "application-ball")["applied_to_contract"] = False

            def forced_marked_applied(decisions: dict[str, object]) -> None:
                row(decisions, "forced-unknown")["applied_to_contract"] = True

            def preserved_marked_applied(decisions: dict[str, object]) -> None:
                row(decisions, "forced-existing")["applied_to_contract"] = True

            def holdout_raw_accept(decisions: dict[str, object]) -> None:
                target = next(item for item in decisions["decisions"] if item["decision_scope"] == "evaluation_only")
                target["raw_decision"] = "accept"

            def forged_forced_reasons(decisions: dict[str, object]) -> None:
                row(decisions, "forced-margin")["forced_abstain_reasons"] = ["evaluation_holdout"]

            def contradictory_existing_reasons(decisions: dict[str, object]) -> None:
                target = row(decisions, "application-ball")
                target["forced_abstain_reasons"] = ["conflicting_existing_decisions", "existing_decision"]
                target["raw_decision"] = "abstain"
                target["decision"] = "abstain"
                target["applied_to_contract"] = False

            mutations = {
                "extra-field": extra_field,
                "missing-field": missing_field,
                "reject-to-accept": reject_flipped_to_accept,
                "accepted-unapplied": accepted_marked_unapplied,
                "forced-applied": forced_marked_applied,
                "preserved-applied": preserved_marked_applied,
                "holdout-raw-accept": holdout_raw_accept,
                "forged-forced-reasons": forged_forced_reasons,
                "contradictory-existing-reasons": contradictory_existing_reasons,
            }
            for name, mutate in mutations.items():
                with self.subTest(name=name):
                    policy = deepcopy(valid_policy)
                    decisions = deepcopy(valid_decisions)
                    mutate(decisions)
                    _reseal_decision_artifact(policy_path, decisions_path, policy, decisions)
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "row fields|raw_decision|decision|applied_to_contract|forced abstain|scope",
                    ):
                        validate_selective_decisions_binding(policy_path, decisions_path)

    def test_resealed_review_only_decisions_cannot_be_applied(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = _write_inputs(
                root,
                calibration_per_class=15,
                audit_per_class=15,
                include_application_cases=True,
            )
            output_dir = root / "policy-output"
            fit_selective_policy(**inputs, output_dir=output_dir)
            policy_path = output_dir / "selective_policy.v1.json"
            decisions_path = output_dir / "selective_decisions.v1.json"
            policy = _read_json(policy_path)
            decisions = _read_json(decisions_path)
            target = next(row for row in decisions["decisions"] if row["candidate_id"] == "application-ball")
            target["decision"] = "accept"
            target["applied_to_contract"] = True
            _reseal_decision_artifact(policy_path, decisions_path, policy, decisions)

            with self.assertRaisesRegex(RuntimeError, "decision.*policy status|applied_to_contract"):
                validate_selective_decisions_binding(policy_path, decisions_path)

    def test_strict_decision_semantics_reject_self_consistent_forgery_against_real_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = _write_inputs(
                root,
                calibration_per_class=1000,
                audit_per_class=400,
                include_application_cases=True,
                include_forced_cases=True,
            )
            output_dir = root / "policy-output"
            fit_selective_policy(**inputs, output_dir=output_dir)
            policy_path = output_dir / "selective_policy.v1.json"
            decisions_path = output_dir / "selective_decisions.v1.json"
            valid_policy = _read_json(policy_path)
            valid_decisions = _read_json(decisions_path)
            contract = _read_json(inputs["resolved_contract_path"])
            model = _read_json(inputs["model_manifest_path"])
            rows = _evaluation_rows(
                _read_json(inputs["predictions_path"]),
                _read_json(inputs["dataset_manifest_path"]),
                _read_json(inputs["annotation_resolution_path"]),
                contract,
                _read_json(inputs["policy_roles_path"]),
                supported_mask=model["supported_mask"],
            )
            validate_selective_decision_semantics(valid_policy, valid_decisions, rows, contract)
            forged_rows = deepcopy(rows)
            forged_evidence = next(row for row in forged_rows if row["candidate_id"] == "application-ball")
            forged_evidence["has_existing_decision"] = True
            forged_evidence["base_forced_reasons"] = ["existing_decision"]
            with self.assertRaisesRegex(RuntimeError, "does not match existing decisions.*resolved contract"):
                validate_selective_decision_semantics(valid_policy, valid_decisions, forged_rows, contract)

            def score_flip(decisions: dict[str, object]) -> None:
                target = next(row for row in decisions["decisions"] if row["candidate_id"] == "application-noise")
                target.update(
                    {
                        "accept_score": 0.995,
                        "reject_score": 0.005,
                        "unknown_score": 0.0,
                        "top_label": "match_ball",
                        "top_margin": 0.99,
                        "raw_decision": "accept",
                        "decision": "accept",
                    }
                )

            def hide_existing_decision(decisions: dict[str, object]) -> None:
                target = next(row for row in decisions["decisions"] if row["candidate_id"] == "forced-existing")
                target["forced_abstain_reasons"] = []
                target["existing_decision_preserved"] = False
                target["raw_decision"] = "accept"
                target["decision"] = "accept"
                target["applied_to_contract"] = True

            for name, mutate in {
                "self-consistent-score-flip": score_flip,
                "hidden-existing-contract-decision": hide_existing_decision,
            }.items():
                with self.subTest(name=name):
                    policy = deepcopy(valid_policy)
                    decisions = deepcopy(valid_decisions)
                    mutate(decisions)
                    _reseal_decision_artifact(policy_path, decisions_path, policy, decisions)
                    forged_policy = _read_json(policy_path)
                    forged_decisions = _read_json(decisions_path)
                    validate_selective_decisions_binding(policy_path, decisions_path)
                    with self.assertRaisesRegex(RuntimeError, "authoritative evidence.*resolved contract"):
                        validate_selective_decision_semantics(forged_policy, forged_decisions, rows, contract)

    def test_qualified_policy_binding_rejects_inconsistent_qualification_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = _write_inputs(root, calibration_per_class=1000, audit_per_class=400)
            output_dir = root / "policy-output"
            fit_selective_policy(**inputs, output_dir=output_dir)
            policy_path = output_dir / "selective_policy.v1.json"
            decisions_path = output_dir / "selective_decisions.v1.json"
            valid_policy = _read_json(policy_path)
            valid_decisions = _read_json(decisions_path)

            def calibration_components_below_minimum(policy: dict[str, object]) -> None:
                policy["inferential_unit"]["calibration_component_count"] = 2
                policy["version_inputs"]["inferential_unit"]["calibration_component_count"] = 2
                policy["calibration"]["calibration_count"] = 2
                policy["calibration"]["calibration_component_count"] = 2
                policy["calibration"]["independent_component_gate"] = {
                    "observed": 2,
                    "minimum": 3,
                    "passed": False,
                }

            def audit_accepted_below_minimum(policy: dict[str, object]) -> None:
                policy["audit"]["one_sided_confidence"]["accepted_component_count"] = 99
                policy["audit"]["sample_gates"]["accepted_components"] = {
                    "observed": 99,
                    "minimum": 100,
                    "passed": False,
                }

            def audit_components_below_minimum(policy: dict[str, object]) -> None:
                policy["audit"]["audit_component_count"] = 2
                policy["audit"]["one_sided_confidence"]["accepted_component_count"] = 2
                policy["audit"]["one_sided_confidence"]["true_ball_component_count"] = 2
                policy["audit"]["sample_gates"] = {
                    "accepted_components": {"observed": 2, "minimum": 100, "passed": False},
                    "true_ball_components": {"observed": 2, "minimum": 300, "passed": False},
                    "independent_components": {"observed": 2, "minimum": 3, "passed": False},
                }
                policy["inferential_unit"]["audit_component_count"] = 2
                policy["version_inputs"]["inferential_unit"]["audit_component_count"] = 2

            def audit_true_balls_below_minimum(policy: dict[str, object]) -> None:
                policy["audit"]["one_sided_confidence"]["true_ball_component_count"] = 299
                policy["audit"]["sample_gates"]["true_ball_components"] = {
                    "observed": 299,
                    "minimum": 300,
                    "passed": False,
                }

            def point_targets_failed(policy: dict[str, object]) -> None:
                policy["audit"]["point_targets_passed"] = False

            def exact_confidence_failed(policy: dict[str, object]) -> None:
                policy["audit"]["one_sided_confidence"]["passed"] = False

            mutations = {
                "calibration-components": calibration_components_below_minimum,
                "audit-components": audit_components_below_minimum,
                "audit-accepted": audit_accepted_below_minimum,
                "audit-true-balls": audit_true_balls_below_minimum,
                "point-targets": point_targets_failed,
                "exact-confidence": exact_confidence_failed,
            }
            for name, mutate in mutations.items():
                with self.subTest(name=name):
                    policy = deepcopy(valid_policy)
                    decisions = deepcopy(valid_decisions)
                    mutate(policy)
                    policy["qualification_evidence"] = _qualification_evidence_summary(
                        policy["calibration"], policy["audit"]
                    )
                    policy["version_inputs"]["qualification_evidence"] = deepcopy(policy["qualification_evidence"])
                    policy["version_inputs"]["calibration_sha256"] = _canonical_hash(policy["calibration"])
                    policy["version_inputs"]["audit_sha256"] = _canonical_hash(policy["audit"])
                    policy_version = _canonical_hash(policy["version_inputs"])
                    policy["policy_version"] = policy_version
                    decisions["policy_version"] = policy_version
                    _write_json(decisions_path, decisions)
                    policy["decisions_artifact"]["sha256"] = _sha256(decisions_path)
                    _write_json(policy_path, policy)

                    with self.assertRaisesRegex(RuntimeError, "calibration|audit|qualification|cohort"):
                        validate_selective_decisions_binding(policy_path, decisions_path)

    def test_resealed_policy_cannot_self_sign_fake_calibration_or_audit_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = _write_inputs(root, calibration_per_class=1000, audit_per_class=400)
            output_dir = root / "policy-output"
            fit_selective_policy(**inputs, output_dir=output_dir)
            policy_path = output_dir / "selective_policy.v1.json"
            decisions_path = output_dir / "selective_decisions.v1.json"
            valid_policy = _read_json(policy_path)
            valid_decisions = _read_json(decisions_path)

            def selected_hypothesis_empty(policy: dict[str, object]) -> None:
                policy["calibration"]["selected_hypothesis"] = {}

            def certified_pair_bad_id(policy: dict[str, object]) -> None:
                policy["calibration"]["certified_pairs"][0]["accept_hypothesis_id"] = "accept-not-rejected"

            def certified_pair_bad_threshold(policy: dict[str, object]) -> None:
                policy["calibration"]["certified_pairs"][0]["accept_threshold"] = 0.123

            def fake_hypothesis_p_value(policy: dict[str, object]) -> None:
                policy["calibration"]["accept_hypotheses"][0]["p_value"] = 0.0

            def fake_holm_set(policy: dict[str, object]) -> None:
                policy["calibration"]["holm_rejected_hypotheses"] = []

            def minimal_audit(policy: dict[str, object]) -> None:
                policy["audit"] = {}

            def wrong_exact_bound(policy: dict[str, object]) -> None:
                policy["audit"]["one_sided_confidence"]["accept_error_exact_upper"] = 0.0

            def wrong_benchmark_error(policy: dict[str, object]) -> None:
                policy["audit"]["benchmark"]["candidate_evaluations"][0]["truth"] = "noise"

            def wrong_sample_gate(policy: dict[str, object]) -> None:
                policy["audit"]["sample_gates"]["accepted_components"]["passed"] = False

            mutations = {
                "selected-empty": selected_hypothesis_empty,
                "pair-bad-id": certified_pair_bad_id,
                "pair-bad-threshold": certified_pair_bad_threshold,
                "fake-p-value": fake_hypothesis_p_value,
                "fake-holm": fake_holm_set,
                "minimal-audit": minimal_audit,
                "wrong-exact-bound": wrong_exact_bound,
                "wrong-benchmark-error": wrong_benchmark_error,
                "wrong-sample-gate": wrong_sample_gate,
            }
            for name, mutate in mutations.items():
                with self.subTest(name=name):
                    policy = deepcopy(valid_policy)
                    decisions = deepcopy(valid_decisions)
                    mutate(policy)
                    policy["qualification_evidence"] = _qualification_evidence_summary(
                        policy["calibration"], policy["audit"]
                    )
                    policy["version_inputs"]["qualification_evidence"] = deepcopy(policy["qualification_evidence"])
                    policy["version_inputs"]["calibration_sha256"] = _canonical_hash(policy["calibration"])
                    policy["version_inputs"]["audit_sha256"] = _canonical_hash(policy["audit"])
                    policy_version = _canonical_hash(policy["version_inputs"])
                    policy["policy_version"] = policy_version
                    decisions["policy_version"] = policy_version
                    _write_json(decisions_path, decisions)
                    policy["decisions_artifact"]["sha256"] = _sha256(decisions_path)
                    _write_json(policy_path, policy)

                    with self.assertRaisesRegex(RuntimeError, "calibration|audit|hypothesis|Holm|pair"):
                        validate_selective_decisions_binding(policy_path, decisions_path)

    def test_resealed_policy_and_decisions_require_exact_evaluation_cohort_partition(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = _write_inputs(
                root,
                calibration_per_class=1000,
                audit_per_class=400,
                include_application_cases=True,
            )
            output_dir = root / "policy-output"
            fit_selective_policy(**inputs, output_dir=output_dir)
            policy_path = output_dir / "selective_policy.v1.json"
            decisions_path = output_dir / "selective_decisions.v1.json"
            valid_policy = _read_json(policy_path)
            valid_decisions = _read_json(decisions_path)

            def bogus_audit_benchmark_id(policy: dict[str, object], decisions: dict[str, object]) -> None:
                del decisions
                policy["audit"]["benchmark"]["candidate_evaluations"][0]["candidate_id"] = "bogus-audit-id"

            def missing_cohort_id(policy: dict[str, object], decisions: dict[str, object]) -> None:
                del decisions
                policy["evaluation_cohorts"]["calibration_candidate_ids"].pop()

            def extra_cohort_id(policy: dict[str, object], decisions: dict[str, object]) -> None:
                del decisions
                policy["evaluation_cohorts"]["audit_candidate_ids"].append("extra-audit-id")

            def duplicate_cohort_id(policy: dict[str, object], decisions: dict[str, object]) -> None:
                del decisions
                candidate_ids = policy["evaluation_cohorts"]["audit_candidate_ids"]
                candidate_ids.append(candidate_ids[0])
                candidate_ids.sort()

            def cohort_role_mismatch(policy: dict[str, object], decisions: dict[str, object]) -> None:
                candidate_id = policy["evaluation_cohorts"]["calibration_candidate_ids"][0]
                row = next(row for row in decisions["decisions"] if row["candidate_id"] == candidate_id)
                row["policy_role"] = "policy_audit"

            def cohort_scope_mismatch(policy: dict[str, object], decisions: dict[str, object]) -> None:
                candidate_id = policy["evaluation_cohorts"]["audit_candidate_ids"][0]
                row = next(row for row in decisions["decisions"] if row["candidate_id"] == candidate_id)
                row["decision_scope"] = "application"

            def missing_cohort_decision(policy: dict[str, object], decisions: dict[str, object]) -> None:
                candidate_id = policy["evaluation_cohorts"]["calibration_candidate_ids"][0]
                decisions["decisions"] = [row for row in decisions["decisions"] if row["candidate_id"] != candidate_id]

            def duplicate_cohort_decision(policy: dict[str, object], decisions: dict[str, object]) -> None:
                candidate_id = policy["evaluation_cohorts"]["audit_candidate_ids"][0]
                row = next(row for row in decisions["decisions"] if row["candidate_id"] == candidate_id)
                decisions["decisions"].append(deepcopy(row))

            def noncohort_evaluation_scope(policy: dict[str, object], decisions: dict[str, object]) -> None:
                cohort_ids = {
                    *policy["evaluation_cohorts"]["calibration_candidate_ids"],
                    *policy["evaluation_cohorts"]["audit_candidate_ids"],
                }
                row = next(row for row in decisions["decisions"] if row["candidate_id"] not in cohort_ids)
                row["decision_scope"] = "evaluation_only"

            def delete_application_decision(policy: dict[str, object], decisions: dict[str, object]) -> None:
                candidate_id = policy["evaluation_cohorts"]["application_candidate_ids"][0]
                decisions["decisions"] = [row for row in decisions["decisions"] if row["candidate_id"] != candidate_id]

            def forge_application_decision(policy: dict[str, object], decisions: dict[str, object]) -> None:
                candidate_id = policy["evaluation_cohorts"]["application_candidate_ids"][0]
                row = deepcopy(next(row for row in decisions["decisions"] if row["candidate_id"] == candidate_id))
                row["candidate_id"] = "forged-application-id"
                decisions["decisions"].append(row)

            def calibration_application_swap(policy: dict[str, object], decisions: dict[str, object]) -> None:
                calibration_ids = policy["evaluation_cohorts"]["calibration_candidate_ids"]
                application_ids = policy["evaluation_cohorts"]["application_candidate_ids"]
                calibration_id = calibration_ids[0]
                application_id = application_ids[0]
                calibration_ids[0] = application_id
                application_ids[0] = calibration_id
                calibration_ids.sort()
                application_ids.sort()
                calibration_row = next(row for row in decisions["decisions"] if row["candidate_id"] == calibration_id)
                calibration_row.update(
                    {
                        "policy_role": None,
                        "decision_scope": "application",
                        "forced_abstain_reasons": [],
                    }
                )
                application_row = next(row for row in decisions["decisions"] if row["candidate_id"] == application_id)
                application_row.update(
                    {
                        "policy_role": "policy_calibration",
                        "decision_scope": "evaluation_only",
                        "decision": "abstain",
                        "applied_to_contract": False,
                        "forced_abstain_reasons": ["evaluation_holdout"],
                    }
                )

            def unsorted_application_cohort(policy: dict[str, object], decisions: dict[str, object]) -> None:
                del decisions
                policy["evaluation_cohorts"]["application_candidate_ids"].reverse()

            def duplicate_application_cohort(policy: dict[str, object], decisions: dict[str, object]) -> None:
                del decisions
                candidate_ids = policy["evaluation_cohorts"]["application_candidate_ids"]
                candidate_ids.append(candidate_ids[0])
                candidate_ids.sort()

            def overlapping_application_cohort(policy: dict[str, object], decisions: dict[str, object]) -> None:
                del decisions
                candidate_ids = policy["evaluation_cohorts"]["application_candidate_ids"]
                candidate_ids.append(policy["evaluation_cohorts"]["calibration_candidate_ids"][0])
                candidate_ids.sort()

            mutations = {
                "bogus-audit-id": bogus_audit_benchmark_id,
                "missing-cohort-id": missing_cohort_id,
                "extra-cohort-id": extra_cohort_id,
                "duplicate-cohort-id": duplicate_cohort_id,
                "role-mismatch": cohort_role_mismatch,
                "scope-mismatch": cohort_scope_mismatch,
                "missing-decision": missing_cohort_decision,
                "duplicate-decision": duplicate_cohort_decision,
                "noncohort-evaluation": noncohort_evaluation_scope,
                "delete-application": delete_application_decision,
                "forge-application": forge_application_decision,
                "calibration-application-swap": calibration_application_swap,
                "unsorted-application-cohort": unsorted_application_cohort,
                "duplicate-application-cohort": duplicate_application_cohort,
                "overlapping-application-cohort": overlapping_application_cohort,
            }
            for name, mutate in mutations.items():
                with self.subTest(name=name):
                    policy = deepcopy(valid_policy)
                    decisions = deepcopy(valid_decisions)
                    mutate(policy, decisions)
                    policy["qualification_evidence"] = _qualification_evidence_summary(
                        policy["calibration"], policy["audit"]
                    )
                    policy["version_inputs"]["qualification_evidence"] = deepcopy(policy["qualification_evidence"])
                    policy["version_inputs"]["evaluation_cohorts"] = deepcopy(policy["evaluation_cohorts"])
                    policy["version_inputs"]["calibration_sha256"] = _canonical_hash(policy["calibration"])
                    policy["version_inputs"]["audit_sha256"] = _canonical_hash(policy["audit"])
                    decisions["summary"] = _decisions_summary(decisions["decisions"])
                    decisions_content = {
                        key: value for key, value in decisions.items() if key not in {"generated_at", "policy_version"}
                    }
                    content_sha256 = _canonical_hash(decisions_content)
                    policy["version_inputs"]["decisions_content_sha256"] = content_sha256
                    policy_version = _canonical_hash(policy["version_inputs"])
                    policy["policy_version"] = policy_version
                    decisions["policy_version"] = policy_version
                    _write_json(decisions_path, decisions)
                    policy["decisions_artifact"] = {
                        "path": "selective_decisions.v1.json",
                        "sha256": _sha256(decisions_path),
                        "content_sha256": content_sha256,
                    }
                    _write_json(policy_path, policy)

                    with self.assertRaisesRegex(
                        RuntimeError, "cohort|candidate_ids|decision|audit benchmark|population"
                    ):
                        validate_selective_decisions_binding(policy_path, decisions_path)

    def test_training_report_must_be_exact_file_in_model_package(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = _write_inputs(root, calibration_per_class=3, audit_per_class=3)
            external = root / "copied-report.json"
            external.write_bytes(inputs["training_report_path"].read_bytes())
            copied_inputs = dict(inputs)
            copied_inputs["training_report_path"] = external

            with self.assertRaisesRegex(RuntimeError, "training report.*model package"):
                fit_selective_policy(**copied_inputs, output_dir=root / "output")
            with self.assertRaisesRegex(RuntimeError, "training report.*model package"):
                build_selective_policy_roles(
                    **_role_builder_inputs(copied_inputs),
                    output_dir=root / "roles",
                )

    def test_large_disjoint_calibration_and_audit_qualify_and_preserve_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = _write_inputs(root, calibration_per_class=1000, audit_per_class=400)
            output_dir = root / "policy-output"

            policy = fit_selective_policy(**inputs, output_dir=output_dir)
            decisions = _read_json(output_dir / "selective_decisions.v1.json")
            report = _read_json(output_dir / "selective_acceptance_report.v1.json")
            derived = _read_json(output_dir / "tracking_contract.v2.json")
            source = _read_json(inputs["resolved_contract_path"])

        self.assertEqual("qualified", policy["status"])
        self.assertGreater(policy["thresholds"]["accept"] + policy["thresholds"]["reject"], 1.0)
        self.assertTrue(policy["calibration"]["certified"])
        self.assertEqual("qualified", report["status"])
        self.assertEqual(1.0, report["audit"]["benchmark"]["metrics"]["auto_accepted_candidate_precision"]["value"])
        self.assertEqual(0.0, report["audit"]["benchmark"]["metrics"]["true_ball_false_reject_rate"]["value"])
        self.assertTrue(report["audit"]["reconciled"])
        self.assertEqual(
            "heterogeneity_descriptive_diagnostic_v2",
            report["audit"]["cluster_gate"]["method"],
        )
        self.assertFalse(report["audit"]["cluster_gate"]["affects_qualification"])
        self.assertEqual("fixed_aggregate_audit_cohort", report["audit"]["qualification_scope"])
        self.assertEqual("none", report["audit"]["cluster_gate"]["per_cluster_statistical_guarantee"])
        self.assertEqual("connected_evidence_component", policy["inferential_unit"]["name"])
        self.assertEqual(2000, policy["inferential_unit"]["calibration_component_count"])
        self.assertEqual(800, policy["inferential_unit"]["audit_component_count"])
        self.assertEqual(2000, policy["calibration"]["calibration_component_count"])
        self.assertEqual(800, report["audit"]["audit_component_count"])
        self.assertEqual(policy["qualification_evidence"], policy["version_inputs"]["qualification_evidence"])
        self.assertEqual(policy["qualification_evidence"], report["qualification_evidence"])
        qualification_evidence = policy["qualification_evidence"]
        self.assertEqual(2000, qualification_evidence["calibration"]["calibration_component_count"])
        self.assertEqual(800, qualification_evidence["audit"]["audit_component_count"])
        self.assertEqual(400, qualification_evidence["audit"]["accepted_component_count"])
        self.assertEqual(400, qualification_evidence["audit"]["true_ball_component_count"])
        self.assertTrue(qualification_evidence["audit"]["point_targets_passed"])
        self.assertTrue(qualification_evidence["audit"]["exact_confidence_passed"])
        self.assertTrue(all(row["decision_scope"] == "evaluation_only" for row in decisions["decisions"]))
        self.assertTrue(all(row["raw_decision"] == "abstain" for row in decisions["decisions"]))
        self.assertTrue(all(row["decision"] == "abstain" for row in decisions["decisions"]))
        self.assertTrue(all(not row["applied_to_contract"] for row in decisions["decisions"]))
        self.assertTrue(all("evaluation_holdout" in row["forced_abstain_reasons"] for row in decisions["decisions"]))
        self.assertEqual(source["classifications"], derived["classifications"])
        self.assertEqual(source["decisions"], derived["decisions"])

    def test_qualified_policy_only_applies_to_application_cohort(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = _write_inputs(
                root,
                calibration_per_class=1000,
                audit_per_class=400,
                include_application_cases=True,
            )
            output_dir = root / "policy-output"

            policy = fit_selective_policy(**inputs, output_dir=output_dir)
            decision_rows = {
                row["candidate_id"]: row for row in _read_json(output_dir / "selective_decisions.v1.json")["decisions"]
            }
            derived = _read_json(output_dir / "tracking_contract.v2.json")

        self.assertEqual("qualified", policy["status"])
        self.assertEqual("accept", decision_rows["application-ball"]["decision"])
        self.assertEqual("reject", decision_rows["application-noise"]["decision"])
        for candidate_id in ("application-ball", "application-noise"):
            self.assertEqual("application", decision_rows[candidate_id]["decision_scope"])
            self.assertTrue(decision_rows[candidate_id]["applied_to_contract"])
        applied = {row["candidate_id"]: row["decision"] for row in derived["decisions"]}
        self.assertEqual({"application-ball": "accept", "application-noise": "reject"}, applied)

    def test_entire_policy_population_is_disjoint_from_all_model_split_evidence(self) -> None:
        overlaps = {
            "candidate_id": ("candidate_ids", "application-ball"),
            "variant_id": ("variant_ids", "application-only"),
            "video_sha256": (
                "video_sha256",
                hashlib.sha256(b"eval-video-application-only").hexdigest(),
            ),
            "group_id": ("group_ids", "group-application-only"),
            "split_group": ("split_groups", "split-application-only"),
            "temporal_group": ("temporal_groups", "temporal-application-only"),
        }
        for dimension, (report_key, value) in overlaps.items():
            with self.subTest(dimension=dimension), tempfile.TemporaryDirectory() as temp_name:
                root = Path(temp_name)
                inputs = _write_inputs(
                    root,
                    calibration_per_class=8,
                    audit_per_class=8,
                    include_application_cases=True,
                )
                _mutate_json(
                    inputs["training_report_path"],
                    lambda report, report_key=report_key, value=value: report["split"]["evidence_by_split"][
                        "train"
                    ].__setitem__(report_key, [value]),
                    update_manifest=inputs["model_manifest_path"],
                )
                roles = _read_json(inputs["policy_roles_path"])
                roles["lineage"]["training_report_sha256"] = _sha256(inputs["training_report_path"])
                roles["lineage"]["model_manifest_sha256"] = _sha256(inputs["model_manifest_path"])
                _write_json(inputs["policy_roles_path"], roles)
                with self.assertRaisesRegex(SelectivePolicyError, f"policy population.*{dimension}"):
                    fit_selective_policy(**inputs, output_dir=root / "output")

    def test_policy_version_ignores_diagnostic_timestamps_but_binds_audit_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = _write_inputs(root, calibration_per_class=8, audit_per_class=8)
            first = fit_selective_policy(**inputs, output_dir=root / "first")
            second = fit_selective_policy(**inputs, output_dir=root / "second")
            self.assertEqual(first["policy_version"], second["policy_version"])
            self.assertEqual(first["version_inputs"]["audit_sha256"], second["version_inputs"]["audit_sha256"])
            for output_name, policy in (("first", first), ("second", second)):
                acceptance = _read_json(root / output_name / "selective_acceptance_report.v1.json")
                self.assertEqual(policy["version_inputs"], acceptance["version_inputs"])
                self.assertEqual(
                    policy["version_inputs"]["audit_sha256"],
                    _canonical_hash(_stable_diagnostic_identity(acceptance["audit"])),
                )
                self.assertEqual(policy["audit"], _stable_diagnostic_identity(acceptance["audit"]))

        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            (root / "baseline").mkdir()
            (root / "changed").mkdir()
            baseline_inputs = _write_inputs(root / "baseline", calibration_per_class=1000, audit_per_class=8)
            changed_inputs = _write_inputs(
                root / "changed",
                calibration_per_class=1000,
                audit_per_class=8,
                audit_cluster_accept_errors=1,
            )
            baseline = fit_selective_policy(**baseline_inputs, output_dir=root / "baseline-output")
            changed = fit_selective_policy(**changed_inputs, output_dir=root / "changed-output")
            self.assertNotEqual(
                baseline["version_inputs"]["audit_sha256"],
                changed["version_inputs"]["audit_sha256"],
            )

    def test_policy_cli_wrappers_return_one_json_error_for_malformed_json_shapes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = _write_inputs(root, calibration_per_class=3, audit_per_class=3)
            dataset = _read_json(inputs["dataset_manifest_path"])
            dataset["summary"] = []
            _write_json(inputs["dataset_manifest_path"], dataset)
            scripts_root = Path(__file__).resolve().parents[1] / "scripts"
            commands = {
                "roles": [
                    sys.executable,
                    str(scripts_root / "build_selective_policy_roles.py"),
                    *_role_builder_argv(inputs, root / "roles-output"),
                ],
                "fit": [
                    sys.executable,
                    str(scripts_root / "fit_selective_policy.py"),
                    *_fit_argv(inputs, root / "fit-output"),
                ],
            }
            for name, command in commands.items():
                with self.subTest(name=name):
                    completed = subprocess.run(command, capture_output=True, text=True, check=False)
                    self.assertNotEqual(0, completed.returncode)
                    self.assertEqual("", completed.stdout)
                    error_lines = completed.stderr.splitlines()
                    self.assertEqual(1, len(error_lines))
                    self.assertFalse(json.loads(error_lines[0])["ok"])
                    self.assertNotIn("Traceback", completed.stderr)

            for script_name in ("build_selective_policy_roles.py", "fit_selective_policy.py"):
                with self.subTest(help=script_name):
                    completed = subprocess.run(
                        [sys.executable, str(scripts_root / script_name), "--help"],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(0, completed.returncode)
                    self.assertIn("usage:", completed.stdout.lower())
                    self.assertEqual("", completed.stderr)

    def test_review_only_application_abstains_remain_unapplied_for_next_round(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = _write_inputs(
                root,
                calibration_per_class=15,
                audit_per_class=15,
                include_application_cases=True,
            )
            output_dir = root / "policy-output"

            policy = fit_selective_policy(**inputs, output_dir=output_dir)
            decision_rows = {
                row["candidate_id"]: row for row in _read_json(output_dir / "selective_decisions.v1.json")["decisions"]
            }
            derived = _read_json(output_dir / "tracking_contract.v2.json")

        self.assertEqual("review_only", policy["status"])
        for candidate_id in ("application-ball", "application-noise"):
            self.assertEqual("abstain", decision_rows[candidate_id]["decision"])
            self.assertFalse(decision_rows[candidate_id]["applied_to_contract"])
        self.assertFalse(
            {"application-ball", "application-noise"} & {row["candidate_id"] for row in derived["decisions"]}
        )

    def test_point_pass_without_statistical_evidence_is_review_only_and_all_abstain(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = _write_inputs(root, calibration_per_class=15, audit_per_class=15)
            output_dir = root / "policy-output"

            policy = fit_selective_policy(**inputs, output_dir=output_dir)
            report = _read_json(output_dir / "selective_acceptance_report.v1.json")
            decisions = _read_json(output_dir / "selective_decisions.v1.json")
            derived = _read_json(output_dir / "tracking_contract.v2.json")
            source = _read_json(inputs["resolved_contract_path"])

        self.assertEqual("review_only", policy["status"])
        self.assertEqual("insufficient_evidence", report["status"])
        self.assertTrue(all(row["decision"] == "abstain" for row in decisions["decisions"]))
        self.assertTrue(all(not row["applied_to_contract"] for row in decisions["decisions"]))
        self.assertEqual(source["decisions"], derived["decisions"])

    def test_many_candidates_from_one_component_fail_before_inference(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = _write_inputs(
                root,
                calibration_per_class=500,
                audit_per_class=400,
                correlated_evaluation=True,
            )
            with self.assertRaisesRegex(RuntimeError, "exactly one human-confirmed evaluation candidate"):
                fit_selective_policy(**inputs, output_dir=root / "policy-output")
            with self.assertRaisesRegex(RuntimeError, "exactly one human-confirmed evaluation candidate"):
                build_selective_policy_roles(
                    **_role_builder_inputs(inputs),
                    output_dir=root / "roles-output",
                )

        self.assertFalse((root / "policy-output").exists())
        self.assertFalse((root / "roles-output").exists())

    def test_no_feasible_certified_pair_is_review_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = _write_inputs(
                root,
                calibration_per_class=1000,
                audit_per_class=400,
                calibration_ambiguous=True,
            )
            output_dir = root / "policy-output"

            policy = fit_selective_policy(**inputs, output_dir=output_dir)
            decisions = _read_json(output_dir / "selective_decisions.v1.json")

        self.assertEqual("review_only", policy["status"])
        self.assertIsNone(policy["calibration"]["selected_hypothesis"])
        self.assertTrue(all(row["decision"] == "abstain" for row in decisions["decisions"]))

    def test_per_value_cluster_support_is_diagnostic_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = _write_inputs(
                root,
                calibration_per_class=500,
                audit_per_class=400,
            )
            output_dir = root / "policy-output"

            policy = fit_selective_policy(**inputs, output_dir=output_dir)
            report = _read_json(output_dir / "selective_acceptance_report.v1.json")

        self.assertEqual("qualified", policy["status"])
        self.assertTrue(report["audit"]["point_targets_passed"])
        self.assertFalse(report["audit"]["cluster_gate"]["passed"])
        self.assertFalse(report["audit"]["cluster_gate"]["affects_qualification"])
        self.assertEqual("diagnostic_only", report["audit"]["cluster_gate"]["purpose"])
        self.assertEqual("qualified", report["audit"]["status"])
        self.assertEqual("qualified", report["status"])

    def test_audit_status_distinguishes_powered_failure_from_insufficient_evidence(self) -> None:
        cases = (
            (
                "aggregate-point-failure",
                {"audit_per_class": 400, "audit_cluster_accept_errors": 20},
                "failed",
                False,
            ),
            (
                "low-component-support",
                {"audit_per_class": 2},
                "insufficient_evidence",
                True,
            ),
            (
                "confidence-only",
                {"audit_per_class": 300},
                "insufficient_evidence",
                True,
            ),
        )
        for name, overrides, expected_status, expected_points in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp_name:
                root = Path(temp_name)
                inputs = _write_inputs(
                    root,
                    calibration_per_class=1000,
                    **overrides,
                )
                output_dir = root / "policy-output"
                policy = fit_selective_policy(**inputs, output_dir=output_dir)
                report = _read_json(output_dir / "selective_acceptance_report.v1.json")
                self.assertEqual("review_only", policy["status"])
                self.assertEqual(expected_points, report["audit"]["point_targets_passed"])
                self.assertEqual(expected_status, report["audit"]["status"])
                self.assertEqual(expected_status, report["status"])

    def test_transaction_keyboard_interrupt_and_input_mutation_roll_back(self) -> None:
        from football_tracking import selective_policy

        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = _write_inputs(root, calibration_per_class=1000, audit_per_class=400)
            output_dir = root / "interrupted"
            real_write = selective_policy._write_json

            def interrupt_decisions(path: Path, value: dict[str, object]) -> None:
                if path.name == "selective_decisions.v1.json":
                    raise KeyboardInterrupt("injected")
                real_write(path, value)

            with patch.object(selective_policy, "_write_json", side_effect=interrupt_decisions):
                with self.assertRaisesRegex(KeyboardInterrupt, "injected"):
                    fit_selective_policy(**inputs, output_dir=output_dir)
            self.assertFalse(output_dir.exists())
            self.assertEqual([], list(root.glob(".interrupted.staging-*")))

            mutation_output = root / "mutated"
            real_audit = selective_policy._audit_fixed_policy

            def mutate_input(*args: object, **kwargs: object) -> dict[str, object]:
                result = real_audit(*args, **kwargs)
                inputs["predictions_path"].write_bytes(inputs["predictions_path"].read_bytes() + b" ")
                return result

            with patch.object(selective_policy, "_audit_fixed_policy", side_effect=mutate_input):
                with self.assertRaisesRegex(RuntimeError, "input changed"):
                    fit_selective_policy(**inputs, output_dir=mutation_output)
            self.assertFalse(mutation_output.exists())
            self.assertEqual([], list(root.glob(".mutated.staging-*")))

    def test_unknown_conflict_existing_decision_and_margin_force_abstain_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = _write_inputs(
                root,
                calibration_per_class=1000,
                audit_per_class=400,
                include_forced_cases=True,
            )
            output_dir = root / "policy-output"

            fit_selective_policy(**inputs, output_dir=output_dir)
            decisions = {
                row["candidate_id"]: row for row in _read_json(output_dir / "selective_decisions.v1.json")["decisions"]
            }
            derived = _read_json(output_dir / "tracking_contract.v2.json")
            source = _read_json(inputs["resolved_contract_path"])

        for candidate_id in ("forced-unknown", "forced-conflict", "forced-existing", "forced-margin"):
            self.assertEqual("abstain", decisions[candidate_id]["decision"])
            self.assertTrue(decisions[candidate_id]["forced_abstain_reasons"])
            self.assertFalse(decisions[candidate_id]["applied_to_contract"])
        self.assertEqual(source["decisions"], derived["decisions"])
        self.assertEqual(1, sum(row["candidate_id"] == "forced-existing" for row in derived["decisions"]))

    def test_tampering_leakage_and_nonfinite_probabilities_fail_closed(self) -> None:
        mutators = {
            "probabilities": lambda paths: _mutate_json(
                paths["predictions_path"],
                lambda value: value["predictions"][0]["probabilities"].__setitem__("match_ball", math.nan),
            ),
            "fingerprint": lambda paths: _mutate_json(
                paths["predictions_path"],
                lambda value: value["predictions"][0].__setitem__("candidate_fingerprint", "0" * 64),
            ),
            "leakage": lambda paths: _mutate_json(
                paths["training_report_path"],
                lambda value: value["split"]["evidence_by_split"]["train"].__setitem__(
                    "video_sha256", [_read_json(paths["dataset_manifest_path"])["sources"][0]["sha256"]]
                ),
                update_manifest=paths["model_manifest_path"],
            ),
        }
        for name, mutate in mutators.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp_name:
                root = Path(temp_name)
                inputs = _write_inputs(root, calibration_per_class=1000, audit_per_class=400)
                mutate(inputs)
                with self.assertRaises((ValueError, RuntimeError)):
                    fit_selective_policy(**inputs, output_dir=root / "output")
                self.assertFalse((root / "output").exists())

    def test_model_weights_tamper_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = _write_inputs(root, calibration_per_class=1000, audit_per_class=400)
            (root / "model.pt").write_bytes(b"tampered")
            with self.assertRaisesRegex(RuntimeError, "weights sha256"):
                fit_selective_policy(**inputs, output_dir=root / "output")

    def test_role_manifest_cannot_cherry_pick_or_change_predeclared_seed(self) -> None:
        for mutation, message in (
            (
                lambda roles: roles["roles"]["policy_audit"].pop(),
                "cover every confirmed binary candidate|component",
            ),
            (
                lambda roles: roles.__setitem__("assignment_seed", "searched-after-labels"),
                "predeclared",
            ),
            (
                lambda roles: roles["candidate_component_mapping"][0].__setitem__("component_id", "0" * 64),
                "candidate/component mapping",
            ),
        ):
            with self.subTest(message=message), tempfile.TemporaryDirectory() as temp_name:
                root = Path(temp_name)
                inputs = _write_inputs(root, calibration_per_class=1000, audit_per_class=400)
                roles = _read_json(inputs["policy_roles_path"])
                mutation(roles)
                _write_json(inputs["policy_roles_path"], roles)
                with self.assertRaisesRegex(RuntimeError, message):
                    fit_selective_policy(**inputs, output_dir=root / "output")

    def test_policy_evaluation_truth_must_be_human_confirmed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = _write_inputs(
                root,
                calibration_per_class=1000,
                audit_per_class=400,
                evaluation_origin="ai_confirmed",
            )

            with self.assertRaisesRegex(RuntimeError, "human_confirmed"):
                fit_selective_policy(**inputs, output_dir=root / "output")
            self.assertFalse((root / "output").exists())

    def test_unsupported_model_class_probability_must_be_zero(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = _write_inputs(
                root,
                calibration_per_class=1000,
                audit_per_class=400,
                unsupported_label="player_body_or_shoe",
                unsupported_probability=0.001,
            )

            with self.assertRaisesRegex(RuntimeError, "unsupported model class"):
                fit_selective_policy(**inputs, output_dir=root / "output")
            self.assertFalse((root / "output").exists())


class SelectivePolicyRoleBuilderTests(unittest.TestCase):
    def test_metadata_renames_do_not_change_component_ids_or_roles(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = _write_inputs(root, calibration_per_class=3, audit_per_class=3)
            baseline = build_selective_policy_roles(
                **_role_builder_inputs(inputs),
                output_dir=root / "baseline-roles",
            )

            dataset = _read_json(inputs["dataset_manifest_path"])
            renamed_variants: dict[str, str] = {}
            for index, source in enumerate(dataset["sources"]):
                old_variant = source["variant_id"]
                renamed_variants[old_variant] = f"renamed-variant-{index}"
                source["variant_id"] = renamed_variants[old_variant]
                source["group_id"] = f"renamed-group-{index}"
                source["split_group"] = f"renamed-split-{index}"
                source["temporal_group"] = f"renamed-temporal-{index}"
            source_by_candidate = {
                candidate_id: source for source in dataset["sources"] for candidate_id in source["candidate_ids"]
            }
            for sample in dataset["samples"]:
                source = source_by_candidate[sample["candidate_id"]]
                sample["variant_id"] = source["variant_id"]
                sample["group_id"] = source["group_id"]
                sample["split_group"] = source["split_group"]
                sample["temporal_group"] = source["temporal_group"]
            _write_json(inputs["dataset_manifest_path"], dataset)
            resolution = _read_json(inputs["annotation_resolution_path"])
            resolution["source_dataset_manifest"]["sha256"] = _sha256(inputs["dataset_manifest_path"])
            _write_json(inputs["annotation_resolution_path"], resolution)

            renamed = build_selective_policy_roles(
                **_role_builder_inputs(inputs),
                output_dir=root / "renamed-roles",
            )

        self.assertEqual(baseline["roles"], renamed["roles"])
        self.assertEqual(
            [component["component_id"] for component in baseline["components"]],
            [component["component_id"] for component in renamed["components"]],
        )

    def test_builder_excludes_ai_confirmed_truth_from_evaluation_roles(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = _write_inputs(
                root,
                calibration_per_class=3,
                audit_per_class=3,
                include_ai_confirmed_case=True,
            )

            manifest = build_selective_policy_roles(
                **_role_builder_inputs(inputs),
                output_dir=root / "roles",
            )

        assigned = {candidate_id for candidate_ids in manifest["roles"].values() for candidate_id in candidate_ids}
        self.assertNotIn("ai-confirmed-application", assigned)

    def test_role_assignment_does_not_depend_on_prediction_scores(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            baseline_root = root / "baseline"
            changed_root = root / "changed-scores"
            baseline_root.mkdir()
            changed_root.mkdir()
            baseline_inputs = _write_inputs(baseline_root, calibration_per_class=3, audit_per_class=3)
            changed_inputs = _write_inputs(
                changed_root,
                calibration_per_class=3,
                audit_per_class=3,
                calibration_ambiguous=True,
            )

            baseline = build_selective_policy_roles(
                **_role_builder_inputs(baseline_inputs),
                output_dir=baseline_root / "roles",
            )
            changed = build_selective_policy_roles(
                **_role_builder_inputs(changed_inputs),
                output_dir=changed_root / "roles",
            )

        self.assertEqual(baseline["roles"], changed["roles"])
        self.assertEqual(baseline["components"], changed["components"])

    def test_builder_is_deterministic_and_feeds_fit_without_manual_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = _write_inputs(root, calibration_per_class=3, audit_per_class=3)
            first_dir = root / "roles-first"
            second_dir = root / "roles-second"

            first = build_selective_policy_roles(**_role_builder_inputs(inputs), output_dir=first_dir)
            second = build_selective_policy_roles(**_role_builder_inputs(inputs), output_dir=second_dir)
            first_path = first_dir / "selective_policy_roles.v1.json"
            second_path = second_dir / "selective_policy_roles.v1.json"

            self.assertEqual(first, second)
            self.assertEqual(first_path.read_bytes(), second_path.read_bytes())
            expected = _read_json(inputs["policy_roles_path"])
            self.assertEqual(expected["roles"], first["roles"])
            self.assertEqual(expected["components"], first["components"])
            self.assertEqual(expected["candidate_component_mapping"], first["candidate_component_mapping"])
            self.assertEqual(first["component_count"], first["evaluation_candidate_count"])
            self.assertEqual(len(first["components"]), first["component_count"])
            self.assertEqual(len(first["candidate_component_mapping"]), first["evaluation_candidate_count"])
            self.assertEqual(
                {row["candidate_id"] for row in first["candidate_component_mapping"]},
                {candidate_id for candidate_ids in first["roles"].values() for candidate_id in candidate_ids},
            )

            fit_inputs = dict(inputs)
            fit_inputs["policy_roles_path"] = first_path
            policy = fit_selective_policy(**fit_inputs, output_dir=root / "policy-output")

        self.assertEqual("review_only", policy["status"])

    def test_generated_manifest_tampering_cannot_cherry_pick(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = _write_inputs(root, calibration_per_class=3, audit_per_class=3)
            output_dir = root / "roles"
            build_selective_policy_roles(**_role_builder_inputs(inputs), output_dir=output_dir)
            roles_path = output_dir / "selective_policy_roles.v1.json"
            roles = _read_json(roles_path)
            roles["roles"]["policy_audit"].pop()
            _write_json(roles_path, roles)
            fit_inputs = dict(inputs)
            fit_inputs["policy_roles_path"] = roles_path

            with self.assertRaisesRegex(RuntimeError, "cover every confirmed binary candidate|component"):
                fit_selective_policy(**fit_inputs, output_dir=root / "policy-output")

    def test_builder_cli_success_and_argument_errors_are_single_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = _write_inputs(root, calibration_per_class=3, audit_per_class=3)
            output_dir = root / "roles"
            argv = _role_builder_argv(inputs, output_dir)
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                return_code = build_roles_cli_main(argv)

            self.assertEqual(0, return_code)
            self.assertEqual("", stderr.getvalue())
            success_lines = stdout.getvalue().splitlines()
            self.assertEqual(1, len(success_lines))
            self.assertTrue(json.loads(success_lines[0])["ok"])

            stderr = io.StringIO()
            with redirect_stderr(stderr):
                return_code = build_roles_cli_main(["--predictions", "only-one.json"])
            failure_lines = stderr.getvalue().splitlines()
            self.assertEqual(2, return_code)
            self.assertEqual(1, len(failure_lines))
            self.assertEqual({"ok": False, "error": "invalid_arguments"}, json.loads(failure_lines[0]))
            self.assertNotIn("usage:", stderr.getvalue().lower())

    def test_builder_rolls_back_on_base_exception_and_input_mutation(self) -> None:
        from football_tracking import selective_policy

        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = _write_inputs(root, calibration_per_class=3, audit_per_class=3)
            interrupted = root / "interrupted"
            real_write = selective_policy._write_json

            def interrupt_roles(path: Path, value: dict[str, object]) -> None:
                if path.name == "selective_policy_roles.v1.json":
                    raise KeyboardInterrupt("injected")
                real_write(path, value)

            with patch.object(selective_policy, "_write_json", side_effect=interrupt_roles):
                with self.assertRaisesRegex(KeyboardInterrupt, "injected"):
                    build_selective_policy_roles(**_role_builder_inputs(inputs), output_dir=interrupted)
            self.assertFalse(interrupted.exists())
            self.assertEqual([], list(root.glob(".interrupted.staging-*")))

            mutated = root / "mutated"
            real_components = selective_policy._policy_components

            def mutate_input(*args: object, **kwargs: object) -> list[dict[str, object]]:
                result = real_components(*args, **kwargs)
                inputs["predictions_path"].write_bytes(inputs["predictions_path"].read_bytes() + b" ")
                return result

            with patch.object(selective_policy, "_policy_components", side_effect=mutate_input):
                with self.assertRaisesRegex(RuntimeError, "input changed"):
                    build_selective_policy_roles(**_role_builder_inputs(inputs), output_dir=mutated)
            self.assertFalse(mutated.exists())
            self.assertEqual([], list(root.glob(".mutated.staging-*")))


def _write_inputs(
    root: Path,
    *,
    calibration_per_class: int,
    audit_per_class: int,
    include_forced_cases: bool = False,
    correlated_evaluation: bool = False,
    calibration_ambiguous: bool = False,
    audit_cluster_accept_errors: int = 0,
    include_application_cases: bool = False,
    include_ai_confirmed_case: bool = False,
    evaluation_origin: str = "human_confirmed",
    unsupported_label: str | None = None,
    unsupported_probability: float = 0.0,
) -> dict[str, Path]:
    candidates: list[dict[str, object]] = []
    classifications: list[dict[str, object]] = []
    resolutions: list[dict[str, object]] = []
    predictions: list[dict[str, object]] = []
    samples: list[dict[str, object]] = []
    role_ids: dict[str, list[str]] = {"policy_calibration": [], "policy_audit": []}
    source_ids: dict[str, list[str]] = {}
    source_sha: dict[str, str] = {}
    assignment_seed = "football-tracking-selective-policy-role-seed-v1"
    variant_cache: dict[tuple[str, str], str] = {}

    def variant_for_role(role: str, component_key: str) -> str:
        key = (role, "shared" if correlated_evaluation else component_key)
        if key in variant_cache:
            return variant_cache[key]
        prefix = "cal" if role == "policy_calibration" else "audit"
        for nonce in range(10_000):
            variant = f"{prefix}-{key[1]}-{nonce}"
            video_sha = hashlib.sha256(f"eval-video-{variant}".encode()).hexdigest()
            evidence = {
                "variant_ids": [variant],
                "video_sha256": [video_sha],
                "group_ids": [f"group-{variant}"],
                "split_groups": [f"split-{variant}"],
                "temporal_groups": [f"temporal-{variant}"],
            }
            immutable_identity = {"video_sha256": evidence["video_sha256"]}
            component_id = hashlib.sha256(
                json.dumps(immutable_identity, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            assigned = (
                "policy_calibration"
                if hashlib.sha256(f"{assignment_seed}:{component_id}".encode()).digest()[0] < 128
                else "policy_audit"
            )
            if assigned == role:
                variant_cache[key] = variant
                return variant
        raise AssertionError("could not construct deterministic role fixture")

    def add(
        candidate_id: str,
        role: str | None,
        truth: str,
        probabilities: dict[str, float],
        *,
        component_key: str | None = None,
        origin: str = "human_confirmed",
        resolution_status: str = "confirmed",
    ) -> None:
        index = len(candidates)
        frame_index = index * 10
        bbox = [10.0, 10.0, 20.0, 20.0]
        candidate = {
            "candidate_id": candidate_id,
            "frame_index": frame_index,
            "bbox": bbox,
            "confidence": 0.8,
            "source": "detector",
        }
        candidates.append(candidate)
        classifications.append(
            {"candidate_id": candidate_id, "label": truth, "label_origin": origin, "confidence": 0.99}
        )
        resolutions.append(
            {
                "candidate_id": candidate_id,
                "status": resolution_status,
                "label": truth,
                "label_origin": origin,
                "training_eligible": resolution_status == "confirmed"
                and (truth != "unknown" or origin == "human_confirmed"),
                "reasons": [] if resolution_status == "confirmed" else ["primary_vote_count"],
            }
        )
        variant = variant_for_role(role, component_key or candidate_id) if role is not None else "application-only"
        source_ids.setdefault(variant, []).append(candidate_id)
        source_sha.setdefault(variant, hashlib.sha256(f"eval-video-{variant}".encode()).hexdigest())
        if role is not None:
            role_ids[role].append(candidate_id)
        sample = {
            "sample_id": candidate_id,
            "candidate_id": candidate_id,
            "detector_source": "detector",
            "frame_index": frame_index,
            "bbox_requested_pixels": bbox,
            "bbox_clamped_pixels": bbox,
            "bbox_normalized": [0.1, 0.1, 0.2, 0.2],
            "confidence": 0.8,
            "variant_id": variant,
            "group_id": f"group-{variant}",
            "split_group": f"split-{variant}",
            "temporal_group": f"temporal-{variant}",
            "artifacts": {},
        }
        samples.append(sample)
        fingerprint_payload = {
            "candidate_id": candidate_id,
            "frame_index": frame_index,
            "bbox": bbox,
            "detector_source": "detector",
            "confidence": 0.8,
        }
        fingerprint = hashlib.sha256(
            json.dumps(fingerprint_payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()
        top_label = max(CLASSIFICATION_LABELS, key=lambda label: probabilities[label])
        predictions.append(
            {
                "candidate_id": candidate_id,
                "candidate_fingerprint": fingerprint,
                "predicted_label": top_label,
                "confidence": probabilities[top_label],
                "probabilities": probabilities,
                "model_version": "model-v1",
            }
        )

    labels = list(CLASSIFICATION_LABELS)
    ball_probs = dict.fromkeys(labels, 0.0)
    ball_probs.update({"match_ball": 0.995, "equipment_or_background": 0.005})
    noise_probs = dict.fromkeys(labels, 0.0)
    noise_probs.update({"match_ball": 0.005, "equipment_or_background": 0.995})
    if unsupported_label is not None and unsupported_probability:
        ball_probs[unsupported_label] = unsupported_probability
        ball_probs["match_ball"] -= unsupported_probability
        noise_probs[unsupported_label] = unsupported_probability
        noise_probs["equipment_or_background"] -= unsupported_probability
    for role, count, prefix in (
        ("policy_calibration", calibration_per_class, "cal"),
        ("policy_audit", audit_per_class, "audit"),
    ):
        injected_audit_errors = 0
        for index in range(count):
            role_ball_probs = dict(ball_probs)
            role_noise_probs = dict(noise_probs)
            if role == "policy_calibration" and calibration_ambiguous:
                role_ball_probs = dict.fromkeys(labels, 0.0)
                role_ball_probs.update({"match_ball": 0.5, "equipment_or_background": 0.5})
                role_noise_probs = dict(role_ball_probs)
            if role == "policy_audit" and injected_audit_errors < audit_cluster_accept_errors:
                role_noise_probs = dict(ball_probs)
                injected_audit_errors += 1
            add(
                f"{prefix}-ball-{index:04d}",
                role,
                "match_ball",
                role_ball_probs,
                component_key=f"{prefix}-ball-{index:04d}",
                origin=evaluation_origin,
            )
            add(
                f"{prefix}-noise-{index:04d}",
                role,
                "equipment_or_background",
                role_noise_probs,
                component_key=f"{prefix}-noise-{index:04d}",
                origin=evaluation_origin,
            )

    if include_application_cases:
        add(
            "application-ball",
            None,
            "unknown",
            dict(ball_probs),
            origin="prelabel",
            resolution_status="pending_adjudication",
        )
        add(
            "application-noise",
            None,
            "unknown",
            dict(noise_probs),
            origin="prelabel",
            resolution_status="pending_adjudication",
        )

    if include_ai_confirmed_case:
        add(
            "ai-confirmed-application",
            None,
            "match_ball",
            dict(ball_probs),
            origin="ai_confirmed",
        )

    existing_decisions: list[dict[str, object]] = []
    if include_forced_cases:
        unknown_probs = dict.fromkeys(labels, 0.0)
        unknown_probs.update({"unknown": 0.99, "match_ball": 0.01})
        add("forced-unknown", None, "unknown", unknown_probs)
        add("forced-conflict", None, "match_ball", dict(ball_probs))
        resolutions[-1].update({"status": "existing_confirmed_conflict", "label": "unknown", "label_origin": None})
        add(
            "forced-existing",
            None,
            "unknown",
            dict(ball_probs),
            origin="prelabel",
            resolution_status="pending_adjudication",
        )
        existing_decisions.append(
            {"candidate_id": "forced-existing", "decision": "abstain", "confidence": 0.7, "reason": "prior"}
        )
        margin_probs = dict.fromkeys(labels, 0.0)
        margin_probs.update({"match_ball": 0.51, "equipment_or_background": 0.49})
        add("forced-margin", "policy_audit", "match_ball", margin_probs)

    source_contract_path = root / "source-contract.json"
    _write_json(source_contract_path, build_tracking_contract(candidates=candidates))
    resolved_contract_path = root / "resolved-contract.json"
    _write_json(
        resolved_contract_path,
        build_tracking_contract(
            candidates=candidates,
            classifications=classifications,
            decisions=existing_decisions,
        ),
    )
    dataset_path = root / "candidate_dataset_manifest.json"
    sources = [
        {
            "path": f"{variant}.mp4",
            "sha256": source_sha[variant],
            "variant_id": variant,
            "width": 100,
            "height": 100,
            "frame_count": max(1, len(candidates) * 10 + 1),
            "group_id": f"group-{variant}",
            "split_group": f"split-{variant}",
            "temporal_group": f"temporal-{variant}",
            "candidate_ids": ids,
        }
        for variant, ids in source_ids.items()
    ]
    _write_json(
        dataset_path,
        {
            "schema_version": "1.0",
            "artifact_type": "candidate_dataset",
            "dataset_version": "dataset-v1",
            "contract": {"sha256": _sha256(source_contract_path)},
            "summary": {"status": "ok", "sample_count": len(samples), "source_count": len(sources)},
            "sources": sources,
            "samples": samples,
        },
    )
    resolution_path = root / "annotation_resolution.v1.json"
    _write_json(
        resolution_path,
        {
            "schema_version": "1.0",
            "artifact_type": "candidate_annotation_resolution",
            "summary": {"status": "complete"},
            "source_contract": {"sha256": _sha256(source_contract_path)},
            "source_dataset_manifest": {"sha256": _sha256(dataset_path), "dataset_version": "dataset-v1"},
            "derived_tracking_contract": {"sha256": _sha256(resolved_contract_path)},
            "resolutions": resolutions,
        },
    )
    weights_path = root / "model.pt"
    weights_path.write_bytes(b"synthetic-bound-model-weights")
    training_report_path = root / "training_report.v1.json"
    split = {
        "evidence_by_split": {
            name: {
                "candidate_ids": [f"model-{name}"],
                "variant_ids": [f"model-{name}-variant"],
                "video_sha256": [hashlib.sha256(f"model-video-{name}".encode()).hexdigest()],
                "group_ids": [f"model-{name}-group"],
                "split_groups": [f"model-{name}-split"],
                "temporal_groups": [f"model-{name}-temporal"],
                "temporal_blocks": [f"model-{name}-block"],
            }
            for name in ("train", "calibration", "test")
        },
        "leakage_checks": {"passed": True, "violations": []},
    }
    data_binding = {
        "dataset_version": "model-training-dataset-v1",
        "dataset_manifest_sha256": hashlib.sha256(b"model-dataset").hexdigest(),
        "annotation_resolution_sha256": hashlib.sha256(b"model-resolution").hexdigest(),
        "resolved_contract_sha256": hashlib.sha256(b"model-contract").hexdigest(),
    }
    supported_mask = [label != unsupported_label for label in labels]
    supported_classes = [label for label, supported in zip(labels, supported_mask) if supported]
    calibration = {"temperature": 1.0}
    training_config = {"seed": 1}
    architecture = {"name": "synthetic-test-model"}
    input_contract = {"semantic_preprocessing": "synthetic"}
    code_sha256 = hashlib.sha256(b"classifier-code").hexdigest()
    runtime = {"device": "cpu", "runtime": "test"}
    version_inputs = {
        "weights_sha256": _sha256(weights_path),
        "data_binding": data_binding,
        "training_config": training_config,
        "calibration": calibration,
        "supported_mask": supported_mask,
        "class_order": labels,
        "architecture": architecture,
        "input_contract": input_contract,
        "code_sha256": code_sha256,
        "runtime": runtime,
    }
    model_version = hashlib.sha256(
        json.dumps(version_inputs, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
    for prediction in predictions:
        prediction["model_version"] = model_version
    report = {
        "schema_version": "1.0",
        "artifact_type": "candidate_classifier_training_report",
        "status": "complete",
        "model_version": model_version,
        "class_order": labels,
        "supported_classes": supported_classes,
        "supported_mask": supported_mask,
        "calibration": calibration,
        "training_config": training_config,
        "data_binding": data_binding,
        "split": split,
    }
    _write_json(training_report_path, report)
    model_manifest_path = root / "model_manifest.v1.json"
    _write_json(
        model_manifest_path,
        {
            "schema_version": "1.0",
            "artifact_type": "candidate_classifier_model",
            "model_version": model_version,
            "weights_path": "model.pt",
            "weights_sha256": _sha256(weights_path),
            "class_order": labels,
            "supported_classes": supported_classes,
            "supported_mask": supported_mask,
            "calibration": calibration,
            "training_config": training_config,
            "data_binding": data_binding,
            "architecture": architecture,
            "input_contract": input_contract,
            "code_sha256": code_sha256,
            "runtime": runtime,
            "training_report_path": "training_report.v1.json",
            "training_report_sha256": _sha256(training_report_path),
        },
    )
    predictions_path = root / "candidate_predictions.v1.json"
    _write_json(
        predictions_path,
        {
            "schema_version": "1.0",
            "artifact_type": "candidate_predictions",
            "model_version": model_version,
            "dataset_version": "dataset-v1",
            "source_contract_sha256": _sha256(source_contract_path),
            "class_order": labels,
            "temperature": 1.0,
            "prediction_count": len(predictions),
            "predictions": predictions,
        },
    )
    policy_roles_path = root / "selective_policy_roles.v1.json"
    role_components = []
    candidate_role = {candidate_id: role for role, candidate_ids in role_ids.items() for candidate_id in candidate_ids}
    for source in sources:
        component_candidate_ids = sorted(
            candidate_id for candidate_id in source["candidate_ids"] if candidate_id in candidate_role
        )
        if not component_candidate_ids:
            continue
        evidence = {
            "variant_ids": [source["variant_id"]],
            "video_sha256": [source["sha256"]],
            "group_ids": [source["group_id"]],
            "split_groups": [source["split_group"]],
            "temporal_groups": [source["temporal_group"]],
        }
        role_components.append(
            {
                "component_id": hashlib.sha256(
                    json.dumps(
                        {"video_sha256": evidence["video_sha256"]},
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest(),
                "candidate_ids": component_candidate_ids,
                "evidence": evidence,
                "role": candidate_role[component_candidate_ids[0]],
            }
        )
    role_components.sort(key=lambda component: component["component_id"])
    candidate_component_mapping = sorted(
        (
            {
                "candidate_id": component["candidate_ids"][0],
                "component_id": component["component_id"],
                "role": component["role"],
            }
            for component in role_components
            if len(component["candidate_ids"]) == 1
        ),
        key=lambda row: row["candidate_id"],
    )
    if correlated_evaluation:
        candidate_component_mapping = sorted(
            (
                {
                    "candidate_id": candidate_id,
                    "component_id": component["component_id"],
                    "role": component["role"],
                }
                for component in role_components
                for candidate_id in component["candidate_ids"]
            ),
            key=lambda row: row["candidate_id"],
        )
    _write_json(
        policy_roles_path,
        {
            "schema_version": "1.0",
            "artifact_type": "selective_policy_roles",
            "assignment_strategy": "sha256_connected_evidence_component_partition_v2",
            "assignment_seed": assignment_seed,
            "component_id_algorithm": "immutable-video-sha-component-v2",
            "inferential_unit": "connected_evidence_component",
            "inferential_unit_algorithm": "one_candidate_per_connected_evidence_component_v1",
            "component_count": len(role_components),
            "evaluation_candidate_count": len(candidate_component_mapping),
            "candidate_component_mapping": candidate_component_mapping,
            "lineage": {
                "predictions_sha256": _sha256(predictions_path),
                "dataset_manifest_sha256": _sha256(dataset_path),
                "annotation_resolution_sha256": _sha256(resolution_path),
                "resolved_contract_sha256": _sha256(resolved_contract_path),
                "model_manifest_sha256": _sha256(model_manifest_path),
                "training_report_sha256": _sha256(training_report_path),
                "model_weights_sha256": _sha256(weights_path),
                "dataset_version": "dataset-v1",
                "model_version": model_version,
            },
            "roles": {role: sorted(candidate_ids) for role, candidate_ids in role_ids.items()},
            "components": role_components,
        },
    )
    return {
        "predictions_path": predictions_path,
        "dataset_manifest_path": dataset_path,
        "annotation_resolution_path": resolution_path,
        "resolved_contract_path": resolved_contract_path,
        "model_manifest_path": model_manifest_path,
        "training_report_path": training_report_path,
        "policy_roles_path": policy_roles_path,
    }


def _role_builder_inputs(inputs: dict[str, Path]) -> dict[str, Path]:
    return {
        name: inputs[name]
        for name in (
            "predictions_path",
            "dataset_manifest_path",
            "annotation_resolution_path",
            "resolved_contract_path",
            "model_manifest_path",
            "training_report_path",
        )
    }


def _role_builder_argv(inputs: dict[str, Path], output_dir: Path) -> list[str]:
    return [
        "--predictions",
        str(inputs["predictions_path"]),
        "--dataset-manifest",
        str(inputs["dataset_manifest_path"]),
        "--annotation-resolution",
        str(inputs["annotation_resolution_path"]),
        "--resolved-contract",
        str(inputs["resolved_contract_path"]),
        "--model-manifest",
        str(inputs["model_manifest_path"]),
        "--training-report",
        str(inputs["training_report_path"]),
        "--output-dir",
        str(output_dir),
    ]


def _fit_argv(inputs: dict[str, Path], output_dir: Path) -> list[str]:
    return [
        *_role_builder_argv(inputs, output_dir),
        "--policy-roles",
        str(inputs["policy_roles_path"]),
    ]


def _mutate_json(path: Path, mutate: object, *, update_manifest: Path | None = None) -> None:
    value = _read_json(path)
    mutate(value)  # type: ignore[operator]
    _write_json(path, value)
    if update_manifest is not None:
        manifest = _read_json(update_manifest)
        manifest["training_report_sha256"] = _sha256(path)
        _write_json(update_manifest, manifest)


def _decisions_summary(rows: list[dict[str, object]]) -> dict[str, int]:
    return {
        "candidate_count": len(rows),
        "accept_count": sum(row["decision"] == "accept" for row in rows),
        "reject_count": sum(row["decision"] == "reject" for row in rows),
        "abstain_count": sum(row["decision"] == "abstain" for row in rows),
        "forced_abstain_count": sum(bool(row["forced_abstain_reasons"]) for row in rows),
        "evaluation_holdout_count": sum(row["decision_scope"] == "evaluation_only" for row in rows),
        "application_count": sum(row["decision_scope"] == "application" for row in rows),
        "preserved_existing_decision_count": sum(bool(row["existing_decision_preserved"]) for row in rows),
        "pending_application_count": sum(
            row["decision_scope"] == "application"
            and not row["applied_to_contract"]
            and not row["existing_decision_preserved"]
            for row in rows
        ),
    }


def _reseal_decision_artifact(
    policy_path: Path,
    decisions_path: Path,
    policy: dict[str, object],
    decisions: dict[str, object],
) -> None:
    decisions["summary"] = _decisions_summary(decisions["decisions"])
    decisions_content = {
        key: value for key, value in decisions.items() if key not in {"generated_at", "policy_version"}
    }
    content_sha256 = _canonical_hash(decisions_content)
    policy["version_inputs"]["decisions_content_sha256"] = content_sha256
    policy_version = _canonical_hash(policy["version_inputs"])
    policy["policy_version"] = policy_version
    decisions["policy_version"] = policy_version
    _write_json(decisions_path, decisions)
    policy["decisions_artifact"] = {
        "path": "selective_decisions.v1.json",
        "sha256": _sha256(decisions_path),
        "content_sha256": content_sha256,
    }
    _write_json(policy_path, policy)


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, sort_keys=True, allow_nan=True) + "\n", encoding="utf-8")


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _stable_diagnostic_identity(value: object) -> object:
    if isinstance(value, dict):
        return {key: _stable_diagnostic_identity(item) for key, item in value.items() if key != "generated_at"}
    if isinstance(value, list):
        return [_stable_diagnostic_identity(item) for item in value]
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()
