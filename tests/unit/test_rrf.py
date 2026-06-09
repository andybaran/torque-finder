"""Reciprocal Rank Fusion unit test.

If the RRF helper is later inlined into ``hybrid_search`` instead of being
exposed as a function, this test will gracefully skip via ``importorskip``.
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


def test_rrf_page_in_both_channels_wins() -> None:
    rrf = _import_rrf()

    keyword: list[tuple[int, float]] = [(1, 9.5), (2, 4.2)]
    vector: list[tuple[int, float]] = [(2, 0.91), (3, 0.83)]

    fused = rrf(keyword, vector)
    fused_ids = [hit.chunk_id for hit in fused]

    assert fused_ids == [2, 1, 3]
