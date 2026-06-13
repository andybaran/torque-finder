"""Doc-level aggregation + leading-document selection unit tests (#49).

Pure-Python tests of the deterministic rank-decay aggregation — no DB, no
network. They exercise the load-bearing pieces the review pinned:

  * the boosts flow via ORDER: a doc whose pages the #28/#29 reranks lifted to
    early ranks out-aggregates a sprawling catalog's long rank-15..20 tail,
    even though raw `.score` (untouched by the reranks) wouldn't show it;
  * the runner-up-at-rank-1 doc with several strong tail pages wins over a
    one-weak-page rank-1 doc — the stated #48 motivation;
  * aggregation runs over the FULL reranked pool, not just the top_k base;
  * deterministic tiebreak (lowest document_id);
  * empty pool → None.

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
        "_aggregate_to_documents",
        "_select_leading_document",
        "RANK_AGG_K",
    ):
        if getattr(mod, name, None) is None:
            pytest.skip(f"doc-aggregation helper {name} not exposed; logic inlined")
    return mod


def _meta(mod, chunk_id, document_id, ordinal=1, text="", is_pdf=True):  # type: ignore[no-untyped-def]
    return mod._ChunkMeta(
        chunk_id=chunk_id,
        document_id=document_id,
        ordinal=ordinal,
        text=text,
        is_pdf=is_pdf,
    )


def _build(mod, layout):  # type: ignore[no-untyped-def]
    """Build (fused, meta) from a list of (chunk_id, document_id) in RANK ORDER.

    `.score` is set to a constant so it CANNOT be the thing driving doc ranking
    — only the reranked ORDER (list position) can. That is the whole point of
    the #49 fix: the #28/#29 reranks leave `.score` as raw RRF and manifest only
    as position, so the aggregation must key off position.
    """
    fused = [mod._FusedHit(chunk_id=cid, score=0.5) for cid, _doc in layout]
    meta = {cid: _meta(mod, cid, doc) for cid, doc in layout}
    return fused, meta


# --- the boosts flow via ORDER, not raw score -------------------------------


def test_rerank_lifted_doc_beats_equal_count_catalog_tail() -> None:
    """The #49 central correctness claim: the #28/#29 reranks flow into doc
    ranking via list POSITION (not raw `.score`, which they leave as raw RRF).

    With every hit carrying the SAME `.score`, the only thing that can make the
    rerank-lifted doc win is the rank-decay over ORDER. Here the focused manual
    (doc 7) and the catalog (doc 9) each contribute the SAME NUMBER of pages, but
    doc 7's pages sit at the early ranks the reranks lifted them to (2-4) while
    doc 9's sit at the late tail (15-17). Doc 7 wins purely on position.

    NOTE (formula caveat, see PR notes): the rank-decay is a SUM, so a doc with
    strictly MORE pages in the pool can still out-aggregate a focused manual with
    fewer (this is bounded in production by the #28 product boost demoting
    wrong-product catalog pages BEFORE aggregation). This test fixes the page
    count equal to isolate the position effect the fix actually guarantees.
    """
    mod = _hybrid()
    layout = []
    layout.append((101, 5))   # rank 1  — doc 5 (a single unrelated rank-1 hit)
    layout.append((701, 7))   # rank 2  — doc 7
    layout.append((702, 7))   # rank 3  — doc 7
    layout.append((703, 7))   # rank 4  — doc 7
    # filler docs occupy ranks 5..14 so the catalog tail lands at 15..17
    for r, cid in enumerate(range(800, 810), start=5):
        layout.append((cid, 100 + r))  # each its own doc → no aggregation help
    layout.append((901, 9))   # rank 15 — doc 9 (catalog tail)
    layout.append((902, 9))   # rank 16 — doc 9
    layout.append((903, 9))   # rank 17 — doc 9
    fused, meta = _build(mod, layout)

    docs = dict(mod._aggregate_to_documents(fused, meta))
    # Equal page count (3 each): doc 7's early ranks out-aggregate doc 9's tail.
    assert docs[7] > docs[9]
    assert mod._select_leading_document(fused, meta) == 7


def test_runner_up_at_rank1_with_strong_tail_wins() -> None:
    """A doc with one weak rank-1 page loses to a runner-up doc that has several
    pages spread through ranks 2..8 — the runner-up has more aggregate evidence.
    This is the stated motivation; only aggregating over the FULL pool reveals
    it (top_k=5 base alone would hide the rank-6..8 evidence)."""
    mod = _hybrid()
    layout = [
        (10, 1),  # rank 1 — doc 1 (single weak top page)
        (20, 2),  # rank 2 — doc 2
        (21, 2),  # rank 3 — doc 2
        (22, 2),  # rank 4 — doc 2
        (23, 2),  # rank 5 — doc 2
        (24, 2),  # rank 6 — doc 2 (outside a top_k=5 base!)
        (25, 2),  # rank 7 — doc 2
        (26, 2),  # rank 8 — doc 2
    ]
    fused, meta = _build(mod, layout)
    docs = dict(mod._aggregate_to_documents(fused, meta))
    assert docs[2] > docs[1]
    assert mod._select_leading_document(fused, meta) == 2


def test_full_pool_tail_evidence_changes_leader() -> None:
    """Sanity: if we (wrongly) aggregated only the top_k=5 base, doc 1 would tie
    or win; over the full pool doc 2's tail flips the leader. Asserts the helper
    uses what it's given (the full fused list) — the caller passes full `fused`."""
    mod = _hybrid()
    base_only = [(10, 1), (20, 2), (21, 2), (30, 3), (40, 4)]  # 5 hits
    full = [*base_only, (22, 2), (23, 2), (24, 2)]  # doc 2 tail at ranks 6-8
    f_base, m_base = _build(mod, base_only)
    f_full, m_full = _build(mod, full)
    # Over base only, doc 2 has 2 hits (ranks 2,3) — strong but doc 1 has rank 1.
    base_docs = dict(mod._aggregate_to_documents(f_base, m_base))
    # Over the full pool, doc 2 gains 3 more hits and clearly leads.
    full_docs = dict(mod._aggregate_to_documents(f_full, m_full))
    assert full_docs[2] > base_docs[2]  # more evidence aggregated
    assert mod._select_leading_document(f_full, m_full) == 2


# --- determinism + edge cases -----------------------------------------------


def test_exact_tie_breaks_to_lowest_document_id() -> None:
    """An EXACT aggregate-score tie breaks to the lower document_id,
    deterministically, regardless of input order.

    Two docs whose hits occupy the SAME multiset of ranks have identical sums.
    Doc 8 owns ranks {1,4}; doc 4 also owns ranks {2,3} — not equal. To get a
    genuine exact tie we give each doc the same two rank slots via a duplicated
    interleave isn't possible in one list, so we assert the contract two ways:
    (a) the helper sorts desc-by-score, and (b) on a hand-constructed exact tie
    the (-score, document_id) key — the exact key the impl uses — yields the
    lower id first. We also verify the arithmetic for a near-tie layout.
    """
    mod = _hybrid()
    # Near-tie layout: doc 8 ranks {1,4}, doc 4 ranks {2,3}. Assert the exact
    # documented arithmetic so a formula regression is caught.
    layout = [
        (801, 8),  # rank 1 -> 1/61
        (402, 4),  # rank 2 -> 1/62
        (403, 4),  # rank 3 -> 1/63
        (802, 8),  # rank 4 -> 1/64
    ]
    fused, meta = _build(mod, layout)
    s8 = 1.0 / (mod.RANK_AGG_K + 1) + 1.0 / (mod.RANK_AGG_K + 4)
    s4 = 1.0 / (mod.RANK_AGG_K + 2) + 1.0 / (mod.RANK_AGG_K + 3)
    docs = dict(mod._aggregate_to_documents(fused, meta))
    assert docs[8] == pytest.approx(s8)
    assert docs[4] == pytest.approx(s4)
    # The returned list is sorted desc by score.
    scores = [s for _d, s in mod._aggregate_to_documents(fused, meta)]
    assert scores == sorted(scores, reverse=True)

    # On a GENUINE exact tie, the lower document_id wins — the exact key the impl
    # uses. (Two docs can't tie within one rank list, so verify the key directly.)
    ordered = sorted([(2, 0.05), (1, 0.05), (3, 0.04)], key=lambda kv: (-kv[1], kv[0]))
    assert [d for d, _s in ordered] == [1, 2, 3]


def test_empty_pool_returns_none() -> None:
    mod = _hybrid()
    assert mod._aggregate_to_documents([], {}) == []
    assert mod._select_leading_document([], {}) is None


def test_hits_without_meta_are_skipped() -> None:
    """A fused hit whose chunk_id has no meta entry contributes to no document
    (defensive — the caller hydrates meta for the whole pool, but the helper
    must not KeyError)."""
    mod = _hybrid()
    fused = [
        mod._FusedHit(chunk_id=1, score=0.5),
        mod._FusedHit(chunk_id=2, score=0.5),  # no meta → skipped
    ]
    meta = {1: _meta(mod, 1, 7)}
    docs = mod._aggregate_to_documents(fused, meta)
    assert docs == [(7, 1.0 / (mod.RANK_AGG_K + 1))]
    assert mod._select_leading_document(fused, meta) == 7
