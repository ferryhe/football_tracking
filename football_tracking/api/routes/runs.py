from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from football_tracking.api.dependencies import get_service
from football_tracking.api.schemas import CreateRunRequest, RunRecord
from football_tracking.api.service import ApiService

router = APIRouter()


@router.get("/runs", response_model=list[RunRecord])
def list_runs(service: ApiService = Depends(get_service)) -> list[RunRecord]:
    return [RunRecord(**item) for item in service.list_runs()]


@router.get("/runs/{run_id}", response_model=RunRecord)
def get_run(run_id: str, service: ApiService = Depends(get_service)) -> RunRecord:
    try:
        return RunRecord(**service.get_run(run_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}") from exc


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
