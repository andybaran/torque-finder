"""Ground truth for the Red AXS HTML publications (spec §8).

Expected values come from the 2026-06-08 investigation of
docs.sram.com/en-US/publications/6TmfV97fHWv8kvGXVegoTy: image caption2
fields carry torque values (e.g. "40 N·m (354 in-lb)") and the toolList
gives tool sizes verbatim (Hex: 2, 2.5, 3, 4, 5, 8 mm; TORX: T25).

NOTE for maintainers: after the first real ingest, sanity-check each query
against the live publication and refine the *phrasing* if retrieval misses —
the expected substrings are facts from the manual and must not be loosened.

Post-ingest phrasing refinements (2026-06-09, per the note above):
- "40 N·m (354 in-lb)" is the *cassette lockring* torque in this publication
  (crank arm bolts are 54 N·m), so the torque case asks about the lockring.
- The publication-level toolList has no anchor on docs.sram.com (no #hash is
  possible), and HTML candidate labels carry no document title (so queries
  that hinge on *naming* the manual stall extraction). The TORX case
  therefore targets the anchored 6-bolt rotor installation module, which
  specifies T25 for the rotor bolts.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HtmlGroundTruthCase:
    case_id: str
    query: str
    tool_contains: str | None
    torque_contains: str | None


HTML_GROUND_TRUTH: list[HtmlGroundTruthCase] = [
    HtmlGroundTruthCase(
        case_id="red-axs-cassette-lockring-torque",
        query=(
            "In the SRAM Road AXS digital user manual, what torque does the "
            "cassette lockring get?"
        ),
        tool_contains=None,
        torque_contains="40 N",
    ),
    HtmlGroundTruthCase(
        case_id="red-axs-torx-size",
        query=(
            "Installing a 6-bolt rotor with new threadlock-prepped rotor "
            "bolts on my Road AXS bike - what TORX size and torque?"
        ),
        tool_contains="T25",
        torque_contains=None,
    ),
]
