"""Product-aware retrieval boost (#28) — pure-Python unit tests.

The contamination fix: when a query names a recognizable product, candidates
whose owning document's product FACET matches the asked product are nudged up
and confirmed wrong-product siblings are nudged down — bounded on the RRF scale
so a far-ahead candidate is never displaced and a NULL facet is a no-op. No DB,
no network: these exercise the pure ``_rerank_by_product`` / ``_product_delta``
helpers over seeded ``_FusedHit`` + ``_ProductMeta`` values.
"""

from __future__ import annotations

import pytest


def _hybrid():  # type: ignore[no-untyped-def]
    pytest.importorskip("parts_lookup.retrieval.hybrid")
    from parts_lookup.retrieval import hybrid as mod

    if not callable(getattr(mod, "_rerank_by_product", None)):
        pytest.skip("No _rerank_by_product helper exposed; boost is inlined")
    return mod


def _adjacent_rank_gap(mod, rank: int) -> float:  # type: ignore[no-untyped-def]
    return 1.0 / (mod.RRF_K + rank) - 1.0 / (mod.RRF_K + rank + 1)


def test_matching_product_wins_near_tie() -> None:
    """A query naming 'Pike' promotes the Pike-family chunk above a near-tied
    Lyrik sibling that happened to fuse one adjacent-rank gap higher."""
    mod = _hybrid()
    hit = mod._FusedHit
    meta = mod._ProductMeta

    base = 1.0 / (mod.RRF_K + 5)
    fused = [
        hit(chunk_id=1, score=base),  # Lyrik — fused one gap higher
        hit(chunk_id=2, score=base - _adjacent_rank_gap(mod, 5)),  # Pike — asked
    ]
    facets = {
        1: meta(family="lyrik", brand=None),
        2: meta(family="pike", brand=None),
    }
    out = mod._rerank_by_product("What torque for the Pike lower-leg bolts?", fused, facets)
    assert [h.chunk_id for h in out] == [2, 1]


def test_mismatch_is_down_ranked() -> None:
    """A confirmed wrong-product sibling (Lyrik) is pushed below an otherwise
    near-tied Pike chunk via the mismatch penalty (combines with the match boost)."""
    mod = _hybrid()
    hit = mod._FusedHit
    meta = mod._ProductMeta

    base = 1.0 / (mod.RRF_K + 5)
    fused = [
        hit(chunk_id=1, score=base + _adjacent_rank_gap(mod, 5)),  # Lyrik, slightly ahead
        hit(chunk_id=2, score=base),  # Pike — asked product
    ]
    facets = {
        1: meta(family="lyrik", brand=None),
        2: meta(family="pike", brand=None),
    }
    out = mod._rerank_by_product("Pike lower-leg bolt torque", fused, facets)
    # Pike boosted up + Lyrik penalized down → Pike leads.
    assert [h.chunk_id for h in out] == [2, 1]


def test_null_facet_is_a_no_op_never_penalized() -> None:
    """FAIL-SAFE: a chunk with no derived product (NULL facet) is neither boosted
    nor penalized, so a missing/mis-derived facet can't drop a real answer."""
    mod = _hybrid()
    hit = mod._FusedHit
    meta = mod._ProductMeta

    fused = [
        hit(chunk_id=1, score=0.0163),  # NULL facet, leads
        hit(chunk_id=2, score=0.0162),  # NULL facet, just behind
    ]
    facets = {1: meta(family=None, brand=None), 2: meta(family=None, brand=None)}
    out = mod._rerank_by_product("Pike lower-leg bolt torque", fused, facets)
    assert [h.chunk_id for h in out] == [1, 2]  # order unchanged


def test_unidentified_query_is_a_no_op() -> None:
    """Degrade-safe: a question naming no recognizable product leaves the fused
    order untouched (never over-reorders an under-specified query)."""
    mod = _hybrid()
    hit = mod._FusedHit
    meta = mod._ProductMeta

    fused = [hit(chunk_id=1, score=0.02), hit(chunk_id=2, score=0.01)]
    facets = {1: meta(family="lyrik", brand=None), 2: meta(family="pike", brand=None)}
    # No product/brand token in the query → extract_query_scope is unidentified.
    out = mod._rerank_by_product("What tool do I use for the top cap bolt?", fused, facets)
    assert [h.chunk_id for h in out] == [1, 2]


def test_boost_is_bounded_does_not_swamp_relevance() -> None:
    """The boost is BOUNDED: a far-ahead candidate (large RRF margin) is NOT
    displaced even when it's a product mismatch and the trailing candidate
    matches — relevance dominates the facet."""
    mod = _hybrid()
    hit = mod._FusedHit
    meta = mod._ProductMeta

    fused = [
        hit(chunk_id=1, score=0.0300),  # Lyrik, far ahead (both channels, top ranks)
        hit(chunk_id=2, score=0.0100),  # Pike, far behind
    ]
    facets = {1: meta(family="lyrik", brand=None), 2: meta(family="pike", brand=None)}
    out = mod._rerank_by_product("Pike lower-leg bolt torque", fused, facets)
    # The combined boost+penalty is one increment each — far below the 0.02 margin.
    assert mod.PRODUCT_MATCH_BOOST + mod.PRODUCT_MISMATCH_PENALTY < (0.0300 - 0.0100)
    assert [h.chunk_id for h in out] == [1, 2]


def test_generic_brand_only_candidate_is_non_blocking() -> None:
    """A brand-only generic manual (family=None, brand set) is treated as a MATCH
    for a same-brand query (non-blocking), mirroring scope_matches' generic-title
    fallback — so it is boosted, NOT penalized, and is not dropped below a
    confirmed wrong-product chunk. The boost faithfully reuses scope_matches: a
    generic same-brand hit and an exact-family hit are both matches (no invented
    exact-family-over-brand tier), so a confirmed MISMATCH still loses to both."""
    mod = _hybrid()
    hit = mod._FusedHit
    meta = mod._ProductMeta

    asked = "Avid BB7 caliper mounting bolt torque?"  # brand=avid, family=bb7
    base = 1.0 / (mod.RRF_K + 5)
    fused = [
        hit(chunk_id=1, score=base + _adjacent_rank_gap(mod, 5)),  # SRAM code — mismatch, ahead
        hit(chunk_id=2, score=base),  # generic avid manual — non-blocking match
    ]
    facets = {
        1: meta(family="code", brand="sram"),  # confirmed different product → penalized
        2: meta(family=None, brand="avid"),  # generic avid manual → boosted (non-blocking)
    }
    out = mod._rerank_by_product(asked, fused, facets)
    # The non-blocking same-brand generic manual is promoted above the confirmed
    # wrong-product (SRAM Code) sibling that fused slightly higher.
    assert [h.chunk_id for h in out] == [2, 1]
