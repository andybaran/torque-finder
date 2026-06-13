"""Hybrid retrieval: keyword + vector search fused via Reciprocal Rank Fusion.

RRF is a rank-only fusion (raw scores from the two channels are not
comparable: ts_rank_cd is unbounded, cosine similarity is in [-1, 1]).
For each chunk that appears in either channel we sum ``1 / (k + rank)`` over
the channels it appears in, where ``rank`` is 1-indexed.

We over-fetch ``DEFAULT_TOP_K_PER_CHANNEL`` from each channel before fusion
so a chunk that's strong in one channel and weak in the other still has a
chance to win the fused ranking.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from parts_lookup.config import Settings
from parts_lookup.domain.errors import RetrievalError
from parts_lookup.domain.models import (
    Query,
    RetrievalSource,
    RetrievedChunk,
    SourceType,
)
from parts_lookup.retrieval.embedder import VoyageEmbedder
from parts_lookup.retrieval.keyword import keyword_search
from parts_lookup.retrieval.vector import vector_search

RRF_K: int = 60
DEFAULT_TOP_K_PER_CHANNEL: int = 20

# --- Stage-3 title rerank (#29) tuning knobs --------------------------------
# When the query names a year/model ("2012 Monarch", "Vivid Air", "Lyrik")
# that appears in a candidate's document_title, nudge that candidate up so the
# *named* doc out-ranks identical-spec siblings that happen to fuse higher.
#
# Calibration is anchored to the RRF score scale. With RRF_K=60 the gap
# between adjacent single-channel fused ranks is 1/(60+r) - 1/(60+r+1): near
# the region where the named doc lands (issue #29 reports fused rank ~4-10)
# that is ~2.1e-4 to ~2.4e-4. The named doc routinely sits a FEW such adjacent
# gaps below an identical-spec sibling cluster, so:
#   * TITLE_TOKEN_BOOST (per matched token) clears ~2 adjacent-rank gaps, so a
#     single year/model match reliably breaks a real near-tie; and
#   * TITLE_BOOST_CAP bounds the total to ~6 adjacent-rank gaps, so a title
#     echoing many query tokens still cannot leapfrog a candidate that leads
#     by a LARGE RRF margin (different channel, much higher rank). The boost
#     only reorders a near-equal cluster — it never overrides strong evidence.
TITLE_TOKEN_BOOST: float = 5.0e-4
TITLE_BOOST_CAP: float = 1.5e-3

# Tokens too generic to disambiguate a year/model — boosting on them would
# reward noise, so they're dropped from the overlap set on both sides.
_RERANK_STOPWORDS: frozenset[str] = frozenset(
    {
        "the", "a", "an", "and", "or", "of", "for", "to", "on", "in", "with",
        "my", "what", "how", "do", "does", "i", "is", "it", "should", "user",
        "manual", "guide", "torque", "spec", "specs", "bolt", "size", "tool",
    }
)
_TOKEN_RE = re.compile(r"[a-z0-9]+")


_LOAD_CHUNKS_SQL = text(
    """
    SELECT
        c.id            AS chunk_id,
        c.document_id   AS document_id,
        c.ordinal       AS ordinal,
        c.text          AS text,
        c.png_r2_key    AS png_r2_key,
        c.anchor        AS anchor,
        c.parent_anchor AS parent_anchor,
        c.source_url    AS source_url,
        d.source_type   AS source_type,
        d.title         AS document_title,
        d.source_url    AS document_source_url
    FROM chunks    AS c
    JOIN documents AS d ON d.id = c.document_id
    WHERE c.id IN :chunk_ids
    """
).bindparams(bindparam("chunk_ids", expanding=True))


@dataclass(frozen=True)
class _FusedHit:
    chunk_id: int
    score: float


def _reciprocal_rank_fusion(
    *channels: list[tuple[int, float]],
    k: int = RRF_K,
) -> list[_FusedHit]:
    """Fuse multiple ranked lists into one. Higher fused score = better."""
    fused: dict[int, float] = {}
    for channel in channels:
        for rank, (chunk_id, _raw_score) in enumerate(channel, start=1):
            fused[chunk_id] = fused.get(chunk_id, 0.0) + 1.0 / (k + rank)
    return sorted(
        (_FusedHit(chunk_id=cid, score=score) for cid, score in fused.items()),
        key=lambda h: h.score,
        reverse=True,
    )


def _tokens(s: str) -> set[str]:
    """Lowercase, split on non-alphanumerics, drop generic stopwords.

    Years stay (e.g. "2012"), model names stay ("monarch", "lyrik"); the
    common filler that every manual title and query shares is dropped so the
    overlap signal reflects the *named* year/model, not boilerplate.
    """
    return {t for t in _TOKEN_RE.findall(s.lower()) if t not in _RERANK_STOPWORDS}


def _title_boost(query_tokens: set[str], document_title: str) -> float:
    """Bounded, additive boost for query/title token overlap.

    ``TITLE_TOKEN_BOOST`` per shared token, capped at ``TITLE_BOOST_CAP``.
    Pure function — no I/O — so it is unit-testable in isolation (#29 Test 2)
    and triggers no new-infra interview.
    """
    if not query_tokens:
        return 0.0
    overlap = len(query_tokens & _tokens(document_title))
    return min(overlap * TITLE_TOKEN_BOOST, TITLE_BOOST_CAP)


def _rerank_by_title(
    query_text: str,
    fused: list[_FusedHit],
    titles: dict[int, str],
) -> list[_FusedHit]:
    """Re-sort fused hits, adding a bounded title-overlap boost to each score.

    Operates on the already-hydrated set (``titles`` keyed by chunk_id) so a
    named year/model out-ranks an identical-spec sibling that fused slightly
    higher — without ever leapfrogging a candidate far enough ahead that the
    capped boost can't close the gap. ``score`` is left untouched on the
    returned hits; only ordering changes. The sort is stable, so equal
    boosted scores keep their RRF order (deterministic).
    """
    query_tokens = _tokens(query_text)
    if not query_tokens:
        return fused

    def boosted(hit: _FusedHit) -> float:
        return hit.score + _title_boost(query_tokens, titles.get(hit.chunk_id, ""))

    return sorted(fused, key=boosted, reverse=True)


async def hybrid_search(
    session: AsyncSession,
    embedder: VoyageEmbedder,
    query_text: str,
    top_k: int,
    *,
    per_channel_k: int = DEFAULT_TOP_K_PER_CHANNEL,
) -> list[RetrievedChunk]:
    """Run keyword + vector sequentially on the shared session, fuse with RRF, hydrate winners.

    The two channels are *not* run via ``asyncio.gather``: SQLAlchemy's
    ``AsyncSession`` does not allow concurrent operations on a single
    session (it only owns one DB connection at a time). End-to-end latency
    is dominated by the Voyage embedding call anyway, and each chunk query
    is single-digit-ms at LAN RTT to Postgres, so sequential is fine; if it
    ever isn't, the right move is two sessions, not gathered ops on one.
    """
    query_embedding = await embedder.embed_query(query_text)

    try:
        keyword_hits = await keyword_search(session, query_text, per_channel_k)
        vector_hits = await vector_search(session, query_embedding, per_channel_k)
    except RetrievalError:
        raise
    except Exception as exc:
        raise RetrievalError("Hybrid retrieval channel failed") from exc

    # Hydrate the WHOLE over-fetched pool (not just the top_k) so the title
    # rerank below has document_title for every candidate and can promote a
    # named doc that fused at rank top_k+1..per_channel_k into the cut (#29).
    fused = _reciprocal_rank_fusion(keyword_hits, vector_hits)
    if not fused:
        return []

    try:
        result = await session.execute(
            _LOAD_CHUNKS_SQL, {"chunk_ids": [hit.chunk_id for hit in fused]}
        )
    except Exception as exc:
        raise RetrievalError("Failed to load fused chunk rows") from exc

    rows = {int(row.chunk_id): row for row in result}

    # Stage-3 bounded title rerank, THEN cut to top_k. The boost only breaks
    # near-ties (see _title_boost), so a far-ahead candidate is never displaced.
    titles = {cid: str(row.document_title) for cid, row in rows.items()}
    fused = _rerank_by_title(query_text, fused, titles)[:top_k]

    retrieved: list[RetrievedChunk] = []
    for hit in fused:
        row = rows.get(hit.chunk_id)
        if row is None:
            continue
        retrieved.append(
            RetrievedChunk(
                chunk_id=int(row.chunk_id),
                document_id=int(row.document_id),
                source_type=SourceType(row.source_type),
                document_title=str(row.document_title),
                document_source_url=str(row.document_source_url),
                ordinal=int(row.ordinal),
                text=str(row.text),
                png_r2_key=None if row.png_r2_key is None else str(row.png_r2_key),
                anchor=None if row.anchor is None else str(row.anchor),
                parent_anchor=None if row.parent_anchor is None else str(row.parent_anchor),
                source_url=str(row.source_url),
                score=hit.score,
                source=RetrievalSource.HYBRID,
            )
        )
    return retrieved


class RetrievalService:
    """Application-layer entry point for hybrid retrieval.

    The API layer constructs one of these at startup (with a configured
    :class:`VoyageEmbedder`) and calls :meth:`search` per request, passing
    in a request-scoped :class:`AsyncSession`.
    """

    def __init__(self, embedder: VoyageEmbedder) -> None:
        self._embedder = embedder

    async def search(
        self,
        session: AsyncSession,
        query: Query,
    ) -> list[RetrievedChunk]:
        return await hybrid_search(
            session=session,
            embedder=self._embedder,
            query_text=query.text,
            top_k=query.top_k,
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> RetrievalService:
        return cls(embedder=VoyageEmbedder(settings))
