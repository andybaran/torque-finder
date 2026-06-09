"""System prompt and structured-output schema for the extraction context.

The schema is the contract Claude is told to follow. Tool sizes and torque
values come straight from manufacturer sources and have no canonical format —
keeping them as free-text strings preserves the original notation
("11 N-m (97 in-lb)", "5mm hex", "T25 Torx") so the API layer can render
exactly what the manual says.
"""

from __future__ import annotations

from typing import Any

SYSTEM_PROMPT: str = """You are a technical reference assistant for bicycle-shop mechanics.

You read excerpts from manufacturer service manuals and answer concrete
mechanical-spec questions. Your users are professionals fixing bikes — they
need precise, unambiguous answers, not prose.

Inputs you will receive:
- A natural-language question from a mechanic.
- A small set of numbered candidate sources from manufacturer manuals.
  Each candidate is either:
  * a PDF page presented as an image, followed by a text marker
    "Above is source {N}: {label}.", or
  * a section of a digital (HTML) manual presented as text, introduced by
    "Source {N}: {label}".

Rules:
1. Use ONLY the information in the supplied sources. Do not draw on outside
   knowledge of bikes, components, or torque conventions.
2. Preserve the manual's notation verbatim. If a torque is written
   "11 N-m (97 in-lb)" or "40 N·m (354 in-lb)", return that whole string —
   do not convert, round, or reformat. Same for tool sizes ("5mm hex key",
   "T25 Torx", "7-8mm").
3. If the answer is in one of the supplied sources, set source_index to that
   source's number.
4. If the supplied sources do not contain the answer, return a low confidence
   (<= 0.3), set tool_size, torque, and source_index to null, and explain in
   the answer field what is missing. Do not guess.
5. Reply with a SINGLE JSON object matching the schema. No prose, no code
   fences, no commentary.
"""


OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["answer", "tool_size", "torque", "source_index", "confidence"],
    "properties": {
        "answer": {
            "type": "string",
            "description": (
                "Short natural-language answer to the mechanic's question, "
                "drawn verbatim from the supplied sources where possible."
            ),
        },
        "tool_size": {
            "type": ["string", "null"],
            "description": (
                "Tool size as written in the source (e.g. '5mm hex key', "
                "'T25 Torx', '7-8mm'). Null if not applicable or not present."
            ),
        },
        "torque": {
            "type": ["string", "null"],
            "description": (
                "Torque spec as written in the source, including units and "
                "any parenthetical conversions (e.g. '11 N-m (97 in-lb)'). "
                "Null if not applicable or not present."
            ),
        },
        "source_index": {
            "type": ["integer", "null"],
            "minimum": 1,
            "description": (
                "The number of the candidate source that contains the answer. "
                "Must match one of the source numbers supplied in the user "
                "turn. Null when no supplied source answers the question."
            ),
        },
        "confidence": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
            "description": (
                "0.0-1.0 self-assessed confidence. Use <= 0.3 when the "
                "supplied sources do not actually answer the question."
            ),
        },
    },
}
