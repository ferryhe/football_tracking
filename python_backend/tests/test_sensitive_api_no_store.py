from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi import Response
from fastapi.testclient import TestClient

from football_tracking.api.app import create_app


class SensitiveApiNoStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        for name in ("config", "data", "outputs", "weights"):
            (root / name).mkdir()
        app = create_app(root, initialize_service=False)

        def controlled_response(status_code: int) -> Response:
            if status_code == 500:
                raise RuntimeError("secret internal failure must not escape")
            return Response(status_code=status_code)

        for prefix in (
            "/api/v1/ball-annotation-sessions",
            "/api/v1/detector-review-proxy-repairs",
        ):
            app.add_api_route(
                f"{prefix}/_no-store-test/{{status_code}}",
                controlled_response,
                methods=["GET"],
            )
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def test_sensitive_prefixes_are_no_store_for_success_and_error_matrix(self) -> None:
        for prefix in (
            "/api/v1/ball-annotation-sessions",
            "/api/v1/detector-review-proxy-repairs",
        ):
            for status_code in (200, 400, 404, 409, 412, 422, 428):
                with self.subTest(prefix=prefix, status_code=status_code):
                    response = self.client.get(f"{prefix}/_no-store-test/{status_code}")
                    self.assertEqual(status_code, response.status_code, response.text)
                    self.assertEqual("no-store", response.headers.get("cache-control"))

    def test_untrusted_origin_403_is_no_store_for_both_sensitive_prefixes(self) -> None:
        for prefix in (
            "/api/v1/ball-annotation-sessions",
            "/api/v1/detector-review-proxy-repairs",
        ):
            with self.subTest(prefix=prefix):
                response = self.client.get(
                    f"{prefix}/_no-store-test/200",
                    headers={"Origin": "https://attacker.example"},
                )
                self.assertEqual(403, response.status_code, response.text)
                self.assertEqual("no-store", response.headers.get("cache-control"))

    def test_unhandled_500_is_sanitized_and_no_store_for_both_sensitive_prefixes(self) -> None:
        for prefix in (
            "/api/v1/ball-annotation-sessions",
            "/api/v1/detector-review-proxy-repairs",
        ):
            with self.subTest(prefix=prefix):
                response = self.client.get(f"{prefix}/_no-store-test/500")
                self.assertEqual(500, response.status_code)
                self.assertEqual(
                    {"detail": "Internal server error"},
                    response.json(),
                )
                self.assertNotIn("secret internal failure", response.text)
                self.assertEqual("no-store", response.headers.get("cache-control"))


if __name__ == "__main__":
    unittest.main()
