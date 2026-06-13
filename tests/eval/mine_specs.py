"""Maintenance miner: regenerate the frozen source-grounded ground-truth set.

Refactored from ``docs/quality-eval/mine_specs.py`` + ``r2_mine.py``. Those
throwaway scripts hand-parsed ``.env`` and used raw ``asyncpg``, bypassing the
indexing bounded context. This version reads the live ``chunks`` store ONLY
through ``Repository.scan_chunks_matching`` (SQLAlchemy), respecting the DDD
boundary (CLAUDE.md), and re-emits the candidate set as JSON for human curation
into ``tests/eval/ground_truth_sampled.py``.

This is NOT part of the gate — it is a manual tool run when the corpus changes:

    set -a && . ./.env && set +a
    uv run python -m tests.eval.mine_specs > /tmp/candidates.json

It needs ``DATABASE_URL`` (live) but no Anthropic/Voyage keys and makes no paid
calls. The output is candidates for review; promoting them into the frozen
snapshot (and authoring questions) is a deliberate human edit so the eval set
never silently changes underfoot.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

# Same POSIX torque regex the original miners used (N·m / Nm / in-lb / ft-lb).
TORQUE_PATTERN = "\\m\\d+([.,]\\d+)?\\s*(n[\u00b7.\u2011\\-]?m|nm|in[\\-. ]?lb|ft[\\-. ]?lb)\\M"


def _database_url() -> str:
    url = os.environ["DATABASE_URL"]
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


async def mine(limit: int = 200, per_document_limit: int = 4) -> list[dict[str, object]]:
    """Scan the live index for torque-bearing chunks via the Repository layer."""
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from parts_lookup.indexing.repository import Repository

    engine = create_async_engine(_database_url(), echo=False)
    try:
        async with AsyncSession(engine) as session:
            repo = Repository(session)
            chunks = await repo.scan_chunks_matching(
                TORQUE_PATTERN,
                exclude_title_ilike="%catalog%",
                per_document_limit=per_document_limit,
                limit=limit,
            )
    finally:
        await engine.dispose()

    return [
        {
            "chunk_id": c.chunk_id,
            "document_id": c.document_id,
            "document_title": c.document_title,
            "source_type": c.source_type.value,
            "ordinal": c.ordinal,
            "has_png": c.has_png,
            "text": c.text[:750],
        }
        for c in chunks
    ]


def main() -> None:
    candidates = asyncio.run(mine())
    json.dump(candidates, sys.stdout, indent=1)
    distinct_docs = len({c["document_id"] for c in candidates})
    print(
        f"\n# mined {len(candidates)} candidates across {distinct_docs} distinct documents",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
