from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from football_tracking.ai_candidate_registry import (
    REGISTRY_REPORT_NAME,
    build_candidate_registry,
    load_candidate_registry,
    normalize_candidate_record,
    write_candidate_registry,
)
from football_tracking.ai_improvement_quality_gate import build_ai_improvement_quality_gate


class AiCandidateRegistryTests(unittest.TestCase):
    def test_normalize_valid_missing_ball_record(self) -> None:
        record = normalize_candidate_record(
            {
                "candidate_id": "candidate-001",
                "approval_id": "approval-001",
                "problem_type": "missing_ball",
                "baseline_dir": Path("baseline"),
                "candidate_dir": Path("candidate"),
                "candidate_artifacts": ["candidate/ball_track.csv"],
                "comparison_report": "missing_ball_recovery_comparison.json",
                "comparison_status": "pass",
                "promotion_status": "not_promoted",
                "consumed_approval_ids": ["approval-001"],
            }
        )

        self.assertEqual("candidate-001", record["candidate_id"])
        self.assertEqual("approval-001", record["approval_id"])
        self.assertEqual("missing_ball", record["problem_type"])
        self.assertEqual("baseline", record["baseline_dir"])
        self.assertEqual("candidate", record["candidate_dir"])
        self.assertEqual(["candidate/ball_track.csv"], record["candidate_artifacts"])
        self.assertEqual("missing_ball_recovery_comparison.json", record["comparison_report"])
        self.assertEqual("pass", record["comparison_status"])
        self.assertEqual("not_promoted", record["promotion_status"])
        self.assertEqual(["approval-001"], record["consumed_approval_ids"])
        self.assertEqual([], record["warnings"])

    def test_reject_duplicate_candidate_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)

            with self.assertRaisesRegex(ValueError, "candidate_id"):
                build_candidate_registry(
                    output_dir,
                    records=[
                        _record("candidate-001", approval_id="approval-001"),
                        _record("candidate-001", approval_id="approval-002"),
                    ],
                )

    def test_reject_same_approval_id_consumed_by_different_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)

            with self.assertRaisesRegex(ValueError, "approval_id"):
                build_candidate_registry(
                    output_dir,
                    records=[
                        _record("candidate-001", approval_id="approval-shared"),
                        _record("candidate-002", approval_id="approval-002", consumed_approval_ids=["approval-shared"]),
                    ],
                )

    def test_reject_unsafe_comparison_report_name(self) -> None:
        with self.assertRaisesRegex(ValueError, "report name"):
            normalize_candidate_record({**_record("candidate-001"), "comparison_report": "../escape.json"})

    def test_build_from_missing_ball_recovery_comparison_style_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            comparison = {
                "candidate_id": "candidate-001",
                "approval_id": "approval-001",
                "problem_type": "missing_ball",
                "comparison_report": "missing_ball_recovery_comparison.json",
                "comparison_status": "warn",
                "promotion_status": "pending_confirmation",
                "consumed_approval_ids": ["approval-001"],
                "candidate_artifacts": ["candidate/ball_track.csv"],
                "baseline": {"path": "baseline/ball_track.csv"},
                "candidate": {"path": "candidate/ball_track.csv"},
            }

            registry = build_candidate_registry(output_dir, comparison_reports=[comparison])

        self.assertEqual("1.0", registry["schema_version"])
        self.assertEqual("warn", registry["summary"]["status"])
        self.assertEqual(1, registry["summary"]["candidate_count"])
        self.assertEqual(
            {
                "candidate_id": "candidate-001",
                "approval_id": "approval-001",
                "problem_type": "missing_ball",
                "baseline_dir": "baseline",
                "candidate_dir": "candidate",
                "candidate_artifacts": ["candidate/ball_track.csv"],
                "comparison_report": "missing_ball_recovery_comparison.json",
                "comparison_status": "warn",
                "promotion_status": "pending_confirmation",
                "consumed_approval_ids": ["approval-001"],
                "warnings": [],
            },
            registry["candidates"][0],
        )

    def test_summary_status_precedence_fail_unavailable_warn_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)

            warn_registry = build_candidate_registry(output_dir, records=[_record("candidate-warn", status="warn")])
            unavailable_registry = build_candidate_registry(
                output_dir,
                records=[
                    _record("candidate-warn", approval_id="approval-warn", status="warn"),
                    _record("candidate-unavailable", approval_id="approval-unavailable", status="unavailable"),
                ],
            )
            fail_registry = build_candidate_registry(
                output_dir,
                records=[
                    _record("candidate-unavailable", approval_id="approval-unavailable", status="unavailable"),
                    _record("candidate-fail", approval_id="approval-fail", status="fail"),
                ],
            )

        self.assertEqual("warn", warn_registry["summary"]["status"])
        self.assertEqual("unavailable", unavailable_registry["summary"]["status"])
        self.assertEqual("fail", fail_registry["summary"]["status"])

    def test_load_missing_registry_returns_unavailable_without_raising(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            loaded = load_candidate_registry(Path(temp_name))

        self.assertEqual("1.0", loaded["schema_version"])
        self.assertEqual("unavailable", loaded["summary"]["status"])
        self.assertEqual(0, loaded["summary"]["candidate_count"])
        self.assertEqual("missing", loaded["artifact_status"])

    def test_write_registry_persists_json_with_summary_and_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)

            payload = write_candidate_registry(output_dir, records=[_record("candidate-001")])
            raw = (output_dir / REGISTRY_REPORT_NAME).read_text(encoding="utf-8")
            loaded = json.loads(raw)

        self.assertTrue(raw.endswith("\n"))
        self.assertEqual(payload, loaded)
        self.assertEqual(1, loaded["summary"]["candidate_count"])
        self.assertEqual("candidate-001", loaded["candidates"][0]["candidate_id"])

    def test_quality_gate_reads_registry_as_candidate_comparison_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_json(output_dir / "candidate_report.json", _comparison_payload("candidate-001", "warn"))
            write_candidate_registry(
                output_dir,
                records=[
                    {
                        **_record("candidate-001", status="pass"),
                        "comparison_report": "candidate_report.json",
                    }
                ],
            )

            gate = build_ai_improvement_quality_gate(output_dir)

        comparison_check = gate["checks"]["candidate_comparisons_ok"]
        self.assertEqual("warn", comparison_check["status"])
        self.assertEqual(1, comparison_check["report_count"])
        self.assertEqual(1, comparison_check["status_counts"]["warn"])
        self.assertEqual("candidate-001", comparison_check["reports"][0]["candidate_id"])
        self.assertEqual("registry_status_mismatch", comparison_check["reports"][0]["artifact_status"])

    def test_quality_gate_registry_requires_actual_comparison_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            write_candidate_registry(output_dir, records=[_record("candidate-001", status="pass")])

            gate = build_ai_improvement_quality_gate(output_dir)

        comparison_check = gate["checks"]["candidate_comparisons_ok"]
        self.assertEqual("unavailable", comparison_check["status"])
        self.assertEqual(1, comparison_check["report_count"])
        self.assertEqual("missing", comparison_check["reports"][0]["artifact_status"])

    def test_quality_gate_registry_report_is_not_hidden_by_same_candidate_id_glob_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_json(output_dir / "glob_comparison.json", _comparison_payload("candidate-001", "pass"))
            _write_json(output_dir / "registry_report.json", _comparison_payload("candidate-001", "fail"))
            write_candidate_registry(
                output_dir,
                records=[
                    {
                        **_record("candidate-001", status="pass"),
                        "comparison_report": "registry_report.json",
                    }
                ],
            )

            gate = build_ai_improvement_quality_gate(output_dir)

        comparison_check = gate["checks"]["candidate_comparisons_ok"]
        self.assertEqual("fail", comparison_check["status"])
        self.assertEqual(2, comparison_check["report_count"])
        self.assertEqual(1, comparison_check["status_counts"]["pass"])
        self.assertEqual(1, comparison_check["status_counts"]["fail"])

    def test_consumed_approval_ids_are_deduped(self) -> None:
        record = normalize_candidate_record(
            _record(
                "candidate-001",
                approval_id="approval-001",
                consumed_approval_ids=["approval-001", "approval-001", "approval-002"],
            )
        )

        self.assertEqual(["approval-001", "approval-002"], record["consumed_approval_ids"])

    def test_candidate_artifacts_validates_list_of_strings(self) -> None:
        with self.assertRaisesRegex(ValueError, "candidate_artifacts"):
            normalize_candidate_record({**_record("candidate-001"), "candidate_artifacts": "candidate/ball_track.csv"})

        with self.assertRaisesRegex(ValueError, "candidate_artifacts"):
            normalize_candidate_record({**_record("candidate-001"), "candidate_artifacts": ["candidate/ball_track.csv", 3]})

        with self.assertRaisesRegex(ValueError, "candidate_artifacts"):
            normalize_candidate_record({**_record("candidate-001"), "candidate_artifacts": ["../escape.csv"]})

        with tempfile.TemporaryDirectory() as temp_name:
            with self.assertRaisesRegex(ValueError, "candidate_artifacts"):
                normalize_candidate_record(
                    {**_record("candidate-001"), "candidate_artifacts": [str(Path(temp_name) / "candidate.csv")]}
                )

    def test_load_invalid_registry_returns_unavailable_without_raising(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_json(
                output_dir / REGISTRY_REPORT_NAME,
                {
                    "schema_version": "1.0",
                    "summary": {"status": "pass"},
                    "candidates": [
                        _record("candidate-001", approval_id="approval-shared"),
                        _record("candidate-002", approval_id="approval-shared"),
                    ],
                },
            )

            loaded = load_candidate_registry(output_dir)

        self.assertEqual("unavailable", loaded["summary"]["status"])
        self.assertEqual("invalid", loaded["artifact_status"])
        self.assertIn("approval_id", loaded["error"])

    def test_load_duplicate_candidate_registry_returns_unavailable_without_raising(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)
            _write_json(
                output_dir / REGISTRY_REPORT_NAME,
                {
                    "schema_version": "1.0",
                    "summary": {"status": "pass"},
                    "candidates": [
                        _record("candidate-001", approval_id="approval-001"),
                        _record("candidate-001", approval_id="approval-002"),
                    ],
                },
            )

            loaded = load_candidate_registry(output_dir)

        self.assertEqual("unavailable", loaded["summary"]["status"])
        self.assertEqual("invalid", loaded["artifact_status"])
        self.assertIn("candidate_id", loaded["error"])

    def test_write_registry_preserves_non_ascii_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output_dir = Path(temp_name)

            write_candidate_registry(
                output_dir,
                records=[
                    {
                        **_record("candidate-001"),
                        "warnings": ["需要人工确认"],
                    }
                ],
            )
            raw = (output_dir / REGISTRY_REPORT_NAME).read_text(encoding="utf-8")

        self.assertIn("需要人工确认", raw)


def _record(
    candidate_id: str,
    *,
    approval_id: str = "approval-001",
    status: str = "pass",
    consumed_approval_ids: list[str] | None = None,
) -> dict[str, object]:
    return {
        "candidate_id": candidate_id,
        "approval_id": approval_id,
        "problem_type": "missing_ball",
        "baseline_dir": "baseline",
        "candidate_dir": "candidate",
        "candidate_artifacts": ["candidate/ball_track.csv"],
        "comparison_report": "missing_ball_recovery_comparison.json",
        "comparison_status": status,
        "promotion_status": "not_promoted",
        "consumed_approval_ids": consumed_approval_ids if consumed_approval_ids is not None else [approval_id],
    }


def _comparison_payload(candidate_id: str, status: str) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "problem_type": "missing_ball",
        "candidate_id": candidate_id,
        "candidate": {"id": candidate_id, "path": f"candidate/{candidate_id}/ball_track.csv"},
        "summary": {
            "status": status,
            "check_count": 1,
            "passed_check_count": 1 if status == "pass" else 0,
            "failed_check_count": 1 if status == "fail" else 0,
            "warning_count": 1 if status == "warn" else 0,
            "unavailable_count": 1 if status == "unavailable" else 0,
        },
        "checks": [{"name": "comparison", "status": status}],
    }


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
