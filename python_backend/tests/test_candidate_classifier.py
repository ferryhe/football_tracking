from __future__ import annotations

import hashlib
import io
import json
import math
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

import numpy as np
import torch

from football_tracking.candidate_classifier import (
    CLASS_LABELS,
    MAX_BATCH_SIZE,
    MODEL_MANIFEST_NAME,
    MODEL_WEIGHTS_NAME,
    PREDICTIONS_NAME,
    TRAINING_REPORT_NAME,
    ClassifierError,
    TrainingConfig,
    classify_candidates,
    classify_cli_main,
    load_candidate_classifier,
    train_candidate_classifier,
    train_cli_main,
    validate_candidate_classifier_package,
    validate_candidate_predictions_package,
)
from football_tracking.tracking_contracts import (
    CLASSIFICATION_LABELS,
    TRACKING_CONTRACT_REPORT_NAME,
    build_tracking_contract,
)


class CandidateClassifierTests(unittest.TestCase):
    def test_batch_size_limit_accepts_128_and_rejects_larger_values_before_loading(self) -> None:
        from football_tracking import candidate_classifier

        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = _write_training_inputs(root)
            package = root / "model"
            train_candidate_classifier(
                inputs["dataset"],
                inputs["resolution"],
                inputs["contract"],
                package,
                config=TrainingConfig(epochs=1, batch_size=MAX_BATCH_SIZE, seed=127),
            )
            source_contract = _write_inference_contract(root, inputs["candidates"])
            predictions_dir = root / "predictions"
            predictions = classify_candidates(
                package,
                inputs["dataset"],
                source_contract,
                predictions_dir,
                batch_size=MAX_BATCH_SIZE,
            )
            self.assertEqual(MAX_BATCH_SIZE, predictions["inference"]["batch_size"])
            self.assertEqual(
                predictions,
                validate_candidate_predictions_package(
                    package,
                    inputs["dataset"],
                    source_contract,
                    predictions_dir / PREDICTIONS_NAME,
                ),
            )

            for rejected in (MAX_BATCH_SIZE + 1, 10**12):
                with self.subTest(operation="train", batch_size=rejected), patch.object(
                    candidate_classifier,
                    "_load_dataset_manifest",
                    side_effect=AssertionError("dataset or tensors loaded"),
                ), self.assertRaisesRegex(ClassifierError, "between 1 and 128"):
                    train_candidate_classifier(
                        root / "unused-dataset.json",
                        root / "unused-resolution.json",
                        root / "unused-contract.json",
                        root / f"rejected-training-{rejected}",
                        config=TrainingConfig(epochs=1, batch_size=rejected),
                    )

                with self.subTest(operation="classify", batch_size=rejected), patch.object(
                    candidate_classifier,
                    "_load_candidate_classifier_with_bindings",
                    side_effect=AssertionError("model loaded"),
                ), self.assertRaisesRegex(ClassifierError, "between 1 and 128"):
                    classify_candidates(
                        root / "unused-model",
                        root / "unused-dataset.json",
                        root / "unused-contract.json",
                        root / f"rejected-predictions-{rejected}",
                        batch_size=rejected,
                    )

                with self.subTest(operation="predict", batch_size=rejected), patch.object(
                    candidate_classifier,
                    "_stack_batch",
                    side_effect=AssertionError("tensor batch loaded"),
                ), self.assertRaisesRegex(ClassifierError, "between 1 and 128"):
                    candidate_classifier._predict_logits(
                        candidate_classifier.CandidateClassifier(),
                        [{}],
                        rejected,
                    )

                malicious_path = root / f"malicious-predictions-{rejected}.json"
                _write_json(
                    malicious_path,
                    {"inference": {"device": "cpu", "batch_size": rejected}},
                )
                with self.subTest(operation="validate", batch_size=rejected), patch.object(
                    candidate_classifier,
                    "classify_candidates",
                    side_effect=AssertionError("inference invoked"),
                ), self.assertRaisesRegex(ClassifierError, "between 1 and 128"):
                    validate_candidate_predictions_package(
                        root / "unused-model",
                        root / "unused-dataset.json",
                        root / "unused-contract.json",
                        malicious_path,
                    )

    def test_model_package_rejects_oversized_training_batch_before_weight_or_tensor_loading(self) -> None:
        from football_tracking import candidate_classifier

        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = _write_training_inputs(root)
            package = root / "model"
            train_candidate_classifier(
                inputs["dataset"],
                inputs["resolution"],
                inputs["contract"],
                package,
                config=TrainingConfig(epochs=1, batch_size=2, seed=129),
            )
            manifest_path = package / MODEL_MANIFEST_NAME
            report_path = package / TRAINING_REPORT_NAME
            manifest = _read_json(manifest_path)
            report = _read_json(report_path)
            manifest["training_config"]["batch_size"] = MAX_BATCH_SIZE + 1
            report["training_config"]["batch_size"] = MAX_BATCH_SIZE + 1
            version_inputs = {
                "weights_sha256": manifest["weights_sha256"],
                "data_binding": manifest["data_binding"],
                "training_config": manifest["training_config"],
                "calibration": manifest["calibration"],
                "supported_mask": manifest["supported_mask"],
                "class_order": manifest["class_order"],
                "architecture": manifest["architecture"],
                "input_contract": manifest["input_contract"],
                "code_sha256": manifest["code_sha256"],
                "runtime": manifest["runtime"],
            }
            model_version = hashlib.sha256(
                json.dumps(version_inputs, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
            ).hexdigest()
            manifest["model_version"] = model_version
            report["model_version"] = model_version
            _write_json(report_path, report)
            manifest["training_report_sha256"] = _sha256(report_path)
            _write_json(manifest_path, manifest)

            with patch.object(
                candidate_classifier.torch,
                "load",
                side_effect=AssertionError("weights loaded"),
            ), patch.object(
                candidate_classifier,
                "_load_examples",
                side_effect=AssertionError("tensors loaded"),
            ), self.assertRaisesRegex(ClassifierError, "between 1 and 128"):
                validate_candidate_classifier_package(
                    package,
                    inputs["dataset"],
                    inputs["resolution"],
                    inputs["contract"],
                )

    def test_cpu_training_filters_truth_builds_leak_free_split_and_calibrates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = _write_training_inputs(root)
            package = root / "model"

            report = train_candidate_classifier(
                inputs["dataset"],
                inputs["resolution"],
                inputs["contract"],
                package,
                config=TrainingConfig(epochs=2, batch_size=3, learning_rate=0.01, seed=17),
            )
            model, manifest = load_candidate_classifier(package)
            validated_package = validate_candidate_classifier_package(
                package,
                inputs["dataset"],
                inputs["resolution"],
                inputs["contract"],
            )
            package_files_exist = all(
                (package / name).is_file() for name in (MODEL_WEIGHTS_NAME, MODEL_MANIFEST_NAME, TRAINING_REPORT_NAME)
            )

        self.assertEqual(tuple(CLASSIFICATION_LABELS), CLASS_LABELS)
        self.assertEqual(manifest["model_version"], validated_package["manifest"]["model_version"])
        self.assertEqual(list(CLASS_LABELS), manifest["class_order"])
        self.assertLess(sum(parameter.numel() for parameter in model.parameters()), 100_000)
        self.assertTrue(all(parameter.device.type == "cpu" for parameter in model.parameters()))
        self.assertEqual(9, report["truth_selection"]["eligible_count"])
        excluded = report["truth_selection"]["excluded"]
        self.assertIn("prelabel-only", excluded)
        self.assertIn("single-vote", excluded)
        self.assertIn("conflict", excluded)
        self.assertIn("ai-unknown", excluded)
        self.assertEqual({"match_ball", "equipment_or_background", "unknown"}, set(manifest["supported_classes"]))
        self.assertEqual(7, len(manifest["supported_mask"]))
        self.assertTrue(report["split"]["leakage_checks"]["passed"])
        self.assertEqual({"train", "calibration", "test"}, set(report["split"]["assignments"].values()))
        for split_name in ("train", "calibration", "test"):
            self.assertGreater(report["split"]["support"][split_name]["match_ball"], 0)
            self.assertGreater(report["split"]["support"][split_name]["equipment_or_background"], 0)
            self.assertIn("field_line_or_mark", report["split"]["masked_classes"][split_name])
        _assert_split_disjoint(self, report["split"])
        calibration = report["calibration"]
        self.assertGreater(calibration["temperature"], 0.0)
        self.assertTrue(math.isfinite(calibration["temperature"]))
        self.assertLessEqual(calibration["after"]["nll"], calibration["before"]["nll"] + 1e-8)
        for phase in ("before", "after"):
            self.assertTrue(math.isfinite(calibration[phase]["ece"]))
            self.assertTrue(math.isfinite(calibration[phase]["brier"]))
            self.assertEqual(7, len(calibration[phase]["confusion"]))
        self.assertEqual("cpu", manifest["runtime"]["device"])
        self.assertTrue(package_files_exist)

    def test_strict_package_validation_recomputes_metrics_instead_of_trusting_self_consistent_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = _write_training_inputs(root)
            package = root / "model"
            train_candidate_classifier(
                inputs["dataset"],
                inputs["resolution"],
                inputs["contract"],
                package,
                config=TrainingConfig(epochs=1, batch_size=3, learning_rate=0.01, seed=19),
            )
            manifest_path = package / MODEL_MANIFEST_NAME
            report_path = package / TRAINING_REPORT_NAME
            manifest = _read_json(manifest_path)
            report = _read_json(report_path)
            forged_temperature = float(manifest["calibration"]["temperature"]) + 0.25
            manifest["calibration"]["temperature"] = forged_temperature
            report["calibration"]["temperature"] = forged_temperature
            version_inputs = {
                "weights_sha256": manifest["weights_sha256"],
                "data_binding": manifest["data_binding"],
                "training_config": manifest["training_config"],
                "calibration": manifest["calibration"],
                "supported_mask": manifest["supported_mask"],
                "class_order": manifest["class_order"],
                "architecture": manifest["architecture"],
                "input_contract": manifest["input_contract"],
                "code_sha256": manifest["code_sha256"],
                "runtime": manifest["runtime"],
            }
            forged_model_version = hashlib.sha256(
                json.dumps(
                    version_inputs,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest()
            manifest["model_version"] = forged_model_version
            report["model_version"] = forged_model_version
            _write_json(report_path, report)
            manifest["training_report_sha256"] = _sha256(report_path)
            _write_json(manifest_path, manifest)

            with self.assertRaisesRegex(ClassifierError, "calibration metrics do not reproduce"):
                validate_candidate_classifier_package(
                    package,
                    inputs["dataset"],
                    inputs["resolution"],
                    inputs["contract"],
                )

    def test_inference_masks_missing_classes_and_preserves_confirmed_unknown_and_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = _write_training_inputs(root)
            package = root / "model"
            train_candidate_classifier(
                inputs["dataset"],
                inputs["resolution"],
                inputs["contract"],
                package,
                config=TrainingConfig(epochs=2, batch_size=4, seed=5),
            )
            source_contract = _write_inference_contract(root, inputs["candidates"])
            output_dir = root / "predictions"

            predictions = classify_candidates(package, inputs["dataset"], source_contract, output_dir, batch_size=4)
            validated_predictions = validate_candidate_predictions_package(
                package,
                inputs["dataset"],
                source_contract,
                output_dir / PREDICTIONS_NAME,
            )
            derived = json.loads((output_dir / TRACKING_CONTRACT_REPORT_NAME).read_text(encoding="utf-8"))
            source = json.loads(source_contract.read_text(encoding="utf-8"))
            predictions_exist = (output_dir / PREDICTIONS_NAME).is_file()

        unsupported = set(CLASS_LABELS) - {"match_ball", "equipment_or_background", "unknown"}
        self.assertEqual({"device": "cpu", "batch_size": 4}, predictions["inference"])
        self.assertEqual(predictions, validated_predictions)
        by_candidate = {row["candidate_id"]: row for row in predictions["predictions"]}
        self.assertEqual(set(inputs["candidate_ids"]), set(by_candidate))
        for row in predictions["predictions"]:
            self.assertAlmostEqual(1.0, sum(row["probabilities"].values()), places=6)
            self.assertTrue(
                all(math.isfinite(value) and 0.0 <= value <= 1.0 for value in row["probabilities"].values())
            )
            self.assertTrue(all(row["probabilities"][label] == 0.0 for label in unsupported))
            self.assertIn(row["predicted_label"], {"match_ball", "equipment_or_background", "unknown"})
            self.assertEqual(predictions["model_version"], row["model_version"])

        self.assertEqual(source["classifications"], derived["classifications"][: len(source["classifications"])])
        self.assertEqual(source["source"], derived["source"])
        confirmed = [row for row in derived["classifications"] if row["label_origin"] != "prelabel"]
        self.assertTrue(any(row["candidate_id"] == "g0-unknown" and row["label"] == "unknown" for row in confirmed))
        self.assertEqual(2, sum(row["candidate_id"] == "conflict" for row in confirmed))
        self.assertEqual(
            [{"candidate_id": "g0-unknown", "decision": "abstain", "confidence": 0.8, "reason": "human review"}],
            derived["decisions"],
        )
        prelabels = [row for row in derived["classifications"] if row["label_origin"] == "prelabel"]
        self.assertFalse(any(row["candidate_id"] in {"g0-unknown", "conflict"} for row in prelabels))
        self.assertTrue(any(row["candidate_id"] == "prelabel-only" and row["label"] == "unknown" for row in prelabels))
        for candidate_id, prediction in by_candidate.items():
            if candidate_id in {"g0-unknown", "conflict"}:
                continue
            self.assertTrue(
                any(
                    row["candidate_id"] == candidate_id and row["label"] == prediction["predicted_label"]
                    for row in prelabels
                )
            )
        self.assertTrue(predictions_exist)

    def test_loader_rejects_weight_class_order_and_version_tampering_and_uses_weights_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = _write_training_inputs(root)
            package = root / "model"
            train_candidate_classifier(
                inputs["dataset"],
                inputs["resolution"],
                inputs["contract"],
                package,
                config=TrainingConfig(epochs=1, seed=3),
            )
            real_load = torch.load
            with patch("football_tracking.candidate_classifier.torch.load", wraps=real_load) as mocked_load:
                load_candidate_classifier(package)
            self.assertTrue(mocked_load.call_args.kwargs["weights_only"])

            class_tamper = root / "class-tamper"
            shutil.copytree(package, class_tamper)
            manifest = _read_json(class_tamper / MODEL_MANIFEST_NAME)
            manifest["class_order"] = list(reversed(manifest["class_order"]))
            _write_json(class_tamper / MODEL_MANIFEST_NAME, manifest)
            with self.assertRaises(ClassifierError):
                load_candidate_classifier(class_tamper)

            version_tamper = root / "version-tamper"
            shutil.copytree(package, version_tamper)
            manifest = _read_json(version_tamper / MODEL_MANIFEST_NAME)
            manifest["model_version"] = "0" * 64
            _write_json(version_tamper / MODEL_MANIFEST_NAME, manifest)
            with self.assertRaises(ClassifierError):
                load_candidate_classifier(version_tamper)

            weight_tamper = root / "weight-tamper"
            shutil.copytree(package, weight_tamper)
            weight_path = weight_tamper / MODEL_WEIGHTS_NAME
            raw = bytearray(weight_path.read_bytes())
            raw[-1] ^= 1
            weight_path.write_bytes(raw)
            with self.assertRaises(ClassifierError):
                load_candidate_classifier(weight_tamper)

    def test_model_version_changes_with_seed_config_weights_or_calibration(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = _write_training_inputs(root)
            first_dir = root / "first"
            second_dir = root / "second"
            train_candidate_classifier(
                inputs["dataset"],
                inputs["resolution"],
                inputs["contract"],
                first_dir,
                config=TrainingConfig(epochs=1, seed=10),
            )
            train_candidate_classifier(
                inputs["dataset"],
                inputs["resolution"],
                inputs["contract"],
                second_dir,
                config=TrainingConfig(epochs=2, seed=11),
            )
            first = _read_json(first_dir / MODEL_MANIFEST_NAME)
            second = _read_json(second_dir / MODEL_MANIFEST_NAME)

        self.assertNotEqual(first["model_version"], second["model_version"])
        self.assertNotEqual(first["weights_sha256"], second["weights_sha256"])

    def test_same_seed_is_reproducible_and_resolution_bindings_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = _write_training_inputs(root)
            config = TrainingConfig(epochs=1, seed=23)
            train_candidate_classifier(
                inputs["dataset"], inputs["resolution"], inputs["contract"], root / "first", config=config
            )
            train_candidate_classifier(
                inputs["dataset"], inputs["resolution"], inputs["contract"], root / "second", config=config
            )
            first = _read_json(root / "first" / MODEL_MANIFEST_NAME)
            second = _read_json(root / "second" / MODEL_MANIFEST_NAME)
            first_report = _read_json(root / "first" / TRAINING_REPORT_NAME)
            second_report = _read_json(root / "second" / TRAINING_REPORT_NAME)

            resolution = _read_json(inputs["resolution"])
            resolution["source_contract"]["sha256"] = "0" * 64
            _write_json(inputs["resolution"], resolution)
            with self.assertRaisesRegex(ClassifierError, "source_contract"):
                train_candidate_classifier(
                    inputs["dataset"],
                    inputs["resolution"],
                    inputs["contract"],
                    root / "invalid",
                    config=config,
                )

        self.assertEqual(first["model_version"], second["model_version"])
        self.assertEqual(first["weights_sha256"], second["weights_sha256"])
        self.assertEqual(first_report["split"], second_report["split"])

    def test_inference_identity_guard_and_atomic_contract_last_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = _write_training_inputs(root)
            package = root / "model"
            train_candidate_classifier(
                inputs["dataset"],
                inputs["resolution"],
                inputs["contract"],
                package,
                config=TrainingConfig(epochs=1, seed=31),
            )
            valid_contract = _write_inference_contract(root, inputs["candidates"])
            bad_candidates = [dict(candidate) for candidate in inputs["candidates"]]
            bad_candidates[0]["frame_index"] = int(bad_candidates[0]["frame_index"]) + 1
            bad_contract = root / "bad-contract.json"
            _write_json(bad_contract, build_tracking_contract(candidates=bad_candidates))
            with self.assertRaisesRegex(ClassifierError, "identity mismatch"):
                classify_candidates(package, inputs["dataset"], bad_contract, root / "bad-output")

            from football_tracking import candidate_classifier

            real_write = candidate_classifier._write_json_file
            write_order: list[str] = []

            def record_write(path: Path, payload: dict[str, object]) -> None:
                write_order.append(path.name)
                real_write(path, payload)

            with patch.object(candidate_classifier, "_write_json_file", side_effect=record_write):
                classify_candidates(package, inputs["dataset"], valid_contract, root / "success-output")
            self.assertEqual(TRACKING_CONTRACT_REPORT_NAME, write_order[-1])

            def fail_contract(path: Path, payload: dict[str, object]) -> None:
                if path.name == TRACKING_CONTRACT_REPORT_NAME:
                    raise OSError("injected contract failure")
                real_write(path, payload)

            with patch.object(candidate_classifier, "_write_json_file", side_effect=fail_contract):
                with self.assertRaisesRegex(OSError, "injected"):
                    classify_candidates(package, inputs["dataset"], valid_contract, root / "failed-output")
            self.assertFalse((root / "failed-output").exists())
            self.assertEqual([], list(root.glob(".failed-output.staging-*")))

            def interrupt_contract(path: Path, payload: dict[str, object]) -> None:
                if path.name == TRACKING_CONTRACT_REPORT_NAME:
                    raise KeyboardInterrupt("injected inference interrupt")
                real_write(path, payload)

            with patch.object(candidate_classifier, "_write_json_file", side_effect=interrupt_contract):
                with self.assertRaisesRegex(KeyboardInterrupt, "injected inference"):
                    classify_candidates(package, inputs["dataset"], valid_contract, root / "interrupted-output")
            self.assertFalse((root / "interrupted-output").exists())
            self.assertEqual([], list(root.glob(".interrupted-output.staging-*")))

    def test_split_search_skips_unsupported_first_pair_for_later_valid_pair(self) -> None:
        from football_tracking import candidate_classifier

        seed = 41
        component_ids = [
            sorted(
                [
                    f"component-{index}-extra",
                    f"component-{index}-match",
                    f"component-{index}-noise",
                ]
            )
            for index in range(5)
        ]
        ordered = sorted(
            component_ids,
            key=lambda values: hashlib.sha256(f"{seed}:{','.join(values)}".encode()).hexdigest(),
        )
        unique_unknown_id = next(value for value in ordered[0] if value.endswith("-extra"))
        selected = []
        sources = []
        for index, candidate_ids in enumerate(component_ids):
            variant_id = f"variant-split-{index}"
            for candidate_offset, candidate_id in enumerate(candidate_ids):
                label = (
                    "unknown"
                    if candidate_id == unique_unknown_id
                    else "equipment_or_background"
                    if candidate_id.endswith("-noise")
                    else "match_ball"
                )
                selected.append(
                    {
                        "candidate_id": candidate_id,
                        "variant_id": variant_id,
                        "group_id": f"group-split-{index}",
                        "split_group": f"split-split-{index}",
                        "temporal_group": f"temporal-split-{index}",
                        "frame_index": index * 20 + candidate_offset,
                        "label": label,
                    }
                )
            sources.append(
                {
                    "variant_id": variant_id,
                    "group_id": f"group-split-{index}",
                    "split_group": f"split-split-{index}",
                    "temporal_group": f"temporal-split-{index}",
                    "sha256": hashlib.sha256(f"split-video-{index}".encode()).hexdigest(),
                }
            )

        split = candidate_classifier._build_split(selected, {"sources": sources}, seed=seed)

        self.assertEqual({"train"}, {split["assignments"][candidate_id] for candidate_id in ordered[0]})
        train_supported = {label for label, count in split["support"]["train"].items() if count}
        for split_name in ("calibration", "test"):
            held_out = {label for label, count in split["support"][split_name].items() if count}
            self.assertIn("match_ball", held_out)
            self.assertTrue(held_out.intersection(candidate_classifier._NOISE_LABELS))
            self.assertTrue(held_out.issubset(train_supported))

    def test_split_construction_and_validation_scale_linearly_after_sorting(self) -> None:
        from football_tracking import candidate_classifier

        component_count = 2_000
        selected = []
        sources = []
        for index in range(component_count):
            variant_id = f"scale-variant-{index}"
            source_fields = {
                "variant_id": variant_id,
                "group_id": f"scale-group-{index}",
                "split_group": f"scale-split-{index}",
                "temporal_group": f"scale-temporal-{index}",
            }
            sources.append(
                {
                    **source_fields,
                    "sha256": hashlib.sha256(f"scale-video-{index}".encode()).hexdigest(),
                }
            )
            for offset, label in enumerate(("match_ball", "equipment_or_background")):
                selected.append(
                    {
                        **source_fields,
                        "candidate_id": f"scale-{index}-{offset}",
                        "frame_index": index * 10 + offset,
                        "label": label,
                    }
                )

        observed_work: list[int] = []
        with patch.object(candidate_classifier, "_observe_split_work", side_effect=observed_work.append):
            split = candidate_classifier._build_split(selected, {"sources": sources}, seed=43)

        self.assertTrue(split["leakage_checks"]["passed"])
        self.assertLessEqual(sum(observed_work), 20 * len(selected))

    def test_split_validation_uses_exact_inclusive_two_frame_windows(self) -> None:
        from football_tracking import candidate_classifier

        assignments = {"left": "train", "touching": "calibration", "separate": "test"}
        video_sha256 = hashlib.sha256(b"window-boundary-video").hexdigest()
        evidence = {}
        for candidate_id, frame_index in (("left", 10), ("touching", 14), ("separate", 19)):
            evidence[candidate_id] = {
                "variant_id": candidate_id,
                "video_sha256": video_sha256,
                "group_id": candidate_id,
                "split_group": candidate_id,
                "temporal_group": candidate_id,
                "frame_index": frame_index,
                "frame_window": [frame_index - 2, frame_index + 2],
                "temporal_block": f"{video_sha256}:{frame_index // 5}",
            }

        temporal_violations = [
            violation
            for violation in candidate_classifier._split_violations(assignments, evidence)
            if violation.startswith("temporal_overlap:")
        ]

        self.assertEqual(["temporal_overlap:left:touching"], temporal_violations)

    def test_real_annotation_resolution_lineage_trains_without_trusting_stale_paths(self) -> None:
        from football_tracking.candidate_annotations import (
            ANNOTATION_RESOLUTION_NAME,
            resolve_candidate_annotations,
            sample_evidence_sha256,
        )

        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = _write_training_inputs(root)
            source_contract = root / "source-contract.json"
            _write_json(source_contract, build_tracking_contract(candidates=inputs["candidates"]))
            manifest = _read_json(inputs["dataset"])
            manifest["contract"] = {"sha256": _sha256(source_contract)}
            _write_json(inputs["dataset"], manifest)
            bound_source_contract = Path(inputs["dataset"]).parent / source_contract.name
            shutil.copyfile(source_contract, bound_source_contract)
            self.assertEqual(_sha256(source_contract), _sha256(bound_source_contract))
            manifest_sha256 = _sha256(inputs["dataset"])
            ledger_path = root / "votes.jsonl"
            records: list[dict[str, object]] = [
                {
                    "schema_version": "1.0",
                    "record_type": "ledger_header",
                    "contract_sha256": _sha256(source_contract),
                    "dataset_version": manifest["dataset_version"],
                    "evidence_manifest_sha256": manifest_sha256,
                }
            ]
            for sample in manifest["samples"]:
                label = "match_ball" if sample["candidate_id"].endswith("-match") else "equipment_or_background"
                evidence_sha256 = sample_evidence_sha256(sample)
                for reviewer_index in range(2):
                    records.append(
                        {
                            "schema_version": "1.0",
                            "record_type": "vote",
                            "vote_id": f"{sample['candidate_id']}-{reviewer_index}",
                            "candidate_id": sample["candidate_id"],
                            "stage": "primary",
                            "reviewer_type": "ai",
                            "annotator_id": f"annotator-{reviewer_index}",
                            "fingerprint": f"model-{reviewer_index}",
                            "label": label,
                            "confidence": 0.95,
                            "blind": True,
                            "created_at": "2026-07-09T12:00:00+00:00",
                            "dataset_version": manifest["dataset_version"],
                            "sample_id": sample["sample_id"],
                            "evidence_sha256": evidence_sha256,
                        }
                    )
            ledger_path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
            resolution_dir = root / "resolved"
            resolve_candidate_annotations(
                source_contract,
                ledger_path,
                resolution_dir,
                dataset_manifest_path=inputs["dataset"],
            )

            report = train_candidate_classifier(
                inputs["dataset"],
                resolution_dir / ANNOTATION_RESOLUTION_NAME,
                resolution_dir / TRACKING_CONTRACT_REPORT_NAME,
                root / "model",
                config=TrainingConfig(epochs=1, seed=101),
            )

        self.assertEqual(len(inputs["candidate_ids"]), report["truth_selection"]["eligible_count"])

    def test_tensor_loading_and_float_conversion_are_bounded_by_batch_size(self) -> None:
        from football_tracking import candidate_classifier

        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = _write_training_inputs(root)
            package = root / "model"
            dataset = _read_json(inputs["dataset"])
            with patch.object(candidate_classifier.np, "load", side_effect=AssertionError("eager tensor load")):
                descriptors = candidate_classifier._load_examples(
                    Path(inputs["dataset"]).parent, dataset["samples"], None
                )
            self.assertTrue(descriptors)
            self.assertIn("tight_descriptor", descriptors[0])
            self.assertNotIn("tight", descriptors[0])
            observed_training_batches: list[int] = []
            with patch.object(
                candidate_classifier,
                "_observe_loaded_batch",
                side_effect=observed_training_batches.append,
                create=True,
            ):
                train_candidate_classifier(
                    inputs["dataset"],
                    inputs["resolution"],
                    inputs["contract"],
                    package,
                    config=TrainingConfig(epochs=1, batch_size=2, seed=111),
                )

            observed_inference_batches: list[int] = []
            source_contract = _write_inference_contract(root, inputs["candidates"])
            with patch.object(
                candidate_classifier,
                "_observe_loaded_batch",
                side_effect=observed_inference_batches.append,
                create=True,
            ):
                classify_candidates(package, inputs["dataset"], source_contract, root / "predictions", batch_size=3)

        self.assertTrue(observed_training_batches)
        self.assertTrue(observed_inference_batches)
        self.assertLessEqual(max(observed_training_batches), 2)
        self.assertLessEqual(max(observed_inference_batches), 3)

    def test_training_rejects_tensor_mutation_after_descriptor_creation(self) -> None:
        from football_tracking import candidate_classifier

        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = _write_training_inputs(root)
            output_dir = root / "mutated-tensor-model"
            real_fit = candidate_classifier._fit_model

            def mutate_then_fit(
                model: candidate_classifier.CandidateClassifier,
                examples: list[dict[str, object]],
                supported_mask: list[bool],
                class_weights: list[float],
                config: TrainingConfig,
            ) -> list[float]:
                descriptor = examples[0]["tight_descriptor"]
                tensor_path = Path(descriptor["path"])  # type: ignore[index]
                tensor_path.write_bytes(tensor_path.read_bytes() + b"mutated")
                return real_fit(model, examples, supported_mask, class_weights, config)

            with patch.object(candidate_classifier, "_fit_model", side_effect=mutate_then_fit):
                with self.assertRaisesRegex(ClassifierError, "sha256 mismatch"):
                    train_candidate_classifier(
                        inputs["dataset"],
                        inputs["resolution"],
                        inputs["contract"],
                        output_dir,
                        config=TrainingConfig(epochs=1, batch_size=2),
                    )

            self.assertFalse(output_dir.exists())
            self.assertEqual([], list(root.glob(".mutated-tensor-model.staging-*")))

    def test_training_rechecks_original_input_hashes_before_publish(self) -> None:
        from football_tracking import candidate_classifier

        for target_name, input_key in (
            ("dataset", "dataset"),
            ("resolution", "resolution"),
            ("contract", "contract"),
        ):
            with self.subTest(target=target_name), tempfile.TemporaryDirectory() as temp_name:
                root = Path(temp_name)
                inputs = _write_training_inputs(root)
                output_dir = root / f"mutated-{target_name}-model"
                target_path = Path(inputs[input_key])
                real_fit = candidate_classifier._fit_model

                def mutate_then_fit(
                    model: candidate_classifier.CandidateClassifier,
                    examples: list[dict[str, object]],
                    supported_mask: list[bool],
                    class_weights: list[float],
                    config: TrainingConfig,
                ) -> list[float]:
                    target_path.write_bytes(target_path.read_bytes() + b" ")
                    return real_fit(model, examples, supported_mask, class_weights, config)

                with patch.object(candidate_classifier, "_fit_model", side_effect=mutate_then_fit):
                    with self.assertRaisesRegex(ClassifierError, "changed during"):
                        train_candidate_classifier(
                            inputs["dataset"],
                            inputs["resolution"],
                            inputs["contract"],
                            output_dir,
                            config=TrainingConfig(epochs=1, batch_size=2),
                        )

                self.assertFalse(output_dir.exists())
                self.assertEqual([], list(root.glob(f".{output_dir.name}.staging-*")))

    def test_inference_rechecks_package_inputs_and_tensor_snapshots_before_publish(self) -> None:
        from football_tracking import candidate_classifier

        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = _write_training_inputs(root)
            package = root / "model"
            train_candidate_classifier(
                inputs["dataset"],
                inputs["resolution"],
                inputs["contract"],
                package,
                config=TrainingConfig(epochs=1, seed=121),
            )
            source_contract = _write_inference_contract(root, inputs["candidates"])
            dataset = _read_json(inputs["dataset"])
            tensor_path = Path(inputs["dataset"]).parent / dataset["samples"][0]["artifacts"]["tight_tensor"]["path"]
            targets = {
                "model_manifest": package / MODEL_MANIFEST_NAME,
                "model_weights": package / MODEL_WEIGHTS_NAME,
                "training_report": package / TRAINING_REPORT_NAME,
                "dataset_manifest": Path(inputs["dataset"]),
                "source_contract": source_contract,
                "sample_tensor": tensor_path,
            }
            real_predict = candidate_classifier._predict_logits

            for target_name, target_path in targets.items():
                with self.subTest(target=target_name):
                    original_bytes = target_path.read_bytes()
                    output_dir = root / f"mutated-{target_name}-predictions"

                    def mutate_then_predict(
                        model: candidate_classifier.CandidateClassifier,
                        examples: list[dict[str, object]],
                        batch_size: int,
                    ) -> tuple[torch.Tensor, torch.Tensor]:
                        target_path.write_bytes(original_bytes + b"mutated")
                        return real_predict(model, examples, batch_size)

                    try:
                        with patch.object(candidate_classifier, "_predict_logits", side_effect=mutate_then_predict):
                            with self.assertRaisesRegex(ClassifierError, "changed during|sha256 mismatch"):
                                classify_candidates(
                                    package,
                                    inputs["dataset"],
                                    source_contract,
                                    output_dir,
                                    batch_size=3,
                                )
                    finally:
                        target_path.write_bytes(original_bytes)

                    self.assertFalse(output_dir.exists())
                    self.assertEqual([], list(root.glob(f".{output_dir.name}.staging-*")))

    def test_atomic_rollback_and_structured_cli_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            inputs = _write_training_inputs(root)
            package = root / "failed-model"

            from football_tracking import candidate_classifier

            real_write = candidate_classifier._write_json_file

            def fail_manifest(path: Path, payload: dict[str, object]) -> None:
                if path.name == MODEL_MANIFEST_NAME:
                    raise OSError("injected manifest failure")
                real_write(path, payload)

            with patch.object(candidate_classifier, "_write_json_file", side_effect=fail_manifest):
                with self.assertRaisesRegex(OSError, "injected"):
                    train_candidate_classifier(
                        inputs["dataset"],
                        inputs["resolution"],
                        inputs["contract"],
                        package,
                        config=TrainingConfig(epochs=1),
                    )
            self.assertFalse(package.exists())
            self.assertEqual([], list(root.glob(".failed-model.staging-*")))

            with patch.object(
                candidate_classifier.torch, "save", side_effect=KeyboardInterrupt("injected model interrupt")
            ):
                with self.assertRaisesRegex(KeyboardInterrupt, "injected model"):
                    train_candidate_classifier(
                        inputs["dataset"],
                        inputs["resolution"],
                        inputs["contract"],
                        package,
                        config=TrainingConfig(epochs=1),
                    )
            self.assertFalse(package.exists())
            self.assertEqual([], list(root.glob(".failed-model.staging-*")))

            train_error = io.StringIO()
            with redirect_stderr(train_error):
                train_code = train_cli_main([])
            classify_error = io.StringIO()
            with redirect_stderr(classify_error):
                classify_code = classify_cli_main([])
            self.assertNotEqual(0, train_code)
            self.assertNotEqual(0, classify_code)
            self.assertFalse(json.loads(train_error.getvalue())["ok"])
            self.assertFalse(json.loads(classify_error.getvalue())["ok"])


def _write_training_inputs(root: Path) -> dict[str, object]:
    dataset_dir = root / "dataset"
    dataset_dir.mkdir()
    candidates: list[dict[str, object]] = []
    classifications: list[dict[str, object]] = []
    resolutions: list[dict[str, object]] = []
    samples: list[dict[str, object]] = []
    sources: list[dict[str, object]] = []
    candidate_ids: list[str] = []
    rng = np.random.default_rng(42)
    labels = (
        ("match", "match_ball", "human_confirmed"),
        ("noise", "equipment_or_background", "ai_confirmed"),
        ("unknown", "unknown", "human_confirmed"),
    )
    for group_index in range(3):
        variant_id = f"variant-{group_index}"
        group_id = f"group-{group_index}"
        video_sha = hashlib.sha256(f"video-{group_index}".encode()).hexdigest()
        group_candidate_ids: list[str] = []
        for label_index, (suffix, label, origin) in enumerate(labels):
            candidate_id = f"g{group_index}-{suffix}"
            frame_index = 20 + label_index * 10
            bbox = [-10.0, 10.0, 20.0, 120.0] if candidate_id == "g0-match" else [10.0, 10.0, 20.0, 20.0]
            candidate_ids.append(candidate_id)
            group_candidate_ids.append(candidate_id)
            candidates.append(_candidate(candidate_id, frame_index, bbox=bbox))
            classifications.append(_classification(candidate_id, label, origin))
            resolutions.append(_resolution(candidate_id, label, origin, training_eligible=True))
            samples.append(
                _write_sample(
                    dataset_dir,
                    rng,
                    candidate_id,
                    frame_index,
                    variant_id,
                    group_id,
                    f"split-{group_index}",
                    bbox,
                )
            )
        sources.append(
            {
                "path": f"video-{group_index}.mp4",
                "sha256": video_sha,
                "variant_id": variant_id,
                "width": 100,
                "height": 100,
                "group_id": group_id,
                "temporal_group": f"{variant_id}-temporal",
                "split_group": f"split-{group_index}",
                "candidate_ids": group_candidate_ids,
            }
        )

    excluded = (
        ("prelabel-only", "pending_adjudication", "unknown", "prelabel", False),
        ("single-vote", "pending_adjudication", "unknown", "prelabel", False),
        ("conflict", "existing_confirmed_conflict", "unknown", None, False),
        ("ai-unknown", "confirmed", "unknown", "ai_confirmed", True),
    )
    extra_ids = []
    for index, (candidate_id, status, label, origin, eligible) in enumerate(excluded):
        frame_index = 100 + index * 10
        candidate_ids.append(candidate_id)
        extra_ids.append(candidate_id)
        candidates.append(_candidate(candidate_id, frame_index))
        resolutions.append(
            {
                **_resolution(candidate_id, label, origin, training_eligible=eligible),
                "status": status,
                "reasons": ["primary_vote_count"] if candidate_id == "single-vote" else [],
            }
        )
        if candidate_id == "conflict":
            classifications.extend(
                [
                    _classification(candidate_id, "match_ball", "ai_confirmed"),
                    _classification(candidate_id, "field_line_or_mark", "human_confirmed"),
                ]
            )
        else:
            classifications.append(_classification(candidate_id, label, origin or "prelabel"))
        samples.append(
            _write_sample(
                dataset_dir,
                rng,
                candidate_id,
                frame_index,
                "variant-extra",
                "group-extra",
                "split-extra",
                [10.0, 10.0, 20.0, 20.0],
            )
        )
    sources.append(
        {
            "path": "video-extra.mp4",
            "sha256": hashlib.sha256(b"video-extra").hexdigest(),
            "variant_id": "variant-extra",
            "width": 100,
            "height": 100,
            "group_id": "group-extra",
            "temporal_group": "variant-extra-temporal",
            "split_group": "split-extra",
            "candidate_ids": extra_ids,
        }
    )

    contract_path = root / "resolved-contract.json"
    _write_json(contract_path, build_tracking_contract(candidates=candidates, classifications=classifications))
    manifest_path = dataset_dir / "candidate_dataset_manifest.json"
    _write_json(
        manifest_path,
        {
            "schema_version": "1.0",
            "artifact_type": "candidate_dataset",
            "builder_version": "candidate-dataset-v1",
            "dataset_version": hashlib.sha256(b"dataset-v1").hexdigest(),
            "contract": {"sha256": _sha256(contract_path)},
            "frame_offsets": [-2, -1, 0, 1, 2],
            "preprocessing_runtime": {
                "pipeline": "candidate-dataset-v1",
                "frame_offsets": [-2, -1, 0, 1, 2],
                "tight_crop_scale": 1.25,
                "context_crop_scale": 4.0,
                "tight_size": 64,
                "context_size": 128,
                "color_conversion": "opencv:BGR2RGB",
                "resize_down": "INTER_AREA",
                "resize_up": "INTER_LINEAR",
                "python": "test",
                "opencv": "test",
                "numpy": np.__version__,
            },
            "tensor_contract": {
                "color_space": "RGB",
                "dtype": "uint8",
                "tight_shape": [5, 3, 64, 64],
                "context_shape": [5, 3, 128, 128],
                "markup": False,
            },
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
            "source_contract": {"path": str(contract_path), "sha256": _sha256(contract_path)},
            "derived_tracking_contract": {"path": str(contract_path), "sha256": _sha256(contract_path)},
            "source_dataset_manifest": {
                "path": str(manifest_path),
                "sha256": _sha256(manifest_path),
                "dataset_version": hashlib.sha256(b"dataset-v1").hexdigest(),
            },
            "resolutions": resolutions,
        },
    )
    return {
        "dataset": manifest_path,
        "resolution": resolution_path,
        "contract": contract_path,
        "candidate_ids": candidate_ids,
        "candidates": candidates,
    }


def _write_sample(
    dataset_dir: Path,
    rng: np.random.Generator,
    candidate_id: str,
    frame_index: int,
    variant_id: str,
    group_id: str,
    split_group: str,
    bbox_requested: list[float],
) -> dict[str, object]:
    sample_id = candidate_id
    sample_dir = dataset_dir / "samples" / sample_id
    sample_dir.mkdir(parents=True)
    tight = rng.integers(0, 256, size=(5, 3, 64, 64), dtype=np.uint8)
    context = rng.integers(0, 256, size=(5, 3, 128, 128), dtype=np.uint8)
    tight_path = sample_dir / "tight.npy"
    context_path = sample_dir / "context.npy"
    montage_path = sample_dir / "review_montage.png"
    np.save(tight_path, tight, allow_pickle=False)
    np.save(context_path, context, allow_pickle=False)
    montage_path.write_bytes(f"review:{candidate_id}".encode())
    bbox_clamped = [
        min(100.0, max(0.0, bbox_requested[0])),
        min(100.0, max(0.0, bbox_requested[1])),
        min(100.0, max(0.0, bbox_requested[2])),
        min(100.0, max(0.0, bbox_requested[3])),
    ]
    return {
        "sample_id": sample_id,
        "candidate_id": candidate_id,
        "detector_source": "detector",
        "frame_index": frame_index,
        "bbox_normalized": [value / 100.0 for value in bbox_clamped],
        "bbox_requested_pixels": bbox_requested,
        "bbox_clamped_pixels": bbox_clamped,
        "confidence": 0.8,
        "variant_id": variant_id,
        "group_id": group_id,
        "temporal_group": f"{variant_id}-temporal",
        "split_group": split_group,
        "frames": [
            {"offset": offset, "requested_index": frame_index + offset, "actual_index": frame_index + offset}
            for offset in (-2, -1, 0, 1, 2)
        ],
        "artifacts": {
            "tight_tensor": {
                "path": tight_path.relative_to(dataset_dir).as_posix(),
                "sha256": _sha256(tight_path),
                "shape": [5, 3, 64, 64],
                "dtype": "uint8",
                "color_space": "RGB",
            },
            "context_tensor": {
                "path": context_path.relative_to(dataset_dir).as_posix(),
                "sha256": _sha256(context_path),
                "shape": [5, 3, 128, 128],
                "dtype": "uint8",
                "color_space": "RGB",
            },
            "review_montage": {
                "path": montage_path.relative_to(dataset_dir).as_posix(),
                "sha256": _sha256(montage_path),
                "color_space": "RGB",
            },
        },
    }


def _write_inference_contract(root: Path, candidates: list[dict[str, object]]) -> Path:
    contract = build_tracking_contract(
        source={
            "video_sha256": "b" * 64,
            "fps": 20.0,
            "width": 1280,
            "height": 720,
            "frame_count": 200,
        },
        candidates=candidates,
        classifications=[
            _classification("g0-unknown", "unknown", "human_confirmed"),
            _classification("prelabel-only", "unknown", "prelabel"),
            _classification("conflict", "match_ball", "ai_confirmed"),
            _classification("conflict", "field_line_or_mark", "human_confirmed"),
        ],
        decisions=[
            {
                "candidate_id": "g0-unknown",
                "decision": "abstain",
                "confidence": 0.8,
                "reason": "human review",
            }
        ],
    )
    path = root / "inference-contract.json"
    _write_json(path, contract)
    return path


def _candidate(candidate_id: str, frame_index: int, *, bbox: list[float] | None = None) -> dict[str, object]:
    return {
        "candidate_id": candidate_id,
        "frame_index": frame_index,
        "bbox": bbox or [10.0, 10.0, 20.0, 20.0],
        "confidence": 0.8,
        "source": "detector",
    }


def _classification(candidate_id: str, label: str, origin: str) -> dict[str, object]:
    return {"candidate_id": candidate_id, "label": label, "label_origin": origin, "confidence": 1.0}


def _resolution(candidate_id: str, label: str, origin: str | None, *, training_eligible: bool) -> dict[str, object]:
    return {
        "candidate_id": candidate_id,
        "status": "confirmed" if training_eligible else "pending_adjudication",
        "label": label,
        "label_origin": origin,
        "confidence": 1.0 if training_eligible else 0.0,
        "resolution_source": "existing_contract" if training_eligible else "unresolved_votes",
        "training_eligible": training_eligible,
        "reasons": [],
        "primary_vote_ids": [],
        "adjudication_vote_ids": [],
    }


def _assert_split_disjoint(test: unittest.TestCase, split: dict[str, object]) -> None:
    evidence = split["evidence_by_split"]
    names = ("train", "calibration", "test")
    for left_index, left in enumerate(names):
        for right in names[left_index + 1 :]:
            for key in ("variant_ids", "video_sha256", "group_ids", "split_groups", "temporal_groups"):
                test.assertTrue(set(evidence[left][key]).isdisjoint(evidence[right][key]))


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    unittest.main()
