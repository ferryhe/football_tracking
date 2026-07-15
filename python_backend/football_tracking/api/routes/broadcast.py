from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from football_tracking.api.dependencies import get_service
from football_tracking.api.schemas import (
    BroadcastOperationResponse,
    BroadcastRenderRequest,
    BroadcastReviewActionsRequest,
    BroadcastReviewWindowsResponse,
    BroadcastTerminalTailReviewRequest,
    BroadcastTrajectoryRecomputeRequest,
)
from football_tracking.api.service import ApiService

router = APIRouter()


@router.get("/runs/{run_id}/broadcast/review-windows", response_model=BroadcastReviewWindowsResponse)
def get_broadcast_review_windows(
    run_id: str,
    service: ApiService = Depends(get_service),
) -> BroadcastReviewWindowsResponse:
    try:
        return BroadcastReviewWindowsResponse(**service.get_broadcast_review_windows(run_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/runs/{run_id}/broadcast/review-actions",
    response_model=BroadcastOperationResponse,
    responses={404: {"description": "Run not found"}, 409: {"description": "Evidence or run state conflict"}},
)
def submit_broadcast_review_actions(
    run_id: str,
    request: BroadcastReviewActionsRequest,
    service: ApiService = Depends(get_service),
) -> BroadcastOperationResponse:
    try:
        return BroadcastOperationResponse(
            **service.submit_broadcast_review_actions(run_id, request.model_dump(mode="json"))
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/runs/{run_id}/broadcast/terminal-tail-review",
    response_model=BroadcastOperationResponse,
    responses={404: {"description": "Run not found"}, 409: {"description": "Evidence or run state conflict"}},
)
def submit_broadcast_terminal_tail_review(
    run_id: str,
    request: BroadcastTerminalTailReviewRequest,
    service: ApiService = Depends(get_service),
) -> BroadcastOperationResponse:
    try:
        return BroadcastOperationResponse(
            **service.submit_broadcast_terminal_tail_review(run_id, request.model_dump(mode="json"))
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/runs/{run_id}/broadcast/trajectory-recompute",
    response_model=BroadcastOperationResponse,
    status_code=202,
    responses={404: {"description": "Run not found"}, 409: {"description": "Evidence or run state conflict"}},
)
def recompute_broadcast_trajectory(
    run_id: str,
    request: BroadcastTrajectoryRecomputeRequest,
    service: ApiService = Depends(get_service),
) -> BroadcastOperationResponse:
    try:
        return BroadcastOperationResponse(
            **service.recompute_broadcast_trajectory(run_id, request.model_dump(mode="json"))
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/runs/{run_id}/broadcast/render",
    response_model=BroadcastOperationResponse,
    status_code=202,
    responses={404: {"description": "Run not found"}, 409: {"description": "Evidence or run state conflict"}},
)
def render_broadcast_hybrid(
    run_id: str,
    request: BroadcastRenderRequest,
    service: ApiService = Depends(get_service),
) -> BroadcastOperationResponse:
    try:
        return BroadcastOperationResponse(**service.render_broadcast_hybrid(run_id, request.model_dump(mode="json")))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
