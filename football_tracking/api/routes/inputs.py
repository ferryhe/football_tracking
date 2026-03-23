from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from football_tracking.api.dependencies import get_service
from football_tracking.api.schemas import (
    DeleteResourceResponse,
    FieldPreviewRequest,
    FieldPreviewResponse,
    FieldSuggestionRequest,
    FieldSuggestionResponse,
    InputCatalogResponse,
)
from football_tracking.api.service import ApiService

router = APIRouter()


@router.get("/inputs", response_model=InputCatalogResponse)
def list_input_videos(service: ApiService = Depends(get_service)) -> InputCatalogResponse:
    return InputCatalogResponse(**service.list_input_videos())


@router.post("/inputs/field-preview", response_model=FieldPreviewResponse)
def capture_field_preview(request: FieldPreviewRequest, service: ApiService = Depends(get_service)) -> FieldPreviewResponse:
    try:
        return FieldPreviewResponse(**service.capture_field_preview(request.input_video, sample_index=request.sample_index))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/inputs/field-suggestion", response_model=FieldSuggestionResponse)
def suggest_field_setup(request: FieldSuggestionRequest, service: ApiService = Depends(get_service)) -> FieldSuggestionResponse:
    try:
        return FieldSuggestionResponse(
            **service.suggest_field_setup(
                request.input_video,
                config_name=request.config_name,
                frame_index=request.frame_index,
            )
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/inputs", response_model=DeleteResourceResponse)
def delete_input_video(name: str, service: ApiService = Depends(get_service)) -> DeleteResourceResponse:
    try:
        return DeleteResourceResponse(**service.delete_input_video(name))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
