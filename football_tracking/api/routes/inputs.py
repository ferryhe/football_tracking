from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from football_tracking.api.dependencies import get_service
from football_tracking.api.schemas import FieldSuggestionRequest, FieldSuggestionResponse, InputCatalogResponse
from football_tracking.api.service import ApiService

router = APIRouter()


@router.get("/inputs", response_model=InputCatalogResponse)
def list_input_videos(service: ApiService = Depends(get_service)) -> InputCatalogResponse:
    return InputCatalogResponse(**service.list_input_videos())


@router.post("/inputs/field-suggestion", response_model=FieldSuggestionResponse)
def suggest_field_setup(request: FieldSuggestionRequest, service: ApiService = Depends(get_service)) -> FieldSuggestionResponse:
    try:
        return FieldSuggestionResponse(**service.suggest_field_setup(request.input_video))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
