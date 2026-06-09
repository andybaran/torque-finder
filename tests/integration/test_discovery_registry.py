# tests/integration/test_discovery_registry.py
from __future__ import annotations

import os
import uuid

import pytest

if not os.environ.get("DATABASE_URL"):
    pytest.skip("DATABASE_URL not set; skipping registry test", allow_module_level=True)

pytest.importorskip("parts_lookup.discovery.registry")

pytestmark = pytest.mark.asyncio


async def test_upsert_insert_then_change_detection():
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from parts_lookup.domain.models import DiscoveredPublication
    from parts_lookup.discovery.registry import PublicationRegistry

    url = os.environ["DATABASE_URL"]
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)

    pub_id = f"test-{uuid.uuid4().hex[:12]}"
    pub = DiscoveredPublication(
        pub_id=pub_id,
        pub_type="UM",
        title="Test",
        locale="en-US",
        source_url=f"https://docs.sram.com/en-US/publications/{pub_id}",
        series=("red-axs",),
        models=("ed-red-e1",),
        procedures=("installation-axs",),
        content_hash="hash-v1",
    )

    engine = create_async_engine(url, echo=False)
    try:
        async with AsyncSession(engine) as session:
            reg = PublicationRegistry(session)

            assert await reg.upsert(pub, ["ed-red-e1"]) == "inserted"
            assert await reg.upsert(pub, ["ed-red-e1"]) == "unchanged"

            changed = pub.model_copy(update={"content_hash": "hash-v2"})
            assert await reg.upsert(changed, ["cn-red-e1"]) == "stale"

            row = await reg.get(pub_id)
            assert row is not None
            assert row.status == "stale"
            assert set(row.referenced_by_models) == {"ed-red-e1", "cn-red-e1"}

            # Clean up so the test DB isn't polluted.
            await session.delete(row)
            await session.commit()
    finally:
        await engine.dispose()
