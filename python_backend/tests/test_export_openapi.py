from __future__ import annotations

import unittest
from pathlib import Path

from scripts.export_openapi import _frontend_paths, build_openapi_document


class ExportOpenApiTests(unittest.TestCase):
    def test_build_openapi_document_exposes_frontend_proxy_paths(self) -> None:
        document = build_openapi_document()
        paths = set(document["paths"])

        expected_paths = {
            "/healthz",
            "/health",
            "/runs",
            "/runs/{run_id}/ai-improvement-status",
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

    def test_generated_clients_expose_highlight_boundary_contracts(self) -> None:
        react_schemas = Path("lib/api-client-react/src/generated/api.schemas.ts").read_text(encoding="utf-8")
        zod_api = Path("lib/api-zod/src/generated/api.ts").read_text(encoding="utf-8")
        zod_candidate = Path("lib/api-zod/src/generated/types/eventCandidate.ts").read_text(encoding="utf-8")
        zod_policy = Path("lib/api-zod/src/generated/types/eventCandidateBufferPolicy.ts").read_text(
            encoding="utf-8"
        )

        for field in ("core_window", "render_window", "buffer_policy", "EventCandidateBufferPolicy"):
            self.assertIn(field, react_schemas)
            self.assertIn(field, zod_candidate)
        for field in ("fps_source", "min_tail_frames"):
            self.assertIn(field, react_schemas)
            self.assertIn(field, zod_policy)
            self.assertIn(field, zod_api)
        self.assertIn("export interface EventCandidateReport", react_schemas)
        self.assertIn("warnings?: string[]", react_schemas)
        self.assertIn("CreateHighlightRenderBody", zod_api)
        self.assertIn("superRefine", zod_api)
        self.assertIn("exactly one of candidate_id, approved_action_id, or start_frame/end_frame", zod_api)
        self.assertEqual(1, zod_api.count("exactly one of candidate_id, approved_action_id, or start_frame/end_frame"))

    def test_generated_zod_highlight_postprocess_uses_stable_anchor(self) -> None:
        postprocess = Path("lib/api-spec/scripts/postprocess-generated.mjs").read_text(encoding="utf-8")

        self.assertIn("CreateHighlightRenderBody", postprocess)
        self.assertIn("exactly one of candidate_id, approved_action_id, or start_frame/end_frame", postprocess)
        self.assertNotIn("Get Player Tracks Report", postprocess)

    def test_generated_clients_expose_approved_child_rerun_fields(self) -> None:
        react_schemas = Path("lib/api-client-react/src/generated/api.schemas.ts").read_text(encoding="utf-8")
        zod_api = Path("lib/api-zod/src/generated/api.ts").read_text(encoding="utf-8")

        for field in ("approved_action_ids", "approved_actions_artifact_name"):
            self.assertIn(field, react_schemas)
            self.assertIn(field, zod_api)

    def test_openapi_run_record_exposes_ai_candidate_lifecycle_schema(self) -> None:
        document = build_openapi_document()
        run_record = document["components"]["schemas"]["RunRecord"]
        lifecycle_ref = run_record["properties"]["ai_candidate_lifecycle"]

        self.assertEqual("#/components/schemas/AICandidateLifecycleReport", lifecycle_ref["$ref"])
        lifecycle = document["components"]["schemas"]["AICandidateLifecycleSummary"]
        candidate = document["components"]["schemas"]["AICandidateLifecycleCandidate"]
        for field in (
            "stage",
            "comparison_status",
            "promotion_status",
            "resolution_status",
            "blocking_reasons",
        ):
            self.assertIn(field, lifecycle["properties"])
            self.assertIn(field, candidate["properties"])

    def test_generated_clients_expose_ai_candidate_lifecycle_fields(self) -> None:
        react_schemas = Path("lib/api-client-react/src/generated/api.schemas.ts").read_text(encoding="utf-8")
        zod_report = Path("lib/api-zod/src/generated/types/aICandidateLifecycleReport.ts").read_text(encoding="utf-8")
        zod_summary = Path("lib/api-zod/src/generated/types/aICandidateLifecycleSummary.ts").read_text(encoding="utf-8")
        zod_candidate = Path("lib/api-zod/src/generated/types/aICandidateLifecycleCandidate.ts").read_text(
            encoding="utf-8"
        )

        for field in (
            "ai_candidate_lifecycle",
            "stage",
            "comparison_status",
            "promotion_status",
            "resolution_status",
            "blocking_reasons",
        ):
            self.assertIn(field, react_schemas)
        for field in ("summary", "candidates"):
            self.assertIn(field, zod_report)
        for field in ("stage", "comparison_status", "promotion_status", "resolution_status", "blocking_reasons"):
            self.assertIn(field, zod_summary)
            self.assertIn(field, zod_candidate)

    def test_openapi_exposes_ai_improvement_status_contract(self) -> None:
        document = build_openapi_document()

        operation = document["paths"]["/runs/{run_id}/ai-improvement-status"]["get"]
        self.assertEqual(
            "#/components/schemas/AIImprovementStatusResponse",
            operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
        )
        response = document["components"]["schemas"]["AIImprovementStatusResponse"]
        for field in (
            "artifacts",
            "items_by_problem_type",
            "final_manifest_status",
            "final_selected_artifacts",
            "final_selected_artifact_candidate_ids",
        ):
            self.assertIn(field, response["properties"])
        item = document["components"]["schemas"]["AIImprovementStatusItem"]
        for field in (
            "improvement_id",
            "candidate_id",
            "approval_ids",
            "frame_window",
            "evidence_ids",
            "recommended_action",
            "approval_status",
            "consumed_approval_ids",
            "comparison_status",
            "promotion_status",
            "artifact_references",
        ):
            self.assertIn(field, item["properties"])

    def test_generated_clients_expose_ai_improvement_status_contract(self) -> None:
        react_api = Path("lib/api-client-react/src/generated/api.ts").read_text(encoding="utf-8")
        react_schemas = Path("lib/api-client-react/src/generated/api.schemas.ts").read_text(encoding="utf-8")
        zod_api = Path("lib/api-zod/src/generated/api.ts").read_text(encoding="utf-8")
        zod_response = Path("lib/api-zod/src/generated/types/aIImprovementStatusResponse.ts").read_text(
            encoding="utf-8"
        )
        zod_item = Path("lib/api-zod/src/generated/types/aIImprovementStatusItem.ts").read_text(encoding="utf-8")

        self.assertIn("getAiImprovementStatus", react_api)
        self.assertIn("/ai-improvement-status", react_api)
        for field in (
            "AIImprovementStatusResponse",
            "AIImprovementStatusItem",
            "items_by_problem_type",
            "final_selected_artifacts",
            "final_selected_artifact_candidate_ids",
        ):
            self.assertIn(field, react_schemas)
        self.assertIn("getAiImprovementStatusResponse", zod_api)
        for field in ("items_by_problem_type", "final_manifest_status", "final_selected_artifacts"):
            self.assertIn(field, zod_response)
        for field in ("approval_status", "comparison_status", "promotion_status", "artifact_references"):
            self.assertIn(field, zod_item)


if __name__ == "__main__":
    unittest.main()
