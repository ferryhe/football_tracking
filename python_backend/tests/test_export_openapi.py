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

    def test_openapi_exposes_fail_closed_trial_diagnosis_and_tuning_contracts(self) -> None:
        document = build_openapi_document()
        paths = document["paths"]

        self.assertIn("/runs/{run_id}/trial-diagnosis", paths)
        self.assertIn("/production-trials/tuning-schema", paths)
        diagnosis = document["components"]["schemas"]["TrialDiagnosisResponse"]
        self.assertEqual(
            "#/components/schemas/TrialSignalGateV2",
            diagnosis["properties"]["trial_signal_gate_v2"]["$ref"],
        )
        gate = document["components"]["schemas"]["TrialSignalGateV2"]
        for field in (
            "coverage_complete",
            "evidence_available",
            "quality_acceptable",
            "operator_confirmation_required",
            "reason_codes",
            "failure_classification",
            "stage_counts",
            "threshold_profile",
            "diagnostics",
        ):
            self.assertIn(field, gate["properties"])
        stages = document["components"]["schemas"]["TrialDetectionStages"]
        for field in ("detected_frames", "predicted_frames", "lost_frames"):
            self.assertIn(field, stages["properties"])
            self.assertIn(field, stages["required"])
        failure = document["components"]["schemas"]["TrialFailureClassification"]
        self.assertIn("no_raw_candidates", failure["properties"]["code"]["enum"])
        self.assertIn("all_candidates_class_rejected", failure["properties"]["code"]["enum"])
        self.assertIn("acceptable", failure["properties"]["code"]["enum"])
        tuning = document["components"]["schemas"]["TrialTuningControl"]
        self.assertFalse(tuning.get("additionalProperties", True))
        self.assertIn("multi_select", tuning["properties"]["kind"]["enum"])
        for field in ("minimum", "maximum", "step", "options", "description", "description_zh"):
            self.assertIn(field, tuning["properties"])
        tuning_schema = document["components"]["schemas"]["TrialTuningSchemaResponse"]
        self.assertIn("actions", tuning_schema["properties"])
        action = document["components"]["schemas"]["TrialTuningAction"]
        self.assertEqual(
            "return_to_field_setup",
            action["properties"]["action_code"]["const"],
        )
        run_record = document["components"]["schemas"]["RunRecord"]
        self.assertEqual(
            "#/components/schemas/TrialSignalGateV2",
            run_record["properties"]["trial_signal_gate_v2"]["anyOf"][0]["$ref"],
        )
        self.assertIn("config_sha256", run_record["properties"])

    def test_generated_clients_expose_trial_diagnosis_and_tuning_contracts(self) -> None:
        react_api = Path("lib/api-client-react/src/generated/api.ts").read_text(encoding="utf-8")
        react_schemas = Path("lib/api-client-react/src/generated/api.schemas.ts").read_text(encoding="utf-8")
        zod_api = Path("lib/api-zod/src/generated/api.ts").read_text(encoding="utf-8")

        for operation in ("getTrialDiagnosis", "getProductionTrialTuningSchema"):
            self.assertIn(operation, react_api)
        for schema in (
            "TrialDiagnosisResponse",
            "TrialFailureClassification",
            "TrialSignalGateV2",
            "TrialGateDiagnostics",
            "TrialNumericObservation",
            "TrialTuningControl",
            "TrialTuningAction",
            "TrialTuningSchemaResponse",
        ):
            self.assertIn(schema, react_schemas)
        for field in ("detected_frames", "predicted_frames", "lost_frames"):
            self.assertIn(f"{field}: TrialCollectedCount", react_schemas)
            self.assertIn(field, zod_api)
        self.assertIn("getTrialDiagnosisResponse", zod_api)
        self.assertIn("getProductionTrialTuningSchemaResponse", zod_api)

    def test_generated_clients_expose_highlight_boundary_contracts(self) -> None:
        react_schemas = Path("lib/api-client-react/src/generated/api.schemas.ts").read_text(encoding="utf-8")
        zod_api = Path("lib/api-zod/src/generated/api.ts").read_text(encoding="utf-8")
        zod_candidate = Path("lib/api-zod/src/generated/types/eventCandidate.ts").read_text(encoding="utf-8")
        zod_policy = Path("lib/api-zod/src/generated/types/eventCandidateBufferPolicy.ts").read_text(encoding="utf-8")

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

    def test_openapi_exposes_broadcast_hybrid_workflow_contracts(self) -> None:
        document = build_openapi_document()
        paths = document["paths"]
        for path in (
            "/runs/{run_id}/broadcast/config-lineage-reconfirmation",
            "/runs/{run_id}/broadcast/review-evidence",
            "/runs/{run_id}/broadcast/review-evidence/import",
            "/runs/{run_id}/broadcast/review-evidence/{generation_id}",
            "/runs/{run_id}/broadcast/review-windows",
            "/runs/{run_id}/broadcast/review-actions",
            "/runs/{run_id}/broadcast/trajectory-recompute",
            "/runs/{run_id}/broadcast/render",
        ):
            self.assertIn(path, paths)
        self.assertIn("202", paths["/runs/{run_id}/broadcast/trajectory-recompute"]["post"]["responses"])
        self.assertIn("202", paths["/runs/{run_id}/broadcast/render"]["post"]["responses"])
        self.assertIn("202", paths["/runs/{run_id}/broadcast/review-evidence/import"]["post"]["responses"])
        revoke = paths["/runs/{run_id}/broadcast/review-evidence/{generation_id}"]["delete"]
        self.assertIn("200", revoke["responses"])
        queue_parameter = next(parameter for parameter in revoke["parameters"] if parameter["name"] == "queue_sha256")
        self.assertTrue(queue_parameter["required"])

        create_run = document["components"]["schemas"]["CreateRunRequest"]
        for field in (
            "pipeline_mode",
            "quality_profile",
            "calibration_confirmation",
            "max_manual_review_windows",
        ):
            self.assertIn(field, create_run["properties"])
        recompute = document["components"]["schemas"]["BroadcastTrajectoryRecomputeRequest"]
        self.assertIn("review_decisions_sha256", recompute["required"])
        render = document["components"]["schemas"]["BroadcastRenderRequest"]
        self.assertIn("trajectory_generation_id", render["required"])
        action = document["components"]["schemas"]["BroadcastReviewAction"]
        self.assertNotIn("correct_trajectory", action["properties"]["action"]["enum"])
        action_request = document["components"]["schemas"]["BroadcastReviewActionsRequest"]
        self.assertIn("queue_sha256", action_request["required"])
        self.assertEqual("^[0-9a-f]{64}$", action_request["properties"]["queue_sha256"]["pattern"])
        operation_response = document["components"]["schemas"]["BroadcastOperationResponse"]
        self.assertIn("parent_run_id", operation_response["properties"])
        self.assertEqual(
            "#/components/schemas/BroadcastOperationDetails",
            operation_response["properties"]["details"]["$ref"],
        )
        review_windows = document["components"]["schemas"]["BroadcastReviewWindowsResponse"]
        self.assertEqual(
            "#/components/schemas/BroadcastReviewWindow",
            review_windows["properties"]["items"]["items"]["$ref"],
        )
        evidence_import = document["components"]["schemas"]["BroadcastReviewEvidenceImportRequest"]
        self.assertIn("bundle_id", evidence_import["required"])
        self.assertIn("bundle_manifest_sha256", evidence_import["required"])
        lineage_request = document["components"]["schemas"]["BroadcastConfigLineageReconfirmationRequest"]
        for field in (
            "target_run_id",
            "confirmed_config_name",
            "confirmed_text_sha256",
            "expected_observed_raw_sha256",
            "operator_id",
            "reviewer_id",
            "workflow_bindings",
        ):
            self.assertIn(field, lineage_request["required"])
        lineage_response = document["components"]["schemas"]["BroadcastConfigLineageReconfirmationResponse"]
        for field in (
            "generation_id",
            "lineage_generation_id",
            "confirmed_text_sha256",
            "observed_raw_sha256",
            "canonical_snapshot_sha256",
        ):
            self.assertIn(field, lineage_response["required"])
        lineage_blocker = document["components"]["schemas"]["BroadcastConfigLineageBlockerResponse"]
        self.assertEqual(
            {
                "confirmed_config_lineage_reconfirmation_required",
                "config_lineage_snapshot_unsafe",
                "config_lineage_snapshot_mismatch",
                "config_lineage_reconfirmation_conflict",
            },
            set(lineage_blocker["properties"]["blocker_code"]["enum"]),
        )
        lineage_error_response = paths[
            "/runs/{run_id}/broadcast/config-lineage-reconfirmation"
        ]["post"]["responses"]["409"]
        self.assertEqual(
            "#/components/schemas/BroadcastConfigLineageBlockerResponse",
            lineage_error_response["content"]["application/json"]["schema"]["$ref"],
        )
        evidence_state = document["components"]["schemas"]["BroadcastReviewEvidenceStateResponse"]
        self.assertIn("capacity", evidence_state["properties"])
        self.assertEqual(
            "#/components/schemas/BroadcastConfigLineageReconfirmationChallenge",
            evidence_state["properties"]["config_lineage_reconfirmation"]["anyOf"][0]["$ref"],
        )
        lineage_challenge = document["components"]["schemas"]["BroadcastConfigLineageReconfirmationChallenge"]
        for field in (
            "target_run_id",
            "confirmed_config_name",
            "confirmed_text_sha256",
            "expected_observed_raw_sha256",
            "workflow_bindings",
        ):
            self.assertIn(field, lineage_challenge["required"])
        self.assertEqual(
            {
                "not_available",
                "available",
                "queued",
                "copying",
                "validating",
                "committing",
                "ready",
                "failed",
                "cancelled",
                "blocked",
            },
            set(evidence_state["properties"]["status"]["enum"]),
        )
        for field in (
            "stage",
            "progress_percent",
            "blocker_code",
            "error_code",
            "recovery_action",
            "retryable",
            "can_cancel",
        ):
            self.assertIn(field, evidence_state["properties"])
        bundle_summary = document["components"]["schemas"]["BroadcastReviewEvidenceBundleSummary"]
        for field in (
            "required_free_bytes",
            "available_free_bytes",
            "attempt_quota_bytes",
            "capacity_status",
            "retention",
            "provisioner_limits",
        ):
            self.assertIn(field, bundle_summary["properties"])
        run_record = document["components"]["schemas"]["RunRecord"]
        self.assertEqual(
            "#/components/schemas/BroadcastRunState",
            run_record["properties"]["broadcast"]["$ref"],
        )
        operation_status = document["components"]["schemas"]["BroadcastRunState"]["properties"]["operation_status"]
        self.assertIn("reconciling", operation_status["anyOf"][0]["enum"])

        for operation in (
            paths["/runs/{run_id}/artifacts"]["get"],
            paths["/runs/{run_id}/artifacts/{artifact_name}"]["get"],
        ):
            generation_parameter = next(
                parameter for parameter in operation["parameters"] if parameter["name"] == "status_generation"
            )
            self.assertEqual("query", generation_parameter["in"])
            self.assertFalse(generation_parameter["required"])
            self.assertEqual("^[0-9a-f]{64}$", generation_parameter["schema"]["anyOf"][0]["pattern"])
            self.assertIn("409", operation["responses"])

    def test_generated_clients_expose_broadcast_hybrid_workflow_contracts(self) -> None:
        react_api = Path("lib/api-client-react/src/generated/api.ts").read_text(encoding="utf-8")
        react_schemas = Path("lib/api-client-react/src/generated/api.schemas.ts").read_text(encoding="utf-8")
        zod_api = Path("lib/api-zod/src/generated/api.ts").read_text(encoding="utf-8")
        zod_broadcast_state = Path("lib/api-zod/src/generated/types/broadcastRunState.ts").read_text(encoding="utf-8")

        for operation in (
            "reconfirmBroadcastConfigLineage",
            "getBroadcastReviewWindows",
            "submitBroadcastReviewActions",
            "recomputeBroadcastTrajectory",
            "renderBroadcastHybrid",
        ):
            self.assertIn(operation, react_api)
        for schema in (
            "BroadcastCalibrationConfirmation",
            "BroadcastConfigLineageReconfirmationChallenge",
            "BroadcastConfigLineageReconfirmationRequest",
            "BroadcastConfigLineageReconfirmationResponse",
            "BroadcastOperationDetails",
            "BroadcastReviewActionsRequest",
            "BroadcastReviewWindow",
            "BroadcastRunState",
            "BroadcastTrajectoryRecomputeRequest",
            "BroadcastRenderRequest",
        ):
            self.assertIn(schema, react_schemas)
        for operation in (
            "reconfirmBroadcastConfigLineageBody",
            "renderBroadcastHybridBody",
            "submitBroadcastReviewActionsBody",
            "getBroadcastReviewWindowsResponse",
            "recomputeBroadcastTrajectoryBody",
        ):
            self.assertIn(operation, zod_api)
        for filename in (
            "broadcastCalibrationConfirmation.ts",
            "broadcastConfigLineageReconfirmationChallenge.ts",
            "broadcastConfigLineageReconfirmationRequest.ts",
            "broadcastConfigLineageReconfirmationResponse.ts",
            "broadcastOperationDetails.ts",
            "broadcastReviewActionsRequest.ts",
            "broadcastReviewWindow.ts",
            "broadcastRunState.ts",
            "broadcastTrajectoryRecomputeRequest.ts",
            "broadcastRenderRequest.ts",
        ):
            self.assertTrue((Path("lib/api-zod/src/generated/types") / filename).is_file())
        for generated_contract in (react_schemas, zod_api, zod_broadcast_state):
            self.assertIn('"reconciling"', generated_contract)
        self.assertIn("export type ListArtifactsParams", react_schemas)
        self.assertIn("export type GetArtifactParams", react_schemas)
        self.assertGreaterEqual(react_schemas.count("status_generation?: string | null"), 2)
        self.assertIn("params?: ListArtifactsParams", react_api)
        self.assertIn("params?: GetArtifactParams", react_api)
        self.assertIn("status_generation: zod", zod_api)

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
