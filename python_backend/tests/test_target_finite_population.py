from __future__ import annotations

import hashlib
import inspect
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from threading import Event, Lock
from unittest.mock import Mock, patch

from football_tracking.candidate_annotations import (
    resolve_candidate_annotations,
    sample_evidence_sha256,
)
from football_tracking.selective_policy import (
    SelectivePolicyError,
    validate_selective_decisions_binding,
)
from football_tracking.target_finite_population import (
    MAX_TARGET_COMMITMENT_BYTES,
    MIN_TRUE_BALL_SUPPORT,
    SAMPLING_ORDERING_SALT_DOMAIN,
    SAMPLING_REQUIRED_DRAW_COUNT,
    SAMPLING_SIZE_RULE,
    SAMPLING_TRUE_BALL_PREVALENCE_LOWER_BOUND,
    TargetFinitePopulationError,
    _ranked_sample,
    build_target_audit_labels_from_annotation_package,
    build_target_audit_plan,
    capture_target_prelabel_registry,
    evaluate_target_audit,
    exact_binomial_upper_bound,
    hypergeometric_upper_bound,
    target_prelabel_commitment_path,
    validate_target_label_non_leakage,
    validate_target_prelabel_commitment,
    validate_target_qualification,
)
from football_tracking.tracking_contracts import build_tracking_contract


def _sha(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _bindings(run_id: str = "run-1") -> dict[str, str]:
    return {
        "target_run_id": run_id,
        "source_sha256": "1" * 64,
        "root_contract_sha256": "2" * 64,
        "candidate_population_sha256": "",
        "model_sha256": "3" * 64,
        "model_version": "model-v1",
        "confirmed_config_sha256": "4" * 64,
        "policy_sha256": "5" * 64,
        "policy_version": "policy-v1",
        "thresholds_sha256": "6" * 64,
    }


def _application(
    population: list[dict[str, str]],
    decisions: list[dict[str, str]],
) -> dict[str, object]:
    binding_evidence = {
        "source_sha256": "1" * 64,
        "root_contract_sha256": "2" * 64,
        "candidate_population_sha256": _sha(sorted(population, key=lambda row: row["candidate_id"])),
        "model_sha256": "3" * 64,
        "model_version": "model-v1",
        "policy_sha256": "5" * 64,
        "policy_version": "policy-v1",
        "thresholds_sha256": "6" * 64,
    }
    content: dict[str, object] = {
        "schema_version": "1.0",
        "artifact_type": "target_finite_population_application",
        "qualification_scope": "target_finite_population",
        "application_algorithm": "target-finite-population-frozen-decisions-v1",
        "status": "frozen_before_labels",
        "training_eligible": False,
        "reusable": False,
        "promotion_scope": "exact_target_only",
        "policy_status_at_freeze": "review_only",
        "policy_version": "policy-v1",
        "dataset_version": "dataset-v1",
        "model_version": "model-v1",
        "lineage": {},
        "target_binding_evidence": binding_evidence,
        "summary": {
            "candidate_count": len(decisions),
            "accept_count": sum(row["decision"] == "accept" for row in decisions),
            "reject_count": sum(row["decision"] == "reject" for row in decisions),
            "abstain_count": sum(row["decision"] == "abstain" for row in decisions),
        },
        "decisions": decisions,
    }
    return {
        **content,
        "generated_at": "2026-01-01T00:00:00Z",
        "application_content_sha256": _sha(content),
    }


def _population(
    count: int,
    *,
    accepted: int,
    abstained: int = 0,
) -> tuple[list[dict[str, str]], list[dict[str, object]]]:
    population = [
        {"candidate_id": f"candidate-{index:04d}", "candidate_fingerprint": f"{index + 1:064x}"}
        for index in range(count)
    ]
    decisions = []
    for index, row in enumerate(population):
        decision = "accept" if index < accepted else "abstain" if index < accepted + abstained else "reject"
        if decision == "accept":
            accept_score, reject_score, top_label, top_margin = 0.99, 0.01, "match_ball", 0.98
            forced_reasons: list[str] = []
        elif decision == "reject":
            accept_score, reject_score, top_label, top_margin = (
                0.01,
                0.99,
                "equipment_or_background",
                0.98,
            )
            forced_reasons = []
        else:
            accept_score, reject_score, top_label, top_margin = 0.5, 0.5, "match_ball", 0.0
            forced_reasons = ["accept_reject_conflict_margin", "top_margin_below_minimum"]
        decisions.append(
            {
                "candidate_id": row["candidate_id"],
                "candidate_fingerprint": row["candidate_fingerprint"],
                "variant_id": "target-variant",
                "frame_index": index,
                "accept_score": accept_score,
                "reject_score": reject_score,
                "unknown_score": 0.0,
                "top_label": top_label,
                "top_margin": top_margin,
                "raw_decision": decision,
                "decision": decision,
                "decision_scope": "application",
                "policy_role": None,
                "forced_abstain_reasons": forced_reasons,
                "existing_decision_preserved": False,
                "applied_to_contract": decision in {"accept", "reject"},
            }
        )
    return population, decisions


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_target_annotation_labels(
    root: Path,
    plan: dict[str, object],
    *,
    commitment_path: Path,
    labels: dict[str, str] | None = None,
    same_reviewer: bool = False,
    omit_evidence: bool = False,
    unresolved_candidate_id: str | None = None,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    sample = plan["sample"]
    candidates = [
        {
            "candidate_id": row["candidate_id"],
            "frame_index": index,
            "bbox": [10.0, 12.0, 18.0, 20.0],
            "confidence": 0.7,
            "source": "detector",
        }
        for index, row in enumerate(sample)
    ]
    contract_path = root / "target-contract.json"
    _write_json(contract_path, build_tracking_contract(candidates=candidates))
    evidence_root = root / "evidence"
    samples = []
    for index, candidate in enumerate(candidates):
        candidate_id = candidate["candidate_id"]
        sample_id = f"{index:06d}-{candidate_id}"
        artifacts = {}
        for artifact_name, filename in (
            ("tight_tensor", "tight.npy"),
            ("context_tensor", "context.npy"),
            ("review_montage", "review_montage.png"),
        ):
            artifact_path = evidence_root / sample_id / filename
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
            artifact_path.write_bytes(f"{artifact_name}:{candidate_id}".encode())
            artifacts[artifact_name] = {
                "path": artifact_path.relative_to(root).as_posix(),
                "sha256": _file_sha(artifact_path),
            }
        samples.append(
            {
                "sample_id": sample_id,
                "candidate_id": candidate_id,
                "artifacts": artifacts,
            }
        )
    dataset = {
        "schema_version": "1.0",
        "artifact_type": "candidate_dataset",
        "dataset_version": _sha(samples),
        "contract": {
            "schema_version": "2.0",
            "path": contract_path.name,
            "sha256": _file_sha(contract_path),
        },
        "frame_offsets": [-2, -1, 0, 1, 2],
        "tensor_contract": {
            "color_space": "RGB",
            "dtype": "uint8",
            "tight_shape": [5, 3, 64, 64],
            "context_shape": [5, 3, 128, 128],
            "markup": False,
        },
        "summary": {
            "status": "ok",
            "sample_count": len(samples),
            "source_count": 1,
        },
        "samples": samples,
    }
    dataset_path = root / "candidate_dataset_manifest.json"
    _write_json(dataset_path, dataset)
    header = {
        "schema_version": "1.0",
        "record_type": "ledger_header",
        "contract_sha256": _file_sha(contract_path),
        "dataset_version": dataset["dataset_version"],
        "evidence_manifest_sha256": _file_sha(dataset_path),
        "append_only_chain": {
            "algorithm": "sha256-ledger-chain-v1",
            "sequence": 1,
            "previous_ledger_sha256": None,
        },
        "usage": "target_finite_population_audit_only",
        "qualification_scope": "target_finite_population",
        "target_run_id": plan["bindings"]["target_run_id"],
        "target_audit_plan_sha256": plan["plan_sha256"],
        "target_external_commitment_sha256": plan["external_commitment"]["record_sha256"],
        "target_plan_commitment_sha256": plan["plan_commitment_sha256"],
        "target_sampling_design_sha256": plan["sampling_design_sha256"],
        "target_sample_sha256": plan["sample_sha256"],
        "training_eligible": False,
        "calibration_eligible": False,
        "reusable": False,
    }
    votes = []
    samples_by_id = {row["candidate_id"]: row for row in samples}
    for index, sampled in enumerate(sample):
        candidate_id = sampled["candidate_id"]
        label = (labels or {}).get(candidate_id, "match_ball")
        evidence_sha256 = sample_evidence_sha256(samples_by_id[candidate_id])
        for reviewer_index in range(2):
            vote = {
                "schema_version": "1.0",
                "record_type": "vote",
                "vote_id": f"primary-{index}-{reviewer_index}",
                "candidate_id": candidate_id,
                "stage": "primary",
                "reviewer_type": "human",
                "annotator_id": (
                    f"reviewer-{index}-same"
                    if same_reviewer
                    else f"reviewer-{index}-{reviewer_index}"
                ),
                "fingerprint": (
                    f"fingerprint-{index}-same"
                    if same_reviewer
                    else f"fingerprint-{index}-{reviewer_index}"
                ),
                "label": (
                    "field_line_or_mark"
                    if candidate_id == unresolved_candidate_id and reviewer_index == 1
                    else label
                ),
                "confidence": 0.99,
                "blind": True,
                "created_at": f"2026-01-01T00:00:0{reviewer_index}Z",
                "dataset_version": dataset["dataset_version"],
                "sample_id": samples_by_id[candidate_id]["sample_id"],
                "evidence_sha256": evidence_sha256,
            }
            if omit_evidence:
                vote.pop("evidence_sha256")
            votes.append(vote)
    ledger_path = root / "target-votes.jsonl"
    ledger_path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in [header, *votes]),
        encoding="utf-8",
    )
    resolution_dir = root / "resolution"
    try:
        resolve_candidate_annotations(
            contract_path,
            ledger_path,
            resolution_dir,
            dataset_manifest_path=dataset_path,
        )
    except ValueError as exc:
        raise TargetFinitePopulationError(f"target annotation package is invalid: {exc}") from exc
    resolution_path = resolution_dir / "annotation_resolution.v1.json"
    manifest = build_target_audit_labels_from_annotation_package(
        plan,
        package_root=root,
        contract_path=contract_path,
        ledger_path=ledger_path,
        dataset_manifest_path=dataset_path,
        annotation_resolution_path=resolution_path,
        commitment_path=commitment_path,
    )
    manifest_path = root / "target_finite_population_audit_labels.v1.json"
    _write_json(manifest_path, manifest)
    return manifest_path


class TargetFinitePopulationStatisticsTests(unittest.TestCase):
    def test_known_zero_error_bounds_and_census(self) -> None:
        self.assertLessEqual(exact_binomial_upper_bound(0, 183, alpha=0.025), 0.02)
        self.assertGreater(exact_binomial_upper_bound(0, 182, alpha=0.025), 0.02)
        self.assertLessEqual(exact_binomial_upper_bound(0, 368, alpha=0.025), 0.01)
        self.assertGreater(exact_binomial_upper_bound(0, 367, alpha=0.025), 0.01)
        self.assertEqual(0.0, hypergeometric_upper_bound(500, 500, 0, alpha=0.025))
        self.assertEqual(1 / 500, hypergeometric_upper_bound(500, 500, 1, alpha=0.025))

    def test_hypergeometric_bound_tightens_without_replacement(self) -> None:
        finite = hypergeometric_upper_bound(500, 183, 0, alpha=0.025)
        binomial = exact_binomial_upper_bound(0, 183, alpha=0.025)
        self.assertLess(finite, binomial)
        self.assertGreater(
            hypergeometric_upper_bound(500, 183, 1, alpha=0.025),
            finite,
        )
        self.assertEqual(
            126 / 6406,
            hypergeometric_upper_bound(6406, 183, 0, alpha=0.025),
        )
        self.assertEqual(
            190 / 6406,
            hypergeometric_upper_bound(6406, 183, 1, alpha=0.025),
        )


class TargetFinitePopulationContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self._commitment_temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._commitment_temp.cleanup)
        self.sandbox = Path(self._commitment_temp.name)
        self.commitment_root = self.sandbox / "commitments"

    def _build_plan(
        self,
        application: dict[str, object],
        *,
        target_run_id: str = "run-1",
        confirmed_config_sha256: str = "4" * 64,
    ) -> dict[str, object]:
        return build_target_audit_plan(
            application,
            target_run_id=target_run_id,
            confirmed_config_sha256=confirmed_config_sha256,
            commitment_root=self.commitment_root,
        )

    def _commitment_path(self, plan: dict[str, object]) -> Path:
        return target_prelabel_commitment_path(self.commitment_root, plan)

    def _labels(
        self,
        root: Path,
        plan: dict[str, object],
        **kwargs: object,
    ) -> Path:
        return _write_target_annotation_labels(
            root,
            plan,
            commitment_path=self._commitment_path(plan),
            **kwargs,
        )

    def _evaluate(self, plan: dict[str, object], labels_path: Path) -> dict[str, object]:
        return evaluate_target_audit(
            plan,
            labels_path,
            commitment_path=self._commitment_path(plan),
        )

    def _plan(self) -> dict[str, object]:
        population, decisions = _population(368, accepted=183, abstained=185)
        return self._build_plan(
            _application(population, decisions),
        )

    def test_public_plan_builder_has_no_seed_or_sample_size_choice(self) -> None:
        parameters = inspect.signature(build_target_audit_plan).parameters
        self.assertNotIn("sampling_seed", parameters)
        self.assertNotIn("sample_size", parameters)
        population, decisions = _population(40, accepted=20, abstained=19)
        for legacy_choice in (
            {"sampling_seed": "legacy-seed"},
            {"sample_size": 20},
        ):
            with self.assertRaises(TypeError):
                build_target_audit_plan(
                    _application(population, decisions),
                    target_run_id="run-1",
                    confirmed_config_sha256="4" * 64,
                    commitment_root=self.commitment_root,
                    **legacy_choice,
                )

    def test_sampling_is_deterministic_hash_bound_and_without_replacement(self) -> None:
        first = self._plan()
        second = self._plan()
        self.assertEqual(first, second)
        ids = [row["candidate_id"] for row in first["sample"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(368, len(ids))
        design = first["sampling_design"]
        self.assertEqual(SAMPLING_ORDERING_SALT_DOMAIN, design["ordering_salt_domain"])
        self.assertNotIn("sampling_seed", design)
        self.assertEqual(SAMPLING_SIZE_RULE, design["sample_size_rule"]["algorithm"])

        short = _ranked_sample(
            first["population"],
            first["frozen_decisions"],
            sample_size=20,
            ordering_salt=design["ordering_salt"],
        )
        longer = _ranked_sample(
            first["population"],
            first["frozen_decisions"],
            sample_size=40,
            ordering_salt=design["ordering_salt"],
        )
        self.assertEqual(short, longer[:20])
        self.assertEqual(longer, first["sample"][:40])

    def test_sample_size_is_derived_from_the_versioned_service_rule(self) -> None:
        small_population, small_decisions = _population(369, accepted=183, abstained=185)
        small = self._build_plan(_application(small_population, small_decisions))
        self.assertEqual(369, len(small["sample"]))
        self.assertEqual(369, small["sampling_design"]["sample_size"])

        large_population, large_decisions = _population(3700, accepted=183, abstained=185)
        large = self._build_plan(_application(large_population, large_decisions))
        rule = large["sampling_design"]["sample_size_rule"]
        self.assertEqual(SAMPLING_REQUIRED_DRAW_COUNT, len(large["sample"]))
        self.assertEqual(SAMPLING_REQUIRED_DRAW_COUNT, large["sampling_design"]["sample_size"])
        self.assertEqual(SAMPLING_SIZE_RULE, rule["algorithm"])
        self.assertEqual(MIN_TRUE_BALL_SUPPORT, rule["true_ball_support_minimum"])
        self.assertEqual(
            SAMPLING_TRUE_BALL_PREVALENCE_LOWER_BOUND,
            rule["true_ball_prevalence_lower_bound"],
        )
        self.assertEqual(SAMPLING_REQUIRED_DRAW_COUNT, rule["required_draw_count"])
        self.assertEqual(3700, rule["population_size"])
        self.assertEqual(SAMPLING_REQUIRED_DRAW_COUNT, rule["derived_sample_size"])

    def test_external_prelabel_commitment_is_exclusive_replayable_and_tamper_evident(self) -> None:
        population, decisions = _population(40, accepted=20, abstained=19)
        application = _application(population, decisions)
        first = self._build_plan(application)
        replay = self._build_plan(application)
        self.assertEqual(first, replay)
        self.assertEqual(1, len(list(self.commitment_root.iterdir())))

        commitment_path = self._commitment_path(first)
        commitment_path.write_bytes(commitment_path.read_bytes() + b" ")
        with self.assertRaisesRegex(TargetFinitePopulationError, "hash|canonical"):
            evaluate_target_audit(
                first,
                Path("labels-must-not-be-read.json"),
                commitment_path=commitment_path,
            )

    def test_forged_alternate_sampling_design_is_rejected_before_registry_access(self) -> None:
        plan = self._plan()
        forged = deepcopy(plan)
        forged["sampling_design"]["ordering_salt"] = "f" * 64
        forged["sampling_design_sha256"] = _sha(forged["sampling_design"])
        content = {
            key: value
            for key, value in forged.items()
            if key not in {"external_commitment", "plan_sha256"}
        }
        forged["plan_sha256"] = _sha(content)
        with self.assertRaisesRegex(TargetFinitePopulationError, "sampling design"):
            evaluate_target_audit(
                forged,
                Path("labels-must-not-be-read.json"),
                commitment_path=Path("registry-must-not-be-read.json"),
            )

    def test_concurrent_exact_replay_commits_one_identical_record(self) -> None:
        population, decisions = _population(40, accepted=20, abstained=19)
        application = _application(population, decisions)
        first_partial_write = Event()
        release_first_writer = Event()
        call_lock = Lock()
        first_call = True
        real_write = os.write

        def delayed_first_write(descriptor: int, payload: bytes) -> int:
            nonlocal first_call
            with call_lock:
                delay = first_call
                first_call = False
            if not delay:
                return real_write(descriptor, payload)
            written = real_write(descriptor, payload[: max(1, len(payload) // 2)])
            first_partial_write.set()
            if not release_first_writer.wait(10):
                raise AssertionError("concurrent commitment writer was not released")
            return written

        with (
            patch(
                "football_tracking.target_finite_population.os.write",
                side_effect=delayed_first_write,
            ),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            first = executor.submit(self._build_plan, application)
            self.assertTrue(first_partial_write.wait(10))
            replay = executor.submit(self._build_plan, application)
            try:
                replay_plan = replay.result(timeout=10)
            finally:
                release_first_writer.set()
            first_plan = first.result(timeout=10)

        self.assertEqual(first_plan, replay_plan)
        self.assertEqual(1, len(list(self.commitment_root.iterdir())))
        self.assertEqual(self._build_plan(application), self._build_plan(application))

    def test_partial_staging_artifact_is_ignored_without_poisoning_the_final_record(self) -> None:
        self.commitment_root.mkdir()
        orphan = (
            self.commitment_root
            / (
                ".orphan.target-prelabel-commitment.v1.json."
                "0123456789abcdef0123456789abcdef.staging"
            )
        )
        orphan.write_bytes(b'{"partial":')
        plan = self._plan()
        record_name = plan["external_commitment"]["record_name"]
        canonical, records = capture_target_prelabel_registry(
            self.commitment_root,
            record_name=record_name,
        )
        self.assertEqual(self._commitment_path(plan).read_bytes(), canonical)
        self.assertEqual([record_name], [name for name, _payload in records])
        self.assertEqual(b'{"partial":', orphan.read_bytes())

    def test_canonical_capture_ignores_concurrent_replay_staging(self) -> None:
        plan = self._plan()
        application = _application(plan["population"], plan["frozen_decisions"])
        staging_written = Event()
        release_writer = Event()
        real_write = os.write
        delayed = False

        def delayed_write(descriptor: int, payload: bytes) -> int:
            nonlocal delayed
            if delayed:
                return real_write(descriptor, payload)
            delayed = True
            written = real_write(descriptor, payload[: max(1, len(payload) // 2)])
            staging_written.set()
            if not release_writer.wait(10):
                raise AssertionError("concurrent replay writer was not released")
            return written

        with (
            patch(
                "football_tracking.target_finite_population.os.write",
                side_effect=delayed_write,
            ),
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            replay = executor.submit(self._build_plan, application)
            self.assertTrue(staging_written.wait(10))
            record_name = plan["external_commitment"]["record_name"]
            canonical, records = capture_target_prelabel_registry(
                self.commitment_root,
                record_name=record_name,
            )
            release_writer.set()
            replay_plan = replay.result(timeout=10)
        self.assertEqual(plan, replay_plan)
        self.assertEqual(self._commitment_path(plan).read_bytes(), canonical)
        self.assertEqual([record_name], [name for name, _payload in records])

    def test_failed_staging_write_cleans_up_without_publishing_a_final_record(self) -> None:
        population, decisions = _population(40, accepted=20, abstained=19)
        real_write = os.write
        failed = False

        def crash_after_partial_write(descriptor: int, payload: bytes) -> int:
            nonlocal failed
            if failed:
                return real_write(descriptor, payload)
            failed = True
            real_write(descriptor, payload[: max(1, len(payload) // 2)])
            raise OSError("simulated commitment writer crash")

        with (
            patch(
                "football_tracking.target_finite_population.os.write",
                side_effect=crash_after_partial_write,
            ),
            self.assertRaisesRegex(OSError, "simulated commitment writer crash"),
        ):
            self._build_plan(_application(population, decisions))
        self.assertEqual([], list(self.commitment_root.iterdir()))

    def test_oversized_commitment_record_fails_closed_before_json_parsing(self) -> None:
        plan = self._plan()
        commitment_path = self._commitment_path(plan)
        commitment_path.write_bytes(b"x" * (MAX_TARGET_COMMITMENT_BYTES + 1))
        with self.assertRaisesRegex(TargetFinitePopulationError, "size limit"):
            validate_target_prelabel_commitment(plan, commitment_path)

    def test_final_commitment_special_node_fails_closed(self) -> None:
        plan = self._plan()
        commitment_path = self._commitment_path(plan)
        commitment_path.unlink()
        commitment_path.mkdir()
        with self.assertRaisesRegex(TargetFinitePopulationError, "regular non-link|regular file"):
            self._build_plan(
                _application(plan["population"], plan["frozen_decisions"]),
            )

    @unittest.skipIf(
        os.name == "nt" or not hasattr(os, "mkfifo"),
        "FIFO commitment safety requires POSIX",
    )
    def test_final_commitment_fifo_fails_closed_without_blocking(self) -> None:
        plan = self._plan()
        commitment_path = self._commitment_path(plan)
        commitment_path.unlink()
        os.mkfifo(commitment_path)
        with self.assertRaisesRegex(TargetFinitePopulationError, "regular file"):
            self._build_plan(
                _application(plan["population"], plan["frozen_decisions"]),
            )

    @unittest.skipUnless(os.name == "nt", "Windows directory handle contract")
    def test_windows_registry_chain_uses_non_delete_shared_locking_access(self) -> None:
        import ctypes

        create_file = ctypes.windll.kernel32.CreateFileW
        calls: list[tuple[str, int, int, int]] = []

        def record_create_file(
            path: str,
            desired_access: int,
            share_mode: int,
            security_attributes: object,
            creation_disposition: int,
            flags: int,
            template: object,
        ) -> int:
            calls.append((path, desired_access, share_mode, flags))
            return create_file(
                path,
                desired_access,
                share_mode,
                security_attributes,
                creation_disposition,
                flags,
                template,
            )

        with patch.object(
            ctypes.windll.kernel32,
            "CreateFileW",
            side_effect=record_create_file,
        ):
            self._plan()

        directory_calls = [
            call
            for call in calls
            if call[3] & 0x02000000 and not call[3] & 0x08000000
        ]
        self.assertGreaterEqual(len(directory_calls), len(self.commitment_root.parts))
        for _path, desired_access, share_mode, _flags in directory_calls:
            self.assertEqual(
                0x00000001 | 0x00000080 | 0x00100000,
                desired_access,
            )
            self.assertEqual(0x00000001 | 0x00000002, share_mode)

    @unittest.skipUnless(os.name == "nt", "Windows directory sharing contract")
    def test_windows_registry_root_rename_after_open_fails_closed(self) -> None:
        population, decisions = _population(40, accepted=20, abstained=19)
        moved_root = self.sandbox / "moved-registry"
        real_write = os.write
        attempted = False

        def rename_root_then_write(descriptor: int, payload: bytes) -> int:
            nonlocal attempted
            if not attempted:
                attempted = True
                self.commitment_root.rename(moved_root)
            return real_write(descriptor, payload)

        with (
            patch(
                "football_tracking.target_finite_population.os.write",
                side_effect=rename_root_then_write,
            ),
            self.assertRaises(OSError),
        ):
            self._build_plan(_application(population, decisions))
        self.assertTrue(attempted)
        self.assertFalse(moved_root.exists())
        self.assertEqual([], list(self.commitment_root.iterdir()))

    @unittest.skipUnless(os.name == "nt", "Windows entry sharing contract")
    def test_windows_commitment_substitution_after_open_fails_closed(self) -> None:
        plan = self._plan()
        commitment_path = self._commitment_path(plan)
        original_bytes = commitment_path.read_bytes()
        displaced = commitment_path.with_suffix(".displaced")
        real_read = os.read
        attempted = False

        def substitute_then_read(descriptor: int, size: int) -> bytes:
            nonlocal attempted
            if not attempted:
                attempted = True
                commitment_path.rename(displaced)
                commitment_path.write_bytes(original_bytes)
            return real_read(descriptor, size)

        with (
            patch(
                "football_tracking.target_finite_population.os.read",
                side_effect=substitute_then_read,
            ),
            self.assertRaises(OSError),
        ):
            validate_target_prelabel_commitment(plan, commitment_path)
        self.assertTrue(attempted)
        self.assertFalse(displaced.exists())
        self.assertEqual(original_bytes, commitment_path.read_bytes())

    @unittest.skipIf(os.name == "nt", "registry root swap test requires POSIX dir_fd")
    def test_registry_root_swap_fails_closed_and_never_publishes_to_replacement(self) -> None:
        population, decisions = _population(40, accepted=20, abstained=19)
        moved_root = self.sandbox / "moved-registry"
        real_link = os.link
        swapped = False

        def swap_root_then_link(*args: object, **kwargs: object) -> None:
            nonlocal swapped
            if not swapped:
                swapped = True
                self.commitment_root.rename(moved_root)
                self.commitment_root.mkdir()
            real_link(*args, **kwargs)

        with (
            patch(
                "football_tracking.target_finite_population.os.link",
                side_effect=swap_root_then_link,
            ),
            self.assertRaisesRegex(TargetFinitePopulationError, "registry path changed"),
        ):
            self._build_plan(_application(population, decisions))
        self.assertEqual([], list(self.commitment_root.iterdir()))
        self.assertEqual(
            1,
            len(
                list(
                    moved_root.glob(
                        "*.target-prelabel-commitment.v1.json"
                    )
                )
            ),
        )

    @unittest.skipIf(os.name == "nt", "entry swap test requires POSIX dir_fd")
    def test_commitment_entry_swap_during_capture_fails_closed(self) -> None:
        plan = self._plan()
        commitment_path = self._commitment_path(plan)
        original_bytes = commitment_path.read_bytes()
        displaced = commitment_path.with_suffix(".displaced")
        real_read = os.read
        swapped = False

        def swap_entry_then_read(descriptor: int, size: int) -> bytes:
            nonlocal swapped
            if not swapped:
                swapped = True
                commitment_path.rename(displaced)
                commitment_path.write_bytes(original_bytes)
            return real_read(descriptor, size)

        with (
            patch(
                "football_tracking.target_finite_population.os.read",
                side_effect=swap_entry_then_read,
            ),
            self.assertRaisesRegex(TargetFinitePopulationError, "changed while"),
        ):
            validate_target_prelabel_commitment(plan, commitment_path)

    @unittest.skipIf(os.name == "nt", "directory fsync assertion requires POSIX")
    def test_commitment_publication_uses_no_replace_link_and_fsyncs_registry(self) -> None:
        real_link = os.link
        real_fsync = os.fsync
        link_calls: list[dict[str, object]] = []
        fsync_calls: list[int] = []

        def record_link(*args: object, **kwargs: object) -> None:
            link_calls.append(dict(kwargs))
            real_link(*args, **kwargs)

        def record_fsync(descriptor: int) -> None:
            fsync_calls.append(descriptor)
            real_fsync(descriptor)

        with (
            patch(
                "football_tracking.target_finite_population.os.link",
                side_effect=record_link,
            ),
            patch(
                "football_tracking.target_finite_population.os.fsync",
                side_effect=record_fsync,
            ),
        ):
            self._plan()
        self.assertEqual(1, len(link_calls))
        registry_fd = link_calls[0]["src_dir_fd"]
        self.assertEqual(registry_fd, link_calls[0]["dst_dir_fd"])
        self.assertFalse(link_calls[0]["follow_symlinks"])
        self.assertGreaterEqual(fsync_calls.count(registry_fd), 2)

    def test_freeze_cli_cannot_bypass_commitment_with_alternate_output(self) -> None:
        from scripts.build_target_finite_population_audit import main

        population, decisions = _population(40, accepted=20, abstained=19)
        application_path = self.commitment_root.parent / "frozen-application.json"
        _write_json(application_path, _application(population, decisions))
        registry = self.commitment_root.parent / "cli-canonical-registry"

        def freeze(output: Path, *extra: str) -> int:
            return main(
                [
                    "freeze",
                    "--frozen-application",
                    str(application_path),
                    "--target-run-id",
                    "run-cli",
                    "--confirmed-config-sha256",
                    "4" * 64,
                    "--commitment-root",
                    str(registry),
                    "--output-dir",
                    str(output),
                    *extra,
                ]
            )

        first_output = self.commitment_root.parent / "cli-plan-a"
        self.assertEqual(0, freeze(first_output))
        first_plan_path = first_output / "target_finite_population_audit_plan.v1.json"
        self.assertTrue(first_plan_path.is_file())

        for index, legacy in enumerate(
            (
                ("--sampling-seed", "post-label-cli-seed"),
                ("--sample-size", "39"),
            )
        ):
            rejected_output = self.commitment_root.parent / f"cli-plan-rejected-{index}"
            with self.assertRaises(SystemExit) as rejected:
                freeze(rejected_output, *legacy)
            self.assertEqual(2, rejected.exception.code)
            self.assertFalse(rejected_output.exists())

        alternate_output = self.commitment_root.parent / "cli-plan-b"
        self.assertEqual(0, freeze(alternate_output))
        alternate_plan_path = alternate_output / "target_finite_population_audit_plan.v1.json"
        self.assertEqual(first_plan_path.read_bytes(), alternate_plan_path.read_bytes())
        self.assertEqual(1, freeze(first_output))

    def test_activation_rejects_alternate_registry_record_and_duplicate_target_anchor(self) -> None:
        if os.name == "nt":
            self.skipTest("canonical activation anchor requires POSIX O_NOFOLLOW handles")
        from football_tracking.review_evidence_bundle import (
            TARGET_BUNDLE_ARTIFACT_TYPE,
            ReviewEvidenceBundleError,
            ValidatedReviewEvidenceBundle,
            _validate_trusted_prelabel_commitment_anchor,
            activate_review_evidence_bundle,
        )

        population, decisions = _population(40, accepted=20, abstained=19)
        application = _application(population, decisions)
        canonical_plan = self._build_plan(application)
        alternate_root = self.commitment_root.parent / "alternate-commitments"
        alternate_root.mkdir()
        alternate_plan = deepcopy(canonical_plan)
        canonical_record = self._commitment_path(canonical_plan)
        forged_record = json.loads(canonical_record.read_text(encoding="utf-8"))
        forged_record["ordering_salt"] = "f" * 64
        alternate_record = alternate_root / canonical_record.name
        alternate_record.write_text(
            json.dumps(
                forged_record,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n",
            encoding="utf-8",
        )
        alternate_plan["external_commitment"]["record_sha256"] = _file_sha(alternate_record)

        def fake_bundle(
            root: Path,
            plan: dict[str, object],
            record_path: Path,
        ) -> ValidatedReviewEvidenceBundle:
            root.mkdir()
            plan_path = root / "target-plan.json"
            bundled_record = root / record_path.name
            _write_json(plan_path, plan)
            bundled_record.write_bytes(record_path.read_bytes())
            queue_path = root / "selective_review_queue.v1.json"
            _write_json(
                queue_path,
                {
                    "bindings": {
                        "target_audit_plan": {
                            "path": plan_path.name,
                            "sha256": _file_sha(plan_path),
                        },
                        "target_prelabel_commitment": {
                            "path": bundled_record.name,
                            "sha256": _file_sha(bundled_record),
                        },
                    }
                },
            )
            manifest_path = root / "review_evidence_bundle.v1.json"
            _write_json(manifest_path, {"artifact_type": TARGET_BUNDLE_ARTIFACT_TYPE})
            return ValidatedReviewEvidenceBundle(
                root=root,
                manifest_path=manifest_path,
                manifest={"artifact_type": TARGET_BUNDLE_ARTIFACT_TYPE},
                bundle_sha256="1" * 64,
                queue_path=queue_path,
                queue_sha256=_file_sha(queue_path),
                total_size_bytes=1,
            )

        canonical_bundle = fake_bundle(
            self.commitment_root.parent / "canonical-bundle",
            canonical_plan,
            canonical_record,
        )
        _validate_trusted_prelabel_commitment_anchor(
            canonical_bundle,
            self.commitment_root,
        )

        alternate_bundle = fake_bundle(
            self.commitment_root.parent / "alternate-bundle",
            alternate_plan,
            alternate_record,
        )
        empty_canonical_root = self.commitment_root.parent / "empty-canonical-registry"
        empty_canonical_root.mkdir()
        with self.assertRaises(ReviewEvidenceBundleError) as missing_anchor:
            _validate_trusted_prelabel_commitment_anchor(
                alternate_bundle,
                empty_canonical_root,
            )
        self.assertEqual(
            "prelabel_commitment_anchor_missing",
            missing_anchor.exception.code,
        )
        output = self.commitment_root.parent / "activation-output"
        output.mkdir()
        with (
            patch(
                "football_tracking.review_evidence_bundle.validate_review_evidence_bundle",
                return_value=alternate_bundle,
            ),
            self.assertRaises(ReviewEvidenceBundleError) as conflict,
        ):
            activate_review_evidence_bundle(
                alternate_bundle.root,
                output,
                expected_run_id="run-1",
                expected_source_sha256="1" * 64,
                expected_root_contract_sha256="2" * 64,
                trusted_prelabel_commitment_root=self.commitment_root,
            )
        self.assertEqual("prelabel_commitment_conflict", conflict.exception.code)

        duplicate = self.commitment_root / "duplicate.target-prelabel-commitment.v1.json"
        duplicate.write_bytes(canonical_record.read_bytes())
        with self.assertRaises(ReviewEvidenceBundleError) as duplicate_error:
            _validate_trusted_prelabel_commitment_anchor(
                canonical_bundle,
                self.commitment_root,
            )
        self.assertEqual("prelabel_commitment_conflict", duplicate_error.exception.code)

    def test_cherry_picked_sample_is_rejected_after_all_self_hashes_are_recomputed(self) -> None:
        population, decisions = _population(40, accepted=20)
        plan = self._build_plan(_application(population, decisions))
        forged = deepcopy(plan)
        forged["sample"] = [
            {**row, "order": index}
            for index, row in enumerate(reversed(forged["sample"]))
        ]
        forged["sample_sha256"] = _sha(forged["sample"])
        forged["plan_commitment"]["sample_sha256"] = forged["sample_sha256"]
        forged["plan_commitment_sha256"] = _sha(forged["plan_commitment"])
        content = {
            key: value
            for key, value in forged.items()
            if key not in {"external_commitment", "plan_sha256"}
        }
        forged["plan_sha256"] = _sha(content)

        with self.assertRaisesRegex(TargetFinitePopulationError, "hash-ranked"):
            evaluate_target_audit(
                forged,
                Path("labels-must-not-be-read.json"),
                commitment_path=self._commitment_path(plan),
            )

    def test_zero_error_audit_qualifies_only_the_exact_target(self) -> None:
        plan = self._plan()
        with tempfile.TemporaryDirectory() as temp:
            labels_path = self._labels(Path(temp), plan)
            qualification = self._evaluate(plan, labels_path)
            validate_target_qualification(
                qualification,
                plan,
                expected_bindings=plan["bindings"],
                commitment_path=self._commitment_path(plan),
                labels_path=labels_path,
            )
        self.assertEqual("qualified", qualification["status"])
        self.assertFalse(qualification["training_eligible"])
        self.assertFalse(qualification["reusable"])
        self.assertEqual("exact_target_only", qualification["promotion_scope"])
        self.assertNotIn("labels", qualification)
        for field, value in plan["bindings"].items():
            wrong_target = dict(plan["bindings"])
            wrong_target[field] = (
                f"{field}-other"
                if not field.endswith("_sha256")
                else ("f" * 64 if value != "f" * 64 else "e" * 64)
            )
            with (
                self.subTest(binding=field),
                self.assertRaisesRegex(TargetFinitePopulationError, "binding"),
            ):
                validate_target_qualification(
                    qualification,
                    plan,
                    expected_bindings=wrong_target,
                    commitment_path=self._commitment_path(plan),
                )

    def test_errors_or_insufficient_support_remain_review_only(self) -> None:
        plan = self._plan()
        accepted_id = next(row["candidate_id"] for row in plan["sample"] if row["decision"] == "accept")
        with tempfile.TemporaryDirectory() as temp:
            one_error = self._evaluate(
                plan,
                self._labels(
                    Path(temp),
                    plan,
                    labels={accepted_id: "field_line_or_mark"},
                ),
            )
        self.assertEqual("review_only", one_error["status"])

        error_population, error_decisions = _population(370, accepted=183, abstained=185)
        error_plan = self._build_plan(_application(error_population, error_decisions))
        with tempfile.TemporaryDirectory() as temp:
            multiple_errors = self._evaluate(
                error_plan,
                self._labels(Path(temp), error_plan),
            )
        self.assertEqual("review_only", multiple_errors["status"])

        population, decisions = _population(367, accepted=182)
        small = self._build_plan(_application(population, decisions))
        with tempfile.TemporaryDirectory() as temp:
            result = self._evaluate(
                small,
                self._labels(Path(temp), small),
            )
        self.assertEqual("review_only", result["status"])
        self.assertFalse(result["support"]["accepted"]["passed"])
        self.assertFalse(result["support"]["true_balls"]["passed"])

    def test_fabricated_mapping_or_tampered_package_manifest_fails_closed(self) -> None:
        plan = self._plan()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fabricated = root / "fabricated-labels.json"
            _write_json(
                fabricated,
                {
                    "labels": {
                        row["candidate_id"]: "match_ball"
                        for row in plan["sample"]
                    }
                },
            )
            with self.assertRaises(TargetFinitePopulationError):
                self._evaluate(plan, fabricated)

            labels_path = self._labels(root / "valid", plan)
            tampered = json.loads(labels_path.read_text(encoding="utf-8"))
            tampered["annotation_package"]["ledger_sha256"] = "f" * 64
            _write_json(labels_path, tampered)
            with self.assertRaisesRegex(TargetFinitePopulationError, "hash|ledger"):
                self._evaluate(plan, labels_path)

    def test_target_labels_require_two_blind_humans_evidence_and_resolution(self) -> None:
        plan = self._plan()
        cases = (
            ("same-reviewer", {"same_reviewer": True}),
            ("missing-evidence", {"omit_evidence": True}),
            (
                "unresolved-disagreement",
                {"unresolved_candidate_id": plan["sample"][0]["candidate_id"]},
            ),
        )
        for name, options in cases:
            with (
                self.subTest(case=name),
                tempfile.TemporaryDirectory() as temp,
                self.assertRaises(TargetFinitePopulationError),
            ):
                self._labels(Path(temp), plan, **options)

    def test_target_truth_cannot_enter_plan_or_reusable_promotion(self) -> None:
        population, decisions = _population(368, accepted=300)
        application = _application(population, decisions)
        application["decisions"][0]["label"] = "match_ball"
        content = {
            key: value
            for key, value in application.items()
            if key not in {"generated_at", "application_content_sha256"}
        }
        application["application_content_sha256"] = _sha(content)
        with self.assertRaisesRegex(TargetFinitePopulationError, "label|truth"):
            self._build_plan(application)

        plan = self._plan()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            labels_path = self._labels(root / "labels", plan)
            plan_path = root / "target-plan.json"
            _write_json(plan_path, plan)
            consumer = root / "training-artifact.json"
            _write_json(
                consumer,
                {
                    "training_data_sha256": _file_sha(labels_path),
                },
            )
            with self.assertRaisesRegex(TargetFinitePopulationError, "referenced"):
                validate_target_label_non_leakage(
                    labels_path,
                    consumer_artifact_paths=[consumer],
                    plan_path=plan_path,
                    commitment_path=self._commitment_path(plan),
                )

    def test_non_leakage_rejects_copied_real_rows_without_legacy_digests_and_allows_clean_artifacts(
        self,
    ) -> None:
        plan = self._plan()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            labels_path = self._labels(root / "labels", plan)
            plan_path = root / "target-plan.json"
            _write_json(plan_path, plan)
            labels = json.loads(labels_path.read_text(encoding="utf-8"))
            copied_row = labels["labels"][0]
            legacy_digests = {
                _file_sha(labels_path),
                labels["annotation_package"]["ledger_sha256"],
                labels["annotation_package"]["resolution_sha256"],
            }
            structured = root / "training-rows.json"
            _write_json(structured, {"training_rows": [copied_row]})
            plain = root / "calibration-notes.txt"
            plain.write_text(
                f"candidate_id={copied_row['candidate_id']} label={copied_row['label']}\n",
                encoding="utf-8",
            )
            clean = root / "clean-audit.json"
            _write_json(
                clean,
                {
                    "candidate_id": "development-only-candidate",
                    "candidate_fingerprint": "f" * 64,
                    "evidence_sha256": "e" * 64,
                },
            )
            self.assertTrue(
                all(
                    digest not in structured.read_text(encoding="utf-8")
                    for digest in legacy_digests
                )
            )

            for consumer in (structured, plain):
                with (
                    self.subTest(consumer=consumer.name),
                    self.assertRaisesRegex(TargetFinitePopulationError, "referenced"),
                ):
                    validate_target_label_non_leakage(
                        labels_path,
                        consumer_artifact_paths=[consumer],
                        plan_path=plan_path,
                        commitment_path=self._commitment_path(plan),
                    )
            validate_target_label_non_leakage(
                labels_path,
                consumer_artifact_paths=[clean],
                plan_path=plan_path,
                commitment_path=self._commitment_path(plan),
            )

    def test_non_leakage_streaming_detects_candidate_and_digest_across_chunk_boundaries(
        self,
    ) -> None:
        plan = self._plan()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            labels_path = self._labels(root / "labels", plan)
            plan_path = root / "target-plan.json"
            _write_json(plan_path, plan)
            labels = json.loads(labels_path.read_text(encoding="utf-8"))
            candidate_id = labels["labels"][0]["candidate_id"]
            evidence_sha256 = labels["labels"][0]["evidence_sha256"]
            for index, protected_token in enumerate((candidate_id, evidence_sha256)):
                consumer = root / f"boundary-{index}.txt"
                consumer.write_text(
                    f"clean-prefix {protected_token} clean-suffix",
                    encoding="utf-8",
                )
                with (
                    self.subTest(token=protected_token),
                    patch(
                        "football_tracking.target_finite_population.NON_LEAKAGE_SCAN_CHUNK_BYTES",
                        7,
                    ),
                    self.assertRaisesRegex(TargetFinitePopulationError, "referenced"),
                ):
                    validate_target_label_non_leakage(
                        labels_path,
                        consumer_artifact_paths=[consumer],
                        plan_path=plan_path,
                        commitment_path=self._commitment_path(plan),
                    )

    def test_non_leakage_streaming_scans_large_clean_file_without_read_bytes(self) -> None:
        plan = self._plan()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            labels_path = self._labels(root / "labels", plan)
            plan_path = root / "target-plan.json"
            _write_json(plan_path, plan)
            consumer = root / "large-clean-stream.txt"
            with consumer.open("wb") as handle:
                for _index in range(128):
                    handle.write(b"development-only-token\n" * 4096)
            real_read_bytes = Path.read_bytes

            def reject_consumer_read_bytes(path: Path) -> bytes:
                if path == consumer:
                    raise AssertionError("non-leakage consumer scan must stream")
                return real_read_bytes(path)

            with patch.object(
                Path,
                "read_bytes",
                reject_consumer_read_bytes,
            ):
                validate_target_label_non_leakage(
                    labels_path,
                    consumer_artifact_paths=[consumer],
                    plan_path=plan_path,
                    commitment_path=self._commitment_path(plan),
                )

    def test_non_leakage_streaming_byte_budget_fails_closed(self) -> None:
        plan = self._plan()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            labels_path = self._labels(root / "labels", plan)
            plan_path = root / "target-plan.json"
            _write_json(plan_path, plan)
            consumer = root / "clean.txt"
            consumer.write_text("development-only-token\n", encoding="utf-8")
            with (
                patch(
                    "football_tracking.target_finite_population.NON_LEAKAGE_MAX_TOTAL_BYTES",
                    1,
                ),
                self.assertRaisesRegex(TargetFinitePopulationError, "byte budget"),
            ):
                validate_target_label_non_leakage(
                    labels_path,
                    consumer_artifact_paths=[consumer],
                    plan_path=plan_path,
                    commitment_path=self._commitment_path(plan),
                )

    def test_non_leakage_uses_declared_lineage_for_large_binary_payload(self) -> None:
        from football_tracking import target_finite_population as target_module

        plan = self._plan()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            labels_path = self._labels(root / "labels", plan)
            plan_path = root / "target-plan.json"
            _write_json(plan_path, plan)
            declared_weights = root / "declared-model-weights.npz"
            with declared_weights.open("wb") as handle:
                handle.truncate(8 * 1024 * 1024)
            streaming_scan = Mock(wraps=target_module._stream_target_tokens)
            with patch(
                "football_tracking.target_finite_population._stream_target_tokens",
                streaming_scan,
            ):
                validate_target_label_non_leakage(
                    labels_path,
                    consumer_artifact_paths=[declared_weights],
                    plan_path=plan_path,
                    commitment_path=self._commitment_path(plan),
                )
            scanned_paths = {Path(call.args[0]) for call in streaming_scan.call_args_list}
            self.assertNotIn(declared_weights, scanned_paths)

    @unittest.skipIf(
        os.name == "nt" or not hasattr(os, "mkfifo"),
        "nonblocking FIFO scan test requires POSIX",
    )
    def test_non_leakage_stream_rejects_fifo_and_symlink_without_following(self) -> None:
        plan = self._plan()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            labels_path = self._labels(root / "labels", plan)
            plan_path = root / "target-plan.json"
            _write_json(plan_path, plan)
            clean = root / "clean.txt"
            clean.write_text("development-only-token\n", encoding="utf-8")
            fifo = root / "consumer-fifo.txt"
            os.mkfifo(fifo)
            link = root / "consumer-link.txt"
            link.symlink_to(clean)
            for consumer in (fifo, link):
                with (
                    self.subTest(consumer=consumer.name),
                    self.assertRaisesRegex(
                        TargetFinitePopulationError,
                        "regular file|unavailable",
                    ),
                ):
                    validate_target_label_non_leakage(
                        labels_path,
                        consumer_artifact_paths=[consumer],
                        plan_path=plan_path,
                        commitment_path=self._commitment_path(plan),
                    )

    def test_plan_binding_and_sampling_design_are_immutable(self) -> None:
        plan = self._plan()
        tampered = deepcopy(plan)
        tampered["bindings"]["confirmed_config_sha256"] = "f" * 64
        with self.assertRaisesRegex(TargetFinitePopulationError, "plan"):
            evaluate_target_audit(
                tampered,
                Path("labels-must-not-be-read.json"),
                commitment_path=self._commitment_path(plan),
            )
        tampered = deepcopy(plan)
        tampered["sample"].reverse()
        with self.assertRaisesRegex(TargetFinitePopulationError, "plan"):
            evaluate_target_audit(
                tampered,
                Path("labels-must-not-be-read.json"),
                commitment_path=self._commitment_path(plan),
            )

    def test_legacy_v1_consumer_rejects_the_new_target_envelope(self) -> None:
        plan = self._plan()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            qualification = self._evaluate(
                plan,
                self._labels(root / "labels", plan),
            )
            policy_path = root / "selective_policy.v1.json"
            decisions_path = root / "selective_decisions.v1.json"
            payload = json.dumps(qualification, sort_keys=True)
            policy_path.write_text(payload, encoding="utf-8")
            decisions_path.write_text(payload, encoding="utf-8")
            with self.assertRaisesRegex(SelectivePolicyError, "invalid selective policy envelope"):
                validate_selective_decisions_binding(policy_path, decisions_path)


if __name__ == "__main__":
    unittest.main()
