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
from anthropic.types.json_output_format_param import JSONOutputFormatParam
from anthropic.types.output_config_param import OutputConfigParam

from parts_lookup.config import Settings
from parts_lookup.domain.errors import ExtractionError
from parts_lookup.domain.models import Answer, SourceType
from parts_lookup.extraction.prompt import OUTPUT_SCHEMA, SYSTEM_PROMPT
from parts_lookup.observability import get_logger

_log = get_logger(__name__)

# How much of the raw model text to keep in failure logs. Long enough to see a
# truncated/near-miss payload, short enough to keep log lines manageable.
_RAW_TEXT_LOG_LIMIT = 2000


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

        # Enforce the structured-output contract at the API: the first content
        # block is guaranteed to be schema-valid JSON text. OUTPUT_SCHEMA is the
        # single source of truth — downstream issues extend it and the change
        # flows through here automatically. See #25: free-form JSON parsed ~6%
        # non-conforming on real traffic. The post-parse path below stays as
        # defense-in-depth (refusal/max_tokens can still yield non-conforming
        # text, and structured outputs strips numeric constraints like
        # source_index>=1, which _parse_response still enforces).
        output_config: OutputConfigParam = {
            "format": JSONOutputFormatParam(
                type="json_schema", schema=OUTPUT_SCHEMA
            )
        }

        try:
            response = await self._client.messages.create(
                model=self._settings.anthropic_model,
                max_tokens=self._settings.extraction_max_tokens,
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user_blocks}],
                output_config=output_config,
            )
        except anthropic.APIError as exc:
            # #33 will split this block by error type (throttling/billing vs.
            # other upstream failures). Keep the single ExtractionError mapping
            # for now so that path stays the clean "Claude never answered"
            # (upstream) signal, distinct from the parse failures below.
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

    @classmethod
    def _parse_response(
        cls,
        response: anthropic.types.Message,
        candidates: list[ExtractionCandidate],
    ) -> Answer:
        stop_reason = getattr(response, "stop_reason", None)

        # Branch on terminal stop reasons before parsing: structured outputs is
        # bypassed when the model truncates or refuses, so the text block (if
        # any) will not conform. Turn the opaque 502 into a labeled one so a
        # future retry/heuristic can react and the log says which it was.
        if stop_reason == "max_tokens":
            cls._log_failure(response, "extraction_truncated")
            raise ExtractionError(
                "Claude response was truncated (stop_reason=max_tokens); "
                "the JSON answer did not fit in max_tokens"
            )
        if stop_reason == "refusal":
            cls._log_failure(response, "extraction_refusal")
            raise ExtractionError(
                "Claude declined to answer (stop_reason=refusal)"
            )

        if not response.content:
            cls._log_failure(response, "extraction_empty_response")
            raise ExtractionError("Claude returned an empty response")

        first = response.content[0]
        if first.type != "text":
            cls._log_failure(response, "extraction_non_text_block")
            raise ExtractionError(
                f"Expected first content block to be text, got {first.type!r}"
            )

        raw_text = _strip_json_fences(first.text)

        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            cls._log_failure(response, "extraction_invalid_json", raw_text=raw_text)
            raise ExtractionError(
                "Claude response was not valid JSON after fence stripping"
            ) from exc

        try:
            answer_text = str(payload["answer"])
            confidence = float(payload["confidence"])
        except (KeyError, TypeError, ValueError) as exc:
            cls._log_failure(response, "extraction_schema_mismatch", raw_text=raw_text)
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
                cls._log_failure(
                    response, "extraction_schema_mismatch", raw_text=raw_text
                )
                raise ExtractionError(
                    "Claude response JSON did not match the expected schema"
                ) from exc
            match = next(
                (c for c in candidates if c.index == source_index),
                None,
            )
            if match is None:
                cls._log_failure(
                    response, "extraction_unknown_source_index", raw_text=raw_text
                )
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
            cls._log_failure(response, "extraction_answer_validation", raw_text=raw_text)
            raise ExtractionError(
                "Claude response failed Answer validation"
            ) from exc

    @staticmethod
    def _log_failure(
        response: anthropic.types.Message,
        event: str,
        *,
        raw_text: str | None = None,
    ) -> None:
        """Emit a structured log line so a rare extraction 502 is diagnosable.

        Carries the model's ``stop_reason``, token usage, request id, and a
        truncated copy of the raw text the parse choked on. ``_request_id`` is
        read null-safely — a missing attr (e.g. a hand-built test response)
        must never turn a parse failure into a second exception.
        """
        usage = getattr(response, "usage", None)
        _log.warning(
            event,
            stop_reason=getattr(response, "stop_reason", None),
            request_id=getattr(response, "_request_id", None),
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
            raw_text=raw_text[:_RAW_TEXT_LOG_LIMIT] if raw_text else None,
        )
