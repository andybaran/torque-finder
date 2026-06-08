"""Async SQLAlchemy engine + session wiring.

The engine is created once per ``Settings`` instance (same lifetime as the
process); sessions are short-lived and acquired per request. ``get_session``
is the FastAPI dependency entry point.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from parts_lookup.config import Settings, get_settings


@lru_cache(maxsize=1)
def _engine_for(database_url: str) -> AsyncEngine:
    # Cache by URL string (the only thing that determines connectivity) so
    # repeated factory calls within a process share a single pool.
    return create_async_engine(
        database_url,
        pool_pre_ping=True,
        future=True,
    )


def async_session_factory(settings: Settings) -> async_sessionmaker[AsyncSession]:
    """Build (or reuse) an ``async_sessionmaker`` bound to ``settings.database_url``."""
    engine = _engine_for(settings.database_url)
    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: yields a session and commits on success.

    Rolls back on exception so the request handler doesn't have to. The
    transactional boundary is the HTTP request.
    """
    factory = async_session_factory(get_settings())
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
