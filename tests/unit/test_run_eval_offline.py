"""Offline unit tests for the run_eval grading branch (#46).

The live eval's per-case grading is a pure function (`_grade_sampled`) so the
#32-abstention branch — the one that crashed the harness — is covered with no
network, no DB, and no paid calls. Builds a real `GradedRecord` so the
`FAIL_MODES` validation also gets exercised.
"""

from __future__ import annotations

from types import SimpleNamespace

from tests.eval.run_eval import _grade_sampled


def _case(expected_torque: str = "40 N·m (354 in-lb)"):  # type: ignore[no-untyped-def]
    return SimpleNamespace(case_id="c1", query="q?", expected_torque=expected_torque)


def _answer(text: str = "", torque: str | None = None):  # type: ignore[no-untyped-def]
    # run_eval only reads .text and .torque off the answer.
    return SimpleNamespace(text=text, torque=torque)


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


def test_value_and_provenance_pass_is_correct() -> None:
    chosen = SimpleNamespace(text="Tighten to 40 N·m (354 in-lb).")
    rec = _grade_sampled(
        _case(),
        _answer(text="40 N·m (354 in-lb)", torque="40 N·m (354 in-lb)"),
        chosen,
    )
    assert rec.passed is True
    assert rec.failure_mode == "correct"


def test_right_value_but_wrong_provenance_is_other() -> None:
    """Value present in the answer, but the cited chunk doesn't document it →
    not a value error, just an ungrounded citation."""
    chosen = SimpleNamespace(text="A completely unrelated page about tire pressure.")
    rec = _grade_sampled(
        _case(),
        _answer(text="40 N·m (354 in-lb)", torque="40 N·m (354 in-lb)"),
        chosen,
    )
    assert rec.passed is False
    assert rec.failure_mode == "other"


def test_wrong_value_is_wrong_value() -> None:
    chosen = SimpleNamespace(text="Tighten to 9 N·m.")
    rec = _grade_sampled(
        _case(),
        _answer(text="9 N·m", torque="9 N·m"),
        chosen,
    )
    assert rec.passed is False
    assert rec.failure_mode == "wrong_value"
