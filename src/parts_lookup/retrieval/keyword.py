"""Postgres full-text keyword search over the ``pages.tsvector`` column."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from parts_lookup.domain.errors import RetrievalError

_KEYWORD_SQL = text(
    """
    SELECT
        p.id AS page_id,
        ts_rank_cd(p.tsvector, plainto_tsquery('english', :q)) AS score
    FROM pages AS p
    WHERE p.tsvector @@ plainto_tsquery('english', :q)
    ORDER BY score DESC
    LIMIT :top_k
    """
)


async def keyword_search(
    session: AsyncSession,
    query_text: str,
    top_k: int,
) -> list[tuple[int, float]]:
    """Return ``(page_id, ts_rank_cd_score)`` sorted by score descending."""
    try:
        result = await session.execute(
            _KEYWORD_SQL,
            {"q": query_text, "top_k": top_k},
        )
    except Exception as exc:
        raise RetrievalError("Keyword search query failed") from exc

    return [(int(row.page_id), float(row.score)) for row in result]
