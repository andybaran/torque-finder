"""Pure, deterministic grading primitives for the quality eval gate.

No I/O, no network, no API keys — every function here is a pure transformation
of strings/dataclasses, which is what makes the offline grader tests in
``tests/unit/test_eval_grading_offline.py`` a $0 CI gate (issue #34).

The eval suites import these so there is exactly ONE notation matcher, ONE
unit-equivalence check, and ONE abstention rule across the whole gate. The
canonical ``thoughts.md`` smoke suite (``test_eval_smoke.py``) and the HTML
suite (``test_eval_html.py``) re-export :func:`normalize` / :func:`matches`
from here so the duplicate definitions that used to live in those modules are
gone.

Why deterministic (and not an LLM judge): the throwaway investigation scripts
used Claude both to invent questions and to judge correctness, which is
non-deterministic and re-incurs cost on every run — unfit for a repeatable
gate. Keeping the grader deterministic (notation + unit-conversion equivalence,
provenance via chunk-text/page containment, abstain detection via confidence +
null torque) is what makes the gate cheap, comparable run-to-run, and
offline-testable. Swapping in an LLM judge would be a new-infrastructure
decision (CLAUDE.md rule 1: three-alternatives interview first).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 1 N·m ≈ 8.8507 in-lb. The round trips through manuals carry rounding, so the
# consistency check uses a relative tolerance band rather than exact equality.
_IN_LB_PER_NM = 8.8507
_UNIT_TOLERANCE = 0.12  # ±12 %: covers manual rounding (e.g. 5 N·m printed as 44 in-lb)

# Abstention threshold (lens A): the throwaway probe harness treated a returned
# torque with confidence > 0.5 as a hallucination, i.e. NOT an abstention. We
# keep that same boundary so the recorded baseline stays comparable.
_ABSTAIN_CONFIDENCE_MAX = 0.5

_NM_VALUE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*n-m")
_IN_LB_VALUE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*in-lb")


def normalize(value: str) -> str:
    """Collapse notation variants so the same number+unit always compares equal.

    Only notation is normalized — the digits and units themselves must still
    match exactly:
    - newton-metre separators: ``N-m`` / ``N·m`` / ``N⋅m`` / ``Nm`` / ``N m``
      (the PDF prints ``N·m``; ``thoughts.md`` writes ``N-m``)
    - number/unit spacing: ``4 mm`` ≡ ``4mm``
    - in-lb spellings: ``in-lb`` / ``in. lb`` / ``in lb`` / ``inlb`` ≡ ``in-lb``
    - numeric ranges: ``7 to 8`` / ``7 and 8`` / en- or em-dash ranges ≡ ``7-8``
    - Torx spelling: ``T-25`` ≡ ``T25``
    """
    normalized = value.lower()
    normalized = re.sub(r"(?<![a-z])n\s*[·⋅-]?\s*m(?![a-z])", "n-m", normalized)
    normalized = re.sub(r"(?<![a-z])in[.\s-]*lb(?![a-z])", "in-lb", normalized)
    normalized = re.sub(r"(\d)\s+mm(?![a-z])", r"\1mm", normalized)
    dashes = "\u2013\u2014"  # en dash, em dash (ruff RUF001 bans the literals)
    normalized = re.sub(
        r"(\d(?:\.\d+)?)\s*(?:[-" + dashes + r"]|to|and)\s*(\d)", r"\1-\2", normalized
    )
    normalized = re.sub(r"(?<![a-z])t\s*-\s*(\d)", r"t\1", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def matches(actual: str | None, expected: str | None) -> bool:
    """Notation-normalized substring match; ``expected=None`` is always satisfied."""
    if expected is None:
        return True
    if actual is None:
        return False
    return normalize(expected) in normalize(actual)


def value_matches(actual: str | None, expected: str | None) -> bool:
    """Alias of :func:`matches` with the value-grading name used by the suites."""
    return matches(actual, expected)


def unit_equivalent(text: str | None) -> bool:
    """Are the N·m and in-lb figures in one answer string internally consistent?

    Lens D: a wrong conversion can mislead a mechanic. When an answer carries
    BOTH a N·m value and an in-lb value, they must agree within
    :data:`_UNIT_TOLERANCE` (1 N·m ≈ 8.85 in-lb). When only one unit (or
    neither) is present there is nothing to contradict, so the check passes —
    this is a *consistency* guard, not a "must state both units" requirement.
    """
    if text is None:
        return True
    norm = normalize(text)
    nm = _NM_VALUE_RE.findall(norm)
    in_lb = _IN_LB_VALUE_RE.findall(norm)
    if not nm or not in_lb:
        return True
    # Compare the first N·m value against the first in-lb value (manuals print
    # them as a pair "X N·m (Y in-lb)").
    nm_val = float(nm[0])
    in_lb_val = float(in_lb[0])
    if nm_val == 0:
        return in_lb_val == 0
    expected_in_lb = nm_val * _IN_LB_PER_NM
    rel_error = abs(in_lb_val - expected_in_lb) / expected_in_lb
    return rel_error <= _UNIT_TOLERANCE


def provenance_ok(
    chunk_text: str | None,
    expected_value: str,
    *,
    is_verified_figure_page: bool = False,
) -> bool:
    """Is the citation genuine — does the cited source actually document the value?

    Either the cited chunk's text (notation-normalized) contains the pinned
    value, OR the hit is a figure page whose rendered PNG was manually verified
    to show the value in its callouts (docling's text extraction of figure
    callouts is lossy, so page identity stands in for text containment there).
    """
    if is_verified_figure_page:
        return True
    if chunk_text is None:
        return False
    return normalize(expected_value) in normalize(chunk_text)


@dataclass(frozen=True)
class ProbeOutcome:
    """A single out-of-corpus probe result, as the grader sees it.

    Mirrors what the live suite extracts from one ``/v1/query`` response:
    the returned torque (or None), the model's confidence, and whether the
    probe is a wrong-premise trap (which must be *corrected*, not merely
    abstained on).
    """

    torque: str | None
    confidence: float
    answer_text: str = ""


def is_abstention(torque: str | None, confidence: float) -> bool:
    """Did the model correctly decline to invent a spec?

    Lens A correct behavior on an out-of-corpus / fabricated / edge probe is to
    ABSTAIN: return no specific torque AND keep confidence low. Returning a
    concrete torque at confidence > 0.5 is a hallucination, not an abstention.
    """
    has_specific_torque = torque is not None and torque.strip() != ""
    return not (has_specific_torque and confidence > _ABSTAIN_CONFIDENCE_MAX)


def is_hallucination(outcome: ProbeOutcome) -> bool:
    """Inverse of :func:`is_abstention` for an out-of-corpus probe outcome.

    A probe HALLUCINATED if it returned a confident specific torque for a
    target that is not in the corpus.
    """
    return not is_abstention(outcome.torque, outcome.confidence)
