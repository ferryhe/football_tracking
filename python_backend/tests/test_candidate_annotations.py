from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from football_tracking.candidate_annotations import (
    ADJUDICATION_QUEUE_NAME,
    ANNOTATION_RESOLUTION_NAME,
    resolve_candidate_annotations,
    sample_evidence_sha256,
)
from football_tracking.tracking_contracts import (
    TRACKING_CONTRACT_REPORT_NAME,
    build_tracking_contract,
)
from scripts.resolve_candidate_annotations import main as annotation_cli_main


def _candidate(candidate_id: str, frame_index: int) -> dict[str, object]:
    return {
        "candidate_id": candidate_id,
        "frame_index": frame_index,
        "bbox": [10.0, 12.0, 18.0, 20.0],
        "confidence": 0.7,
        "source": "detector",
    }


def _vote(
    vote_id: str,
    candidate_id: str,
    label: str,
    *,
    reviewer_type: str = "ai",
    stage: str = "primary",
    annotator_id: str | None = None,
    fingerprint: str | None = None,
    confidence: float = 0.95,
    blind: bool = True,
) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "record_type": "vote",
        "vote_id": vote_id,
        "candidate_id": candidate_id,
        "stage": stage,
        "reviewer_type": reviewer_type,
        "annotator_id": annotator_id or f"annotator-{vote_id}",
        "fingerprint": fingerprint or f"fingerprint-{vote_id}",
        "label": label,
        "confidence": confidence,
        "blind": blind,
        "created_at": f"2026-01-01T00:00:{len(vote_id):02d}Z",
        "audit_note": f"preserve-{vote_id}",
    }


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_jsonl(
    path: Path,
    records: list[dict[str, object]],
    *,
    contract_path: Path,
    dataset_version: str | None = None,
    evidence_manifest_sha256: str | None = None,
    dataset_manifest_path: Path | None = None,
) -> None:
    if dataset_manifest_path is not None:
        manifest = json.loads(dataset_manifest_path.read_text(encoding="utf-8"))
        dataset_version = manifest["dataset_version"]
        evidence_manifest_sha256 = _sha256(dataset_manifest_path)
        samples = {sample["candidate_id"]: sample for sample in manifest["samples"]}
        for record in records:
            if record.get("record_type") != "vote":
                continue
            sample = samples.get(str(record["candidate_id"]))
            if sample is None:
                continue
            record.update(
                {
                    "dataset_version": dataset_version,
                    "sample_id": sample["sample_id"],
                    "evidence_sha256": sample_evidence_sha256(sample),
                }
            )
    header: dict[str, object] = {
        "schema_version": "1.0",
        "record_type": "ledger_header",
        "contract_sha256": _sha256(contract_path),
    }
    if dataset_version is not None:
        header["dataset_version"] = dataset_version
    if evidence_manifest_sha256 is not None:
        header["evidence_manifest_sha256"] = evidence_manifest_sha256
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n" for record in [header, *records]),
        encoding="utf-8",
    )


def _write_dataset_manifest(
    root: Path,
    contract_path: Path,
    candidates: list[dict[str, object]],
) -> Path:
    samples = []
    evidence_root = root / "evidence"
    for index, candidate in enumerate(candidates):
        candidate_id = str(candidate["candidate_id"])
        sample_id = f"{index:06d}-{candidate_id}"
        artifacts: dict[str, dict[str, object]] = {}
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
                "sha256": _sha256(artifact_path),
            }
        samples.append({"sample_id": sample_id, "candidate_id": candidate_id, "artifacts": artifacts})
    dataset_version = hashlib.sha256(json.dumps(samples, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    manifest = {
        "schema_version": "1.0",
        "artifact_type": "candidate_dataset",
        "dataset_version": dataset_version,
        "contract": {"schema_version": "2.0", "path": contract_path.name, "sha256": _sha256(contract_path)},
        "frame_offsets": [-2, -1, 0, 1, 2],
        "tensor_contract": {
            "color_space": "RGB",
            "dtype": "uint8",
            "tight_shape": [5, 3, 64, 64],
            "context_shape": [5, 3, 128, 128],
            "markup": False,
        },
        "summary": {"status": "ok", "sample_count": len(samples), "source_count": 1},
        "samples": samples,
    }
    manifest_path = root / "candidate_dataset_manifest.json"
    _write_json(manifest_path, manifest)
    return manifest_path


class CandidateAnnotationTests(unittest.TestCase):
    def _paths(
        self,
        root: Path,
        *,
        candidates: list[dict[str, object]],
        classifications: list[dict[str, object]] | None = None,
        votes: list[dict[str, object]] | None = None,
    ) -> tuple[Path, Path, Path]:
        contract_path = root / "source-contract.json"
        ledger_path = root / "votes.jsonl"
        output_dir = root / "resolved"
        _write_json(
            contract_path,
            build_tracking_contract(candidates=candidates, classifications=classifications or []),
        )
        manifest_path = _write_dataset_manifest(root, contract_path, candidates)
        _write_jsonl(
            ledger_path,
            votes or [],
            contract_path=contract_path,
            dataset_manifest_path=manifest_path,
        )
        return contract_path, ledger_path, output_dir

    def test_two_independent_blind_votes_confirm_ai_and_human_labels(self) -> None:
        votes = [
            _vote("a1", "c-ai", "match_ball"),
            _vote("a2", "c-ai", "match_ball"),
            _vote("h1", "c-human", "field_line_or_mark", reviewer_type="human"),
            _vote("h2", "c-human", "field_line_or_mark", reviewer_type="human"),
        ]
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            contract_path, ledger_path, output_dir = self._paths(
                root,
                candidates=[_candidate("c-ai", 1), _candidate("c-human", 2)],
                votes=votes,
            )

            report = resolve_candidate_annotations(contract_path, ledger_path, output_dir, min_confidence=0.8)
            derived_path = output_dir / TRACKING_CONTRACT_REPORT_NAME
            derived_bytes = derived_path.read_bytes()
            derived = json.loads(derived_bytes)
            persisted = json.loads((output_dir / ANNOTATION_RESOLUTION_NAME).read_text(encoding="utf-8"))

        classifications = {row["candidate_id"]: row for row in derived["classifications"]}
        self.assertEqual("ai_confirmed", classifications["c-ai"]["label_origin"])
        self.assertEqual("human_confirmed", classifications["c-human"]["label_origin"])
        self.assertTrue(all(row["confirmed"] for row in classifications.values()))
        self.assertEqual([], report["adjudication_queue"])
        self.assertEqual(votes, persisted["vote_history"])
        self.assertTrue(all(item["training_eligible"] for item in report["resolutions"]))
        self.assertEqual(
            {
                "path": TRACKING_CONTRACT_REPORT_NAME,
                "sha256": hashlib.sha256(derived_bytes).hexdigest(),
            },
            persisted["derived_tracking_contract"],
        )

    def test_unknown_disagreement_duplicate_low_confidence_and_single_votes_queue(self) -> None:
        candidates = [
            _candidate("unknown", 0),
            _candidate("disagree", 1),
            _candidate("duplicate", 2),
            _candidate("low", 3),
            _candidate("single", 4),
        ]
        votes = [
            _vote("u1", "unknown", "unknown"),
            _vote("u2", "unknown", "unknown"),
            _vote("d1", "disagree", "match_ball"),
            _vote("d2", "disagree", "equipment_or_background"),
            _vote("x1", "duplicate", "match_ball", annotator_id="same"),
            _vote("x2", "duplicate", "match_ball", annotator_id="same"),
            _vote("l1", "low", "match_ball", confidence=0.79),
            _vote("l2", "low", "match_ball"),
            _vote("s1", "single", "match_ball"),
        ]
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            contract_path, ledger_path, output_dir = self._paths(root, candidates=candidates, votes=votes)

            report = resolve_candidate_annotations(contract_path, ledger_path, output_dir, min_confidence=0.8)
            derived = json.loads((output_dir / TRACKING_CONTRACT_REPORT_NAME).read_text(encoding="utf-8"))
            queue = json.loads((output_dir / ADJUDICATION_QUEUE_NAME).read_text(encoding="utf-8"))

        reasons = {item["candidate_id"]: item["reasons"] for item in report["adjudication_queue"]}
        self.assertIn("unknown_primary_label", reasons["unknown"])
        self.assertIn("primary_label_disagreement", reasons["disagree"])
        self.assertIn("duplicate_annotator_id", reasons["duplicate"])
        self.assertIn("below_confidence_threshold", reasons["low"])
        self.assertIn("primary_vote_count", reasons["single"])
        self.assertEqual(5, queue["candidate_count"])
        self.assertTrue(all(not item["training_eligible"] for item in report["resolutions"]))
        self.assertEqual(
            {candidate["candidate_id"] for candidate in candidates},
            {row["candidate_id"] for row in derived["classifications"] if row["label"] == "unknown"},
        )
        self.assertTrue(all(row["label_origin"] == "prelabel" for row in derived["classifications"]))

    def test_independent_human_adjudicator_can_confirm_unknown(self) -> None:
        votes = [
            _vote("p1", "c1", "match_ball"),
            _vote("p2", "c1", "equipment_or_background"),
            _vote(
                "judge",
                "c1",
                "unknown",
                reviewer_type="human",
                stage="adjudication",
                blind=False,
            ),
        ]
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            contract_path, ledger_path, output_dir = self._paths(
                root,
                candidates=[_candidate("c1", 0)],
                votes=votes,
            )

            report = resolve_candidate_annotations(contract_path, ledger_path, output_dir)
            derived = json.loads((output_dir / TRACKING_CONTRACT_REPORT_NAME).read_text(encoding="utf-8"))

        resolution = report["resolutions"][0]
        self.assertEqual("human_adjudication", resolution["resolution_source"])
        self.assertEqual("unknown", resolution["label"])
        self.assertEqual("human_confirmed", resolution["label_origin"])
        self.assertTrue(resolution["training_eligible"])
        self.assertEqual([], report["adjudication_queue"])
        self.assertEqual("human_confirmed", derived["classifications"][0]["label_origin"])
        self.assertEqual("unknown", derived["classifications"][0]["label"])

    def test_invalid_or_reused_adjudicator_identity_does_not_override_queue(self) -> None:
        votes = [
            _vote("p1", "c1", "match_ball", annotator_id="same", fingerprint="fp-p1"),
            _vote("p2", "c1", "equipment_or_background"),
            _vote(
                "judge",
                "c1",
                "match_ball",
                reviewer_type="human",
                stage="adjudication",
                annotator_id="same",
                fingerprint="fp-judge",
                blind=False,
            ),
        ]
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            contract_path, ledger_path, output_dir = self._paths(
                root,
                candidates=[_candidate("c1", 0)],
                votes=votes,
            )

            report = resolve_candidate_annotations(contract_path, ledger_path, output_dir)

        resolution = report["resolutions"][0]
        self.assertFalse(resolution["training_eligible"])
        self.assertIn("duplicate_annotator_id", resolution["reasons"])
        self.assertEqual(1, len(report["adjudication_queue"]))

    def test_independent_human_adjudication_survives_duplicate_ai_primary_identity(self) -> None:
        votes = [
            _vote("p1", "c1", "match_ball", annotator_id="same-ai", fingerprint="same-build"),
            _vote("p2", "c1", "equipment_or_background", annotator_id="same-ai", fingerprint="same-build"),
            _vote(
                "judge",
                "c1",
                "match_ball",
                reviewer_type="human",
                stage="adjudication",
                annotator_id="independent-human",
                fingerprint="human-session",
                blind=False,
            ),
        ]
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            contract_path, ledger_path, output_dir = self._paths(
                root,
                candidates=[_candidate("c1", 0)],
                votes=votes,
            )

            report = resolve_candidate_annotations(contract_path, ledger_path, output_dir)

        resolution = report["resolutions"][0]
        self.assertEqual("confirmed", resolution["status"])
        self.assertEqual("human_adjudication", resolution["resolution_source"])
        self.assertTrue(resolution["training_eligible"])

    def test_duplicate_fingerprint_and_non_blind_primary_votes_queue(self) -> None:
        votes = [
            _vote("p1", "c1", "match_ball", fingerprint="same-model-build", blind=False),
            _vote("p2", "c1", "match_ball", fingerprint="same-model-build"),
        ]
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            contract_path, ledger_path, output_dir = self._paths(
                root,
                candidates=[_candidate("c1", 0)],
                votes=votes,
            )

            report = resolve_candidate_annotations(contract_path, ledger_path, output_dir)

        reasons = report["resolutions"][0]["reasons"]
        self.assertIn("duplicate_fingerprint", reasons)
        self.assertIn("primary_vote_not_blind", reasons)
        self.assertFalse(report["resolutions"][0]["training_eligible"])

    def test_existing_confirmed_rows_are_preserved_and_conflicts_are_not_selected(self) -> None:
        existing = [
            {
                "candidate_id": "stable",
                "label": "match_ball",
                "label_origin": "human_confirmed",
                "confidence": 0.99,
            },
            {
                "candidate_id": "conflict",
                "label": "match_ball",
                "label_origin": "ai_confirmed",
                "confidence": 0.91,
            },
            {
                "candidate_id": "conflict",
                "label": "field_line_or_mark",
                "label_origin": "human_confirmed",
                "confidence": 1.0,
            },
            {
                "candidate_id": "same-label",
                "label": "match_ball",
                "label_origin": "ai_confirmed",
                "confidence": 0.91,
            },
            {
                "candidate_id": "same-label",
                "label": "match_ball",
                "label_origin": "human_confirmed",
                "confidence": 0.99,
            },
        ]
        votes = [
            _vote("s1", "stable", "equipment_or_background"),
            _vote("s2", "stable", "equipment_or_background"),
            _vote("c1", "conflict", "equipment_or_background"),
            _vote("c2", "conflict", "equipment_or_background"),
        ]
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            contract_path, ledger_path, output_dir = self._paths(
                root,
                candidates=[_candidate("stable", 0), _candidate("conflict", 1), _candidate("same-label", 2)],
                classifications=existing,
                votes=votes,
            )
            source = json.loads(contract_path.read_text(encoding="utf-8"))

            report = resolve_candidate_annotations(contract_path, ledger_path, output_dir)
            derived = json.loads((output_dir / TRACKING_CONTRACT_REPORT_NAME).read_text(encoding="utf-8"))

        self.assertEqual(source["classifications"], derived["classifications"])
        resolutions = {item["candidate_id"]: item for item in report["resolutions"]}
        self.assertEqual("existing_contract", resolutions["stable"]["resolution_source"])
        self.assertTrue(resolutions["stable"]["training_eligible"])
        self.assertEqual("existing_confirmed_conflict", resolutions["conflict"]["status"])
        self.assertFalse(resolutions["conflict"]["training_eligible"])
        self.assertIn("conflict", {item["candidate_id"] for item in report["adjudication_queue"]})
        self.assertEqual("human_confirmed", resolutions["same-label"]["label_origin"])
        self.assertTrue(resolutions["same-label"]["training_eligible"])
        self.assertNotIn("same-label", {item["candidate_id"] for item in report["adjudication_queue"]})
        self.assertFalse(any(row["label"] == "unknown" for row in derived["classifications"]))

    def test_existing_prelabel_is_retained_when_consensus_adds_confirmed_row(self) -> None:
        prelabel = {
            "candidate_id": "c1",
            "label": "unknown",
            "label_origin": "prelabel",
            "confidence": 0.2,
        }
        votes = [_vote("p1", "c1", "match_ball"), _vote("p2", "c1", "match_ball")]
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            contract_path, ledger_path, output_dir = self._paths(
                root,
                candidates=[_candidate("c1", 0)],
                classifications=[prelabel],
                votes=votes,
            )

            resolve_candidate_annotations(contract_path, ledger_path, output_dir)
            derived = json.loads((output_dir / TRACKING_CONTRACT_REPORT_NAME).read_text(encoding="utf-8"))

        self.assertEqual(["prelabel", "ai_confirmed"], [row["label_origin"] for row in derived["classifications"]])
        self.assertEqual(["unknown", "match_ball"], [row["label"] for row in derived["classifications"]])

    def test_rejects_wrong_label_nonfinite_json_duplicate_vote_id_and_absent_candidate(self) -> None:
        invalid_ledgers = [
            [_vote("bad", "c1", "not-a-v2-label")],
            [_vote("same-id", "c1", "match_ball"), _vote("same-id", "c1", "match_ball")],
            [_vote("absent", "missing", "match_ball")],
        ]
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            for index, votes in enumerate(invalid_ledgers):
                case = root / str(index)
                case.mkdir()
                contract_path, ledger_path, output_dir = self._paths(
                    case,
                    candidates=[_candidate("c1", 0)],
                    votes=votes,
                )
                with self.assertRaises(ValueError):
                    resolve_candidate_annotations(contract_path, ledger_path, output_dir)

            case = root / "nan"
            case.mkdir()
            contract_path, ledger_path, output_dir = self._paths(
                case,
                candidates=[_candidate("c1", 0)],
            )
            ledger_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "record_type": "ledger_header",
                        "contract_sha256": _sha256(contract_path),
                    }
                )
                + "\n"
                + json.dumps(_vote("nan", "c1", "match_ball")).replace("0.95", "NaN")
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                resolve_candidate_annotations(contract_path, ledger_path, output_dir)

    def test_rejects_invalid_stage_reviewer_type_timestamp_and_source_overwrite(self) -> None:
        mutations = [
            {"stage": "review"},
            {"reviewer_type": "model"},
            {"created_at": "2026-01-01T00:00:00"},
            {"created_at": "not-a-timestamp"},
        ]
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            for index, mutation in enumerate(mutations):
                case = root / str(index)
                case.mkdir()
                vote = {**_vote(f"v{index}", "c1", "match_ball"), **mutation}
                contract_path, ledger_path, output_dir = self._paths(
                    case,
                    candidates=[_candidate("c1", 0)],
                    votes=[vote],
                )
                with self.assertRaises(ValueError):
                    resolve_candidate_annotations(contract_path, ledger_path, output_dir)

            output_dir = root / "same-output"
            output_dir.mkdir()
            contract_path = output_dir / TRACKING_CONTRACT_REPORT_NAME
            ledger_path = root / "valid-votes.jsonl"
            _write_json(contract_path, build_tracking_contract(candidates=[_candidate("c1", 0)]))
            _write_jsonl(
                ledger_path,
                [_vote("v1", "c1", "match_ball"), _vote("v2", "c1", "match_ball")],
                contract_path=contract_path,
            )
            original = contract_path.read_bytes()
            with self.assertRaisesRegex(ValueError, "overwrite"):
                resolve_candidate_annotations(contract_path, ledger_path, output_dir)
            after = contract_path.read_bytes()

        self.assertEqual(original, after)

    def test_all_final_artifacts_reject_aliasing_either_input(self) -> None:
        final_names = [ANNOTATION_RESOLUTION_NAME, ADJUDICATION_QUEUE_NAME, TRACKING_CONTRACT_REPORT_NAME]
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            for index, final_name in enumerate(final_names):
                case = root / f"contract-{index}"
                output_dir = case / "output"
                output_dir.mkdir(parents=True)
                contract_path = output_dir / final_name
                ledger_path = case / "votes.jsonl"
                _write_json(contract_path, build_tracking_contract(candidates=[_candidate("c1", 0)]))
                _write_jsonl(ledger_path, [], contract_path=contract_path)
                with self.assertRaisesRegex(ValueError, "overwrite"):
                    resolve_candidate_annotations(contract_path, ledger_path, output_dir)

            for index, final_name in enumerate(final_names):
                case = root / f"ledger-{index}"
                output_dir = case / "output"
                output_dir.mkdir(parents=True)
                contract_path = case / "source.json"
                ledger_path = output_dir / final_name
                _write_json(contract_path, build_tracking_contract(candidates=[_candidate("c1", 0)]))
                _write_jsonl(ledger_path, [], contract_path=contract_path)
                with self.assertRaisesRegex(ValueError, "overwrite"):
                    resolve_candidate_annotations(contract_path, ledger_path, output_dir)

    def test_ledger_contract_hash_prevents_same_candidate_id_replay(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            first_contract = root / "first.json"
            second_contract = root / "second.json"
            ledger_path = root / "votes.jsonl"
            _write_json(first_contract, build_tracking_contract(candidates=[_candidate("same-id", 1)]))
            _write_json(second_contract, build_tracking_contract(candidates=[_candidate("same-id", 2)]))
            _write_jsonl(
                ledger_path,
                [_vote("v1", "same-id", "match_ball"), _vote("v2", "same-id", "match_ball")],
                contract_path=first_contract,
                dataset_version="dataset-v1",
                evidence_manifest_sha256="a" * 64,
            )

            with self.assertRaisesRegex(ValueError, "contract_sha256"):
                resolve_candidate_annotations(second_contract, ledger_path, root / "output")

    def test_dataset_bound_ledger_requires_sample_evidence_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            contract_path = root / "contract.json"
            ledger_path = root / "votes.jsonl"
            candidates = [_candidate("c1", 1)]
            _write_json(contract_path, build_tracking_contract(candidates=candidates))
            manifest_path = _write_dataset_manifest(root, contract_path, candidates)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            votes = [_vote("v1", "c1", "match_ball"), _vote("v2", "c1", "match_ball")]
            _write_jsonl(
                ledger_path,
                votes,
                contract_path=contract_path,
                dataset_manifest_path=manifest_path,
            )

            report = resolve_candidate_annotations(contract_path, ledger_path, root / "output")
            self.assertEqual(manifest["dataset_version"], report["source_vote_ledger"]["dataset_version"])
            self.assertEqual("000000-c1", report["vote_history"][0]["sample_id"])

            del votes[1]["evidence_sha256"]
            _write_jsonl(
                ledger_path,
                votes,
                contract_path=contract_path,
                dataset_version=manifest["dataset_version"],
                evidence_manifest_sha256=_sha256(manifest_path),
            )
            with self.assertRaisesRegex(ValueError, "evidence_sha256"):
                resolve_candidate_annotations(contract_path, ledger_path, root / "invalid-output")

    def test_ai_votes_require_verified_manifest_sample_and_artifact_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            contract_path = root / "contract.json"
            ledger_path = root / "votes.jsonl"
            candidates = [_candidate("c1", 1)]
            _write_json(contract_path, build_tracking_contract(candidates=candidates))
            votes = [_vote("v1", "c1", "match_ball"), _vote("v2", "c1", "match_ball")]
            _write_jsonl(ledger_path, votes, contract_path=contract_path)

            with self.assertRaisesRegex(ValueError, "dataset manifest"):
                resolve_candidate_annotations(contract_path, ledger_path, root / "missing-manifest")

            manifest_path = _write_dataset_manifest(root, contract_path, candidates)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            _write_jsonl(ledger_path, votes, contract_path=contract_path, dataset_manifest_path=manifest_path)
            _write_jsonl(
                ledger_path,
                votes,
                contract_path=contract_path,
                dataset_version=manifest["dataset_version"],
                evidence_manifest_sha256="0" * 64,
            )
            with self.assertRaisesRegex(ValueError, "evidence_manifest_sha256"):
                resolve_candidate_annotations(contract_path, ledger_path, root / "wrong-manifest")

            wrong_candidate_manifest = json.loads(json.dumps(manifest))
            wrong_candidate_manifest["samples"][0]["candidate_id"] = "other-candidate"
            _write_json(manifest_path, wrong_candidate_manifest)
            _write_jsonl(
                ledger_path,
                votes,
                contract_path=contract_path,
                dataset_version=manifest["dataset_version"],
                evidence_manifest_sha256=_sha256(manifest_path),
            )
            with self.assertRaisesRegex(ValueError, "candidate IDs"):
                resolve_candidate_annotations(contract_path, ledger_path, root / "wrong-candidate")
            _write_json(manifest_path, manifest)

            votes[0]["sample_id"] = "wrong-sample"
            _write_jsonl(
                ledger_path,
                votes,
                contract_path=contract_path,
                dataset_version=manifest["dataset_version"],
                evidence_manifest_sha256=_sha256(manifest_path),
            )
            with self.assertRaisesRegex(ValueError, "sample_id"):
                resolve_candidate_annotations(contract_path, ledger_path, root / "wrong-sample")

            votes[0]["sample_id"] = manifest["samples"][0]["sample_id"]
            votes[0]["evidence_sha256"] = "f" * 64
            _write_jsonl(
                ledger_path,
                votes,
                contract_path=contract_path,
                dataset_version=manifest["dataset_version"],
                evidence_manifest_sha256=_sha256(manifest_path),
            )
            with self.assertRaisesRegex(ValueError, "evidence_sha256"):
                resolve_candidate_annotations(contract_path, ledger_path, root / "wrong-evidence")

            _write_jsonl(ledger_path, votes, contract_path=contract_path, dataset_manifest_path=manifest_path)
            tight_path = root / manifest["samples"][0]["artifacts"]["tight_tensor"]["path"]
            tight_path.write_bytes(b"tampered")
            with self.assertRaisesRegex(ValueError, "artifact sha256 mismatch"):
                resolve_candidate_annotations(contract_path, ledger_path, root / "tampered-artifact")

    def test_human_visual_votes_require_evidence_but_empty_ledger_may_be_contract_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            contract_path = root / "contract.json"
            ledger_path = root / "votes.jsonl"
            candidates = [_candidate("c1", 1)]
            _write_json(contract_path, build_tracking_contract(candidates=candidates))
            human_votes = [
                _vote("h1", "c1", "match_ball", reviewer_type="human"),
                _vote("h2", "c1", "match_ball", reviewer_type="human"),
            ]
            _write_jsonl(ledger_path, human_votes, contract_path=contract_path)

            with self.assertRaisesRegex(ValueError, "dataset manifest"):
                resolve_candidate_annotations(contract_path, ledger_path, root / "human-without-evidence")

            _write_jsonl(ledger_path, [], contract_path=contract_path)
            report = resolve_candidate_annotations(contract_path, ledger_path, root / "empty-ledger")

        self.assertIsNone(report["source_dataset_manifest"])
        self.assertEqual("pending_adjudication", report["resolutions"][0]["status"])
        self.assertFalse(report["resolutions"][0]["training_eligible"])

    def test_publish_failure_rolls_back_existing_artifacts_without_stale_files(self) -> None:
        votes = [_vote("p1", "c1", "match_ball"), _vote("p2", "c1", "match_ball")]
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            contract_path, ledger_path, output_dir = self._paths(
                root,
                candidates=[_candidate("c1", 0)],
                votes=votes,
            )
            output_dir.mkdir()
            old_contents = {
                ANNOTATION_RESOLUTION_NAME: "old-resolution\n",
                ADJUDICATION_QUEUE_NAME: "old-queue\n",
                TRACKING_CONTRACT_REPORT_NAME: "old-contract\n",
            }
            for name, content in old_contents.items():
                (output_dir / name).write_text(content, encoding="utf-8")

            from football_tracking import candidate_annotations

            real_replace = candidate_annotations.os.replace
            failed = False

            def fail_second_publish(source: str | Path, destination: str | Path) -> None:
                nonlocal failed
                if not failed and Path(source).suffix == ".tmp" and Path(destination).name == ADJUDICATION_QUEUE_NAME:
                    failed = True
                    raise OSError("injected publish failure")
                real_replace(source, destination)

            with patch.object(candidate_annotations.os, "replace", side_effect=fail_second_publish):
                with self.assertRaisesRegex(OSError, "injected"):
                    resolve_candidate_annotations(contract_path, ledger_path, output_dir)

            restored = {name: (output_dir / name).read_text(encoding="utf-8") for name in old_contents}
            leftovers = [path.name for path in output_dir.iterdir() if path.name.startswith(".")]

        self.assertEqual(old_contents, restored)
        self.assertEqual([], leftovers)

    def test_keyboard_interrupt_during_publish_restores_all_existing_artifacts(self) -> None:
        votes = [_vote("p1", "c1", "match_ball"), _vote("p2", "c1", "match_ball")]
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            contract_path, ledger_path, output_dir = self._paths(
                root,
                candidates=[_candidate("c1", 0)],
                votes=votes,
            )
            output_dir.mkdir()
            old_contents = {
                ANNOTATION_RESOLUTION_NAME: b"old-resolution\n",
                ADJUDICATION_QUEUE_NAME: b"old-queue\n",
                TRACKING_CONTRACT_REPORT_NAME: b"old-contract\n",
            }
            for name, content in old_contents.items():
                (output_dir / name).write_bytes(content)

            from football_tracking import candidate_annotations

            real_replace = candidate_annotations.os.replace
            interrupted = False

            def interrupt_second_publish(source: str | Path, destination: str | Path) -> None:
                nonlocal interrupted
                if (
                    not interrupted
                    and Path(source).suffix == ".tmp"
                    and Path(destination).name == ADJUDICATION_QUEUE_NAME
                ):
                    interrupted = True
                    raise KeyboardInterrupt
                real_replace(source, destination)

            with patch.object(candidate_annotations.os, "replace", side_effect=interrupt_second_publish):
                with self.assertRaises(KeyboardInterrupt):
                    resolve_candidate_annotations(contract_path, ledger_path, output_dir)

            restored = {name: (output_dir / name).read_bytes() for name in old_contents}
            leftovers = [path.name for path in output_dir.iterdir() if path.name.startswith(".")]

        self.assertEqual(old_contents, restored)
        self.assertEqual([], leftovers)

    def test_input_mutation_after_parse_fails_before_publish_without_partial_artifacts(self) -> None:
        for input_name in ("contract", "ledger", "manifest"):
            with self.subTest(input_name=input_name), tempfile.TemporaryDirectory() as temp_name:
                root = Path(temp_name)
                contract_path, ledger_path, output_dir = self._paths(
                    root,
                    candidates=[_candidate("c1", 0)],
                    votes=[_vote("p1", "c1", "match_ball"), _vote("p2", "c1", "match_ball")],
                )
                manifest_path = root / "candidate_dataset_manifest.json"
                mutation_path = {
                    "contract": contract_path,
                    "ledger": ledger_path,
                    "manifest": manifest_path,
                }[input_name]
                output_dir.mkdir()
                old_contents = {
                    ANNOTATION_RESOLUTION_NAME: b"old-resolution\n",
                    ADJUDICATION_QUEUE_NAME: b"old-queue\n",
                    TRACKING_CONTRACT_REPORT_NAME: b"old-contract\n",
                }
                for name, content in old_contents.items():
                    (output_dir / name).write_bytes(content)

                from football_tracking import candidate_annotations

                real_verify = candidate_annotations._verify_unchanged_snapshots

                def mutate_then_verify(snapshots: object) -> None:
                    mutation_path.write_bytes(mutation_path.read_bytes() + b" ")
                    real_verify(snapshots)

                with patch.object(
                    candidate_annotations,
                    "_verify_unchanged_snapshots",
                    side_effect=mutate_then_verify,
                ):
                    with self.assertRaisesRegex(ValueError, "changed during annotation resolution"):
                        resolve_candidate_annotations(
                            contract_path,
                            ledger_path,
                            output_dir,
                            dataset_manifest_path=manifest_path,
                        )

                restored = {name: (output_dir / name).read_bytes() for name in old_contents}
                leftovers = [path.name for path in output_dir.iterdir() if path.name.startswith(".")]
                self.assertEqual(old_contents, restored)
                self.assertEqual([], leftovers)

    def test_rejects_empty_or_invalid_contract_and_cli_returns_concise_json_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            contract_path, ledger_path, output_dir = self._paths(root, candidates=[], votes=[])
            stderr = StringIO()
            stdout = StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = annotation_cli_main(
                    [
                        "--contract",
                        str(contract_path),
                        "--ledger",
                        str(ledger_path),
                        "--output-dir",
                        str(output_dir),
                    ]
                )

        self.assertEqual(1, code)
        self.assertEqual("", stdout.getvalue())
        failure = json.loads(stderr.getvalue())
        self.assertEqual("failed", failure["status"])
        self.assertIn("candidate", failure["error"].lower())

        stderr = StringIO()
        with redirect_stdout(StringIO()), redirect_stderr(stderr):
            parse_code = annotation_cli_main([])
        parse_failure = json.loads(stderr.getvalue())
        self.assertEqual(1, parse_code)
        self.assertEqual("failed", parse_failure["status"])
        self.assertIn("required", parse_failure["error"])

    def test_success_cli_emits_json_and_publishes_contract_last(self) -> None:
        votes = [_vote("a", "c1", "match_ball"), _vote("b", "c1", "match_ball")]
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            contract_path, ledger_path, output_dir = self._paths(
                root,
                candidates=[_candidate("c1", 0)],
                votes=votes,
            )
            stdout = StringIO()
            stderr = StringIO()
            replace_targets: list[str] = []

            from football_tracking import candidate_annotations

            real_replace = candidate_annotations.os.replace

            def record_replace(source: str | Path, destination: str | Path) -> None:
                if Path(destination).parent == output_dir and not str(destination).endswith(".bak"):
                    replace_targets.append(Path(destination).name)
                real_replace(source, destination)

            with patch.object(candidate_annotations.os, "replace", side_effect=record_replace):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = annotation_cli_main(
                        [
                            "--contract",
                            str(contract_path),
                            "--ledger",
                            str(ledger_path),
                            "--dataset-manifest",
                            str(root / "candidate_dataset_manifest.json"),
                            "--output-dir",
                            str(output_dir),
                            "--min-confidence",
                            "0.8",
                        ]
                    )

        self.assertEqual(0, code)
        self.assertEqual("", stderr.getvalue())
        success = json.loads(stdout.getvalue())
        self.assertEqual("complete", success["status"])
        self.assertEqual(TRACKING_CONTRACT_REPORT_NAME, replace_targets[-1])

    def test_publish_reports_uses_internal_order_for_scrambled_input_and_rejects_invalid_sets(self) -> None:
        from football_tracking import candidate_annotations

        resolution = {"artifact": "resolution"}
        queue = {"artifact": "queue"}
        contract = {"artifact": "contract"}
        exact_contract_bytes = b'{"artifact":"contract","exact":true}\n'
        scrambled = (
            (TRACKING_CONTRACT_REPORT_NAME, contract),
            (ADJUDICATION_QUEUE_NAME, queue),
            (ANNOTATION_RESOLUTION_NAME, resolution),
        )
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            output_dir = root / "ordered"
            real_replace = candidate_annotations.os.replace
            publish_targets: list[str] = []

            def record_replace(source: str | Path, destination: str | Path) -> None:
                if Path(source).suffix == ".tmp":
                    publish_targets.append(Path(destination).name)
                real_replace(source, destination)

            with patch.object(candidate_annotations.os, "replace", side_effect=record_replace):
                candidate_annotations._publish_reports(
                    output_dir,
                    scrambled,
                    preencoded={TRACKING_CONTRACT_REPORT_NAME: exact_contract_bytes},
                )

            contract_bytes = (output_dir / TRACKING_CONTRACT_REPORT_NAME).read_bytes()
            with self.assertRaisesRegex(ValueError, "duplicate"):
                candidate_annotations._publish_reports(
                    root / "duplicate",
                    (
                        (ANNOTATION_RESOLUTION_NAME, resolution),
                        (ANNOTATION_RESOLUTION_NAME, resolution),
                        (ADJUDICATION_QUEUE_NAME, queue),
                        (TRACKING_CONTRACT_REPORT_NAME, contract),
                    ),
                )
            with self.assertRaisesRegex(ValueError, "missing"):
                candidate_annotations._publish_reports(
                    root / "missing",
                    (
                        (ANNOTATION_RESOLUTION_NAME, resolution),
                        (ADJUDICATION_QUEUE_NAME, queue),
                    ),
                )

        self.assertEqual(
            [ANNOTATION_RESOLUTION_NAME, ADJUDICATION_QUEUE_NAME, TRACKING_CONTRACT_REPORT_NAME],
            publish_targets,
        )
        self.assertEqual(exact_contract_bytes, contract_bytes)


if __name__ == "__main__":
    unittest.main()
