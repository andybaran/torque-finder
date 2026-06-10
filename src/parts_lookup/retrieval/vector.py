"""pgvector cosine-similarity search over the ``chunks.embedding`` column.

The HNSW index uses ``vector_cosine_ops``, so the ``<=>`` operator returns
cosine *distance*. We convert to similarity (``1 - distance``) so higher
scores mean better matches — same direction as the keyword channel, which
keeps fusion math simple.
"""

from __future__ import annotations

from pgvector.sqlalchemy import Vector
from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from parts_lookup.domain.errors import RetrievalError

_VECTOR_SQL = text(
    """
    SELECT
        c.id AS chunk_id,
        1 - (c.embedding <=> :embedding) AS score
    FROM chunks AS c
    ORDER BY c.embedding <=> :embedding
    LIMIT :top_k
    """
).bindparams(bindparam("embedding", type_=Vector()))


async def vector_search(
    session: AsyncSession,
    query_embedding: list[float],
    top_k: int,
) -> list[tuple[int, float]]:
    """Return ``(chunk_id, score)`` sorted by similarity descending."""
    try:
        result = await session.execute(
            _VECTOR_SQL,
            {"embedding": query_embedding, "top_k": top_k},
        )
    except Exception as exc:
        raise RetrievalError("Vector search query failed") from exc

    return [(int(row.chunk_id), float(row.score)) for row in result]
