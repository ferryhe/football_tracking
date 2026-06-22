from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from football_tracking.api.dependencies import get_service
from football_tracking.api.schemas import (
    AIConfigDiffRequest,
    AIConfigDiffResponse,
    AIExplainRequest,
    AIExplainResponse,
    AIImproveApprovalRequest,
    AIImproveApprovalResponse,
    AIImproveRequest,
    AIImproveResponse,
    AIRecommendRequest,
    AISuggestion,
)
from football_tracking.api.service import ApiService

router = APIRouter()


@router.post("/ai/explain", response_model=AIExplainResponse)
def explain(request: AIExplainRequest, service: ApiService = Depends(get_service)) -> AIExplainResponse:
    try:
        return AIExplainResponse(
            **service.ai_explain(
                run_id=request.run_id,
                config_name=request.config_name,
                focus=request.focus,
                language=request.language,
            )
        )
    except (KeyError, FileNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/ai/recommend", response_model=AISuggestion)
def recommend(request: AIRecommendRequest, service: ApiService = Depends(get_service)) -> AISuggestion:
    try:
        return AISuggestion(
            **service.ai_recommend(
                run_id=request.run_id,
                objective=request.objective,
                language=request.language,
            )
        )
    except (KeyError, FileNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/ai/improve", response_model=AIImproveResponse)
def improve(request: AIImproveRequest, service: ApiService = Depends(get_service)) -> AIImproveResponse:
    try:
        return AIImproveResponse(
            **service.ai_improve(
                run_id=request.run_id,
                objective=request.objective,
                model=request.model,
                dry_run=request.dry_run,
                max_items=request.max_items,
                language=request.language,
            )
        )
    except (KeyError, FileNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/ai/improve/{run_id}/approve", response_model=AIImproveApprovalResponse)
def approve_improvements(
    run_id: str,
    request: AIImproveApprovalRequest,
    service: ApiService = Depends(get_service),
) -> AIImproveApprovalResponse:
    try:
        return AIImproveApprovalResponse(
            **service.ai_improvement_approve(
                run_id=run_id,
                improvement_ids=request.improvement_ids,
                approved_by=request.approved_by,
                rerun_scope_overrides={
                    key: value.model_dump(mode="json") for key, value in request.rerun_scope_overrides.items()
                },
                local_search_roi_overrides={
                    key: value.model_dump(mode="json") for key, value in request.local_search_roi_overrides.items()
                },
                config_patch_overrides=request.config_patch_overrides,
                suggested_window_overrides={
                    key: value.model_dump(mode="json") for key, value in request.suggested_window_overrides.items()
                },
                clip_action_overrides=request.clip_action_overrides,
                follow_cam_rerender_plan_overrides=request.follow_cam_rerender_plan_overrides,
            )
        )
    except (KeyError, FileNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/ai/config-diff", response_model=AIConfigDiffResponse)
def config_diff(request: AIConfigDiffRequest, service: ApiService = Depends(get_service)) -> AIConfigDiffResponse:
    return AIConfigDiffResponse(
        **service.ai_config_diff(
            base_config_name=request.base_config_name,
            patch=request.patch,
            output_name=request.output_name,
        )
    )
