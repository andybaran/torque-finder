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


async def test_hybrid_search_returns_mixed_source_chunks() -> None:
    """PDF and HTML chunks rank in one fused list (insert + rollback, stub embedder)."""
    from sqlalchemy.ext.asyncio import AsyncSession

    from parts_lookup.config import Settings
    from parts_lookup.domain.models import SourceType
    from parts_lookup.indexing.repository import Repository
    from parts_lookup.retrieval.embedder import VoyageEmbedder
    from parts_lookup.retrieval.hybrid import hybrid_search

    settings = Settings(
        database_url=os.environ["DATABASE_URL"],
        stub_external_apis=True,
        _env_file=None,
    )
    embedder = VoyageEmbedder(settings)
    marker = f"zzqxv{uuid.uuid4().hex[:8]}"  # token unique to our rows

    engine = _engine()
    try:
        async with AsyncSession(engine) as session:
            repo = Repository(session)
            texts = [
                f"{marker} crank arm bolt torque 40 N-m pdf page",
                f"{marker} crank arm bolt torque 40 N-m html block",
            ]
            vecs = await embedder.embed_documents(texts)

            pdf_doc = await repo.upsert_document(
                source_type=SourceType.PDF,
                title="mixed-test.pdf",
                source_url="pdfs/mixedtest.pdf",
                source_ref=f"mixed-pdf-{uuid.uuid4().hex}",
            )
            await repo.insert_chunk(
                document_id=pdf_doc.id, ordinal=1, text=texts[0], embedding=vecs[0],
                png_r2_key="pages/mixedtest/0001.png",
                source_url="pdfs/mixedtest.pdf#page=1",
            )
            html_doc = await repo.upsert_document(
                source_type=SourceType.HTML,
                title="Mixed Test Pub",
                source_url="https://docs.sram.com/en-US/publications/mixedtest",
                source_ref=f"mixed-html-{uuid.uuid4().hex}",
            )
            await repo.insert_chunk(
                document_id=html_doc.id, ordinal=1, text=texts[1], embedding=vecs[1],
                anchor="blk1", parent_anchor="mod1",
                source_url="https://docs.sram.com/en-US/publications/mixedtest#blk1",
            )

            hits = await hybrid_search(
                session, embedder, f"{marker} crank arm bolt torque", top_k=10
            )
            ours = [h for h in hits if marker in h.text]
            assert {h.source_type for h in ours} == {SourceType.PDF, SourceType.HTML}
            html_hit = next(h for h in ours if h.source_type is SourceType.HTML)
            assert html_hit.parent_anchor == "mod1"
            assert html_hit.source_url.endswith("#blk1")
            assert html_hit.png_r2_key is None

            await session.rollback()
    finally:
        await engine.dispose()
