from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status

from football_tracking.api.dependencies import get_service
from football_tracking.api.schemas import (
    BallAnnotationFinalizeRequest,
    BallAnnotationFinalResultResponse,
    BallAnnotationRevisionRequest,
    BallAnnotationRevisionResponse,
    BallAnnotationSessionCreateRequest,
    BallAnnotationSessionResponse,
    BallApiErrorResponse,
    BallPropagationCreateRequest,
    BallPropagationJobResponse,
)
from football_tracking.api.service import ApiService
from football_tracking.detector_development_common import DetectorDevelopmentError

router = APIRouter()

_API_SAFE_ERROR_RESPONSE = {
    "model": BallApiErrorResponse,
    "description": "API-safe error with a stable code and message.",
}

_NO_STORE_HEADER = {
    "description": "This evidence response must not be cached.",
    "schema": {"type": "string", "enum": ["no-store"]},
}

_JSON_NO_STORE_SUCCESS_RESPONSE = {
    "headers": {"Cache-Control": _NO_STORE_HEADER},
}


def _development_error(error: DetectorDevelopmentError) -> HTTPException:
    return HTTPException(
        status_code=error.status_code,
        detail={"code": error.code, "message": str(error)},
    )


@router.post(
    "/ball-annotation-sessions",
    response_model=BallAnnotationSessionResponse,
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="create_ball_annotation_session",
    responses={
        202: {
            **_JSON_NO_STORE_SUCCESS_RESPONSE,
            "description": "Annotation session accepted or replayed idempotently.",
        },
        400: _API_SAFE_ERROR_RESPONSE,
        404: _API_SAFE_ERROR_RESPONSE,
        409: _API_SAFE_ERROR_RESPONSE,
    },
)
def create_ball_annotation_session(
    request: BallAnnotationSessionCreateRequest,
    response: Response,
    service: ApiService = Depends(get_service),
) -> BallAnnotationSessionResponse:
    try:
        result = service.create_ball_annotation_session(request.model_dump(mode="json"))
        response.headers["Cache-Control"] = "no-store"
        return BallAnnotationSessionResponse.model_validate(result)
    except (KeyError, DetectorDevelopmentError) as exc:
        if isinstance(exc, DetectorDevelopmentError):
            raise _development_error(exc) from exc
        raise HTTPException(
            status_code=404,
            detail={
                "code": "development_probe_not_found",
                "message": "Detector probe was not found",
            },
        ) from exc


@router.get(
    "/ball-annotation-sessions/{session_id}",
    response_model=BallAnnotationSessionResponse,
    operation_id="get_ball_annotation_session",
    responses={
        200: {
            **_JSON_NO_STORE_SUCCESS_RESPONSE,
            "description": "Current annotation session state.",
        },
        400: _API_SAFE_ERROR_RESPONSE,
        404: _API_SAFE_ERROR_RESPONSE,
        409: _API_SAFE_ERROR_RESPONSE,
    },
)
def get_ball_annotation_session(
    session_id: str,
    response: Response,
    service: ApiService = Depends(get_service),
) -> BallAnnotationSessionResponse:
    try:
        result = service.get_ball_annotation_session(session_id)
        response.headers["Cache-Control"] = "no-store"
        return BallAnnotationSessionResponse.model_validate(result)
    except DetectorDevelopmentError as exc:
        raise _development_error(exc) from exc


@router.get(
    "/ball-annotation-sessions/{session_id}/frames/{frame_index}",
    response_class=Response,
    operation_id="get_ball_annotation_frame",
    responses={
        200: {
            "description": "Exact immutable source-frame JPEG evidence.",
            "content": {"image/jpeg": {"schema": {"type": "string", "format": "binary"}}},
            "headers": {
                "Content-Length": {
                    "description": "JPEG payload length in bytes.",
                    "schema": {"type": "integer", "minimum": 1},
                },
                "ETag": {
                    "description": "Strong ETag for the exact JPEG bytes.",
                    "schema": {"type": "string"},
                },
                "X-Content-SHA256": {
                    "description": "SHA-256 digest of the exact JPEG bytes.",
                    "schema": {
                        "type": "string",
                        "pattern": "^[0-9a-f]{64}$",
                    },
                },
                "X-Source-Frame-Index": {
                    "description": "Bound source-video frame index.",
                    "schema": {"type": "integer", "minimum": 0},
                },
                "Cache-Control": _NO_STORE_HEADER,
            },
        },
        400: _API_SAFE_ERROR_RESPONSE,
        404: _API_SAFE_ERROR_RESPONSE,
        409: _API_SAFE_ERROR_RESPONSE,
    },
)
def get_ball_annotation_frame(
    session_id: str,
    frame_index: int,
    service: ApiService = Depends(get_service),
) -> Response:
    try:
        content, media_type, digest = service.read_ball_annotation_frame(session_id, frame_index)
    except DetectorDevelopmentError as exc:
        raise _development_error(exc) from exc
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Cache-Control": "no-store",
            "ETag": f'"{digest}"',
            "X-Content-SHA256": digest,
            "X-Source-Frame-Index": str(frame_index),
        },
    )


@router.put(
    "/ball-annotation-sessions/{session_id}/annotations/{frame_index}",
    response_model=BallAnnotationRevisionResponse,
    operation_id="put_ball_annotation",
    openapi_extra={
        "parameters": [
            {
                "name": "If-Match",
                "in": "header",
                "required": True,
                "description": "Strong ETag of the current effective annotation revision. Runtime omission returns 428.",
                "schema": {"type": "string"},
            }
        ]
    },
    responses={
        200: {
            "description": "Append-only annotation revision accepted.",
            "headers": {
                "ETag": {
                    "description": "Strong ETag for the new effective annotation revision.",
                    "schema": {"type": "string"},
                },
                "Cache-Control": _NO_STORE_HEADER,
            },
        },
        400: _API_SAFE_ERROR_RESPONSE,
        404: _API_SAFE_ERROR_RESPONSE,
        409: _API_SAFE_ERROR_RESPONSE,
        412: {
            **_API_SAFE_ERROR_RESPONSE,
            "description": "If-Match or expected_revision is stale.",
        },
        428: {
            **_API_SAFE_ERROR_RESPONSE,
            "description": "A strong If-Match header is required.",
        },
    },
)
def put_ball_annotation(
    session_id: str,
    frame_index: int,
    request: BallAnnotationRevisionRequest,
    response: Response,
    if_match: str | None = Header(
        default=None,
        alias="If-Match",
        description="Strong ETag of the current effective annotation revision.",
        include_in_schema=False,
    ),
    service: ApiService = Depends(get_service),
) -> BallAnnotationRevisionResponse:
    try:
        result = service.put_ball_annotation(
            session_id,
            frame_index,
            request.model_dump(mode="json"),
            if_match=if_match,
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["ETag"] = f'"{result["annotation_etag"]}"'
        return BallAnnotationRevisionResponse.model_validate(result)
    except DetectorDevelopmentError as exc:
        raise _development_error(exc) from exc


@router.post(
    "/ball-annotation-sessions/{session_id}/propagation-jobs",
    response_model=BallPropagationJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="create_ball_propagation_job",
    openapi_extra={
        "parameters": [
            {
                "name": "If-Match",
                "in": "header",
                "required": True,
                "description": "Strong ETag of the confirmed seed annotation revision. Runtime omission returns 428.",
                "schema": {"type": "string"},
            }
        ]
    },
    responses={
        202: {
            **_JSON_NO_STORE_SUCCESS_RESPONSE,
            "description": "Bound propagation job accepted or replayed idempotently.",
        },
        400: _API_SAFE_ERROR_RESPONSE,
        404: _API_SAFE_ERROR_RESPONSE,
        409: _API_SAFE_ERROR_RESPONSE,
        412: {
            **_API_SAFE_ERROR_RESPONSE,
            "description": "If-Match or expected_seed_revision is stale.",
        },
        428: {
            **_API_SAFE_ERROR_RESPONSE,
            "description": "A strong If-Match header is required.",
        },
    },
)
def create_ball_propagation_job(
    session_id: str,
    request: BallPropagationCreateRequest,
    response: Response,
    if_match: str | None = Header(
        default=None,
        alias="If-Match",
        description="Strong ETag of the confirmed seed annotation revision.",
        include_in_schema=False,
    ),
    service: ApiService = Depends(get_service),
) -> BallPropagationJobResponse:
    try:
        result = service.create_ball_propagation_job(
            session_id,
            request.model_dump(mode="json"),
            if_match=if_match,
        )
        response.headers["Cache-Control"] = "no-store"
        return BallPropagationJobResponse.model_validate(result)
    except DetectorDevelopmentError as exc:
        raise _development_error(exc) from exc


@router.get(
    "/ball-annotation-sessions/{session_id}/propagation-jobs/{job_id}",
    response_model=BallPropagationJobResponse,
    operation_id="get_ball_propagation_job",
    responses={
        200: {
            **_JSON_NO_STORE_SUCCESS_RESPONSE,
            "description": "Current propagation job state.",
        },
        400: _API_SAFE_ERROR_RESPONSE,
        404: _API_SAFE_ERROR_RESPONSE,
        409: _API_SAFE_ERROR_RESPONSE,
    },
)
def get_ball_propagation_job(
    session_id: str,
    job_id: str,
    response: Response,
    service: ApiService = Depends(get_service),
) -> BallPropagationJobResponse:
    try:
        result = service.get_ball_propagation_job(session_id, job_id)
        response.headers["Cache-Control"] = "no-store"
        return BallPropagationJobResponse.model_validate(result)
    except DetectorDevelopmentError as exc:
        raise _development_error(exc) from exc


@router.post(
    "/ball-annotation-sessions/{session_id}/propagation-jobs/{job_id}/cancel",
    response_model=BallPropagationJobResponse,
    operation_id="cancel_ball_propagation_job",
    responses={
        200: {
            **_JSON_NO_STORE_SUCCESS_RESPONSE,
            "description": "Propagation cancellation state.",
        },
        400: _API_SAFE_ERROR_RESPONSE,
        404: _API_SAFE_ERROR_RESPONSE,
        409: _API_SAFE_ERROR_RESPONSE,
    },
)
def cancel_ball_propagation_job(
    session_id: str,
    job_id: str,
    response: Response,
    service: ApiService = Depends(get_service),
) -> BallPropagationJobResponse:
    try:
        result = service.cancel_ball_propagation_job(session_id, job_id)
        response.headers["Cache-Control"] = "no-store"
        return BallPropagationJobResponse.model_validate(result)
    except DetectorDevelopmentError as exc:
        raise _development_error(exc) from exc


@router.post(
    "/ball-annotation-sessions/{session_id}/finalize",
    response_model=BallAnnotationFinalResultResponse,
    operation_id="finalize_ball_annotation_session",
    responses={
        200: {
            **_JSON_NO_STORE_SUCCESS_RESPONSE,
            "description": "Immutable annotation package and feasibility report.",
        },
        400: _API_SAFE_ERROR_RESPONSE,
        404: _API_SAFE_ERROR_RESPONSE,
        409: _API_SAFE_ERROR_RESPONSE,
    },
)
def finalize_ball_annotation_session(
    session_id: str,
    request: BallAnnotationFinalizeRequest,
    response: Response,
    service: ApiService = Depends(get_service),
) -> BallAnnotationFinalResultResponse:
    try:
        result = service.finalize_ball_annotation_session(session_id, request.mutation_id)
        response.headers["Cache-Control"] = "no-store"
        return BallAnnotationFinalResultResponse.model_validate(result)
    except DetectorDevelopmentError as exc:
        raise _development_error(exc) from exc


@router.get(
    "/ball-annotation-sessions/{session_id}/result",
    response_model=BallAnnotationFinalResultResponse,
    operation_id="get_ball_annotation_result",
    responses={
        200: {
            **_JSON_NO_STORE_SUCCESS_RESPONSE,
            "description": "Verified immutable final result.",
        },
        400: _API_SAFE_ERROR_RESPONSE,
        404: _API_SAFE_ERROR_RESPONSE,
        409: {
            **_API_SAFE_ERROR_RESPONSE,
            "description": "The immutable final result is not ready.",
        },
    },
)
def get_ball_annotation_result(
    session_id: str,
    response: Response,
    service: ApiService = Depends(get_service),
) -> BallAnnotationFinalResultResponse:
    try:
        result = service.get_ball_annotation_result(session_id)
        response.headers["Cache-Control"] = "no-store"
        return BallAnnotationFinalResultResponse.model_validate(result)
    except DetectorDevelopmentError as exc:
        raise _development_error(exc) from exc
