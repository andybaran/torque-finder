"""FastAPI dependency providers.

These are the seams the API layer uses to acquire the per-process services
(retrieval, extraction, R2). Constructed once during app lifespan and exposed
via ``request.app.state``; the dependency functions just hand them out.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from parts_lookup.assets.r2_client import R2Client
from parts_lookup.config import Settings, get_settings
from parts_lookup.extraction.claude_client import ClaudeExtractor
from parts_lookup.indexing.session import get_session
from parts_lookup.retrieval.hybrid import RetrievalService


def get_retrieval_service(request: Request) -> RetrievalService:
    return request.app.state.retrieval_service  # type: ignore[no-any-return]


def get_claude_extractor(request: Request) -> ClaudeExtractor:
    return request.app.state.claude_extractor  # type: ignore[no-any-return]


def get_r2_client(request: Request) -> R2Client:
    return request.app.state.r2_client  # type: ignore[no-any-return]


SettingsDep = Annotated[Settings, Depends(get_settings)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]
RetrievalDep = Annotated[RetrievalService, Depends(get_retrieval_service)]
ExtractorDep = Annotated[ClaudeExtractor, Depends(get_claude_extractor)]
R2Dep = Annotated[R2Client, Depends(get_r2_client)]
