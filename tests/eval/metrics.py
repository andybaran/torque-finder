"""Pure metric aggregation for the quality eval gate (issue #34).

No I/O. Given a list of graded records (built by the live suites, or by hand in
the offline tests), compute the headline metrics the issue asks one command to
print: pass rate, recall@k, contamination %, hallucination %, tool-size
completeness, and a per-failure-mode histogram. :func:`compare_to_baseline`
emits the deltas a fix has to show.

The failure-mode vocabulary is lifted verbatim from the throwaway round-2
workflow (``docs/quality-eval/torque-quality-r2-eval-wf_*.js``) so the
histogram keys stay stable across the codified gate and the historical runs.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field

# Lifted verbatim from FAIL_MODES in the round-2 eval workflow script.
FAIL_MODES: tuple[str, ...] = (
    "correct",
    "hallucinated_out_of_corpus",
    "confirmed_wrong_premise",
    "paraphrase_recall_miss",
    "wrong_value",
    "nondeterministic_value",
    "inconsistent_unit_conversion",
    "figure_wrong_value",
    "figure_abstain_should_answer",
    "range_truncated",
    "missing_procedure_caveat",
    "http_502",
    "http_error",
    "other",
    "skip",
)

_FAIL_MODE_SET = frozenset(FAIL_MODES)


@dataclass(frozen=True)
class GradedRecord:
    """One graded eval case (source-grounded or adversarial).

    ``passed`` is the boolean verdict. ``failure_mode`` is one of
    :data:`FAIL_MODES` and is only meaningful when ``passed`` is False.
    ``retrieved_expected`` records whether the right source appeared anywhere in
    the top-k hits (recall@k), independent of whether extraction got the value.
    ``tool_size_expected`` / ``tool_size_present`` drive tool-size completeness.
    """

    case_id: str
    passed: bool
    failure_mode: str = "correct"
    retrieved_expected: bool = False
    tool_size_expected: bool = False
    tool_size_present: bool = False

    def __post_init__(self) -> None:
        if self.failure_mode not in _FAIL_MODE_SET:
            raise ValueError(
                f"unknown failure_mode {self.failure_mode!r}; must be one of FAIL_MODES"
            )


@dataclass(frozen=True)
class EvalMetrics:
    """Aggregate metrics over a set of :class:`GradedRecord`."""

    n: int
    n_pass: int
    pass_rate: float
    recall_at_k: float
    contamination_pct: float
    tool_size_completeness: float
    failure_mode_histogram: dict[str, int] = field(default_factory=dict)


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def aggregate(records: list[GradedRecord]) -> EvalMetrics:
    """Aggregate source-grounded graded records into the headline metric set."""
    n = len(records)
    n_pass = sum(1 for r in records if r.passed)
    histogram = Counter(r.failure_mode for r in records if not r.passed)

    # Contamination = answered from the WRONG product's manual (in-corpus). The
    # round-2 vocab calls that wrong_value when the value is wrong; the
    # dedicated wrong-doc signal is the wrong_value share of failures.
    contamination = sum(1 for r in records if r.failure_mode == "wrong_value")

    tool_expected = [r for r in records if r.tool_size_expected]
    tool_present = sum(1 for r in tool_expected if r.tool_size_present)

    recall_hits = sum(1 for r in records if r.retrieved_expected)

    return EvalMetrics(
        n=n,
        n_pass=n_pass,
        pass_rate=_ratio(n_pass, n),
        recall_at_k=_ratio(recall_hits, n),
        contamination_pct=100.0 * _ratio(contamination, n),
        tool_size_completeness=_ratio(tool_present, len(tool_expected)),
        failure_mode_histogram=dict(histogram),
    )


@dataclass(frozen=True)
class RecallMetrics:
    """Aggregate recall@k over the #29 recall regression set.

    ``recalled`` counts cases where a chunk documenting the expected value (and
    within the named document family) appeared anywhere in the top-k retrieved
    hits, *independent of extraction*. This isolates the Stage 1-3 retrieval
    fix from the abstain/extraction behavior #28/#32 own.
    """

    k: int
    n: int
    n_recalled: int
    recall_at_k: float
    missed_case_ids: tuple[str, ...] = ()


def recall_at_k(per_case_recalled: dict[str, bool], *, k: int) -> RecallMetrics:
    """Aggregate a {case_id: recalled?} map into a RecallMetrics for depth ``k``."""
    n = len(per_case_recalled)
    n_recalled = sum(1 for ok in per_case_recalled.values() if ok)
    missed = tuple(cid for cid, ok in per_case_recalled.items() if not ok)
    return RecallMetrics(
        k=k,
        n=n,
        n_recalled=n_recalled,
        recall_at_k=_ratio(n_recalled, n),
        missed_case_ids=missed,
    )


@dataclass(frozen=True)
class AdversarialMetrics:
    """Aggregate metrics over the out-of-corpus adversarial probes (lens A)."""

    n_probes: int
    n_hallucinated: int
    hallucination_rate: float
    by_ptype: dict[str, dict[str, int]] = field(default_factory=dict)


@dataclass(frozen=True)
class ProbeRecord:
    """One graded adversarial probe.

    ``hallucinated`` is True if the model returned a confident specific torque
    for the out-of-corpus / fabricated / edge target, or confirmed a
    wrong-premise's planted false number.
    """

    probe_id: str
    ptype: str
    hallucinated: bool


def aggregate_adversarial(records: list[ProbeRecord]) -> AdversarialMetrics:
    """Aggregate adversarial probe records into a hallucination rate + ptype split."""
    n = len(records)
    n_hall = sum(1 for r in records if r.hallucinated)

    by_ptype: dict[str, dict[str, int]] = {}
    for r in records:
        bucket = by_ptype.setdefault(r.ptype, {"probes": 0, "hallucinated": 0})
        bucket["probes"] += 1
        if r.hallucinated:
            bucket["hallucinated"] += 1

    return AdversarialMetrics(
        n_probes=n,
        n_hallucinated=n_hall,
        hallucination_rate=_ratio(n_hall, n),
        by_ptype=by_ptype,
    )


@dataclass(frozen=True)
class BaselineDelta:
    """A single metric's current value, its recorded baseline, and the delta."""

    metric: str
    baseline: float
    current: float
    delta: float
    regressed: bool


def compare_to_baseline(
    current: dict[str, float],
    baseline: dict[str, float],
    *,
    higher_is_better: frozenset[str] = frozenset(
        {"pass_rate", "recall_at_k", "tool_size_completeness"}
    ),
    tolerance: float = 0.0,
) -> list[BaselineDelta]:
    """Emit per-metric deltas against a recorded baseline.

    For metrics in ``higher_is_better`` a drop below ``baseline - tolerance`` is
    a regression; for the rest (hallucination rate, contamination %) a rise
    above ``baseline + tolerance`` is a regression. ``tolerance`` lets the gate
    absorb run-to-run noise without flagging a phantom regression.
    """
    deltas: list[BaselineDelta] = []
    for metric, base in baseline.items():
        cur = current.get(metric, 0.0)
        delta = cur - base
        regressed = (
            cur < base - tolerance
            if metric in higher_is_better
            else cur > base + tolerance
        )
        deltas.append(
            BaselineDelta(
                metric=metric,
                baseline=base,
                current=cur,
                delta=delta,
                regressed=regressed,
            )
        )
    return deltas


def metrics_as_dict(metrics: EvalMetrics) -> dict[str, float]:
    """Flatten :class:`EvalMetrics` to the scalar metrics for baseline comparison."""
    d = asdict(metrics)
    d.pop("failure_mode_histogram", None)
    return {k: float(v) for k, v in d.items()}
