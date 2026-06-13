"""Reciprocal Rank Fusion + Stage-3 title-rerank unit tests (#29).

Pure-Python tests of the fusion and the bounded document-title rerank — no
DB, no network. If a helper is later inlined into ``hybrid_search`` instead
of being exposed as a function, the affected test skips via ``importorskip``.
"""

from __future__ import annotations

import pytest


def _import_rrf():  # type: ignore[no-untyped-def]
    """Locate the RRF helper.

    TODO: if the helper is renamed or inlined, update the candidate list.
    """
    pytest.importorskip("parts_lookup.retrieval.hybrid")
    from parts_lookup.retrieval import hybrid as mod

    for name in ("_reciprocal_rank_fusion", "_rrf_fuse", "rrf_fuse", "reciprocal_rank_fusion"):
        fn = getattr(mod, name, None)
        if callable(fn):
            return fn
    pytest.skip("No RRF helper exposed on parts_lookup.retrieval.hybrid; helper is inlined")


def _import_hybrid():  # type: ignore[no-untyped-def]
    pytest.importorskip("parts_lookup.retrieval.hybrid")
    from parts_lookup.retrieval import hybrid as mod

    return mod


def test_rrf_chunk_in_both_channels_wins() -> None:
    rrf = _import_rrf()

    keyword: list[tuple[int, float]] = [(1, 9.5), (2, 4.2)]
    vector: list[tuple[int, float]] = [(2, 0.91), (3, 0.83)]

    fused = rrf(keyword, vector)
    fused_ids = [hit.chunk_id for hit in fused]

    assert fused_ids == [2, 1, 3]


# --- Stage-3 title rerank (#29) ---------------------------------------------


def _rerank():  # type: ignore[no-untyped-def]
    mod = _import_hybrid()
    fn = getattr(mod, "_rerank_by_title", None)
    if not callable(fn):
        pytest.skip("No _rerank_by_title helper exposed; rerank is inlined")
    return fn, mod


def _adjacent_rank_gap(mod, rank: int) -> float:  # type: ignore[no-untyped-def]
    """The RRF score gap between single-channel fused ranks ``rank`` and rank+1."""
    return 1.0 / (mod.RRF_K + rank) - 1.0 / (mod.RRF_K + rank + 1)


def test_title_rerank_breaks_near_tie_for_named_doc() -> None:
    """Given two near-equal RRF candidates, the one whose title carries a query
    token (the *named* year/model) out-ranks the identical-spec sibling."""
    rerank, mod = _rerank()
    hit_cls = mod._FusedHit

    # A realistic near-tie: chunk 1 fuses one adjacent-rank gap above chunk 2
    # (the real-world spacing of identical-spec year/model siblings in the
    # vector channel near rank 5). chunk 2's doc names "2012 Monarch".
    base = 1.0 / (mod.RRF_K + 5)
    fused = [
        hit_cls(chunk_id=1, score=base),  # 2014 Monarch — fused one gap higher
        hit_cls(chunk_id=2, score=base - _adjacent_rank_gap(mod, 5)),  # 2012 — named
    ]
    titles = {1: "2014 Monarch RL RT XX", 2: "2012 Monarch"}

    out = rerank("2012 Monarch seal head torque", fused, titles)
    assert [h.chunk_id for h in out] == [2, 1]


def test_title_rerank_does_not_leapfrog_far_ahead_candidate() -> None:
    """The boost is BOUNDED: a candidate many ranks ahead (a large RRF margin)
    is NOT displaced by a title match on a far-lower candidate."""
    rerank, mod = _rerank()
    hit_cls = mod._FusedHit

    # chunk 1 leads by a wide RRF margin (it's in BOTH channels near the top);
    # chunk 2 only matches the title. Even fully boosted, chunk 2 cannot pass.
    fused = [
        hit_cls(chunk_id=1, score=0.0300),  # far ahead, no title match
        hit_cls(chunk_id=2, score=0.0100),  # title matches but far behind
    ]
    titles = {1: "Pike Select", 2: "2012 Monarch"}

    out = rerank("2012 Monarch seal head torque", fused, titles)
    assert mod.TITLE_BOOST_CAP < (0.0300 - 0.0100)  # the margin really is large
    assert [h.chunk_id for h in out] == [1, 2]


def test_title_boost_is_bounded_by_cap() -> None:
    """A title echoing many query tokens cannot accrue unbounded boost."""
    _rerank()  # ensures helper present / else skip
    mod = _import_hybrid()
    boost = mod._title_boost

    qtokens = mod._tokens("2012 2013 monarch vivid lyrik pike")
    # A title repeating every one of those tokens still caps out.
    title = "2012 2013 Monarch Vivid Lyrik Pike"
    assert boost(qtokens, title) == pytest.approx(mod.TITLE_BOOST_CAP)
    # The cap is sized in adjacent-rank-gap units: it clears a few of them (so
    # it breaks a near-tie cluster) but stays an order of magnitude below the
    # gap from rank 1 down to ~rank 12 (a "large margin"), so it can never
    # override strong, many-rank-ahead evidence.
    one_gap = _adjacent_rank_gap(mod, 5)
    assert one_gap < mod.TITLE_TOKEN_BOOST  # one token breaks a real near-tie
    large_margin = 1.0 / (mod.RRF_K + 1) - 1.0 / (mod.RRF_K + 12)
    assert large_margin > mod.TITLE_BOOST_CAP


def test_title_boost_zero_when_no_overlap() -> None:
    mod = _import_hybrid()
    boost = mod._title_boost

    qtokens = mod._tokens("2012 Monarch seal head torque")
    assert boost(qtokens, "Pike Select Charger") == 0.0


def test_title_rerank_ignores_generic_stopword_overlap() -> None:
    """Overlap on filler ("torque", "manual", "bolt") must NOT promote a doc —
    only the disambiguating year/model tokens count."""
    rerank, mod = _rerank()
    hit_cls = mod._FusedHit

    fused = [
        hit_cls(chunk_id=1, score=0.0163),
        hit_cls(chunk_id=2, score=0.0162),
    ]
    # chunk 2's title shares only stopwords with the query → no promotion.
    titles = {1: "Pike Select", 2: "Generic Torque Manual Bolt Tool"}
    out = rerank("torque manual bolt tool spec", fused, titles)
    assert [h.chunk_id for h in out] == [1, 2]
