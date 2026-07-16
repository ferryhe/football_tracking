from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

from football_tracking.api.dependencies import get_service
from football_tracking.api.schemas import (
    BroadcastConfigLineageBlockerResponse,
    BroadcastConfigLineageReconfirmationRequest,
    BroadcastConfigLineageReconfirmationResponse,
    BroadcastOperationResponse,
    BroadcastRenderRequest,
    BroadcastReviewActionsRequest,
    BroadcastReviewEvidenceImportRequest,
    BroadcastReviewEvidenceRevokeResponse,
    BroadcastReviewEvidenceStateResponse,
    BroadcastReviewWindowsResponse,
    BroadcastTerminalTailReviewRequest,
    BroadcastTrajectoryRecomputeRequest,
)
from football_tracking.api.service import ApiService
from football_tracking.config_lineage import ConfigLineageError

router = APIRouter()


@router.post(
    "/runs/{run_id}/broadcast/config-lineage-reconfirmation",
    response_model=BroadcastConfigLineageReconfirmationResponse,
    responses={
        404: {"description": "Run not found"},
        409: {
            "model": BroadcastConfigLineageBlockerResponse,
            "description": "Stable config-lineage blocker",
        },
    },
)
def reconfirm_broadcast_config_lineage(
    run_id: str,
    request: BroadcastConfigLineageReconfirmationRequest,
    service: ApiService = Depends(get_service),
) -> BroadcastConfigLineageReconfirmationResponse | JSONResponse:
    try:
        return BroadcastConfigLineageReconfirmationResponse(
            **service.reconfirm_broadcast_config_lineage(run_id, request.model_dump(mode="json"))
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}") from exc
    except ConfigLineageError as exc:
        return JSONResponse(
            status_code=409,
            content={
                "status": "blocked",
                "blocker_code": exc.code,
                "detail": str(exc),
                "retryable": False,
            },
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get(
    "/runs/{run_id}/broadcast/review-evidence",
    response_model=BroadcastReviewEvidenceStateResponse,
    responses={404: {"description": "Run not found"}, 409: {"description": "Run evidence state conflict"}},
)
def get_broadcast_review_evidence(
    run_id: str,
    service: ApiService = Depends(get_service),
) -> BroadcastReviewEvidenceStateResponse:
    try:
        return BroadcastReviewEvidenceStateResponse(**service.get_broadcast_review_evidence(run_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/runs/{run_id}/broadcast/review-evidence/import",
    response_model=BroadcastOperationResponse,
    status_code=202,
    responses={404: {"description": "Run not found"}, 409: {"description": "Evidence or run state conflict"}},
)
def import_broadcast_review_evidence(
    run_id: str,
    request: BroadcastReviewEvidenceImportRequest,
    service: ApiService = Depends(get_service),
) -> BroadcastOperationResponse:
    try:
        return BroadcastOperationResponse(
            **service.import_broadcast_review_evidence(run_id, request.model_dump(mode="json"))
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.delete(
    "/runs/{run_id}/broadcast/review-evidence/{generation_id}",
    response_model=BroadcastReviewEvidenceRevokeResponse,
    responses={404: {"description": "Run not found"}, 409: {"description": "Evidence already consumed or changed"}},
)
def revoke_broadcast_review_evidence(
    run_id: str,
    generation_id: str,
    queue_sha256: str = Query(pattern=r"^[0-9a-f]{64}$"),
    service: ApiService = Depends(get_service),
) -> BroadcastReviewEvidenceRevokeResponse:
    try:
        return BroadcastReviewEvidenceRevokeResponse(
            **service.revoke_broadcast_review_evidence(run_id, generation_id, queue_sha256)
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


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
