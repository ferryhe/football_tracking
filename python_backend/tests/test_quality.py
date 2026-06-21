from __future__ import annotations

import unittest

from football_tracking.quality import overall_status_for_checks


class QualityTests(unittest.TestCase):
    def test_overall_status_fails_when_single_check_fails(self) -> None:
        checks = [
            {"key": "brightness", "status": "pass"},
            {"key": "blur", "status": "pass"},
            {"key": "field_visibility", "status": "pass"},
            {"key": "camera_stability", "status": "pass"},
            {"key": "calibration", "status": "fail"},
        ]

        self.assertEqual("fail", overall_status_for_checks(checks, 0.8))


if __name__ == "__main__":
    unittest.main()
