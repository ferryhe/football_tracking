from __future__ import annotations

import unittest

from scripts.export_openapi import _frontend_paths, build_openapi_document


class ExportOpenApiTests(unittest.TestCase):
    def test_build_openapi_document_exposes_frontend_proxy_paths(self) -> None:
        document = build_openapi_document()
        paths = set(document["paths"])

        expected_paths = {
            "/healthz",
            "/health",
            "/runs",
            "/configs",
            "/inputs",
            "/ai/recommend",
            "/ai/improve",
        }

        self.assertTrue(expected_paths.issubset(paths))
        self.assertFalse(any(path.startswith("/api/v1/") for path in paths))
        self.assertEqual("get_config", document["paths"]["/configs/{name}"]["get"]["operationId"])
        self.assertEqual(
            "get_artifact",
            document["paths"]["/runs/{run_id}/artifacts/{artifact_name}"]["get"]["operationId"],
        )
        self.assertEqual("Api", document["info"]["title"])
        self.assertEqual([{"url": "/api"}], document["servers"])

    def test_frontend_paths_only_accept_api_v1_prefix_boundary(self) -> None:
        paths = _frontend_paths(
            {
                "/api/v1/runs": {},
                "/api/v10/not-real": {},
                "/api/v1beta/not-real": {},
            },
        )

        self.assertEqual({"/runs"}, set(paths))

    def test_operation_id_normalization_only_strips_route_suffix(self) -> None:
        paths = _frontend_paths(
            {
                "/api/v1/sample": {
                    "get": {
                        "operationId": "keep_api_v1_name_api_v1_sample_get",
                    },
                },
            },
        )

        self.assertEqual("keep_api_v1_name", paths["/sample"]["get"]["operationId"])


if __name__ == "__main__":
    unittest.main()
