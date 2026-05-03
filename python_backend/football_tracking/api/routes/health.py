from __future__ import annotations

from fastapi import APIRouter, Depends

from football_tracking.api.dependencies import get_service
from football_tracking.api.schemas import HealthResponse
from football_tracking.api.service import ApiService

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
def get_health(service: ApiService = Depends(get_service)) -> HealthResponse:
    return HealthResponse(**service.health_summary())
