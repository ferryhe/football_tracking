from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from football_tracking.api.dependencies import get_service
from football_tracking.api.schemas import (
    AssetGroup,
    CreateRunRequest,
    DeleteResourceResponse,
    FollowCamRenderRequest,
    RunRecord,
)
from football_tracking.api.service import ApiService

router = APIRouter()


@router.get("/runs", response_model=list[RunRecord])
def list_runs(service: ApiService = Depends(get_service)) -> list[RunRecord]:
    return [RunRecord(**item) for item in service.list_runs()]


@router.get("/runs/asset-groups", response_model=list[AssetGroup])
def list_asset_groups(service: ApiService = Depends(get_service)) -> list[AssetGroup]:
    return [AssetGroup(**item) for item in service.list_asset_groups()]


@router.get("/runs/{run_id}", response_model=RunRecord)
def get_run(run_id: str, service: ApiService = Depends(get_service)) -> RunRecord:
    try:
        return RunRecord(**service.get_run(run_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}") from exc


@router.delete("/runs", response_model=DeleteResourceResponse)
def delete_run_output(run_id: str, service: ApiService = Depends(get_service)) -> DeleteResourceResponse:
    try:
        return DeleteResourceResponse(**service.delete_run_output(run_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/runs", response_model=RunRecord, status_code=202)
def create_run(request: CreateRunRequest, service: ApiService = Depends(get_service)) -> RunRecord:
    try:
        return RunRecord(**service.create_run(request.model_dump()))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=f"Output dir already exists: {exc}") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/runs/{run_id}/cancel", response_model=RunRecord)
def cancel_run(run_id: str, service: ApiService = Depends(get_service)) -> RunRecord:
    try:
        return RunRecord(**service.cancel_run(run_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/runs/{run_id}/follow-cam-render", response_model=RunRecord, status_code=202)
def create_follow_cam_render(
    run_id: str,
    request: FollowCamRenderRequest,
    service: ApiService = Depends(get_service),
) -> RunRecord:
    try:
        return RunRecord(**service.create_follow_cam_render(run_id, request.model_dump()))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}") from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=f"Output dir already exists: {exc}") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
