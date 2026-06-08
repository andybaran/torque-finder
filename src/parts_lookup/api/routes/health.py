"""Liveness / version endpoint."""

from __future__ import annotations

from fastapi import APIRouter

from parts_lookup import __version__
from parts_lookup.api.schemas import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/healthz", response_model=HealthResponse)
async def healthz() -> HealthResponse:
    return HealthResponse(status="ok", version=__version__)
