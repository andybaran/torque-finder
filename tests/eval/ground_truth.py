"""Canonical ground truth from ``thoughts.md``.

The PDF in question is ``pdf.pdf`` at the project root. ``page_no`` matches
the page number printed in the lower-right of each page (which equals the
1-indexed PDF page number for this document).

Each case carries a natural-language ``query`` a mechanic might ask. These
queries are placeholders — the project owner will refine them as real
mechanic phrasings are gathered.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GroundTruthCase:
    case_id: str
    page_no: int
    tool_size: str | None
    torque: str | None
    query: str


GROUND_TRUTH: list[GroundTruthCase] = [
    GroundTruthCase(
        case_id="p27",
        page_no=27,
        tool_size="7-8 mm",
        torque=None,
        query="What size tool is needed for the 7 to 8 mm fitting on page 27?",
    ),
    GroundTruthCase(
        case_id="p28",
        page_no=28,
        tool_size=None,
        torque="40 N-m",
        query="What torque value applies to the component on page 28?",
    ),
    GroundTruthCase(
        case_id="p31",
        page_no=31,
        tool_size="5mm hex",
        torque="11 N-m",
        query="What tool and torque are needed for the 5mm hex fitting on page 31?",
    ),
    GroundTruthCase(
        case_id="p50-a",
        page_no=50,
        tool_size="T25",
        torque="3 N-m",
        query="What tool and torque are needed for the T25 fastener on page 50 with a 3 N-m spec?",
    ),
    GroundTruthCase(
        case_id="p50-b",
        page_no=50,
        tool_size="T25",
        torque="2 N-m",
        query="What tool and torque are needed for the T25 fastener on page 50 with a 2 N-m spec?",
    ),
    GroundTruthCase(
        case_id="p51-a",
        page_no=51,
        tool_size="4mm hex",
        torque="5.5 N-m",
        query="What tool and torque are needed for the 4mm hex / T25 fitting on page 51?",
    ),
    GroundTruthCase(
        case_id="p51-b",
        page_no=51,
        tool_size="T25",
        torque="3 N-m",
        query="What tool and torque are needed for the T25 fastener on page 51 with a 3 N-m spec?",
    ),
    GroundTruthCase(
        case_id="p51-c",
        page_no=51,
        tool_size="2.5mm hex",
        torque="2 N-m",
        query="What tool and torque are needed for the 2.5mm hex fitting on page 51?",
    ),
    GroundTruthCase(
        case_id="p51-d",
        page_no=51,
        tool_size="T25",
        torque="3 N-m",
        query="What tool and torque are needed for the second T25 fastener on page 51 at 3 N-m?",
    ),
]
