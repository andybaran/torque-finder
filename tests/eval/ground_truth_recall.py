"""Recall regression set for issue #29 — the eight evidence rows.

These are the eight failing cases from the #29 issue body: specs that
**demonstrably exist** in an indexed manual but that retrieval failed to
surface within ``top_k``, so extraction abstained and the mechanic got a
confident "not found."

Unlike the value-pinned smoke gate (``ground_truth.py``), this set grades
**retrieval recall in isolation** — does a chunk that documents the expected
value appear anywhere in the top-k *retrieved* hits, independent of what Claude
does with them? That cleanly separates the Stage 1-3 retrieval fix (#29) from
the extraction/abstain behavior that #28/#32 own.

Family matching, not strict document identity
----------------------------------------------
Five of the eight cases are the **byte-identical** RockShox damper/eyelet/piston
spec repeated across the whole Monarch/Vivid manual family. The named document
the mechanic asked about is the recall target, but a same-family sibling page
carrying the identical spec text is an acceptable recall hit — so each case
pins a set of ``family_tokens`` (e.g. {"monarch"}) and recall succeeds when a
top-k hit (a) documents the value and (b) belongs to a document whose title
shares the family tokens. Strict single-document identity is NOT asserted here
(that is the smoke gate's ``strict`` knob, used only for corpus-unique values).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RecallCase:
    """One #29 evidence row, graded for retrieval recall.

    ``recall_value`` is the string that must appear (notation-normalized) in a
    retrieved chunk's text for the case to count as recalled. ``family_tokens``
    are lowercased document-title tokens that the recalled hit's title must all
    contain, so a hit only counts if it is within the named manual family.
    ``page_no`` and ``document_title`` are documentation of the canonical source
    (not asserted strictly — see module docstring).
    """

    case_id: str
    document_title: str
    page_no: int
    query: str
    recall_value: str
    family_tokens: tuple[str, ...]
    tool_size: str | None = None
    alt_recall_values: tuple[str, ...] = field(default_factory=tuple)


# The eight evidence rows, in issue-body order. Queries are realistic shop
# phrasings that name the product/year (so Stage 3's title rerank has a signal)
# without ever naming the page number.
RECALL_GROUND_TRUTH: list[RecallCase] = [
    RecallCase(
        case_id="r1-2010-sram-red-force-rival",
        document_title="2010-sram-technical-manual.pdf",
        page_no=8,
        query=(
            "What assembly torque for a 2010 SRAM RED / Force / Rival rear "
            "derailleur?"
        ),
        recall_value="5 N-m",
        family_tokens=("2010",),
    ),
    RecallCase(
        case_id="r2-2011-monarch-plus",
        document_title="2011-monarch-plus-service-manual.pdf",
        page_no=15,
        query=(
            "What socket and torque to thread the piston assembly onto the "
            "shaft on a 2011 Monarch Plus?"
        ),
        recall_value="4.5 N-m",
        family_tokens=("monarch",),
        tool_size="10 mm",
    ),
    RecallCase(
        case_id="r3-2011-vivid-air",
        document_title="2011-vivid-air-service-manual.pdf",
        page_no=16,
        query=(
            "Eyelet thread-on torque and crow's foot size for a 2011 Vivid "
            "Air shock?"
        ),
        recall_value="26 N-m",
        family_tokens=("vivid",),
        tool_size="13 mm",
    ),
    RecallCase(
        case_id="r4-2012-monarch-plus",
        document_title="2012-monarch-plus-service-manual.pdf",
        page_no=24,
        query=(
            "Socket and torque to thread the main piston onto the damper "
            "shaft on a 2012 Monarch Plus?"
        ),
        recall_value="4.5 N-m",
        family_tokens=("monarch",),
        tool_size="10 mm",
    ),
    RecallCase(
        case_id="r5-2012-monarch",
        document_title="2012-monarch-service-manual.pdf",
        page_no=25,
        query=(
            "Socket and torque to thread the main piston onto the damper "
            "shaft on a 2012 Monarch?"
        ),
        recall_value="4.5 N-m",
        family_tokens=("monarch",),
        tool_size="10 mm",
    ),
    RecallCase(
        case_id="r6-2013-2018-reverb-stealth",
        document_title="2013-2018-reverb-stealth-a2-and-b1-service-manual.pdf",
        page_no=8,
        query=(
            "Wrench and torque for the internal seal head on a Reverb Stealth "
            "dropper post?"
        ),
        recall_value="28 N-m",
        family_tokens=("reverb",),
        tool_size="23 mm",
    ),
    RecallCase(
        case_id="r7-2013-monarch-rl-rt",
        document_title="2013-monarch-rl-rt-service-manual.pdf",
        page_no=29,
        query=(
            "Crowfoot size and torque for the seal head eyelet on a 2013 "
            "Monarch RL/RT?"
        ),
        recall_value="4.6 N-m",
        family_tokens=("monarch",),
        tool_size="13 mm",
    ),
    RecallCase(
        case_id="r8-2014-kage",
        document_title="2014-present-kage-service-manual-.pdf",
        page_no=11,
        query=(
            "What socket to unthread the main piston nut on a RockShox Kage "
            "shock?"
        ),
        recall_value="17 mm",
        family_tokens=("kage",),
        tool_size="17 mm",
    ),
]

# Sanity: the issue body pins exactly eight evidence rows.
assert len(RECALL_GROUND_TRUTH) == 8
