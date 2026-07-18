from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status

from football_tracking.api.dependencies import get_service
from football_tracking.api.schemas import (
    DetectorModelCatalogResponse,
    DetectorModelImportRequest,
    DetectorModelImportResponse,
    DetectorProbeCreateRequest,
    DetectorProbeCreateResponse,
    DetectorProbeJobResponse,
)
from football_tracking.api.service import ApiService
from football_tracking.detector_development_common import DetectorDevelopmentError

router = APIRouter()


def _development_error(error: DetectorDevelopmentError) -> HTTPException:
    return HTTPException(
        status_code=error.status_code,
        detail={"code": error.code, "message": str(error)},
    )


@router.get("/detector-models", response_model=DetectorModelCatalogResponse)
def list_detector_models(
    service: ApiService = Depends(get_service),
) -> DetectorModelCatalogResponse:
    try:
        return DetectorModelCatalogResponse.model_validate(service.list_detector_models())
    except DetectorDevelopmentError as exc:
        raise _development_error(exc) from exc


@router.post("/detector-models/import", response_model=DetectorModelImportResponse)
def import_detector_model(
    request: DetectorModelImportRequest,
    service: ApiService = Depends(get_service),
) -> DetectorModelImportResponse:
    try:
        result = service.import_detector_model(request.model_dump(mode="json"))
        return DetectorModelImportResponse.model_validate(result)
    except DetectorDevelopmentError as exc:
        raise _development_error(exc) from exc


@router.post(
    "/detector-probes",
    response_model=DetectorProbeCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_detector_probe(
    request: DetectorProbeCreateRequest,
    service: ApiService = Depends(get_service),
) -> DetectorProbeCreateResponse:
    try:
        result = service.create_detector_probe(request.model_dump(mode="json"))
        return DetectorProbeCreateResponse.model_validate(
            {
                key: result.get(key)
                for key in (
                    "job_id",
                    "request_sha256",
                    "status",
                    "status_url",
                    "cancel_url",
                    "retry_from_job_id",
                )
            }
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Parent production trial was not found") from exc
    except DetectorDevelopmentError as exc:
        raise _development_error(exc) from exc


@router.get("/detector-probes/{job_id}", response_model=DetectorProbeJobResponse)
def get_detector_probe(
    job_id: str,
    service: ApiService = Depends(get_service),
) -> DetectorProbeJobResponse:
    try:
        return DetectorProbeJobResponse.model_validate(service.get_detector_probe(job_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Detector probe was not found") from exc
    except DetectorDevelopmentError as exc:
        raise _development_error(exc) from exc


@router.post(
    "/detector-probes/{job_id}/cancel",
    response_model=DetectorProbeJobResponse,
)
def cancel_detector_probe(
    job_id: str,
    service: ApiService = Depends(get_service),
) -> DetectorProbeJobResponse:
    try:
        return DetectorProbeJobResponse.model_validate(service.cancel_detector_probe(job_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Detector probe was not found") from exc
    except DetectorDevelopmentError as exc:
        raise _development_error(exc) from exc


@router.get("/detector-probes/{job_id}/artifacts/{artifact_id}")
def get_detector_probe_artifact(
    job_id: str,
    artifact_id: str,
    service: ApiService = Depends(get_service),
) -> Response:
    try:
        content, media_type, digest = service.read_detector_probe_artifact(job_id, artifact_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Detector probe was not found") from exc
    except DetectorDevelopmentError as exc:
        raise _development_error(exc) from exc
    except (FileNotFoundError, OSError) as exc:
        raise HTTPException(status_code=404, detail="Detector probe artifact was not found") from exc
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Cache-Control": "no-store",
            "ETag": f'"{digest}"',
            "X-Content-SHA256": digest,
        },
    )
