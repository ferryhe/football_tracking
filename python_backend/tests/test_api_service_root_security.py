from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from football_tracking.api.service import _validated_api_repo_root
from football_tracking.detector_development_common import DetectorDevelopmentError


class ApiServiceRootSecurityTests(unittest.TestCase):
    def test_rejects_ambiguous_windows_segments_before_normalization(self) -> None:
        for candidate in (
            Path("trailing."),
            Path("trailing "),
            Path("safe.") / "nested",
            Path("safe ") / "nested",
        ):
            with self.subTest(candidate=candidate):
                with self.assertRaises(DetectorDevelopmentError) as caught:
                    _validated_api_repo_root(candidate)
                self.assertEqual("invalid_path", caught.exception.code)

    def test_creates_and_returns_one_unambiguous_named_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            expected = parent / "safe-repository"

            actual = _validated_api_repo_root(expected)

            self.assertEqual(expected.resolve(), actual)
            self.assertTrue(actual.is_dir())


if __name__ == "__main__":
    unittest.main()
