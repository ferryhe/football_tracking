from __future__ import annotations

import base64
import gzip
import hashlib
import json
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from football_tracking.ball_frame_evidence import (
    BallFrameEvidenceError,
    build_detector_probe_job_authority,
    build_detector_probe_result_manifest_authority,
    validate_detector_probe_job_authority,
)
from football_tracking.detector_audited_authority import (
    AUDITED_T2_LEGACY_PROBE_BINDINGS,
)

_FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures"
_AUDITED_CASES = (
    {
        "fixture_name": "audited_t2_probe_308.job.json.gz.b64",
        "job_id": "probe-308adcc1feaa99cc-bddecb26d127",
        "raw_size": 164_074,
        "raw_sha256": "b5653d93474786328a182f504a7e590d1f3cff67b3d547c45234fba844cb6cb3",
        "binding": {
            "canonical_job_record_sha256": "9fc18e56adbfc1cb4577746d807494e533aa5fbea2ce45b5fe4e05e3fc8a25df",
            "request_sha256": "308adcc1feaa99cc818ed05abba3d960ffe5a724439d2b86eddcc1b823954eb1",
            "report_sha256": "9f422a6b0270e7e4e933505949cab5aabc32eaa0d4e36ebe28a269bdcf083644",
            "result_manifest_sha256": "cee7fe623740356457d40493d45ba98fdd109138de14bb7d73bbeeabb60bcbee",
            "execution_bundle_sha256": "fe8ffe2aca1d83f5b3ed19d6fff96999fa3224d3688147c23e1622fe58947494",
            "runtime_environment_sha256": "6ae75ef83b968801c2c0f16a628e9eba94fe88aed27384c2896d08c7e90054d3",
        },
    },
    {
        "fixture_name": "audited_t2_probe_b59.job.json.gz.b64",
        "job_id": "probe-b59e904ee0b8a14f-e2aee08f414e",
        "raw_size": 164_201,
        "raw_sha256": "2b826bc4c6b97f3568530e42391900eb4f3537dea4d1b046e76a0d47743c2066",
        "binding": {
            "canonical_job_record_sha256": "5032ed68a95659a2321255e3815dc49d6b755260026870d3ed1e0b390c6edc8b",
            "request_sha256": "b59e904ee0b8a14fd8ee61d4d26909b2c4c08be8d2d8787687d19e3f6b2ca293",
            "report_sha256": "bc339d742993075849a0b892d90e36800ea7c4bcab1be0349b601056bfc67bcc",
            "result_manifest_sha256": "78258e7078c7892814891f7e5797623dfc778c601613c0ea67a7609b79af81b6",
            "execution_bundle_sha256": "fe8ffe2aca1d83f5b3ed19d6fff96999fa3224d3688147c23e1622fe58947494",
            "runtime_environment_sha256": "6ae75ef83b968801c2c0f16a628e9eba94fe88aed27384c2896d08c7e90054d3",
        },
    },
)


def _audited_job(case: dict) -> dict:
    encoded = b"".join((_FIXTURE_ROOT / case["fixture_name"]).read_bytes().split())
    raw = gzip.decompress(base64.b64decode(encoded, validate=True))
    if len(raw) != case["raw_size"] or hashlib.sha256(raw).hexdigest() != case["raw_sha256"]:
        raise AssertionError("audited T2 fixture bytes changed")
    return json.loads(raw)


class DetectorProbeJobAuthorityTests(unittest.TestCase):
    def test_real_legacy_job_builds_and_revalidates_without_semantic_rewrite(
        self,
    ) -> None:
        authority_field_by_binding = {
            "canonical_job_record_sha256": "canonical_job_record_sha256",
            "request_sha256": "request_sha256",
            "report_sha256": "probe_report_sha256",
            "result_manifest_sha256": "probe_result_manifest_sha256",
            "execution_bundle_sha256": "execution_bundle_sha256",
            "runtime_environment_sha256": "runtime_environment_sha256",
        }
        for case in _AUDITED_CASES:
            with self.subTest(job_id=case["job_id"]):
                job = _audited_job(case)
                with patch(
                    "football_tracking.detector_probe.semantic_probe_intent_sha256",
                    side_effect=AssertionError("legacy must not call current derivation"),
                ):
                    authority = build_detector_probe_job_authority(job)

                self.assertEqual(case["job_id"], authority["job_id"])
                self.assertEqual("audited_t2_legacy", authority["audit_anchor_kind"])
                self.assertIsNone(authority["semantic_intent_sha256"])
                self.assertEqual(job, authority["probe_job_record"])
                shared_binding = AUDITED_T2_LEGACY_PROBE_BINDINGS[case["job_id"]]
                self.assertEqual(case["binding"], dict(shared_binding))
                for binding_field, authority_field in authority_field_by_binding.items():
                    self.assertEqual(
                        shared_binding[binding_field],
                        authority[authority_field],
                    )
                self.assertEqual(
                    authority,
                    validate_detector_probe_job_authority(authority),
                )

    def test_shared_trust_root_is_deeply_immutable(self) -> None:
        with self.assertRaises(TypeError):
            AUDITED_T2_LEGACY_PROBE_BINDINGS["invented"] = {}  # type: ignore[index]
        with self.assertRaises(TypeError):
            AUDITED_T2_LEGACY_PROBE_BINDINGS[_AUDITED_CASES[0]["job_id"]]["request_sha256"] = "0" * 64  # type: ignore[index]

    def test_full_record_anchor_rejects_coherent_report_and_job_reseal(
        self,
    ) -> None:
        job = _audited_job(_AUDITED_CASES[0])
        report = job["report"]
        locked = report["frames"][0]["profile_results"][-1]
        invented = {
            "bbox_source_px": [10.0, 10.0, 14.0, 14.0],
            "confidence": 0.9,
        }
        locked.update(
            {
                "candidate_count": 1,
                "raw_candidates": [invented],
                "display_candidate": invented,
                "filter_reasons": {},
            }
        )
        from football_tracking.detector_development_common import canonical_sha256

        report["report_sha256"] = canonical_sha256(
            {key: value for key, value in report.items() if key != "report_sha256"}
        )
        _manifest, job["result_manifest_sha256"] = build_detector_probe_result_manifest_authority(report)

        with self.assertRaisesRegex(
            BallFrameEvidenceError,
            "changed from its trust anchor",
        ):
            build_detector_probe_job_authority(job)

    def test_exported_full_record_cannot_be_replaced_and_resealed(self) -> None:
        authority = build_detector_probe_job_authority(_audited_job(_AUDITED_CASES[0]))
        forged = deepcopy(authority)
        forged["probe_job_record"]["stage"] = "forged-ready"
        from football_tracking.detector_development_common import canonical_sha256

        forged["canonical_job_record_sha256"] = canonical_sha256(forged["probe_job_record"])
        body = {key: value for key, value in forged.items() if key != "job_record_authority_sha256"}
        forged["job_record_authority_sha256"] = canonical_sha256(body)

        with self.assertRaises(BallFrameEvidenceError):
            validate_detector_probe_job_authority(forged)


if __name__ == "__main__":
    unittest.main()
