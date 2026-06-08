"""Anthropic Claude client for the extraction bounded context.

Takes a query plus a small set of candidate page images, sends them to Claude
with vision, and parses a structured ``Answer``. Prompt caching is applied to
the system prompt so repeated queries (which all share the same prompt) only
pay full input cost once per cache window.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any

import anthropic

from parts_lookup.config import Settings
from parts_lookup.domain.errors import ExtractionError
from parts_lookup.domain.models import Answer
from parts_lookup.extraction.prompt import SYSTEM_PROMPT


@dataclass(frozen=True)
class ExtractionCandidate:
    """A single page image being considered as a possible answer source."""

    pdf_id: int
    page_no: int
    png_bytes: bytes


def _strip_json_fences(text: str) -> str:
    """Tolerate the model wrapping JSON in ```json ... ``` despite instructions."""
    stripped = text.strip()
    if stripped.startswith("```"):
        # Drop the opening fence (which may be ```json or just ```).
        first_newline = stripped.find("\n")
        if first_newline != -1:
            stripped = stripped[first_newline + 1 :]
        if stripped.endswith("```"):
            stripped = stripped[: -len("```")]
    return stripped.strip()


class ClaudeExtractor:
    """Sends candidate pages + a query to Claude and returns a parsed Answer."""

    def __init__(
        self,
        settings: Settings,
        client: anthropic.AsyncAnthropic | None = None,
    ) -> None:
        self._settings = settings
        self._stub = settings.stub_external_apis
        if self._stub:
            self._client: anthropic.AsyncAnthropic | None = None
        else:
            self._client = client or anthropic.AsyncAnthropic(
                api_key=settings.anthropic_api_key.get_secret_value()
            )

    async def extract(
        self,
        query: str,
        candidates: list[ExtractionCandidate],
    ) -> Answer:
        if not candidates:
            raise ExtractionError("extract() requires at least one candidate page")

        if self._stub:
            top = candidates[0]
            return Answer(
                text=(
                    f"[STUB EXTRACTION] Top candidate is page {top.page_no} "
                    f"of PDF {top.pdf_id}. Real Claude call skipped because "
                    "STUB_EXTERNAL_APIS=true."
                ),
                tool_size=None,
                torque=None,
                source_pdf_id=top.pdf_id,
                source_page_no=top.page_no,
                confidence=0.5,
            )

        assert self._client is not None
        user_blocks = self._build_user_blocks(query, candidates)

        try:
            response = await self._client.messages.create(
                model=self._settings.anthropic_model,
                max_tokens=1024,
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user_blocks}],
            )
        except anthropic.APIError as exc:
            raise ExtractionError("Claude API call failed") from exc

        return self._parse_response(response, candidates)

    @staticmethod
    def _build_user_blocks(
        query: str,
        candidates: list[ExtractionCandidate],
    ) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        for candidate in candidates:
            encoded = base64.standard_b64encode(candidate.png_bytes).decode()
            blocks.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": encoded,
                    },
                }
            )
            blocks.append(
                {
                    "type": "text",
                    "text": (
                        f"Above is page {candidate.page_no} of PDF "
                        f"{candidate.pdf_id}."
                    ),
                }
            )

        blocks.append(
            {
                "type": "text",
                "text": (
                    f"Question: {query}\n\n"
                    "Answer the question using only these pages. "
                    "Reply ONLY with JSON matching the schema."
                ),
            }
        )
        return blocks

    @staticmethod
    def _parse_response(
        response: anthropic.types.Message,
        candidates: list[ExtractionCandidate],
    ) -> Answer:
        if not response.content:
            raise ExtractionError("Claude returned an empty response")

        first = response.content[0]
        if first.type != "text":
            raise ExtractionError(
                f"Expected first content block to be text, got {first.type!r}"
            )

        raw_text = _strip_json_fences(first.text)

        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise ExtractionError(
                "Claude response was not valid JSON after fence stripping"
            ) from exc

        try:
            source_page_no = int(payload["source_page_no"])
            answer_text = str(payload["answer"])
            tool_size = payload.get("tool_size")
            torque = payload.get("torque")
            confidence = float(payload["confidence"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ExtractionError(
                "Claude response JSON did not match the expected schema"
            ) from exc

        # Resolve which candidate the model pointed at — guarantees we never
        # surface a page_no that wasn't actually in the request.
        match = next(
            (c for c in candidates if c.page_no == source_page_no),
            None,
        )
        if match is None:
            raise ExtractionError(
                f"Claude cited page_no={source_page_no}, which was not among "
                f"the supplied candidates"
            )

        try:
            return Answer(
                text=answer_text,
                tool_size=tool_size if tool_size is None else str(tool_size),
                torque=torque if torque is None else str(torque),
                source_pdf_id=match.pdf_id,
                source_page_no=match.page_no,
                confidence=confidence,
            )
        except (ValueError, TypeError) as exc:
            raise ExtractionError(
                "Claude response failed Answer validation"
            ) from exc
