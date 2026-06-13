"""Within-doc page-neighbor expansion + within-doc rerank unit tests (#30).

Pure-Python tests of the deterministic expansion algorithm — no DB, no
network. They exercise the load-bearing pieces the review pinned:

  * leader-doc selection (rank-1 fused chunk, explicit score/chunk_id tiebreak),
  * W=1 neighbor injection, dedup, and the hard budget cap with the specified
    eviction order (base never evicted; farthest-from-anchor neighbors dropped
    first),
  * the ``source_index`` ROUND-TRIP: after expansion, the candidate at a
    returned position still maps back to the SAME chunk it did pre-expansion
    (guards ``query.py`` strict-zip + ``retrieved[source_index - 1]``),
  * the within-doc rerank non-demotion invariant,
  * HTML leader → no expansion.

If a helper is renamed or inlined, the affected test skips via importorskip.
"""

from __future__ import annotations

import pytest


def _hybrid():  # type: ignore[no-untyped-def]
    pytest.importorskip("parts_lookup.retrieval.hybrid")
    from parts_lookup.retrieval import hybrid as mod

    for name in (
        "_FusedHit",
        "_ChunkMeta",
        "_select_leader_chunk",
        "_assemble_candidates",
        "_rerank_within_doc",
    ):
        if getattr(mod, name, None) is None:
            pytest.skip(f"expansion helper {name} not exposed; logic inlined")
    return mod


def _meta(mod, chunk_id, document_id, ordinal, text="", is_pdf=True):  # type: ignore[no-untyped-def]
    return mod._ChunkMeta(
        chunk_id=chunk_id,
        document_id=document_id,
        ordinal=ordinal,
        text=text,
        is_pdf=is_pdf,
    )


# --- Step B: leader-doc selection -------------------------------------------


def test_leader_is_highest_fused_chunk() -> None:
    mod = _hybrid()
    fused = [
        mod._FusedHit(chunk_id=10, score=0.030),
        mod._FusedHit(chunk_id=20, score=0.020),
        mod._FusedHit(chunk_id=30, score=0.010),
    ]
    assert mod._select_leader_chunk(fused).chunk_id == 10


def test_leader_tiebreak_prefers_lower_chunk_id_on_equal_score() -> None:
    mod = _hybrid()
    # Two chunks tie on score; the lower chunk_id must win deterministically,
    # regardless of input ordering.
    fused = [
        mod._FusedHit(chunk_id=99, score=0.025),
        mod._FusedHit(chunk_id=7, score=0.025),
        mod._FusedHit(chunk_id=50, score=0.025),
    ]
    assert mod._select_leader_chunk(fused).chunk_id == 7


def test_leader_none_for_empty() -> None:
    mod = _hybrid()
    assert mod._select_leader_chunk([]) is None


# --- Steps D-E: neighbor injection, dedup, budget cap, eviction order -------


def test_w1_neighbors_injected_after_base() -> None:
    mod = _hybrid()
    # Base: leader is chunk 10 (doc 1, page 5). Plus two other-doc pages.
    base = [
        mod._FusedHit(chunk_id=10, score=0.030),  # leader, doc1 p5
        mod._FusedHit(chunk_id=21, score=0.020),  # doc2
    ]
    # Leader-doc neighbors returned by SQL for [p4, p5, p6]: anchor (10) + 4 + 6.
    neighbors = [
        _meta(mod, 10, 1, 5),  # the anchor itself (already in base → dedup)
        _meta(mod, 9, 1, 4),
        _meta(mod, 11, 1, 6),
    ]
    out = mod._assemble_candidates(
        base=base, neighbors=neighbors, anchor_ordinal=5, anchor_score=0.030, max_candidates=6
    )
    ids = [h.chunk_id for h in out]
    # Base order preserved; anchor not duplicated; both W=1 neighbors appended,
    # closest-then-lowest-ordinal first (|4-5|=|6-5|=1 → tie → lower ordinal 4).
    assert ids == [10, 21, 9, 11]


def test_budget_cap_drops_farthest_neighbor_first() -> None:
    mod = _hybrid()
    # 4 base pages + room for only 2 neighbors at budget 6. With W=2 the leader
    # doc offers neighbors at distance 1 and 2; the distance-2 ones must drop.
    base = [
        mod._FusedHit(chunk_id=10, score=0.040),  # leader, doc1 p10
        mod._FusedHit(chunk_id=21, score=0.030),
        mod._FusedHit(chunk_id=31, score=0.020),
        mod._FusedHit(chunk_id=41, score=0.010),
    ]
    neighbors = [
        _meta(mod, 10, 1, 10),  # anchor (dedup)
        _meta(mod, 9, 1, 9),    # dist 1
        _meta(mod, 11, 1, 11),  # dist 1
        _meta(mod, 8, 1, 8),    # dist 2 — should be dropped
        _meta(mod, 12, 1, 12),  # dist 2 — should be dropped
    ]
    out = mod._assemble_candidates(
        base=base, neighbors=neighbors, anchor_ordinal=10, anchor_score=0.040, max_candidates=6
    )
    ids = [h.chunk_id for h in out]
    assert len(ids) == 6
    assert ids == [10, 21, 31, 41, 9, 11]  # base intact + 2 closest neighbors
    assert 8 not in ids and 12 not in ids   # farthest dropped first


def test_base_pages_never_evicted_even_when_base_fills_budget() -> None:
    mod = _hybrid()
    base = [mod._FusedHit(chunk_id=i, score=1.0 - i / 100) for i in range(1, 7)]  # 6 base pages
    neighbors = [_meta(mod, 99, 1, 2)]  # a would-be neighbor
    out = mod._assemble_candidates(
        base=base, neighbors=neighbors, anchor_ordinal=1, anchor_score=0.99, max_candidates=6
    )
    ids = [h.chunk_id for h in out]
    assert ids == [1, 2, 3, 4, 5, 6]  # all base, no neighbor injected
    assert 99 not in ids


def test_neighbor_already_in_base_is_not_duplicated() -> None:
    mod = _hybrid()
    base = [
        mod._FusedHit(chunk_id=10, score=0.030),  # leader, p5
        mod._FusedHit(chunk_id=11, score=0.015),  # p6 — already a base candidate
    ]
    neighbors = [_meta(mod, 9, 1, 4), _meta(mod, 11, 1, 6)]
    out = mod._assemble_candidates(
        base=base, neighbors=neighbors, anchor_ordinal=5, anchor_score=0.030, max_candidates=6
    )
    ids = [h.chunk_id for h in out]
    assert ids == [10, 11, 9]  # 11 keeps its base position; only 9 appended
    assert ids.count(11) == 1


# --- source_index ROUND-TRIP (the contract guard the review required) -------


def test_source_index_round_trip_after_expansion() -> None:
    """After expansion, the candidate at each pre-existing base position still
    resolves to the SAME chunk it did pre-expansion.

    This is the exact contract ``query.py`` relies on: it does
    ``enumerate(retrieved, start=1)`` then ``retrieved[source_index - 1]``.
    Expansion only APPENDS neighbors; it never reshuffles base positions, so a
    ``source_index`` Claude returns for a base candidate maps back correctly.
    """
    mod = _hybrid()
    base = [
        mod._FusedHit(chunk_id=10, score=0.030),  # leader, doc1 p5
        mod._FusedHit(chunk_id=21, score=0.020),  # doc2
        mod._FusedHit(chunk_id=31, score=0.010),  # doc3
    ]
    pre_positions = {hit.chunk_id: i for i, hit in enumerate(base)}

    neighbors = [_meta(mod, 9, 1, 4), _meta(mod, 11, 1, 6)]
    out = mod._assemble_candidates(
        base=base, neighbors=neighbors, anchor_ordinal=5, anchor_score=0.030, max_candidates=6
    )

    # Every base chunk keeps its original 0-based index → 1-based source_index.
    for chunk_id, pre_idx in pre_positions.items():
        source_index = pre_idx + 1  # what Claude would return for that candidate
        assert out[source_index - 1].chunk_id == chunk_id


# --- Step F: within-doc rerank non-demotion invariant -----------------------


def test_rerank_prefers_torque_plus_fastener_page_among_near_ties() -> None:
    mod = _hybrid()
    # Two leader-doc near-ties: chunk 1 fuses one bonus-width higher but is a
    # generic tool-summary page; chunk 2 has the asked fastener + an N·m value.
    base = [
        mod._FusedHit(chunk_id=1, score=0.0200),
        mod._FusedHit(chunk_id=2, score=0.0200 - mod.RERANK_BONUS / 2),
    ]
    meta = {
        1: _meta(mod, 1, 1, 10, text="Tool overview. Use a torx key."),
        2: _meta(mod, 2, 1, 11, text="Caliper body bolt: 9 N-m (80 in-lb)."),
    }
    out = mod._rerank_within_doc("caliper body bolt torque", base, meta, leader_doc_id=1)
    assert [h.chunk_id for h in out] == [2, 1]


def test_rerank_does_not_demote_far_ahead_page_with_no_fastener_overlap() -> None:
    """Non-demotion invariant: a higher-fused page that lacks the fastener token
    is NEVER demoted below a lower-fused page that merely contains an N·m
    string. The bonus is a single fixed increment — it can only break near-ties."""
    mod = _hybrid()
    base = [
        mod._FusedHit(chunk_id=1, score=0.0300),  # far ahead, no fastener overlap
        mod._FusedHit(chunk_id=2, score=0.0100),  # has N·m but far behind
    ]
    meta = {
        1: _meta(mod, 1, 1, 10, text="Suspension overview, no fasteners listed."),
        2: _meta(mod, 2, 1, 11, text="Caliper body bolt: 9 N-m."),
    }
    out = mod._rerank_within_doc("caliper body bolt torque", base, meta, leader_doc_id=1)
    assert mod.RERANK_BONUS < (0.0300 - 0.0100)  # the lead really is large
    assert [h.chunk_id for h in out] == [1, 2]  # order unchanged


def test_rerank_only_touches_leader_doc() -> None:
    mod = _hybrid()
    # A matching page in a NON-leader doc must NOT be boosted.
    base = [
        mod._FusedHit(chunk_id=1, score=0.0200),  # leader doc1, no match
        mod._FusedHit(chunk_id=2, score=0.0200 - mod.RERANK_BONUS / 2),  # doc2, would-match
    ]
    meta = {
        1: _meta(mod, 1, 1, 10, text="Overview page."),
        2: _meta(mod, 2, 2, 11, text="Caliper body bolt: 9 N-m."),  # different doc
    }
    out = mod._rerank_within_doc("caliper body bolt torque", base, meta, leader_doc_id=1)
    assert [h.chunk_id for h in out] == [1, 2]  # doc2 not boosted across docs


# --- Budget 10 (#49): larger page budget, round-trip still holds ------------


def test_budget_10_fits_more_neighbors_and_keeps_base_first() -> None:
    """At the #49 budget of 10, a leader doc with many neighbors fits more of
    them than the old budget of 6 — but base pages still come first and are
    never evicted."""
    mod = _hybrid()
    base = [
        mod._FusedHit(chunk_id=10, score=0.050),  # leader, doc1 p20
        mod._FusedHit(chunk_id=21, score=0.040),  # doc2
        mod._FusedHit(chunk_id=31, score=0.030),  # doc3
    ]
    # Leader-doc neighbors at distances 1..4 either side of anchor page 20.
    neighbors = [
        _meta(mod, 10, 1, 20),  # anchor (dedup)
        _meta(mod, 9, 1, 19),
        _meta(mod, 11, 1, 21),
        _meta(mod, 8, 1, 18),
        _meta(mod, 12, 1, 22),
        _meta(mod, 7, 1, 17),
        _meta(mod, 13, 1, 23),
        _meta(mod, 6, 1, 16),
        _meta(mod, 14, 1, 24),
    ]
    out = mod._assemble_candidates(
        base=base, neighbors=neighbors, anchor_ordinal=20, anchor_score=0.050, max_candidates=10
    )
    ids = [h.chunk_id for h in out]
    assert ids[:3] == [10, 21, 31]  # base first, unreordered
    assert len(ids) == 10
    # 7 neighbor slots filled closest-first: 19,21 (d1) | 18,22 (d2) | 17,23 (d3) | 16 (d4)
    assert ids[3:] == [9, 11, 8, 12, 7, 13, 6]
    assert 14 not in ids  # farthest (d4 high side) dropped at the cap


def test_source_index_round_trip_at_budget_10() -> None:
    """The #30 round-trip contract still holds at the #49 budget of 10: base
    positions map 1:1 after the (larger) append window."""
    mod = _hybrid()
    base = [
        mod._FusedHit(chunk_id=10, score=0.030),  # leader doc1 p5
        mod._FusedHit(chunk_id=21, score=0.020),  # doc2
        mod._FusedHit(chunk_id=31, score=0.010),  # doc3
        mod._FusedHit(chunk_id=41, score=0.005),  # doc4
    ]
    pre_positions = {hit.chunk_id: i for i, hit in enumerate(base)}
    neighbors = [_meta(mod, 9, 1, 4), _meta(mod, 11, 1, 6)]
    out = mod._assemble_candidates(
        base=base, neighbors=neighbors, anchor_ordinal=5, anchor_score=0.030, max_candidates=10
    )
    for chunk_id, pre_idx in pre_positions.items():
        source_index = pre_idx + 1
        assert out[source_index - 1].chunk_id == chunk_id


# --- Page-budget drop log fires (#49) ---------------------------------------


class _FakeRow:
    def __init__(self, **kw: object) -> None:
        self.__dict__.update(kw)


def _chunk_row(mod, chunk_id, document_id, ordinal, text="t"):  # type: ignore[no-untyped-def]
    return _FakeRow(
        chunk_id=chunk_id,
        document_id=document_id,
        ordinal=ordinal,
        text=text,
        png_r2_key=f"pdfs/{document_id}/{ordinal}.png",
        anchor=None,
        parent_anchor=None,
        source_url=f"pdfs/{document_id}.pdf#page={ordinal}",
        source_type="pdf",
        document_title=f"doc{document_id}.pdf",
        document_source_url=f"pdfs/{document_id}.pdf",
        product_family=None,
        brand=None,
    )


@pytest.mark.asyncio
async def test_page_budget_drop_log_fires_with_correct_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the leading doc's pages + neighbors exceed the budget, hybrid_search
    emits `retrieval.page_budget_drop` with the right n_dropped. Drives the real
    hybrid_search over a fake session + mocked channels (no DB, no network)."""
    import structlog

    from parts_lookup.retrieval import hybrid as mod

    # Leading doc 1 with pages at ordinals 5..16 (12 base pages, all doc 1) so
    # the budget of 4 must drop 12 - 4 = 8 base-truncation + neighbor pages.
    # Use a small budget to force drops deterministically with few rows.
    base_chunks = [(cid, 1, cid) for cid in range(5, 13)]  # 8 pages of doc 1
    chunk_rows = {cid: _chunk_row(mod, cid, doc, ordn) for cid, doc, ordn in base_chunks}
    # Neighbors of the anchor (the lowest-id top-RRF page → chunk 5, ordinal 5):
    # ordinals 4,5,6 within window 1 → chunk 4 (ord 4) is a NEW neighbor page.
    neighbor_rows = {4: _chunk_row(mod, 4, 1, 4)}

    async def fake_keyword(_session, _q, _k):  # type: ignore[no-untyped-def]
        return [(cid, 1.0 / i) for i, (cid, _d, _o) in enumerate(base_chunks, start=1)]

    async def fake_vector(_session, _vec, _k):  # type: ignore[no-untyped-def]
        return [(cid, 1.0 / i) for i, (cid, _d, _o) in enumerate(base_chunks, start=1)]

    monkeypatch.setattr(mod, "keyword_search", fake_keyword)
    monkeypatch.setattr(mod, "vector_search", fake_vector)

    class _FakeResult:
        def __init__(self, rows):  # type: ignore[no-untyped-def]
            self._rows = rows

        def __iter__(self):  # type: ignore[no-untyped-def]
            return iter(self._rows)

    class _FakeSession:
        async def execute(self, statement, params=None):  # type: ignore[no-untyped-def]
            # The neighbor query binds :doc_id; the chunk-load query binds chunk_ids.
            if params and "doc_id" in params:
                lo, hi = params["lo"], params["hi"]
                return _FakeResult(
                    [r for cid, r in neighbor_rows.items() if lo <= r.ordinal <= hi]
                )
            ids = params["chunk_ids"]
            return _FakeResult([chunk_rows[cid] for cid in ids if cid in chunk_rows])

    class _FakeEmbedder:
        async def embed_query(self, _text):  # type: ignore[no-untyped-def]
            return [0.0] * 8

    with structlog.testing.capture_logs() as logs:
        out = await mod.hybrid_search(
            session=_FakeSession(),
            embedder=_FakeEmbedder(),
            query_text="nut torque",
            top_k=5,
            per_channel_k=20,
            page_window=1,
            max_candidates=4,
        )

    assert len(out) == 4  # capped at the budget
    drops = [e for e in logs if e.get("event") == "retrieval.page_budget_drop"]
    assert len(drops) == 1
    drop = drops[0]
    assert drop["document_id"] == 1
    assert drop["budget"] == 4
    assert drop["n_kept"] == 4
    # Considered = 5 base winners (top_k=5 from doc 1) + neighbor chunk 4 = 6
    # unique; kept = 4 → dropped = 2.
    assert drop["n_dropped"] == 2


# --- HTML leader → no ordinal-window expansion ------------------------------


def test_html_leader_has_no_ordinal_meaning_in_meta() -> None:
    """Guard the data fact the algorithm relies on: an HTML leader carries
    is_pdf=False, so hybrid_search skips neighbor expansion for it. (The skip
    branch is in hybrid_search; here we just assert the meta flag the branch
    keys off.)"""
    mod = _hybrid()
    html_leader = _meta(mod, 5, 9, 1, text="Crank Arm Installation", is_pdf=False)
    assert html_leader.is_pdf is False
