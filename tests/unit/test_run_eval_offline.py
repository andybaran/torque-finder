"""Offline unit tests for the run_eval grading branch (#46, doc-level #49).

The live eval's per-case grading is a pure function (`_grade_sampled`) so the
#32-abstention branch — the one that crashed the harness — is covered with no
network, no DB, and no paid calls. Builds a real `GradedRecord` so the
`FAIL_MODES` validation also gets exercised.

As of #49 the headline is DOC-LEVEL: `passed` = value-correct AND the cited
document is the right manual (doc identity), with the strict doc+page signal
demoted to the secondary `doc_page_precise` metric.
"""

from __future__ import annotations

from types import SimpleNamespace

from tests.eval.metrics import aggregate
from tests.eval.run_eval import _grade_sampled

_DOC = "2010-sram-technical-manual.pdf"


def _case(
    expected_torque: str = "40 N·m (354 in-lb)",
    document_title: str = _DOC,
):  # type: ignore[no-untyped-def]
    return SimpleNamespace(
        case_id="c1",
        query="q?",
        expected_torque=expected_torque,
        document_title=document_title,
    )


def _answer(text: str = "", torque: str | None = None):  # type: ignore[no-untyped-def]
    # run_eval only reads .text and .torque off the answer.
    return SimpleNamespace(text=text, torque=torque)


def _chosen(
    text: str,
    document_title: str = _DOC,
    document_source_url: str = "pdfs/abc123.pdf#page=8",
):  # type: ignore[no-untyped-def]
    # run_eval reads .text, .document_title, .document_source_url off chosen.
    return SimpleNamespace(
        text=text,
        document_title=document_title,
        document_source_url=document_source_url,
    )


def test_abstention_is_graded_as_a_miss_not_a_crash() -> None:
    """chosen=None (the #32 abstention shape that used to crash run_query) is a
    fail with failure_mode='abstained', never an exception."""
    rec = _grade_sampled(
        _case(),
        _answer(text="I can't find that product in the supplied sources.", torque=None),
        None,
    )
    assert rec.passed is False
    assert rec.failure_mode == "abstained"
    assert rec.retrieved_expected is False
    assert rec.doc_page_precise is False


def test_value_and_right_doc_and_right_page_is_correct_and_precise() -> None:
    """Value correct + right manual + cited chunk documents the value →
    doc-level pass AND doc+page precise."""
    chosen = _chosen(text="Tighten to 40 N·m (354 in-lb).")
    rec = _grade_sampled(
        _case(),
        _answer(text="40 N·m (354 in-lb)", torque="40 N·m (354 in-lb)"),
        chosen,
    )
    assert rec.passed is True
    assert rec.failure_mode == "correct"
    assert rec.retrieved_expected is True
    assert rec.doc_page_precise is True


def test_value_and_right_doc_but_wrong_page_is_doc_level_pass_not_precise() -> None:
    """Value correct + right manual, but the cited chunk text doesn't document
    the value (wrong page of the right doc) → doc-level pass, NOT doc+page
    precise. This is the #49 headline relaxation: the right manual is enough."""
    chosen = _chosen(text="A page from the right manual about tire pressure.")
    rec = _grade_sampled(
        _case(),
        _answer(text="40 N·m (354 in-lb)", torque="40 N·m (354 in-lb)"),
        chosen,
    )
    assert rec.passed is True
    assert rec.failure_mode == "correct"
    assert rec.retrieved_expected is True
    assert rec.doc_page_precise is False


def test_value_correct_but_wrong_doc_is_a_miss() -> None:
    """Value present in the answer, but the cited document is the WRONG manual →
    fail (doc-recall miss), mode 'other'. The value-match guard does not rescue
    a citation to the wrong manual."""
    chosen = _chosen(
        text="Tighten to 40 N·m (354 in-lb).",
        document_title="some-other-manual.pdf",
        document_source_url="pdfs/def456.pdf#page=3",
    )
    rec = _grade_sampled(
        _case(),
        _answer(text="40 N·m (354 in-lb)", torque="40 N·m (354 in-lb)"),
        chosen,
    )
    assert rec.passed is False
    assert rec.failure_mode == "other"
    assert rec.retrieved_expected is False
    assert rec.doc_page_precise is False


def test_wrong_value_is_wrong_value() -> None:
    chosen = _chosen(text="Tighten to 9 N·m.")
    rec = _grade_sampled(
        _case(),
        _answer(text="9 N·m", torque="9 N·m"),
        chosen,
    )
    assert rec.passed is False
    assert rec.failure_mode == "wrong_value"
    assert rec.doc_page_precise is False


def test_recall_at_k_never_below_page_precision() -> None:
    """Aggregate invariant (#49): doc-level recall@k >= doc+page page_precision,
    since doc_page_precise implies the doc was right (retrieved_expected)."""
    records = [
        # doc-level pass, page-precise
        _grade_sampled(
            _case(),
            _answer(text="40 N·m (354 in-lb)", torque="40 N·m (354 in-lb)"),
            _chosen(text="Tighten to 40 N·m (354 in-lb)."),
        ),
        # doc-level pass, NOT page-precise (right doc, wrong page)
        _grade_sampled(
            _case(),
            _answer(text="40 N·m (354 in-lb)", torque="40 N·m (354 in-lb)"),
            _chosen(text="Unrelated page of the right manual."),
        ),
        # abstention miss
        _grade_sampled(_case(), _answer(text="no source", torque=None), None),
    ]
    m = aggregate(records)
    assert m.recall_at_k >= m.page_precision
    assert m.recall_at_k == 2 / 3
    assert m.page_precision == 1 / 3
