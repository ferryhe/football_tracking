from __future__ import annotations

from fastapi import APIRouter, Depends

from football_tracking.api.dependencies import get_service
from football_tracking.api.schemas import InputCatalogResponse
from football_tracking.api.service import ApiService

router = APIRouter()


@router.get("/inputs", response_model=InputCatalogResponse)
def list_input_videos(service: ApiService = Depends(get_service)) -> InputCatalogResponse:
    return InputCatalogResponse(**service.list_input_videos())
