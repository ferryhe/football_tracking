from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse

from football_tracking.api.dependencies import get_service
from football_tracking.api.schemas import ArtifactSummary, CameraPathResponse
from football_tracking.api.service import ApiService

router = APIRouter()


@router.get("/runs/{run_id}/artifacts", response_model=list[ArtifactSummary])
def list_artifacts(run_id: str, service: ApiService = Depends(get_service)) -> list[ArtifactSummary]:
    try:
        return [ArtifactSummary(**item) for item in service.list_artifacts(run_id)]
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}") from exc


@router.get("/runs/{run_id}/artifacts/{artifact_name:path}")
def get_artifact(run_id: str, artifact_name: str, service: ApiService = Depends(get_service)) -> FileResponse:
    try:
        path = service.get_artifact_path(run_id, artifact_name)
        media_type = next(
            (item.get("content_type") for item in service.list_artifacts(run_id) if item.get("name") == path.name),
            None,
        )
        return FileResponse(path, media_type=media_type, filename=path.name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}") from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Artifact not found: {artifact_name}") from exc


@router.get("/runs/{run_id}/cleanup-report")
def get_cleanup_report(run_id: str, service: ApiService = Depends(get_service)) -> JSONResponse:
    try:
        return JSONResponse(content=service.get_cleanup_report(run_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}") from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="cleanup_report.json not found") from exc


@router.get("/runs/{run_id}/follow-cam-report")
def get_follow_cam_report(run_id: str, service: ApiService = Depends(get_service)) -> JSONResponse:
    try:
        return JSONResponse(content=service.get_follow_cam_report(run_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}") from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="follow_cam_report.json not found") from exc


@router.get("/runs/{run_id}/camera-path", response_model=CameraPathResponse)
def get_camera_path(
    run_id: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(500, ge=1, le=5000),
    service: ApiService = Depends(get_service),
) -> CameraPathResponse:
    try:
        return CameraPathResponse(**service.get_camera_path(run_id, offset=offset, limit=limit))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}") from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="camera_path.csv not found") from exc
