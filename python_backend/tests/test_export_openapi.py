from __future__ import annotations

from pathlib import Path
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
            "/ai/improve/{run_id}/approve",
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

    def test_generated_clients_expose_ai_camera_improvement_fields(self) -> None:
        react_schemas = Path("lib/api-client-react/src/generated/api.schemas.ts").read_text(encoding="utf-8")
        zod_summary = Path("lib/api-zod/src/generated/types/aIImproveSummary.ts").read_text(encoding="utf-8")
        zod_item = Path("lib/api-zod/src/generated/types/aIImprovementItem.ts").read_text(encoding="utf-8")
        zod_action = Path("lib/api-zod/src/generated/types/aIApprovedAction.ts").read_text(encoding="utf-8")
        zod_api = Path("lib/api-zod/src/generated/api.ts").read_text(encoding="utf-8")

        for field in ("camera_improvement_count", "camera_severity_counts", "camera_action_counts"):
            self.assertIn(field, react_schemas)
            self.assertIn(field, zod_summary)
            self.assertIn(field, zod_api)
        for field in (
            "camera_motion_event_id",
            "camera_motion_severity",
            "evidence_payload",
            "follow_cam_rerender_plan",
        ):
            self.assertIn(field, react_schemas)
            self.assertIn(field, zod_item)
        for field in ("camera_motion_event_id", "camera_motion_severity"):
            self.assertIn(field, zod_action)


if __name__ == "__main__":
    unittest.main()
