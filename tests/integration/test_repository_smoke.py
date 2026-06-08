"""Smoke test for the indexing Repository against a live Postgres.

Skips entirely if ``DATABASE_URL`` is not set, so the suite still collects
cleanly on a fresh checkout.
"""

from __future__ import annotations

import hashlib
import os

import pytest

# Skip the whole module if there's no Postgres URL available.
if not os.environ.get("DATABASE_URL"):
    pytest.skip("DATABASE_URL not set; skipping repository smoke test", allow_module_level=True)

# Skip if the indexing module isn't importable yet (parallel agent may not have merged).
pytest.importorskip("parts_lookup.indexing.repository")
pytest.importorskip("parts_lookup.indexing.session")
pytest.importorskip("sqlalchemy.ext.asyncio")


pytestmark = pytest.mark.asyncio


async def test_pdf_round_trip() -> None:
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from parts_lookup.indexing.repository import Repository

    database_url = os.environ["DATABASE_URL"]
    # asyncpg driver is required for AsyncEngine.
    if database_url.startswith("postgresql://"):
        database_url = database_url.replace("postgresql://", "postgresql+asyncpg://", 1)

    engine = create_async_engine(database_url, echo=False)
    sha = hashlib.sha256(b"smoke-test-fixture").hexdigest()

    try:
        async with AsyncSession(engine) as session:
            repo = Repository(session)
            inserted = await repo.upsert_pdf(
                filename="smoke.pdf",
                sha256=sha,
                r2_key="pdfs/smoke.pdf",
                page_count=1,
            )
            assert inserted.sha256 == sha

            fetched = await repo.get_pdf_by_sha256(sha)
            assert fetched is not None
            assert fetched.id == inserted.id
            assert fetched.filename == "smoke.pdf"

            # Don't pollute the test DB with leftover rows.
            await session.rollback()
    finally:
        await engine.dispose()
