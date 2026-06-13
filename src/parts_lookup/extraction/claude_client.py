"""Anthropic Claude client for the extraction bounded context.

Takes a query plus a small set of candidate sources (PDF page images and/or
HTML manual sections), sends them to Claude, and parses a structured
``Answer``. Prompt caching is applied to the system prompt so repeated queries
(which all share the same prompt) only pay full input cost once per cache
window.
"""

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass
from typing import Any

import anthropic

# OverloadedError (529) and RequestTooLargeError (413) are NOT exported from the
# top-level `anthropic` namespace in 0.97.0 (hasattr(anthropic, "OverloadedError")
# is False); they live under the underscore-private `_exceptions`. This import
# path is an SDK-bump risk — re-verify on any anthropic version bump.
from anthropic._exceptions import OverloadedError, RequestTooLargeError
from anthropic.types.json_output_format_param import JSONOutputFormatParam
from anthropic.types.output_config_param import OutputConfigParam

from parts_lookup.config import Settings
from parts_lookup.domain.errors import (
    ExtractionError,
    ExtractionUpstreamUnauthorized,
    ExtractionUpstreamUnavailable,
)
from parts_lookup.domain.models import Answer, ProductScope, SourceType
from parts_lookup.extraction.prompt import OUTPUT_SCHEMA, SYSTEM_PROMPT
from parts_lookup.observability import get_logger
from parts_lookup.retrieval.product_match import (
    extract_query_scope,
    normalize_product,
    scope_matches,
)

_log = get_logger(__name__)

# Confidence ceiling applied when the deterministic out-of-corpus gate abstains.
# Matches the "<= 0.3 = sources do not answer the question" convention in the
# system prompt, so an abstention is indistinguishable from a low-confidence
# miss to anything keying on confidence alone — except it carries abstained=True.
_ABSTAIN_CONFIDENCE_CEILING = 0.3

# Canned abstention text. The mechanic asked about a product/brand our manuals
# do not cover; saying so plainly is safer than a confident wrong torque.
_ABSTAIN_TEXT = (
    "I don't have a manual for that product in the corpus, so I can't give a "
    "torque or tool size for it. Try the exact manufacturer and model as named "
    "in a SRAM, RockShox, Avid, Zipp, or Quarq service manual."
)

# How much of the raw model text to keep in failure logs. Long enough to see a
# truncated/near-miss payload, short enough to keep log lines manageable.
_RAW_TEXT_LOG_LIMIT = 2000

# Anthropic signals billing exhaustion as a 400 BadRequestError whose *body
# message* contains this substring — there is no dedicated exception class for
# it (verified against anthropic==0.97.0). Matching a substring is brittle if
# Anthropic rewords it, so the full upstream message is always logged; any
# BadRequestError that does NOT match this stays a base ExtractionError → 502.
_CREDIT_BALANCE_MARKER = "credit balance is too low"

# Retry-After to advertise when the upstream gave us none (e.g. a connection or
# timeout failure has no HTTP response, so no header to propagate).
_DEFAULT_RETRY_AFTER_SECONDS = "5"


@dataclass(frozen=True)
class ExtractionCandidate:
    """One numbered source being considered as a possible answer origin.

    ``index`` is 1-based and is what Claude cites back as ``source_index``.
    PDF candidates carry ``png_bytes`` (vision); HTML candidates carry
    ``text`` — the parent module reconstructed from sibling chunks.

    ``document_title`` is the owning document's title/filename, carried so the
    deterministic out-of-corpus gate (#32) can derive the candidate's product
    identity without a model in the loop. It is the retrieved hit's already-
    hydrated ``document_title``; ``None`` only if the caller couldn't supply it
    (in which case the gate treats the candidate as unidentified, never a false
    mismatch).
    """

    index: int
    source_type: SourceType
    label: str
    png_bytes: bytes | None = None
    text: str | None = None
    document_title: str | None = None


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
        # Cap concurrent vision calls so request bursts queue instead of
        # stampeding Anthropic's rate limit (mirrors discovery/fetcher.py:45).
        self._semaphore = asyncio.Semaphore(settings.extraction_max_concurrency)
        # Cheap in-process readiness signal for /readyz: the outcome of the most
        # recent *upstream* call. None until the first call. A transient/auth
        # outage flips this False; the next success flips it back — so an
        # uptime monitor stops reporting green during a full Anthropic outage
        # WITHOUT spending a vision call per probe. Starts True so a freshly
        # booted process is ready before it has served any traffic.
        self._last_upstream_ok = True
        if self._stub:
            self._client: anthropic.AsyncAnthropic | None = None
        else:
            # max_retries + timeout use the SDK's built-in bounded exponential
            # backoff (honors Retry-After) — no extra dependency. An injected
            # client (tests) is used as-is so its retry behavior stays under the
            # test's control.
            self._client = client or anthropic.AsyncAnthropic(
                api_key=settings.anthropic_api_key.get_secret_value(),
                max_retries=settings.extraction_max_retries,
                timeout=settings.extraction_timeout_seconds,
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
            # The semaphore caps how many vision calls are in flight at once so
            # a burst queues here rather than stampeding Anthropic's rate limit
            # (mirrors discovery/fetcher.py's pattern). The SDK retries
            # transient failures internally per `max_retries`, honoring
            # Retry-After — so by the time an exception escapes here the
            # transient class is genuinely unrecoverable, not a first blip.
            async with self._semaphore:
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
            # The upstream call itself failed → not ready. (Parse failures below
            # are NOT counted: Claude answered, so the upstream is up.)
            self._last_upstream_ok = False
            raise self._classify_upstream_error(exc) from exc

        self._last_upstream_ok = True
        return self._parse_response(response, candidates, query=query)

    @property
    def upstream_healthy(self) -> bool:
        """Whether the most recent upstream call succeeded (for /readyz).

        Cheap and lock-free: a single bool reflecting the last outcome. It can
        lag a just-started outage by one request window — acceptable for an
        uptime monitor (documented in the /readyz route). In stub mode there is
        no upstream, so this stays True.
        """
        return self._last_upstream_ok

    @classmethod
    def _classify_upstream_error(cls, exc: anthropic.APIError) -> ExtractionError:
        """Map a raw Anthropic SDK error into a typed, logged domain error.

        Order is load-bearing. ``OverloadedError`` (529) is a *sibling* of
        ``InternalServerError`` — NOT a subclass — and ``InternalServerError``
        is the generic ``>=500`` fallback. So 529 must be caught BEFORE the
        ``InternalServerError``/``APIError`` arms, or a real overload falls
        through to the base class and is misreported as a 502 parse failure
        (the exact bug this issue exists to kill).

        Returns the domain error to ``raise ... from exc`` at the call site so
        the original SDK exception stays chained for the traceback.
        """
        status_code = getattr(exc, "status_code", None)
        request_id = getattr(exc, "request_id", None)

        # --- Transient (retryable) → ExtractionUpstreamUnavailable → 503 ---
        if isinstance(exc, OverloadedError | anthropic.RateLimitError):
            retry_after = cls._retry_after_from(exc)
            cls._log_upstream_failure(exc, status_code, request_id, retry_after)
            return ExtractionUpstreamUnavailable(
                "Anthropic is temporarily unavailable "
                f"({type(exc).__name__}, status={status_code})",
                status_code=status_code,
                request_id=request_id,
                retry_after=retry_after,
            )
        # APITimeoutError subclasses APIConnectionError; neither carries an HTTP
        # response, so status_code/request_id are None here — hence the
        # None-guard above via getattr. Advertise a default Retry-After.
        if isinstance(exc, anthropic.APIConnectionError):
            cls._log_upstream_failure(
                exc, status_code, request_id, _DEFAULT_RETRY_AFTER_SECONDS
            )
            return ExtractionUpstreamUnavailable(
                f"Could not reach Anthropic ({type(exc).__name__})",
                status_code=status_code,
                request_id=request_id,
                retry_after=_DEFAULT_RETRY_AFTER_SECONDS,
            )

        # --- Operator/code-fault (NOT retryable) → Unauthorized → 503 + Sentry ---
        if isinstance(
            exc,
            anthropic.AuthenticationError
            | anthropic.PermissionDeniedError
            | RequestTooLargeError,
        ):
            cls._log_upstream_failure(exc, status_code, request_id, None)
            return ExtractionUpstreamUnauthorized(
                "Anthropic rejected the request "
                f"({type(exc).__name__}, status={status_code})",
                status_code=status_code,
                request_id=request_id,
            )
        # Billing exhaustion is a 400 whose *body message* says the credit
        # balance is too low — no dedicated class. A 400 that does NOT match
        # stays a base ExtractionError (→502): it's our request shape, not an
        # operator fault we can page on.
        if isinstance(exc, anthropic.BadRequestError):
            if _CREDIT_BALANCE_MARKER in str(exc).lower():
                cls._log_upstream_failure(exc, status_code, request_id, None)
                return ExtractionUpstreamUnauthorized(
                    "Anthropic credit balance is exhausted",
                    status_code=status_code,
                    request_id=request_id,
                )
            cls._log_upstream_failure(exc, status_code, request_id, None)
            return ExtractionError(
                f"Anthropic rejected the request as malformed (status={status_code})"
            )

        # --- 5xx fallback (500/502/503/504) → transient → 503 ---
        if isinstance(exc, anthropic.InternalServerError):
            cls._log_upstream_failure(
                exc, status_code, request_id, _DEFAULT_RETRY_AFTER_SECONDS
            )
            return ExtractionUpstreamUnavailable(
                f"Anthropic server error (status={status_code})",
                status_code=status_code,
                request_id=request_id,
                retry_after=_DEFAULT_RETRY_AFTER_SECONDS,
            )

        # --- Anything else: an APIError we did not classify → base → 502 ---
        cls._log_upstream_failure(exc, status_code, request_id, None)
        return ExtractionError(f"Anthropic API call failed ({type(exc).__name__})")

    @staticmethod
    def _retry_after_from(exc: anthropic.APIError) -> str | None:
        """Propagate the upstream ``Retry-After`` header when present.

        Present on RateLimitError / OverloadedError responses; absent on
        connection/timeout errors (no HTTP response at all).
        """
        response = getattr(exc, "response", None)
        if response is None:
            return None
        header: str | None = response.headers.get("retry-after")
        return header

    @staticmethod
    def _log_upstream_failure(
        exc: anthropic.APIError,
        status_code: int | None,
        request_id: str | None,
        retry_after: str | None,
    ) -> None:
        """Emit the structured ``extraction.upstream_failure`` log line.

        This is the on-call's primary signal in Grafana (the failure-rate alert
        keys off it) and closes the "raw upstream cause was never logged" gap.
        Sibling to ``_log_failure`` (the parse-path logger #25 added). The full
        upstream message is logged verbatim so a reworded billing string is
        still visible even when the substring match misses.
        """
        _log.error(
            "extraction.upstream_failure",
            error_class=type(exc).__name__,
            status_code=status_code,
            request_id=request_id,
            retry_after=retry_after,
            upstream_message=str(exc),
        )

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
    def _is_out_of_corpus(
        query: str,
        candidates: list[ExtractionCandidate],
    ) -> bool:
        """Deterministic out-of-corpus gate (#32) — the PRIMARY abstention signal.

        Engages ONLY when the query names a recognizable product/brand AND no
        retrieved candidate's ``document_title`` matches it. This is model-
        independent on purpose: the issue evidence shows Claude will confidently
        fuse a fabricated/wrong-brand part onto a near-neighbour page, so its own
        self-report ``product_in_corpus`` can never be the gate — it can only
        corroborate.

        Degrade-safe: an unidentified asked scope (no recognized product/brand —
        e.g. "what tool for the top cap?") is a NO-OP that returns ``False`` so
        the guard never over-abstains on an under-specified query. We abstain
        only on a *positive* product mismatch.
        """
        asked = extract_query_scope(query)
        if not asked.is_identified:
            return False

        for candidate in candidates:
            candidate_scope = (
                normalize_product(candidate.document_title)
                if candidate.document_title
                else ProductScope()
            )
            if scope_matches(asked, candidate_scope):
                return False
        return True

    @classmethod
    def _abstain(
        cls,
        query: str,
        candidates: list[ExtractionCandidate],
        *,
        model_product_in_corpus: bool | None = None,
        model_cited_product: str | None = None,
    ) -> Answer:
        """Build the out-of-corpus abstention Answer.

        Crucially does NOT fall back to ``candidates[0]`` — ``source_index`` is
        ``None`` so the caller surfaces NO deep link to the wrong-product near-
        neighbour. ``model_*`` are the model's SECONDARY corroborating signals,
        logged only; they can never veto this abstention.
        """
        asked = extract_query_scope(query)
        _log.info(
            "extraction.out_of_corpus_abstention",
            asked_family=asked.family,
            asked_brand=asked.brand,
            asked_confidence=asked.confidence,
            candidate_titles=[c.document_title for c in candidates],
            model_product_in_corpus=model_product_in_corpus,
            model_cited_product=model_cited_product,
        )
        return Answer(
            text=_ABSTAIN_TEXT,
            tool_size=None,
            torque=None,
            source_index=None,
            confidence=_ABSTAIN_CONFIDENCE_CEILING,
            abstained=True,
        )

    @classmethod
    def _parse_response(
        cls,
        response: anthropic.types.Message,
        candidates: list[ExtractionCandidate],
        *,
        query: str = "",
    ) -> Answer:
        # PRIMARY safety gate, run BEFORE trusting the model's answer: if the
        # query names a product/brand that matches no retrieved document, abstain
        # deterministically — regardless of what the model returned. This is the
        # signal the model cannot launder (see _is_out_of_corpus). The model's
        # product self-report (parsed below, if present) is corroborating only.
        if cls._is_out_of_corpus(query, candidates):
            return cls._abstain(query, candidates)

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
        # Secondary, corroborating-only signal (logged in _abstain when relevant).
        # Per #32 it can NEVER veto an abstention and is never the sole gate; the
        # deterministic gate above already decided the out-of-corpus case.
        model_product_in_corpus = payload.get("product_in_corpus")

        # Resolve which candidate the model pointed at — guarantees we never
        # surface a source that wasn't actually in the request.
        if raw_index is None:
            # The model itself found no answering source (source_index=null). Do
            # NOT fall back to candidates[0]: surfacing the top near-neighbour's
            # deep link as the "answer" is exactly the wrong-product link defect
            # (#32). Return an honest abstention with null source instead. The
            # candidate list still carries the weak hits for transparency; they
            # just are not promoted to "the answer".
            return cls._abstain(
                query,
                candidates,
                model_product_in_corpus=model_product_in_corpus,
                model_cited_product=payload.get("cited_product"),
            )

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
