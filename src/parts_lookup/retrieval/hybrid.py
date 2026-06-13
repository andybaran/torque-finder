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
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from parts_lookup.config import Settings
from parts_lookup.domain.errors import RetrievalError
from parts_lookup.domain.models import (
    ProductScope,
    Query,
    RetrievalSource,
    RetrievedChunk,
    SourceType,
)
from parts_lookup.retrieval.embedder import VoyageEmbedder
from parts_lookup.retrieval.keyword import keyword_search
from parts_lookup.retrieval.product_match import extract_query_scope, scope_matches
from parts_lookup.retrieval.vector import vector_search

RRF_K: int = 60
DEFAULT_TOP_K_PER_CHANNEL: int = 20

# --- Within-doc page expansion (#30) defaults --------------------------------
# Used only when hybrid_search is called without explicit settings (tests,
# legacy callers). The real values flow from config.Settings.
DEFAULT_PAGE_WINDOW: int = 1
DEFAULT_MAX_CANDIDATES: int = 6

# --- Within-doc rerank (#30, Step F) tuning knobs ----------------------------
# Among the leader document's expanded candidates, nudge up the page whose text
# carries BOTH a torque value (an "N·m" string) AND a token from the question's
# fastener/component noun phrase. This is a deterministic precision pass — NO
# model-based reranker (that decision is deferred to and owned by #28).
#
# Calibration mirrors the title-rerank discipline (#29): the bonus is sized to
# break only NEAR-TIES *within the leader document*. With RRF_K=60 the gap
# between adjacent single-channel fused ranks near the region the answer page
# lands (~rank 4-10) is ~2.1e-4 to ~2.4e-4. RERANK_BONUS clears ~2 such gaps,
# so it reliably reorders a real near-tie but, being a single fixed increment,
# can NEVER lift a regex-only page above a higher-fused page that lacks the
# fastener token (non-demotion invariant — see _rerank_within_doc and the unit
# test). The boost only reorders; it never crosses the base/neighbor budget
# boundary (eviction keeps all base pages first regardless).
RERANK_BONUS: float = 5.0e-4

# Torque value shape: "40 N-m", "5.5 N·m", "11 Nm", "9 N.m" (notation varies by
# manual). Case-insensitive; the optional separator covers ·, ., - or a space.
_TORQUE_RE = re.compile(r"\d+(?:\.\d+)?\s*N[·.\-]?\s?m", re.IGNORECASE)

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

# --- Product-aware boost (#28) tuning knobs ----------------------------------
# Contamination fix: when the query names a recognizable product (via the
# shared deterministic extract_query_scope), nudge candidates whose owning
# document's product FACET matches the asked product UP, and nudge documents
# whose product is a confirmed DIFFERENT in-corpus product DOWN. This reorders
# the fused set toward the right product before the top_k cut + page expansion,
# attacking the "a different in-corpus product out-ranked the asked one" defect.
#
# Calibration is anchored to the same RRF-gap scale as the title rerank. The
# boost is sized to break the near-tie clusters of identical-spec sibling
# manuals (fused rank ~4-10, adjacent gap ~2.1e-4 to ~2.4e-4): PRODUCT_MATCH_BOOST
# clears ~2 adjacent gaps so a matching product reliably wins a near-tie, while
# PRODUCT_MISMATCH_PENALTY pushes a confirmed wrong-product sibling a similar
# distance down. Both are BOUNDED single increments on the RRF scale — they
# reorder near-equal clusters but can NEVER swamp a candidate that leads by a
# large RRF margin (different channel / much higher rank), so a genuinely strong
# hit is never displaced by the facet alone. This is a precision pass, not a
# hard filter: a missing/NULL facet (the FAIL-SAFE default) is a no-op, so a
# mis-derived or absent product can't zero out recall. #32's abstention guard
# remains the backstop for residual mismatch; this only improves ordering.
PRODUCT_MATCH_BOOST: float = 5.0e-4
PRODUCT_MISMATCH_PENALTY: float = 5.0e-4

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
        d.source_url    AS document_source_url,
        d.product_family AS product_family,
        d.brand         AS brand
    FROM chunks    AS c
    JOIN documents AS d ON d.id = c.document_id
    WHERE c.id IN :chunk_ids
    """
).bindparams(bindparam("chunk_ids", expanding=True))


# Within-doc page-neighbor lookup (#30): the leader document's pages in a
# contiguous ordinal window [lo, hi]. Same projection as _LOAD_CHUNKS_SQL so
# hydration is uniform. UniqueConstraint("document_id","ordinal") guarantees at
# most one chunk per (doc, ordinal), so this returns ≤ (hi-lo+1) rows.
_LOAD_DOC_NEIGHBORS_SQL = text(
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
        d.source_url    AS document_source_url,
        d.product_family AS product_family,
        d.brand         AS brand
    FROM chunks    AS c
    JOIN documents AS d ON d.id = c.document_id
    WHERE c.document_id = :doc_id
      AND c.ordinal BETWEEN :lo AND :hi
    """
)


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


# --- Product-aware boost (#28) -----------------------------------------------


@dataclass(frozen=True)
class _ProductMeta:
    """The per-chunk product facet the boost needs (pure value, no ORM)."""

    family: str | None
    brand: str | None


def _product_delta(asked: ProductScope, candidate_facet: _ProductMeta) -> float:
    """Bounded score delta for one candidate given the asked product scope.

    * Candidate product MATCHES the asked scope → ``+PRODUCT_MATCH_BOOST``.
    * Candidate product is a CONFIRMED DIFFERENT in-corpus product (identified
      facet that does not match) → ``-PRODUCT_MISMATCH_PENALTY``.
    * Candidate has NO derived product (NULL facet — the FAIL-SAFE default) →
      ``0.0``: never penalize a product-blind document, so a missing/mis-derived
      facet can't drop a real answer out of the cut.
    """
    candidate_scope = ProductScope(
        family=candidate_facet.family,
        brand=candidate_facet.brand,
        # Confidence only gates persistence/derivation upstream; by here the
        # facet is already the trusted stored value. Mark it identified so
        # scope_matches compares on family/brand, matching #32's live usage.
        confidence=0.9 if candidate_facet.family is not None else 0.6,
    )
    if not candidate_scope.is_identified:
        return 0.0
    if scope_matches(asked, candidate_scope):
        return PRODUCT_MATCH_BOOST
    return -PRODUCT_MISMATCH_PENALTY


def _rerank_by_product(
    query_text: str,
    fused: list[_FusedHit],
    facets: dict[int, _ProductMeta],
) -> list[_FusedHit]:
    """Re-sort fused hits toward the asked product (#28 contamination fix).

    Degrade-safe: if ``extract_query_scope`` cannot confidently identify a
    product in the query (unidentified scope), this is a NO-OP — the fused order
    is returned unchanged, so an under-specified question never has its recall
    harmed. When a product IS named, each hit's bounded ``_product_delta`` is
    added before re-sorting (``score`` is left untouched; only ordering moves).
    The sort is stable, so equal boosted scores keep their prior order.

    Coordination with #32: this only reorders candidates so the right product
    surfaces first; #32's abstention guard is the separate backstop that
    declines to answer when no in-corpus product matches at all.
    """
    asked = extract_query_scope(query_text)
    if not asked.is_identified:
        return fused

    def boosted(hit: _FusedHit) -> float:
        facet = facets.get(hit.chunk_id)
        if facet is None:
            return hit.score
        return hit.score + _product_delta(asked, facet)

    return sorted(fused, key=boosted, reverse=True)


# --- Within-doc page expansion (#30) -----------------------------------------


@dataclass(frozen=True)
class _ChunkMeta:
    """The minimum per-chunk facts the expansion algorithm needs.

    Pure value object (no ORM, no I/O) so Steps B-F are offline-testable: the
    leader-doc selection, neighbor merge, eviction, and within-doc rerank all
    operate on these, not on hydrated rows.
    """

    chunk_id: int
    document_id: int
    ordinal: int
    text: str
    is_pdf: bool


def _select_leader_chunk(fused: list[_FusedHit]) -> _FusedHit | None:
    """The single highest-fused chunk — the expansion anchor (Step B).

    Exactly one chunk (hence one document) is expanded — no cross-doc
    aggregation, no "(s)". The tiebreak among equal scores is made explicit:
    higher score, then lower ``chunk_id`` (stable and deterministic regardless
    of the order ``fused`` happens to arrive in). Returns ``None`` only for an
    empty list.
    """
    if not fused:
        return None
    return min(fused, key=lambda h: (-h.score, h.chunk_id))


def _rerank_within_doc(
    query_text: str,
    fused: list[_FusedHit],
    meta: dict[int, _ChunkMeta],
    leader_doc_id: int,
) -> list[_FusedHit]:
    """Deterministic within-leader-doc precision pass (Step F).

    Adds a single fixed ``RERANK_BONUS`` to a leader-doc candidate whose text
    matches BOTH a torque value (``_TORQUE_RE``) AND a token from the question's
    fastener/component noun phrase (token overlap, generic stopwords dropped).

    Non-demotion invariant (unit-tested): because the bonus is one fixed small
    increment sized to clear only ~2 adjacent RRF-rank gaps, it can break a real
    near-tie *within the same document* but can NEVER lift a regex-only page
    above a higher-fused page that lacks the fastener token, nor demote a
    higher-fused page below a lower-fused page that merely contains an "N·m"
    string. ``score`` is left untouched; only ordering changes. The sort is
    stable, so equal boosted scores keep their prior order (deterministic).
    """
    query_tokens = _tokens(query_text)
    if not query_tokens:
        return fused

    def matches_fastener_spec(m: _ChunkMeta) -> bool:
        if not _TORQUE_RE.search(m.text):
            return False
        return bool(query_tokens & _tokens(m.text))

    def boosted(hit: _FusedHit) -> float:
        m = meta.get(hit.chunk_id)
        if m is None or m.document_id != leader_doc_id or not m.is_pdf:
            return hit.score
        return hit.score + (RERANK_BONUS if matches_fastener_spec(m) else 0.0)

    return sorted(fused, key=boosted, reverse=True)


def _assemble_candidates(
    base: list[_FusedHit],
    neighbors: list[_ChunkMeta],
    anchor_ordinal: int,
    anchor_score: float,
    max_candidates: int,
) -> list[_FusedHit]:
    """Merge base winners + leader-doc neighbors into one budgeted list (Steps D-E).

    Deterministic and pure so the ``source_index`` contract is unit-testable.

    - ``base`` is the fused-winner list in fused order. Its pages are NEVER
      reordered and NEVER evicted — that stability is what keeps
      ``retrieved[source_index - 1]`` resolving to the same chunk after
      expansion (``query.py`` L108 strict-zip + L122 index).
    - Neighbors not already in ``base`` are appended, ordered by ascending
      ``|ordinal - anchor|`` then ascending ``ordinal`` (closest-to-anchor,
      lowest page first). A neighbor already present in ``base`` keeps its base
      position (dedup by ``chunk_id``).
    - The merged list is capped at ``max_candidates``. Base pages fill first; if
      base alone exceeds the budget it is truncated from the bottom of fused
      order (only possible when top_k > max_candidates). Remaining room goes to
      neighbors in the order above; neighbors that don't fit are dropped
      (farthest-from-anchor dropped first).

    Injected neighbors carry no fused score of their own (they were in neither
    channel). They are given a display score just below ``anchor_score`` so the
    user-facing ordering is sane; the actual candidate order is fixed by append
    position, not re-sorted by this score.
    """
    kept: list[_FusedHit] = base[:max_candidates]
    if len(kept) >= max_candidates:
        return kept

    present = {hit.chunk_id for hit in kept}
    ordered_neighbors = sorted(
        (n for n in neighbors if n.chunk_id not in present),
        key=lambda n: (abs(n.ordinal - anchor_ordinal), n.ordinal),
    )
    for rank, n in enumerate(ordered_neighbors, start=1):
        if len(kept) >= max_candidates:
            break
        # Strictly below the anchor, and monotonically decreasing with distance,
        # so a presentation layer that sorts by score keeps closest-first order.
        kept.append(_FusedHit(chunk_id=n.chunk_id, score=anchor_score - rank * RERANK_BONUS))
    return kept


def _row_to_retrieved(row: Any, score: float) -> RetrievedChunk:
    """Hydrate one SQL row (from either chunk-load query) into a domain object."""
    return RetrievedChunk(
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
        score=score,
        source=RetrievalSource.HYBRID,
        product_family=None if row.product_family is None else str(row.product_family),
        brand=None if row.brand is None else str(row.brand),
    )


async def hybrid_search(
    session: AsyncSession,
    embedder: VoyageEmbedder,
    query_text: str,
    top_k: int,
    *,
    per_channel_k: int = DEFAULT_TOP_K_PER_CHANNEL,
    page_window: int = DEFAULT_PAGE_WINDOW,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
) -> list[RetrievedChunk]:
    """Run keyword + vector sequentially on the shared session, fuse with RRF, hydrate winners.

    The two channels are *not* run via ``asyncio.gather``: SQLAlchemy's
    ``AsyncSession`` does not allow concurrent operations on a single
    session (it only owns one DB connection at a time). End-to-end latency
    is dominated by the Voyage embedding call anyway, and each chunk query
    is single-digit-ms at LAN RTT to Postgres, so sequential is fine; if it
    ever isn't, the right move is two sessions, not gathered ops on one.

    Within-doc page expansion (#30): after fusion + the title rerank, the single
    top-RRF document is expanded by ``±page_window`` neighbor pages (PDF only),
    capped at ``max_candidates`` total pages. ``top_k`` selects the base
    candidate breadth (which docs/pages seed the set); ``max_candidates`` is the
    hard page budget sent to Claude. Expansion happens HERE — inside
    ``hybrid_search`` — and returns one ordered list, so ``query.py``'s
    ``enumerate``/strict-``zip``/``source_index`` 1:1 contract is preserved
    (base candidates are never reordered; neighbors are append-only).
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

    # Stage-3 bounded title rerank, then the #28 product-aware boost, THEN cut to
    # top_k for the BASE set. Both boosts only break near-ties (bounded on the RRF
    # scale), so a far-ahead candidate is never displaced. Title rerank runs first
    # (model/year disambiguation), then the product boost reorders toward the
    # asked product family; both are degrade-safe no-ops when the query names
    # nothing recognizable. This base list seeds within-doc expansion below.
    titles = {cid: str(row.document_title) for cid, row in rows.items()}
    fused = _rerank_by_title(query_text, fused, titles)
    facets = {
        cid: _ProductMeta(
            family=None if row.product_family is None else str(row.product_family),
            brand=None if row.brand is None else str(row.brand),
        )
        for cid, row in rows.items()
    }
    fused = _rerank_by_product(query_text, fused, facets)
    base = fused[:top_k]

    # --- Within-doc page-neighbor expansion (#30) ---------------------------
    # Build the per-chunk metadata the expansion algorithm needs (pure values).
    meta: dict[int, _ChunkMeta] = {
        cid: _ChunkMeta(
            chunk_id=cid,
            document_id=int(row.document_id),
            ordinal=int(row.ordinal),
            text=str(row.text),
            is_pdf=SourceType(row.source_type) is SourceType.PDF,
        )
        for cid, row in rows.items()
    }

    leader = _select_leader_chunk(base)
    neighbor_rows: dict[int, Any] = {}
    assembled = base
    if leader is not None and (leader_meta := meta.get(leader.chunk_id)) is not None:
        # HTML docs are never expanded (no ordinal-window meaning; small-to-big
        # module reconstruction in the route already covers them).
        if leader_meta.is_pdf and page_window > 0:
            anchor = leader_meta.ordinal
            try:
                neighbor_result = await session.execute(
                    _LOAD_DOC_NEIGHBORS_SQL,
                    {
                        "doc_id": leader_meta.document_id,
                        "lo": anchor - page_window,
                        "hi": anchor + page_window,
                    },
                )
            except Exception as exc:
                raise RetrievalError("Failed to load leader-doc neighbor rows") from exc

            neighbor_metas: list[_ChunkMeta] = []
            for row in neighbor_result:
                cid = int(row.chunk_id)
                neighbor_rows[cid] = row
                neighbor_metas.append(
                    _ChunkMeta(
                        chunk_id=cid,
                        document_id=int(row.document_id),
                        ordinal=int(row.ordinal),
                        text=str(row.text),
                        is_pdf=True,
                    )
                )

            # Step F: deterministic within-leader-doc rerank (reorders only
            # near-ties inside the leader doc; base positions of OTHER docs are
            # unaffected, and no neighbor can cross the base/budget boundary —
            # eviction in _assemble_candidates keeps all base pages first).
            base = _rerank_within_doc(query_text, base, meta, leader_meta.document_id)
            assembled = _assemble_candidates(
                base=base,
                neighbors=neighbor_metas,
                anchor_ordinal=anchor,
                anchor_score=leader.score,
                max_candidates=max_candidates,
            )
        else:
            assembled = base[:max_candidates]
    else:
        assembled = base[:max_candidates]

    retrieved: list[RetrievedChunk] = []
    for hit in assembled:
        hydrated_row = rows.get(hit.chunk_id)
        if hydrated_row is None:
            hydrated_row = neighbor_rows.get(hit.chunk_id)
        if hydrated_row is None:
            continue
        retrieved.append(_row_to_retrieved(hydrated_row, hit.score))
    return retrieved


class RetrievalService:
    """Application-layer entry point for hybrid retrieval.

    The API layer constructs one of these at startup (with a configured
    :class:`VoyageEmbedder`) and calls :meth:`search` per request, passing
    in a request-scoped :class:`AsyncSession`.
    """

    def __init__(
        self,
        embedder: VoyageEmbedder,
        *,
        page_window: int = DEFAULT_PAGE_WINDOW,
        max_candidates: int = DEFAULT_MAX_CANDIDATES,
    ) -> None:
        self._embedder = embedder
        self._page_window = page_window
        self._max_candidates = max_candidates

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
            page_window=self._page_window,
            max_candidates=self._max_candidates,
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> RetrievalService:
        return cls(
            embedder=VoyageEmbedder(settings),
            page_window=settings.retrieval_page_window,
            max_candidates=settings.retrieval_max_candidates,
        )
