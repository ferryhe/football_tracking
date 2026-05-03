from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from football_tracking.api.dependencies import get_service
from football_tracking.api.schemas import (
    AIConfigDiffRequest,
    AIConfigDiffResponse,
    AIExplainRequest,
    AIExplainResponse,
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


@router.post("/ai/config-diff", response_model=AIConfigDiffResponse)
def config_diff(request: AIConfigDiffRequest, service: ApiService = Depends(get_service)) -> AIConfigDiffResponse:
    return AIConfigDiffResponse(
        **service.ai_config_diff(
            base_config_name=request.base_config_name,
            patch=request.patch,
            output_name=request.output_name,
        )
    )
