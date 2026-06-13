"""Route-level mapping of extraction failures to HTTP status (#33).

DB-free: the route's collaborators (session, retrieval, extractor, R2) are
replaced via FastAPI ``dependency_overrides`` so this runs offline with no
Postgres/R2/Anthropic. Uses an HTML candidate so the route never touches R2
(HTML carries its own deep link and has no screenshot).
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from parts_lookup.api.dependencies import (
    get_claude_extractor,
    get_r2_client,
    get_retrieval_service,
)
from parts_lookup.api.main import create_app
from parts_lookup.config import Settings
from parts_lookup.domain.errors import (
    ExtractionError,
    ExtractionUpstreamUnauthorized,
    ExtractionUpstreamUnavailable,
)
from parts_lookup.domain.models import RetrievalSource, RetrievedChunk, SourceType
from parts_lookup.indexing.session import get_session


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://stub/stub",
        stub_external_apis=True,
        _env_file=None,
    )


def _html_hit() -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=1,
        document_id=1,
        source_type=SourceType.HTML,
        document_title="Crank Installation",
        document_source_url="https://docs.sram.com/pub/crank",
        ordinal=1,
        text="Crank Installation\n\nTighten to 40 N·m (354 in-lb)",
        png_r2_key=None,
        anchor="crank",
        parent_anchor=None,  # → route uses hit.text directly, no DB call
        source_url="https://docs.sram.com/pub/crank#crank",
        score=0.9,
        source=RetrievalSource.HYBRID,
    )


class _StubRetrieval:
    async def search(self, _session: Any, _query: Any) -> list[RetrievedChunk]:
        return [_html_hit()]


class _RaisingExtractor:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def extract(self, _q: str, _c: list[Any]) -> Any:
        raise self._exc


def _client(extractor_exc: Exception) -> TestClient:
    app = create_app(_settings())
    app.dependency_overrides[get_session] = lambda: None
    app.dependency_overrides[get_retrieval_service] = lambda: _StubRetrieval()
    app.dependency_overrides[get_claude_extractor] = lambda: _RaisingExtractor(
        extractor_exc
    )
    app.dependency_overrides[get_r2_client] = lambda: None
    # raise_server_exceptions=False so a 5xx returns a response instead of
    # re-raising into the test (HTTPException returns normally regardless).
    return TestClient(app, raise_server_exceptions=False)


def _post(client: TestClient) -> Any:
    return client.post("/v1/query", json={"question": "what torque?", "top_k": 3})


def test_transient_upstream_maps_to_503_with_retry_after() -> None:
    exc = ExtractionUpstreamUnavailable(
        "overloaded", status_code=529, request_id="req_x", retry_after="7"
    )
    resp = _post(_client(exc))
    assert resp.status_code == 503
    assert resp.headers["retry-after"] == "7"
    # The opaque legacy string is gone.
    assert "Claude API call failed" not in resp.text


def test_transient_without_retry_after_uses_default() -> None:
    exc = ExtractionUpstreamUnavailable("conn reset", retry_after=None)
    resp = _post(_client(exc))
    assert resp.status_code == 503
    assert resp.headers["retry-after"] == "5"


def test_operator_fault_maps_to_503_and_captures_sentry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[BaseException] = []
    monkeypatch.setattr(
        "parts_lookup.api.routes.query._capture_exception",
        lambda exc: captured.append(exc),
    )
    exc = ExtractionUpstreamUnauthorized(
        "credit exhausted", status_code=400, request_id="req_y"
    )
    resp = _post(_client(exc))
    assert resp.status_code == 503
    assert len(captured) == 1
    assert isinstance(captured[0], ExtractionUpstreamUnauthorized)
    # Client-facing detail stays generic (no credit/auth leak).
    assert "credit" not in resp.text.lower()


def test_parse_failure_stays_502() -> None:
    """Base ExtractionError (#25's parse/stop_reason family) is unchanged: 502."""
    resp = _post(_client(ExtractionError("Claude response was not valid JSON")))
    assert resp.status_code == 502
