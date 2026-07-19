from __future__ import annotations

import unittest

from football_tracking.review_proxy_mapping import (
    ReviewProxyError,
    build_review_proxy_manifest,
    validate_review_proxy_manifest,
)


class ReviewProxyMappingTests(unittest.TestCase):
    def _manifest(self, **patch: object) -> dict[str, object]:
        values: dict[str, object] = {
            "source": {
                "sha256": "a" * 64,
                "file_identity_sha256": "b" * 64,
                "size_bytes": 1000,
                "width": 5120,
                "height": 1440,
                "fps": 20.0,
                "frame_count": 300,
                "codec": "hevc",
            },
            "proxy": {
                "sha256": "c" * 64,
                "size_bytes": 900,
                "width": 2560,
                "height": 720,
                "fps": 20.0,
                "frame_count": 300,
                "codec": "h264",
            },
            "mappings": [
                {
                    "source_frame_index": 10,
                    "source_decoder_pos_msec": 500.0,
                    "proxy_frame_index": 10,
                    "proxy_timing_basis": "verified_cfr_frame_index_time_v1",
                    "proxy_cfr_time_msec": 500.0,
                    "source_frame_sha256": "d" * 64,
                    "proxy_frame_sha256": "e" * 64,
                    "media_integrity": {
                        "status": "ok",
                        "gray": False,
                        "low_information": False,
                        "likely_corrupt": False,
                    },
                }
            ],
            "expected_frame_indices": [10],
            "decoder_fingerprint_sha256": "f" * 64,
            "requested_decode_mode": "preroll",
            "effective_decode_mode": "preroll_verified",
            "map_time_tolerance_msec": 12.5,
            "declared_offset_msec": 0.0,
        }
        values.update(patch)
        return build_review_proxy_manifest(**values)

    def test_manifest_binds_source_proxy_pts_map_and_integrity_digest(self) -> None:
        manifest = self._manifest()
        self.assertEqual("ball_review_proxy", manifest["artifact_type"])
        self.assertEqual("a" * 64, manifest["source"]["sha256"])
        self.assertRegex(manifest["mapping_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(manifest["integrity_report_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(manifest["manifest_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(manifest, validate_review_proxy_manifest(manifest))

    def test_gray_shortfall_pts_and_mapping_mismatch_fail_closed(self) -> None:
        bad_mapping = [
            {
                "source_frame_index": 10,
                "source_decoder_pos_msec": 500.0,
                "proxy_frame_index": 11,
                "proxy_timing_basis": "verified_cfr_frame_index_time_v1",
                "proxy_cfr_time_msec": 550.0,
                "source_frame_sha256": "d" * 64,
                "proxy_frame_sha256": "e" * 64,
                "media_integrity": {"status": "ok", "gray": False, "low_information": False, "likely_corrupt": False},
            }
        ]
        for patch, message in (
            ({"mappings": bad_mapping}, "mapping mismatch"),
            (
                {
                    "proxy": {
                        "sha256": "c" * 64,
                        "size_bytes": 900,
                        "width": 2560,
                        "height": 720,
                        "fps": 20.0,
                        "frame_count": 299,
                        "codec": "h264",
                    }
                },
                "shortfall",
            ),
            (
                {
                    "mappings": [
                        {
                            **bad_mapping[0],
                            "proxy_frame_index": 10,
                            "proxy_cfr_time_msec": 500.0,
                            "media_integrity": {
                                "status": "ok",
                                "gray": True,
                                "low_information": False,
                                "likely_corrupt": False,
                            },
                        }
                    ]
                },
                "integrity",
            ),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(ReviewProxyError, message):
                self._manifest(**patch)

    def test_proxy_requires_exact_frozen_set_and_uniform_coordinate_transform(self) -> None:
        with self.assertRaisesRegex(ReviewProxyError, "exact frozen frame set"):
            self._manifest(expected_frame_indices=[10, 20])
        with self.assertRaisesRegex(ReviewProxyError, "distorted"):
            self._manifest(
                proxy={
                    "sha256": "c" * 64,
                    "size_bytes": 900,
                    "width": 2560,
                    "height": 700,
                    "fps": 20.0,
                    "frame_count": 300,
                    "codec": "h264",
                }
            )


if __name__ == "__main__":
    unittest.main()
