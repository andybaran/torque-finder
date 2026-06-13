"""Doc-level / page-pinned response composition at the route (#49).

DB- and network-free: the route's collaborators (session, retrieval, extractor,
R2) are replaced via FastAPI ``dependency_overrides``; the PNG fetch is
monkeypatched so no R2/HTTP call is made. Asserts the #49 response contract:

  * page-pinned PDF (Claude cites a page) → ``source_url`` has ``#page=N`` and
    ``screenshot_url`` is present;
  * doc-level best-effort (chosen PDF has no page screenshot) → ``source_url``
    is the page-LESS PDF URL and ``screenshot_url is None``;
  * #32 abstention → ``source_type``/``source_url``/``screenshot_url`` all null
    (unchanged).
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

import parts_lookup.api.routes.query as query_route
from parts_lookup.api.dependencies import (
    get_claude_extractor,
    get_r2_client,
    get_retrieval_service,
)
from parts_lookup.api.main import create_app
from parts_lookup.config import Settings
from parts_lookup.domain.models import Answer, RetrievalSource, RetrievedChunk, SourceType
from parts_lookup.indexing.session import get_session

_PDF_DOC_URL = "https://r2.example/pdfs/abc123.pdf"


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://stub/stub",
        stub_external_apis=True,
        _env_file=None,
    )


def _pdf_hit(*, png_key: str | None) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=42,
        document_id=7,
        source_type=SourceType.PDF,
        document_title="2010-sram-technical-manual.pdf",
        document_source_url="pdfs/abc123.pdf",
        ordinal=8,
        text="Tighten to 5 N-m.",
        png_r2_key=png_key,
        anchor=None,
        parent_anchor=None,
        source_url="pdfs/abc123.pdf#page=8",
        score=0.9,
        source=RetrievalSource.HYBRID,
    )


class _StubRetrieval:
    def __init__(self, hit: RetrievedChunk) -> None:
        self._hit = hit

    async def search(self, _session: Any, _query: Any) -> list[RetrievedChunk]:
        return [self._hit]


class _StubExtractor:
    def __init__(self, answer: Answer) -> None:
        self._answer = answer

    async def extract(self, _q: str, _c: list[Any]) -> Answer:
        return self._answer


class _StubR2:
    """Returns a deterministic public URL for any key; never hits the network."""

    def public_url(self, key: str) -> str:
        return f"https://r2.example/{key}"

    async def generate_presigned_url(self, key: str, expires_in: int = 0) -> str:
        return f"https://r2.example/{key}?sig=stub"


def _client(hit: RetrievedChunk, answer: Answer) -> TestClient:
    app = create_app(_settings())
    app.dependency_overrides[get_session] = lambda: None
    app.dependency_overrides[get_retrieval_service] = lambda: _StubRetrieval(hit)
    app.dependency_overrides[get_claude_extractor] = lambda: _StubExtractor(answer)
    app.dependency_overrides[get_r2_client] = lambda: _StubR2()
    return TestClient(app, raise_server_exceptions=False)


def _post(client: TestClient) -> Any:
    return client.post("/v1/query", json={"question": "nut torque?", "top_k": 3})


@pytest.fixture(autouse=True)
def _no_png_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_fetch(_r2: Any, _key: str) -> bytes:
        return b"\x89PNG-stub"

    monkeypatch.setattr(query_route, "_fetch_png", fake_fetch)


def test_page_pinned_pdf_has_page_link_and_screenshot() -> None:
    hit = _pdf_hit(png_key="pdfs/abc123/8.png")
    answer = Answer(
        text="5 N-m",
        tool_size=None,
        torque="5 N-m",
        source_index=1,
        confidence=0.8,
        abstained=False,
    )
    resp = _post(_client(hit, answer))
    assert resp.status_code == 200
    body = resp.json()
    assert body["abstained"] is False
    assert body["source_type"] == "pdf"
    assert body["source_url"].endswith("#page=8")
    assert body["screenshot_url"] is not None


@pytest.mark.asyncio
async def test_doc_level_links_helper_drops_page_and_screenshot() -> None:
    """`_doc_level_links` composes the doc-level best-effort citation (#49): the
    page-LESS PDF url and a null screenshot. This is the graceful fallback the
    response uses when a chosen PDF candidate can't form a page-pinned citation
    (defensive — the route's PNG invariant means a PDF reaching extraction
    normally carries a screenshot, so the page-pinned path is the common case)."""
    hit = _pdf_hit(png_key=None)
    source_url, screenshot_url = await query_route._doc_level_links(_StubR2(), hit)
    assert source_url == _PDF_DOC_URL  # original PDF, NO #page fragment
    assert "#page=" not in source_url
    assert screenshot_url is None


def test_abstention_returns_all_null_links() -> None:
    """#32 abstention is unchanged: source_type/source_url/screenshot_url null."""
    hit = _pdf_hit(png_key="pdfs/abc123/8.png")
    answer = Answer(
        text="I don't have a manual for that product.",
        tool_size=None,
        torque=None,
        source_index=None,
        confidence=0.2,
        abstained=True,
    )
    resp = _post(_client(hit, answer))
    assert resp.status_code == 200
    body = resp.json()
    assert body["abstained"] is True
    assert body["source_type"] is None
    assert body["source_url"] is None
    assert body["screenshot_url"] is None
    # Candidates still carried for transparency.
    assert len(body["candidates"]) == 1
