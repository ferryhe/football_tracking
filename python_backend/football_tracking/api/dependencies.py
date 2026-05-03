from __future__ import annotations

from fastapi import Request

from football_tracking.api.service import ApiService


def get_service(request: Request) -> ApiService:
    return request.app.state.api_service
