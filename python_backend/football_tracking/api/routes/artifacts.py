from __future__ import annotations

import mimetypes
import os
import weakref
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import MutableHeaders

from football_tracking.api.dependencies import get_service
from football_tracking.api.schemas import (
    AIReviewTriggerReport,
    ApiErrorResponse,
    ArtifactSummary,
    BallAuditReport,
    CameraPathResponse,
    EventCandidateReport,
    PlayerTracksReport,
)
from football_tracking.api.service import ApiService

router = APIRouter()


class _LeasedFileResponse(FileResponse):
    """Range-capable response that never reopens a validated artifact by pathname."""

    def __init__(self, lease: Any, *, media_type: str | None, filename: str) -> None:
        self._lease = lease
        self._lease_finalizer = weakref.finalize(self, lease.close)
        super().__init__(
            lease.path,
            media_type=media_type,
            filename=filename,
            stat_result=os.fstat(lease.handle.fileno()),
        )

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            if self._lease_finalizer.alive:
                self._lease_finalizer()

    async def _seek(self, offset: int) -> None:
        await run_in_threadpool(self._lease.handle.seek, offset)

    async def _read(self, size: int) -> bytes:
        return await run_in_threadpool(self._lease.handle.read, size)

    async def _handle_simple(self, send: Any, send_header_only: bool, send_pathsend: bool) -> None:
        del send_pathsend
        await send({"type": "http.response.start", "status": self.status_code, "headers": self.raw_headers})
        if send_header_only:
            await send({"type": "http.response.body", "body": b"", "more_body": False})
            return
        await self._seek(0)
        remaining = self.stat_result.st_size if self.stat_result is not None else 0
        if remaining == 0:
            await send({"type": "http.response.body", "body": b"", "more_body": False})
            return
        while remaining:
            chunk = await self._read(min(self.chunk_size, remaining))
            if not chunk:
                raise RuntimeError("leased artifact ended before the declared content length")
            remaining -= len(chunk)
            await send({"type": "http.response.body", "body": chunk, "more_body": remaining > 0})

    async def _handle_single_range(
        self,
        send: Any,
        start: int,
        end: int,
        file_size: int,
        send_header_only: bool,
    ) -> None:
        headers = MutableHeaders(raw=list(self.raw_headers))
        headers["content-range"] = f"bytes {start}-{end - 1}/{file_size}"
        headers["content-length"] = str(end - start)
        await send({"type": "http.response.start", "status": 206, "headers": headers.raw})
        if send_header_only:
            await send({"type": "http.response.body", "body": b"", "more_body": False})
            return
        await self._seek(start)
        while start < end:
            chunk = await self._read(min(self.chunk_size, end - start))
            if not chunk:
                raise RuntimeError("leased artifact ended before the declared range")
            start += len(chunk)
            await send({"type": "http.response.body", "body": chunk, "more_body": start < end})

    async def _handle_multiple_ranges(
        self,
        send: Any,
        ranges: list[tuple[int, int]],
        file_size: int,
        send_header_only: bool,
    ) -> None:
        boundary = os.urandom(13).hex()
        content_length, header_generator = self.generate_multipart(
            ranges,
            boundary,
            file_size,
            self.headers["content-type"],
        )
        headers = MutableHeaders(raw=list(self.raw_headers))
        headers["content-type"] = f"multipart/byteranges; boundary={boundary}"
        headers["content-length"] = str(content_length)
        await send({"type": "http.response.start", "status": 206, "headers": headers.raw})
        if send_header_only:
            await send({"type": "http.response.body", "body": b"", "more_body": False})
            return
        for start, end in ranges:
            await send({"type": "http.response.body", "body": header_generator(start, end), "more_body": True})
            await self._seek(start)
            while start < end:
                chunk = await self._read(min(self.chunk_size, end - start))
                if not chunk:
                    raise RuntimeError("leased artifact ended before the declared range")
                start += len(chunk)
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
            await send({"type": "http.response.body", "body": b"\r\n", "more_body": True})
        await send(
            {
                "type": "http.response.body",
                "body": f"--{boundary}--".encode("latin-1"),
                "more_body": False,
            }
        )


@router.get("/runs/{run_id}/artifacts", response_model=list[ArtifactSummary])
def list_artifacts(run_id: str, service: ApiService = Depends(get_service)) -> list[ArtifactSummary]:
    try:
        return [ArtifactSummary(**item) for item in service.list_artifacts(run_id)]
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}") from exc


@router.get("/runs/{run_id}/artifacts/{artifact_name:path}")
@router.head("/runs/{run_id}/artifacts/{artifact_name:path}", include_in_schema=False)
def get_artifact(run_id: str, artifact_name: str, service: ApiService = Depends(get_service)) -> FileResponse:
    try:
        lease = service.acquire_artifact_response_lease(run_id, artifact_name)
        media_type, _ = mimetypes.guess_type(artifact_name)
        try:
            return _LeasedFileResponse(
                lease,
                media_type=media_type,
                filename=artifact_name.rsplit("/", 1)[-1],
            )
        except BaseException:
            lease.close()
            raise
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}") from exc
    except (FileNotFoundError, OSError, RuntimeError) as exc:
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


@router.get(
    "/runs/{run_id}/ball-audit",
    response_model=BallAuditReport,
    responses={404: {"model": ApiErrorResponse, "description": "Run or ball audit report not found"}},
)
def get_ball_audit_report(run_id: str, service: ApiService = Depends(get_service)) -> BallAuditReport:
    try:
        return BallAuditReport(**service.get_ball_audit_report(run_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}") from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="ball_audit.json not found") from exc


@router.get(
    "/runs/{run_id}/ai-review-triggers",
    response_model=AIReviewTriggerReport,
    responses={404: {"model": ApiErrorResponse, "description": "Run or AI review trigger report not found"}},
)
def get_ai_review_triggers_report(
    run_id: str,
    service: ApiService = Depends(get_service),
) -> AIReviewTriggerReport:
    try:
        return AIReviewTriggerReport(**service.get_ai_review_triggers_report(run_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}") from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="ai_review_triggers.json not found") from exc


@router.get(
    "/runs/{run_id}/event-candidates",
    response_model=EventCandidateReport,
    responses={404: {"model": ApiErrorResponse, "description": "Run or event candidate report not found"}},
)
def get_event_candidates_report(
    run_id: str,
    service: ApiService = Depends(get_service),
) -> EventCandidateReport:
    try:
        return EventCandidateReport(**service.get_event_candidates_report(run_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}") from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="event_candidates.json not found") from exc


@router.get(
    "/runs/{run_id}/player-tracks",
    response_model=PlayerTracksReport,
    responses={404: {"model": ApiErrorResponse, "description": "Run or player tracks report not found"}},
)
def get_player_tracks_report(
    run_id: str,
    service: ApiService = Depends(get_service),
) -> PlayerTracksReport:
    try:
        return PlayerTracksReport(**service.get_player_tracks_report(run_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}") from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="player_tracks.json not found") from exc


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
