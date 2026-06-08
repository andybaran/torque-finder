"""System prompt and structured-output schema for the extraction context.

The schema is the contract Claude is told to follow. Tool sizes and torque
values come straight from manufacturer pages and have no canonical format —
keeping them as free-text strings preserves the original notation
("11 N-m (97 in-lb)", "5mm hex", "T25 Torx") so the API layer can render
exactly what the manual says.
"""

from __future__ import annotations

from typing import Any

SYSTEM_PROMPT: str = """You are a technical reference assistant for bicycle-shop mechanics.

You read pages from manufacturer service manuals and answer concrete
mechanical-spec questions. Your users are professionals fixing bikes — they
need precise, unambiguous answers, not prose.

Inputs you will receive:
- A natural-language question from a mechanic.
- A small set of candidate pages from manufacturer PDFs, presented as images.
  Each page image is followed by a text marker identifying it as
  "page {page_no} of PDF {pdf_id}".

Rules:
1. Use ONLY the information visible in the supplied page images. Do not draw
   on outside knowledge of bikes, components, or torque conventions.
2. Preserve the manual's notation verbatim. If a torque is written
   "11 N-m (97 in-lb)", return that whole string — do not convert, round, or
   reformat. Same for tool sizes ("5mm hex key", "T25 Torx", "7-8mm").
3. If the answer is on one of the supplied pages, set source_page_no to the
   page_no marker that accompanied that page image.
4. If the supplied pages do not contain the answer, return a low confidence
   (<= 0.3), set tool_size and torque to null, and explain in the answer
   field what is missing. Do not guess.
5. Reply with a SINGLE JSON object matching the schema. No prose, no code
   fences, no commentary.
"""


OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["answer", "tool_size", "torque", "source_page_no", "confidence"],
    "properties": {
        "answer": {
            "type": "string",
            "description": (
                "Short natural-language answer to the mechanic's question, "
                "drawn verbatim from the supplied pages where possible."
            ),
        },
        "tool_size": {
            "type": ["string", "null"],
            "description": (
                "Tool size as written in the manual (e.g. '5mm hex key', "
                "'T25 Torx', '7-8mm'). Null if not applicable or not present."
            ),
        },
        "torque": {
            "type": ["string", "null"],
            "description": (
                "Torque spec as written in the manual, including units and "
                "any parenthetical conversions (e.g. '11 N-m (97 in-lb)'). "
                "Null if not applicable or not present."
            ),
        },
        "source_page_no": {
            "type": "integer",
            "minimum": 1,
            "description": (
                "The page_no marker of the candidate page that contains the "
                "answer. Must match one of the page_no markers supplied in "
                "the user turn."
            ),
        },
        "confidence": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
            "description": (
                "0.0-1.0 self-assessed confidence. Use <= 0.3 when the "
                "supplied pages do not actually answer the question."
            ),
        },
    },
}
