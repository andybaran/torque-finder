"""FastAPI application factory.

The app is built on demand by ``create_app()`` so tests can spin up isolated
instances. The lifespan event constructs the per-process services
(retrieval, extraction, R2) once and exposes them through ``app.state``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from parts_lookup import __version__
from parts_lookup.api.routes import health, query
from parts_lookup.assets.r2_client import R2Client
from parts_lookup.config import Settings, get_settings
from parts_lookup.extraction.claude_client import ClaudeExtractor
from parts_lookup.observability import get_logger, setup_observability
from parts_lookup.retrieval.hybrid import RetrievalService

logger = get_logger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    setup_observability(settings)

    app.state.retrieval_service = RetrievalService.from_settings(settings)
    app.state.claude_extractor = ClaudeExtractor(settings)
    app.state.r2_client = R2Client(settings)

    logger.info(
        "app.startup",
        env=settings.app_env,
        model=settings.anthropic_model,
        bucket=settings.r2_bucket,
    )
    try:
        yield
    finally:
        logger.info("app.shutdown")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    app = FastAPI(
        title="parts-lookup",
        version=__version__,
        description=(
            "Natural-language lookup over manufacturer manuals (PDF and "
            "digital/HTML) for bicycle-shop mechanics. Returns a structured "
            "answer plus a source deep link, and a screenshot URL for PDF "
            "sources."
        ),
        lifespan=_lifespan,
    )
    app.state.settings = settings

    app.include_router(health.router)
    app.include_router(query.router)

    return app
