"""Anthropic Claude client for the extraction bounded context.

Takes a query plus a small set of candidate sources (PDF page images and/or
HTML manual sections), sends them to Claude, and parses a structured
``Answer``. Prompt caching is applied to the system prompt so repeated queries
(which all share the same prompt) only pay full input cost once per cache
window.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any

import anthropic

from parts_lookup.config import Settings
from parts_lookup.domain.errors import ExtractionError
from parts_lookup.domain.models import Answer, SourceType
from parts_lookup.extraction.prompt import SYSTEM_PROMPT


@dataclass(frozen=True)
class ExtractionCandidate:
    """One numbered source being considered as a possible answer origin.

    ``index`` is 1-based and is what Claude cites back as ``source_index``.
    PDF candidates carry ``png_bytes`` (vision); HTML candidates carry
    ``text`` — the parent module reconstructed from sibling chunks.
    """

    index: int
    source_type: SourceType
    label: str
    png_bytes: bytes | None = None
    text: str | None = None


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
    """Sends candidate sources + a query to Claude and returns a parsed Answer."""

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
            raise ExtractionError("extract() requires at least one candidate source")

        if self._stub:
            top = candidates[0]
            return Answer(
                text=(
                    f"[STUB EXTRACTION] Top candidate is source {top.index} "
                    f"({top.label}). Real Claude call skipped because "
                    "STUB_EXTERNAL_APIS=true."
                ),
                tool_size=None,
                torque=None,
                source_index=top.index,
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
            if candidate.source_type is SourceType.PDF:
                if candidate.png_bytes is None:
                    raise ExtractionError(
                        f"PDF candidate {candidate.index} is missing png_bytes"
                    )
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
                            f"Above is source {candidate.index}: {candidate.label}."
                        ),
                    }
                )
            else:
                if candidate.text is None:
                    raise ExtractionError(
                        f"HTML candidate {candidate.index} is missing text"
                    )
                blocks.append(
                    {
                        "type": "text",
                        "text": (
                            f"Source {candidate.index}: {candidate.label}\n"
                            f"---\n{candidate.text}\n---"
                        ),
                    }
                )

        blocks.append(
            {
                "type": "text",
                "text": (
                    f"Question: {query}\n\n"
                    "Answer the question using only these sources. "
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
            answer_text = str(payload["answer"])
            confidence = float(payload["confidence"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ExtractionError(
                "Claude response JSON did not match the expected schema"
            ) from exc

        tool_size = payload.get("tool_size")
        torque = payload.get("torque")
        raw_index = payload.get("source_index")

        # Resolve which candidate the model pointed at — guarantees we never
        # surface a source that wasn't actually in the request.
        if raw_index is None:
            # "Not found" path: fall back to the top candidate so the response
            # still carries a deep link; low confidence flags it as best-guess.
            match = candidates[0]
        else:
            try:
                source_index = int(raw_index)
            except (TypeError, ValueError) as exc:
                raise ExtractionError(
                    "Claude response JSON did not match the expected schema"
                ) from exc
            match = next(
                (c for c in candidates if c.index == source_index),
                None,
            )
            if match is None:
                raise ExtractionError(
                    f"Claude cited source_index={source_index}, which was not "
                    f"among the supplied candidates"
                )

        try:
            return Answer(
                text=answer_text,
                tool_size=tool_size if tool_size is None else str(tool_size),
                torque=torque if torque is None else str(torque),
                source_index=match.index,
                confidence=confidence,
            )
        except (ValueError, TypeError) as exc:
            raise ExtractionError(
                "Claude response failed Answer validation"
            ) from exc
