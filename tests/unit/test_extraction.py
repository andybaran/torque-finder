# tests/unit/test_extraction.py
"""Pure tests for extraction block building + response parsing (no network)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, ClassVar
from unittest.mock import AsyncMock

import anthropic
import httpx
import pytest
import structlog
from anthropic._exceptions import OverloadedError, RequestTooLargeError

from parts_lookup.config import Settings
from parts_lookup.domain.errors import (
    ExtractionError,
    ExtractionUpstreamUnauthorized,
    ExtractionUpstreamUnavailable,
)
from parts_lookup.domain.models import SourceType
from parts_lookup.extraction import prompt
from parts_lookup.extraction.claude_client import ClaudeExtractor, ExtractionCandidate


def _pdf_candidate(index: int = 1) -> ExtractionCandidate:
    return ExtractionCandidate(
        index=index,
        source_type=SourceType.PDF,
        label="p. 28 of manual.pdf",
        png_bytes=b"\x89PNG fake",
    )


def _html_candidate(index: int = 2) -> ExtractionCandidate:
    return ExtractionCandidate(
        index=index,
        source_type=SourceType.HTML,
        label="Crank Installation",
        text="Crank Installation\n\nTighten to 40 N·m (354 in-lb)",
    )


def _response(
    payload: dict | None = None,
    *,
    text: str | None = None,
    content: list[Any] | None = None,
    stop_reason: str = "end_turn",
    request_id: str | None = "req_test_123",
    input_tokens: int = 100,
    output_tokens: int = 50,
) -> SimpleNamespace:
    """Build a fake Anthropic Message.

    Carries ``stop_reason``, ``_request_id``, and ``usage`` so the failure-path
    tests can assert on the structured-log diagnostics. ``payload`` serializes
    to a JSON text block (the happy path); ``text`` supplies raw text directly
    (e.g. malformed JSON); ``content`` overrides the block list entirely (e.g.
    an empty response or a non-text first block).
    """
    if content is None:
        if text is None:
            text = json.dumps(payload)
        content = [SimpleNamespace(type="text", text=text)]
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        _request_id=request_id,
        usage=SimpleNamespace(
            input_tokens=input_tokens, output_tokens=output_tokens
        ),
    )


class TestBuildUserBlocks:
    def test_pdf_candidate_becomes_image_plus_marker(self) -> None:
        blocks = ClaudeExtractor._build_user_blocks("q?", [_pdf_candidate()])
        assert blocks[0]["type"] == "image"
        assert blocks[1]["type"] == "text"
        assert "source 1" in blocks[1]["text"]
        assert "p. 28 of manual.pdf" in blocks[1]["text"]

    def test_html_candidate_becomes_text_block(self) -> None:
        blocks = ClaudeExtractor._build_user_blocks("q?", [_html_candidate()])
        assert blocks[0]["type"] == "text"
        assert "Source 2: Crank Installation" in blocks[0]["text"]
        assert "40 N·m (354 in-lb)" in blocks[0]["text"]

    def test_pdf_candidate_without_png_rejected(self) -> None:
        bad = ExtractionCandidate(
            index=1, source_type=SourceType.PDF, label="p. 1", png_bytes=None
        )
        with pytest.raises(ExtractionError):
            ClaudeExtractor._build_user_blocks("q?", [bad])

    def test_html_candidate_without_text_rejected(self) -> None:
        bad = ExtractionCandidate(
            index=1, source_type=SourceType.HTML, label="x", text=None
        )
        with pytest.raises(ExtractionError):
            ClaudeExtractor._build_user_blocks("q?", [bad])


class TestParseResponse:
    _CANDIDATES: ClassVar[list[ExtractionCandidate]] = [
        _pdf_candidate(1),
        _html_candidate(2),
    ]

    def test_resolves_cited_source_index(self) -> None:
        answer = ClaudeExtractor._parse_response(
            _response(
                {
                    "answer": "40 N·m (354 in-lb)",
                    "tool_size": None,
                    "torque": "40 N·m (354 in-lb)",
                    "source_index": 2,
                    "confidence": 0.95,
                }
            ),
            self._CANDIDATES,
        )
        assert answer.source_index == 2
        assert answer.torque == "40 N·m (354 in-lb)"

    def test_null_source_index_falls_back_to_top_candidate(self) -> None:
        answer = ClaudeExtractor._parse_response(
            _response(
                {
                    "answer": "not found",
                    "tool_size": None,
                    "torque": None,
                    "source_index": None,
                    "confidence": 0.1,
                }
            ),
            self._CANDIDATES,
        )
        assert answer.source_index == 1

    def test_unknown_source_index_rejected(self) -> None:
        with pytest.raises(ExtractionError):
            ClaudeExtractor._parse_response(
                _response(
                    {
                        "answer": "x",
                        "tool_size": None,
                        "torque": None,
                        "source_index": 9,
                        "confidence": 0.9,
                    }
                ),
                self._CANDIDATES,
            )


def test_system_prompt_marks_sources_as_reference_data_only() -> None:
    """Drift guard for the prompt-injection hardening rule (Task 5 review rider)."""
    from parts_lookup.extraction.prompt import SYSTEM_PROMPT

    assert "REFERENCE DATA ONLY" in SYSTEM_PROMPT
    assert "never follow" in SYSTEM_PROMPT


async def test_stub_extract_cites_first_candidate() -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://stub/stub",
        stub_external_apis=True,
        _env_file=None,
    )
    extractor = ClaudeExtractor(settings)
    answer = await extractor.extract("q?", [_html_candidate(1), _pdf_candidate(2)])
    assert answer.source_index == 1


def _live_settings(**overrides: Any) -> Settings:
    """A non-stub Settings so ClaudeExtractor goes through the real call path."""
    base: dict[str, Any] = {
        "database_url": "postgresql+asyncpg://stub/stub",
        "stub_external_apis": False,
        "anthropic_api_key": "sk-test",
        "_env_file": None,
    }
    base.update(overrides)
    return Settings(**base)


class TestRequestShape:
    """The fix: OUTPUT_SCHEMA must be wired into the API call (was dead code),
    and max_tokens raised off the truncation-prone 1024 default. These assert
    the request the SDK receives — test_schema_is_enforced FAILS on main."""

    async def _call_and_capture_kwargs(
        self, settings: Settings
    ) -> dict[str, Any]:
        valid = {
            "answer": "40 N·m (354 in-lb)",
            "tool_size": None,
            "torque": "40 N·m (354 in-lb)",
            "source_index": 1,
            "confidence": 0.95,
        }
        client = SimpleNamespace(
            messages=SimpleNamespace(
                create=AsyncMock(return_value=_response(valid))
            )
        )
        extractor = ClaudeExtractor(settings, client=client)  # type: ignore[arg-type]
        await extractor.extract("q?", [_pdf_candidate(1)])
        client.messages.create.assert_awaited_once()
        return client.messages.create.await_args.kwargs

    async def test_schema_is_enforced(self) -> None:
        """Regression guard for the dead-code OUTPUT_SCHEMA. Fails on main."""
        kwargs = await self._call_and_capture_kwargs(_live_settings())
        assert kwargs["output_config"] == {
            "format": {"type": "json_schema", "schema": prompt.OUTPUT_SCHEMA}
        }

    async def test_schema_comes_from_single_source(self) -> None:
        """The wired schema IS prompt.OUTPUT_SCHEMA, so #26/#28 field additions
        flow through automatically rather than drifting."""
        kwargs = await self._call_and_capture_kwargs(_live_settings())
        assert (
            kwargs["output_config"]["format"]["schema"] is prompt.OUTPUT_SCHEMA
        )

    async def test_max_tokens_off_the_truncation_default(self) -> None:
        kwargs = await self._call_and_capture_kwargs(_live_settings())
        assert kwargs["max_tokens"] >= 2048

    async def test_max_tokens_is_tunable_via_settings(self) -> None:
        kwargs = await self._call_and_capture_kwargs(
            _live_settings(extraction_max_tokens=4096)
        )
        assert kwargs["max_tokens"] == 4096


class TestFailureLogging:
    """Every failure path raises a labeled ExtractionError AND emits a
    structured log line carrying stop_reason / request_id / usage / raw_text,
    so the rare production 502 is diagnosable instead of opaque."""

    _CANDIDATES: ClassVar[list[ExtractionCandidate]] = [
        _pdf_candidate(1),
        _html_candidate(2),
    ]

    def _parse(self, response: SimpleNamespace) -> None:
        ClaudeExtractor._parse_response(response, self._CANDIDATES)

    def test_max_tokens_stop_reason_raises_truncation_and_logs(self) -> None:
        resp = _response(text="{ truncated json", stop_reason="max_tokens")
        with (
            structlog.testing.capture_logs() as logs,
            pytest.raises(ExtractionError, match="truncated"),
        ):
            self._parse(resp)
        entry = _only_log(logs)
        assert entry["event"] == "extraction_truncated"
        assert entry["stop_reason"] == "max_tokens"
        assert entry["request_id"] == "req_test_123"
        assert entry["output_tokens"] == 50

    def test_refusal_stop_reason_raises_distinct_error_and_logs(self) -> None:
        resp = _response(text="I cannot help", stop_reason="refusal")
        with (
            structlog.testing.capture_logs() as logs,
            pytest.raises(ExtractionError, match="refusal"),
        ):
            self._parse(resp)
        entry = _only_log(logs)
        assert entry["event"] == "extraction_refusal"
        assert entry["stop_reason"] == "refusal"

    def test_malformed_json_raises_and_logs_raw_text(self) -> None:
        resp = _response(text="this is not json at all", stop_reason="end_turn")
        with (
            structlog.testing.capture_logs() as logs,
            pytest.raises(
                ExtractionError, match="not valid JSON after fence stripping"
            ),
        ):
            self._parse(resp)
        entry = _only_log(logs)
        assert entry["event"] == "extraction_invalid_json"
        assert entry["stop_reason"] == "end_turn"
        assert entry["request_id"] == "req_test_123"
        assert "this is not json at all" in entry["raw_text"]

    def test_schema_mismatch_raises_and_logs(self) -> None:
        # Valid JSON, but missing the required "answer"/"confidence" keys.
        resp = _response({"torque": "40 N·m", "source_index": 1})
        with (
            structlog.testing.capture_logs() as logs,
            pytest.raises(
                ExtractionError, match="did not match the expected schema"
            ),
        ):
            self._parse(resp)
        entry = _only_log(logs)
        assert entry["event"] == "extraction_schema_mismatch"
        assert entry["raw_text"] is not None

    def test_request_id_read_is_null_safe(self) -> None:
        """A response with no _request_id attr must still log, not double-fault."""
        bare = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="nope")],
            stop_reason="end_turn",
        )
        with (
            structlog.testing.capture_logs() as logs,
            pytest.raises(ExtractionError),
        ):
            self._parse(bare)
        entry = _only_log(logs)
        assert entry["request_id"] is None
        assert entry["output_tokens"] is None

    def test_happy_path_emits_no_failure_log(self) -> None:
        valid = {
            "answer": "40 N·m (354 in-lb)",
            "tool_size": None,
            "torque": "40 N·m (354 in-lb)",
            "source_index": 2,
            "confidence": 0.95,
        }
        with structlog.testing.capture_logs() as logs:
            answer = ClaudeExtractor._parse_response(
                _response(valid), self._CANDIDATES
            )
        assert answer.source_index == 2
        assert logs == []


def _only_log(logs: list[dict[str, Any]]) -> dict[str, Any]:
    """Exactly one failure log line is expected; return it."""
    assert len(logs) == 1, f"expected one log line, got {logs!r}"
    return logs[0]


# --- SDK-exception builders (#33) -------------------------------------------
# Anthropic SDK exceptions cannot be raised bare: the APIStatusError family
# needs a constructed httpx.Response, and APIConnectionError/APITimeoutError
# need an httpx.Request. These helpers build instances that match the
# production exception shape (status_code/request_id/retry-after all survive),
# so the gating tests aren't accidentally testing a fake that wouldn't classify
# the same way.

_REQUEST = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def _status_exc(
    exc_cls: type[anthropic.APIStatusError],
    status_code: int,
    message: str = "boom",
    *,
    request_id: str = "req_test",
    retry_after: str | None = None,
) -> anthropic.APIStatusError:
    headers = {"request-id": request_id}
    if retry_after is not None:
        headers["retry-after"] = retry_after
    response = httpx.Response(status_code, headers=headers, request=_REQUEST)
    return exc_cls(message, response=response, body=None)


def _extractor_raising(exc: BaseException) -> ClaudeExtractor:
    """A live-path extractor whose messages.create raises ``exc``."""
    client = SimpleNamespace(
        messages=SimpleNamespace(create=AsyncMock(side_effect=exc))
    )
    return ClaudeExtractor(_live_settings(), client=client)  # type: ignore[arg-type]


class TestUpstreamFailureClassification:
    """#33: every Anthropic call failure becomes a typed, logged, attributable
    domain error with a distinct HTTP status — no more opaque 502."""

    @pytest.mark.parametrize(
        ("exc", "expect_status", "expect_request_id", "expect_retry_after"),
        [
            (
                _status_exc(
                    OverloadedError, 529, request_id="req_over", retry_after="3"
                ),
                529,
                "req_over",
                "3",
            ),
            (
                _status_exc(
                    anthropic.RateLimitError, 429, request_id="req_rl", retry_after="7"
                ),
                429,
                "req_rl",
                "7",
            ),
            (
                anthropic.APITimeoutError(request=_REQUEST),
                None,
                None,
                "5",  # default Retry-After (no upstream response)
            ),
            (
                anthropic.APIConnectionError(message="dns", request=_REQUEST),
                None,
                None,
                "5",
            ),
            (
                _status_exc(anthropic.InternalServerError, 500, request_id="req_5xx"),
                500,
                "req_5xx",
                "5",
            ),
        ],
    )
    async def test_transient_errors_become_unavailable(
        self,
        exc: BaseException,
        expect_status: int | None,
        expect_request_id: str | None,
        expect_retry_after: str | None,
    ) -> None:
        extractor = _extractor_raising(exc)
        with pytest.raises(ExtractionUpstreamUnavailable) as ei:
            await extractor.extract("q?", [_pdf_candidate(1)])
        assert ei.value.status_code == expect_status
        assert ei.value.request_id == expect_request_id
        assert ei.value.retry_after == expect_retry_after

    async def test_529_does_not_fall_through_to_base_502(self) -> None:
        """Regression guard for the v1 bug: OverloadedError(529) is a SIBLING of
        InternalServerError, so a broad `except InternalServerError` would miss
        it and a 529 would degrade to base ExtractionError → 502. It must be the
        transient class."""
        extractor = _extractor_raising(_status_exc(OverloadedError, 529))
        with pytest.raises(ExtractionUpstreamUnavailable):
            await extractor.extract("q?", [_pdf_candidate(1)])

    async def test_connection_error_branch_tolerates_missing_attrs(self) -> None:
        """APIConnectionError/APITimeoutError carry no HTTP response, so
        status_code/request_id are absent — the branch must None-guard them
        rather than AttributeError."""
        extractor = _extractor_raising(
            anthropic.APIConnectionError(message="reset", request=_REQUEST)
        )
        with pytest.raises(ExtractionUpstreamUnavailable) as ei:
            await extractor.extract("q?", [_pdf_candidate(1)])
        assert ei.value.status_code is None
        assert ei.value.request_id is None

    @pytest.mark.parametrize(
        ("exc", "expect_status"),
        [
            (
                _status_exc(
                    anthropic.BadRequestError,
                    400,
                    "Your credit balance is too low to access the Anthropic API.",
                ),
                400,
            ),
            (_status_exc(anthropic.AuthenticationError, 401), 401),
            (_status_exc(anthropic.PermissionDeniedError, 403), 403),
            (_status_exc(RequestTooLargeError, 413), 413),
        ],
    )
    async def test_operator_faults_become_unauthorized(
        self, exc: anthropic.APIStatusError, expect_status: int
    ) -> None:
        extractor = _extractor_raising(exc)
        with pytest.raises(ExtractionUpstreamUnauthorized) as ei:
            await extractor.extract("q?", [_pdf_candidate(1)])
        assert ei.value.status_code == expect_status
        assert ei.value.request_id == "req_test"

    async def test_non_credit_400_stays_base_extraction_error(self) -> None:
        """A 400 that is NOT a credit-balance message is our request shape, not
        an operator fault to page on — stays base ExtractionError → 502."""
        extractor = _extractor_raising(
            _status_exc(anthropic.BadRequestError, 400, "messages: invalid role")
        )
        with pytest.raises(ExtractionError) as ei:
            await extractor.extract("q?", [_pdf_candidate(1)])
        assert not isinstance(ei.value, ExtractionUpstreamUnavailable)
        assert not isinstance(ei.value, ExtractionUpstreamUnauthorized)

    async def test_emits_structured_upstream_failure_log(self) -> None:
        exc = _status_exc(
            anthropic.RateLimitError, 429, request_id="req_log", retry_after="9"
        )
        extractor = _extractor_raising(exc)
        with (
            structlog.testing.capture_logs() as logs,
            pytest.raises(ExtractionUpstreamUnavailable),
        ):
            await extractor.extract("q?", [_pdf_candidate(1)])
        entry = _only_log(logs)
        assert entry["event"] == "extraction.upstream_failure"
        assert entry["error_class"] == "RateLimitError"
        assert entry["status_code"] == 429
        assert entry["request_id"] == "req_log"
        assert entry["retry_after"] == "9"
        assert "upstream_message" in entry

    async def test_successful_call_flips_upstream_healthy_true(self) -> None:
        valid = {
            "answer": "40 N·m",
            "tool_size": None,
            "torque": "40 N·m",
            "source_index": 1,
            "confidence": 0.9,
        }
        client = SimpleNamespace(
            messages=SimpleNamespace(create=AsyncMock(return_value=_response(valid)))
        )
        extractor = ClaudeExtractor(_live_settings(), client=client)  # type: ignore[arg-type]
        await extractor.extract("q?", [_pdf_candidate(1)])
        assert extractor.upstream_healthy is True

    async def test_upstream_failure_flips_upstream_healthy_false(self) -> None:
        extractor = _extractor_raising(_status_exc(OverloadedError, 529))
        assert extractor.upstream_healthy is True
        with pytest.raises(ExtractionUpstreamUnavailable):
            await extractor.extract("q?", [_pdf_candidate(1)])
        assert extractor.upstream_healthy is False


class TestRetryAndConcurrency:
    """#33(b): SDK built-in retry is wired (no new dep), and a Semaphore caps
    concurrency. Billing/auth are never retried."""

    async def test_client_constructed_with_max_retries_and_timeout(self) -> None:
        """The non-stub path hands the SDK its built-in retry/backoff knobs."""
        captured: dict[str, Any] = {}

        class _FakeAsyncAnthropic:
            def __init__(self, **kwargs: Any) -> None:
                captured.update(kwargs)

        import parts_lookup.extraction.claude_client as cc

        original = anthropic.AsyncAnthropic
        cc.anthropic.AsyncAnthropic = _FakeAsyncAnthropic  # type: ignore[misc,assignment]
        try:
            ClaudeExtractor(_live_settings(extraction_max_retries=5))
        finally:
            cc.anthropic.AsyncAnthropic = original  # type: ignore[misc]
        assert captured["max_retries"] == 5
        assert captured["timeout"] == 60.0

    async def test_billing_error_is_not_retried_by_app(self) -> None:
        """Billing is an operator fault: a single call, never an app-level retry
        loop (the SDK also won't retry a 400). Call count stays 1."""
        create = AsyncMock(
            side_effect=_status_exc(
                anthropic.BadRequestError,
                400,
                "Your credit balance is too low to access the Anthropic API.",
            )
        )
        client = SimpleNamespace(messages=SimpleNamespace(create=create))
        extractor = ClaudeExtractor(_live_settings(), client=client)  # type: ignore[arg-type]
        with pytest.raises(ExtractionUpstreamUnauthorized):
            await extractor.extract("q?", [_pdf_candidate(1)])
        assert create.await_count == 1

    async def test_semaphore_caps_in_flight_calls(self) -> None:
        import asyncio

        max_concurrency = 2
        in_flight = 0
        peak = 0

        async def _create(**_: Any) -> SimpleNamespace:
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1
            return _response(
                {
                    "answer": "x",
                    "tool_size": None,
                    "torque": None,
                    "source_index": 1,
                    "confidence": 0.5,
                }
            )

        client = SimpleNamespace(messages=SimpleNamespace(create=_create))
        extractor = ClaudeExtractor(
            _live_settings(extraction_max_concurrency=max_concurrency),
            client=client,  # type: ignore[arg-type]
        )
        await asyncio.gather(
            *(extractor.extract("q?", [_pdf_candidate(1)]) for _ in range(8))
        )
        assert peak <= max_concurrency
