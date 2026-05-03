from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from football_tracking.api.dependencies import get_service
from football_tracking.api.schemas import (
    ConfigDetail,
    ConfigListItem,
    DeleteResourceResponse,
    DeriveConfigRequest,
    UpdateConfigRequest,
)
from football_tracking.api.service import ApiService

router = APIRouter()


@router.get("/configs", response_model=list[ConfigListItem])
def list_configs(service: ApiService = Depends(get_service)) -> list[ConfigListItem]:
    return [ConfigListItem(**item) for item in service.list_configs()]


@router.get("/configs/{name:path}", response_model=ConfigDetail)
def get_config(name: str, service: ApiService = Depends(get_service)) -> ConfigDetail:
    try:
        return ConfigDetail(**service.get_config(name))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Config not found: {name}") from exc


@router.put("/configs/{name:path}", response_model=ConfigDetail)
def update_config(name: str, request: UpdateConfigRequest, service: ApiService = Depends(get_service)) -> ConfigDetail:
    try:
        return ConfigDetail(**service.update_config(name, request.content))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Config not found: {name}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/configs/derive", response_model=ConfigDetail)
def derive_config(request: DeriveConfigRequest, service: ApiService = Depends(get_service)) -> ConfigDetail:
    try:
        return ConfigDetail(
            **service.derive_config(
                base_config_name=request.base_config_name,
                output_name=request.output_name,
                patch=request.patch,
            )
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Base config not found: {request.base_config_name}") from exc


@router.delete("/configs", response_model=DeleteResourceResponse)
def delete_config(name: str, service: ApiService = Depends(get_service)) -> DeleteResourceResponse:
    try:
        return DeleteResourceResponse(**service.delete_config(name))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Config not found: {name}") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
