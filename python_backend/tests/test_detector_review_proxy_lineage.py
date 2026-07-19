from __future__ import annotations

import unittest
from copy import deepcopy

from football_tracking.detector_probe import (
    _review_proxy_continuation_execution_binding,
    semantic_probe_intent_sha256,
)

SHA_A = "a" * 64
SHA_B = "b" * 64


def _frozen_request() -> dict[str, object]:
    return {
        "parent_trial_id": "production-trial-1",
        "source_id": "source-1",
        "source_relative_path": "data/source.mp4",
        "source_sha256": SHA_A,
        "source_file_identity_sha256": SHA_B,
        "source_size_bytes": 100,
        "source_width": 5120,
        "source_height": 1440,
        "source_frame_count": 104_820,
        "tracking_contract_relative_path": "outputs/tracking_contract.v2.json",
        "tracking_contract_sha256": SHA_A,
        "base_config_relative_path": "config/base.yaml",
        "base_config_sha256": SHA_A,
        "effective_config_relative_path": "config/effective.yaml",
        "effective_config_sha256": SHA_B,
        "trial_intent_sha256": SHA_A,
        "tuning_patch_binding": {
            "state": "absent",
            "schema_version": "1.0",
            "version_id": None,
            "parent_version_id": None,
            "values_sha256": SHA_A,
        },
        "tuning_patch_sha256": SHA_B,
        "profile_ids": ["control", "locked"],
        "frozen_profiles_sha256": SHA_A,
        "profile_sha256s": {"control": SHA_A, "locked": SHA_B},
        "profile_bindings": [
            {
                "profile_id": "control",
                "profile_sha256": SHA_A,
                "model_id": "model-a",
                "model_version": "1",
                "model_descriptor_sha256": SHA_A,
                "weights_sha256": SHA_A,
                "weights_size_bytes": 10,
            },
            {
                "profile_id": "locked",
                "profile_sha256": SHA_B,
                "model_id": "model-b",
                "model_version": "1",
                "model_descriptor_sha256": SHA_B,
                "weights_sha256": SHA_B,
                "weights_size_bytes": 20,
            },
        ],
        "execution_bundle": {"code_bundle_files": {"legacy.py": SHA_A}},
        "execution_bundle_sha256": SHA_A,
        "runtime_environment_sha256": SHA_A,
        "frame_indices": [1500, 1560, 1620],
        "top_k": 5,
        "requested_decode_mode": "preroll",
        "annotation_sampling_manifest_sha256": SHA_B,
    }


class DetectorReviewProxySemanticLineageTests(unittest.TestCase):
    def test_continuation_binding_covers_detector_freeze_runner_and_annotation_closure(
        self,
    ) -> None:
        binding = _review_proxy_continuation_execution_binding()
        self.assertTrue(
            {
                "football_tracking/detector_model_registry.py",
                "football_tracking/detector_probe_runner.py",
                "football_tracking/media_integrity.py",
                "football_tracking/review_proxy_mapping.py",
                "football_tracking/ball_annotation_service.py",
                "football_tracking/api/service.py",
            }.issubset(binding["code_files"])
        )

    def test_runtime_execution_retry_and_proxy_attempt_fields_do_not_change_semantic_intent(self) -> None:
        parent = _frozen_request()
        child = deepcopy(parent)
        child.update(
            {
                "execution_bundle": {"code_bundle_files": {"current-expanded.py": SHA_B}},
                "execution_bundle_sha256": SHA_B,
                "runtime_environment_sha256": SHA_B,
                "retry_from_job_id": "probe-parent",
                "retry_kind": "review_proxy_decode_upgrade",
                "review_proxy_binding": {
                    "review_proxy_id": "review-proxy-1",
                    "manifest_sha256": SHA_A,
                },
            }
        )

        self.assertEqual(
            semantic_probe_intent_sha256(parent),
            semantic_probe_intent_sha256(child),
        )

    def test_every_semantic_authority_family_changes_the_digest(self) -> None:
        baseline = _frozen_request()
        cases = {
            "source": ("source_sha256", SHA_B),
            "contract": ("tracking_contract_sha256", SHA_B),
            "config": ("base_config_sha256", SHA_B),
            "trial": ("trial_intent_sha256", SHA_B),
            "profiles": ("frozen_profiles_sha256", SHA_B),
            "frames": ("frame_indices", [1500, 1560, 1621]),
            "decode": ("requested_decode_mode", "sequential"),
            "sampling": ("annotation_sampling_manifest_sha256", SHA_A),
        }
        expected = semantic_probe_intent_sha256(baseline)
        for label, (field, value) in cases.items():
            with self.subTest(label=label):
                changed = deepcopy(baseline)
                changed[field] = value
                self.assertNotEqual(expected, semantic_probe_intent_sha256(changed))


if __name__ == "__main__":
    unittest.main()
