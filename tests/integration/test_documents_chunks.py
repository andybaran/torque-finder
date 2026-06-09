# tests/integration/test_documents_chunks.py
"""documents/chunks repository smoke vs the live Postgres (insert + rollback only).

NEVER commits: every test inserts inside a transaction and rolls back, so the
production database is untouched.
"""

from __future__ import annotations

import os
import uuid

import pytest

if not os.environ.get("DATABASE_URL"):
    pytest.skip("DATABASE_URL not set; skipping documents/chunks tests", allow_module_level=True)

pytestmark = pytest.mark.asyncio

_STUB_VEC = [0.0] * 1023 + [1.0]


def _engine():  # type: ignore[no-untyped-def]
    from sqlalchemy.ext.asyncio import create_async_engine

    url = os.environ["DATABASE_URL"]
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return create_async_engine(url, echo=False)


async def test_document_chunk_round_trip_and_module_reconstruction() -> None:
    from sqlalchemy.ext.asyncio import AsyncSession

    from parts_lookup.domain.models import SourceType
    from parts_lookup.indexing.repository import Repository

    engine = _engine()
    ref = f"test-{uuid.uuid4().hex}"
    try:
        async with AsyncSession(engine) as session:
            repo = Repository(session)
            doc = await repo.upsert_document(
                source_type=SourceType.HTML,
                title="Test Pub",
                source_url=f"https://docs.sram.com/en-US/publications/{ref}",
                source_ref=ref,
            )
            assert doc.source_type is SourceType.HTML

            # Same source_ref upserts to the same row (re-ingest path).
            again = await repo.upsert_document(
                source_type=SourceType.HTML,
                title="Test Pub v2",
                source_url=doc.source_url,
                source_ref=ref,
            )
            assert again.id == doc.id
            assert again.title == "Test Pub v2"

            base = doc.source_url
            rows = [
                (1, "Crank Installation", "mod1", "mod1"),
                (2, "Tools: TORX: T25", None, "mod1"),
                (3, "Tighten to 40 N·m (354 in-lb)", "blk1", "mod1"),
                (4, "Unrelated module heading", "mod2", "mod2"),
            ]
            for ordinal, text, anchor, parent in rows:
                await repo.insert_chunk(
                    document_id=doc.id,
                    ordinal=ordinal,
                    text=text,
                    embedding=_STUB_VEC,
                    png_r2_key=None,
                    anchor=anchor,
                    parent_anchor=parent,
                    source_url=f"{base}#{anchor or parent}",
                )

            module_text = await repo.fetch_module_text(doc.id, "mod1")
            assert module_text == (
                "Crank Installation\n\nTools: TORX: T25\n\nTighten to 40 N·m (354 in-lb)"
            )

            await repo.delete_chunks(doc.id)
            assert await repo.fetch_module_text(doc.id, "mod1") == ""

            await session.rollback()
    finally:
        await engine.dispose()


async def test_migration_copy_preserved_pages() -> None:
    """Read-only check that 0004's copy kept every page (skips once 0005 dropped pages)."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession

    engine = _engine()
    try:
        async with AsyncSession(engine) as session:
            exists = (
                await session.execute(text("SELECT to_regclass('public.pages')"))
            ).scalar_one()
            if exists is None:
                pytest.skip("legacy pages table already dropped (post-0005)")
            pages = (await session.execute(text("SELECT count(*) FROM pages"))).scalar_one()
            chunks = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM chunks c "
                        "JOIN documents d ON d.id = c.document_id "
                        "WHERE d.source_type = 'pdf'"
                    )
                )
            ).scalar_one()
            assert pages == chunks
    finally:
        await engine.dispose()
