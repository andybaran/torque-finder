"""Offline, $0 unit tests for the deterministic eval grader (issue #34).

NO network, NO API keys, NO DB — these import only the pure ``tests.eval.grading``
module and run under ``uv run --extra dev pytest tests/unit``. This is the CI
gate that proves the harness logic without spending money. Crucially there is
NO module-level live-env skip here (unlike the ``eval``-marked live suites), so
these always run.
"""

from __future__ import annotations

import pytest

from tests.eval.grading import (
    ProbeOutcome,
    is_abstention,
    is_hallucination,
    matches,
    normalize,
    provenance_ok,
    unit_equivalent,
    value_matches,
)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("40 N·m", "40 N-m"),
        ("40 Nm", "40 N-m"),
        ("5.5 N⋅m", "5.5 N-m"),
        ("4 mm", "4mm"),
        ("7 to 8 mm", "7-8 mm"),
        ("T-25", "T25"),
        ("49 in. lb", "49 in-lb"),
        ("49 in lb", "49 in-lb"),
    ],
)
def test_normalize_collapses_notation(a: str, b: str) -> None:
    assert normalize(a) == normalize(b)


def test_normalize_does_not_equate_different_numbers() -> None:
    assert normalize("40 N-m") != normalize("41 N-m")


def test_value_matches_substring_under_normalization() -> None:
    # The canonical thoughts.md case: PDF prints "40 N·m (354 in-lb)".
    assert value_matches("The cassette lockring is 40 N·m (354 in-lb).", "40 N-m")
    assert matches("Use a 5 mm hex key", "5mm hex")


def test_value_matches_rejects_wrong_value() -> None:
    # Negative-result guard: a planted-wrong number must NOT grade as a match.
    assert not value_matches("Torque to 25 N·m", "6 N-m")


def test_value_matches_expected_none_is_satisfied() -> None:
    assert value_matches(None, None)
    assert value_matches("anything", None)
    assert not value_matches(None, "5 N-m")


def test_unit_equivalent_accepts_consistent_pair() -> None:
    # 5 N·m ≈ 44 in-lb.
    assert unit_equivalent("Torque to 5 N·m (44 in-lb).")


def test_unit_equivalent_accepts_manual_rounding() -> None:
    # The manual prints 5.5 N·m as 49 in-lb (true value 48.7) — within tolerance.
    assert unit_equivalent("5.5 N·m (49 in-lb)")


def test_unit_equivalent_rejects_inconsistent_pair() -> None:
    # Negative-result guard: 5 N·m is ~44 in-lb, not 88 — a wrong conversion fails.
    assert not unit_equivalent("Torque to 5 N·m (88 in-lb).")


def test_unit_equivalent_passes_when_only_one_unit() -> None:
    assert unit_equivalent("Torque to 5 N·m.")
    assert unit_equivalent("44 in-lb")
    assert unit_equivalent(None)


def test_provenance_ok_text_containment() -> None:
    assert provenance_ok("step 3: 40 N·m (354 in-lb)", "40 N-m")
    assert not provenance_ok("step 3: tighten firmly", "40 N-m")


def test_provenance_ok_verified_figure_page_escape_hatch() -> None:
    # Figure-page citations cannot be text-checked (docling loses callouts), so
    # a verified figure page passes provenance regardless of (empty) text.
    assert provenance_ok("", "5.5 N-m", is_verified_figure_page=True)


def test_is_abstention_low_confidence_no_torque() -> None:
    assert is_abstention(None, 0.1)
    assert is_abstention("", 0.2)


def test_is_abstention_confident_torque_is_not_abstention() -> None:
    # A confident specific torque for an out-of-corpus probe is a hallucination.
    assert not is_abstention("25 N-m", 0.9)


def test_is_abstention_low_confidence_torque_still_abstains() -> None:
    # Returned a value but flagged it as a best-guess (<=0.5) — counts as abstain.
    assert is_abstention("25 N-m", 0.3)


def test_is_hallucination_is_inverse_of_abstention() -> None:
    confident = ProbeOutcome(torque="25 N-m", confidence=0.9)
    abstained = ProbeOutcome(torque=None, confidence=0.1)
    assert is_hallucination(confident)
    assert not is_hallucination(abstained)
