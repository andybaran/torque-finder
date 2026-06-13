"""Liveness / readiness / version endpoints.

``/healthz`` is a static liveness probe: it answers 200 as long as the process
is up, regardless of downstream health (used by the platform to decide whether
to restart the container). ``/readyz`` is a *deep* readiness probe reflecting
recent upstream (Anthropic) success, so an uptime monitor stops reporting green
during a full answer-service outage — without spending a vision call per probe.
"""

from __future__ import annotations

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse

from parts_lookup import __version__
from parts_lookup.api.dependencies import ExtractorDep
from parts_lookup.api.schemas import HealthResponse, ReadinessResponse

router = APIRouter(tags=["health"])


@router.get("/healthz", response_model=HealthResponse)
async def healthz() -> HealthResponse:
    return HealthResponse(status="ok", version=__version__)


@router.get("/readyz", response_model=ReadinessResponse)
async def readyz(extractor: ExtractorDep) -> JSONResponse:
    """Report whether the answer service is ready to serve queries.

    Returns the cached outcome of the most recent extraction upstream call —
    no live API call is made, so probing is free. The signal can lag a
    just-started outage by one request window (acceptable for an uptime
    monitor). Returns 503 when the upstream was unhealthy on the last call so
    monitors and load balancers can drain this instance.
    """
    healthy = extractor.upstream_healthy
    body = ReadinessResponse(
        ready=healthy,
        extraction_upstream_healthy=healthy,
        version=__version__,
    )
    code = (
        status.HTTP_200_OK
        if healthy
        else status.HTTP_503_SERVICE_UNAVAILABLE
    )
    return JSONResponse(status_code=code, content=body.model_dump())
