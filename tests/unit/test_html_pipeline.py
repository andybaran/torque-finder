# tests/unit/test_html_pipeline.py
"""HTML pipeline orchestration with duck-typed fakes — no network, no DB."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from parts_lookup.domain.errors import IngestionError
from parts_lookup.domain.models import (
    IndexedDocument,
    RegisteredPublication,
    SourceType,
)
from parts_lookup.ingestion.html_pipeline import HtmlIngestionPipeline

_PUB = RegisteredPublication(
    pub_id="TESTPUB",
    pub_type="UM",
    title="Registry Title",
    locale="en-US",
    source_url="https://docs.sram.com/en-US/publications/TESTPUB",
    series=(),
    models=(),
    procedures=(),
    referenced_by_models=(),
    content_hash="x",
    status="discovered",
    discovered_at=datetime(2026, 6, 8, tzinfo=UTC),
    last_seen_at=datetime(2026, 6, 8, tzinfo=UTC),
)

_HTML = (
    '<html><body><script id="manual-data" type="application/json">'
    + json.dumps(
        {
            "title": "Road AXS Test Manual",
            "modules": [
                {
                    "title": "Crank Arm Installation",
                    "hash": "crank-install",
                    "children": [
                        {
                            "hash": "crank-bolt",
                            "content": "<p>Tighten the bolt.</p>",
                            "images": [{"caption2": "40 N·m (354 in-lb)"}],
                        }
                    ],
                }
            ],
        },
        ensure_ascii=False,
    )
    + "</script></body></html>"
)


class FakeRepo:
    def __init__(self) -> None:
        self.inserted: list[dict] = []
        self.deleted_for: list[int] = []

    async def upsert_document(self, **kwargs):  # type: ignore[no-untyped-def]
        self.upserted = kwargs
        return IndexedDocument(
            id=7,
            source_type=SourceType.HTML,
            title=kwargs["title"],
            source_url=kwargs["source_url"],
            source_ref=kwargs["source_ref"],
            created_at=datetime(2026, 6, 9, tzinfo=UTC),
        )

    async def delete_chunks(self, document_id: int) -> int:
        self.deleted_for.append(document_id)
        return 0

    async def insert_chunk(self, **kwargs):  # type: ignore[no-untyped-def]
        self.inserted.append(kwargs)
        return len(self.inserted)


class FakeRegistry:
    def __init__(self) -> None:
        self.statuses: list[tuple[str, str]] = []

    async def set_status(self, pub_id: str, status: str) -> None:
        self.statuses.append((pub_id, status))


class FakeFetcher:
    def __init__(self, body: str) -> None:
        self.body = body
        self.urls: list[str] = []

    async def get(self, url: str) -> str:
        self.urls.append(url)
        return self.body


class FakeEmbedder:
    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * 1024 for _ in texts]


def _pipeline(body: str = _HTML):  # type: ignore[no-untyped-def]
    repo, registry, fetcher = FakeRepo(), FakeRegistry(), FakeFetcher(body)
    pipeline = HtmlIngestionPipeline(
        repository=repo, registry=registry, fetcher=fetcher, embedder=FakeEmbedder()
    )
    return pipeline, repo, registry, fetcher


async def test_ingest_publication_end_to_end() -> None:
    pipeline, repo, registry, fetcher = _pipeline()
    doc = await pipeline.ingest_publication(_PUB)

    assert fetcher.urls == [_PUB.source_url]
    assert repo.upserted["source_type"] is SourceType.HTML
    assert repo.upserted["source_ref"] == "TESTPUB"
    assert repo.upserted["title"] == "Road AXS Test Manual"  # parsed beats registry
    assert repo.deleted_for == [7]  # stale re-ingest wipes old chunks first
    assert len(repo.inserted) == 2  # heading chunk + block chunk
    assert repo.inserted[0]["text"] == "Crank Arm Installation"
    assert "40 N·m (354 in-lb)" in repo.inserted[1]["text"]
    assert repo.inserted[1]["anchor"] == "crank-bolt"
    assert repo.inserted[1]["parent_anchor"] == "crank-install"
    assert repo.inserted[1]["png_r2_key"] is None
    assert registry.statuses == [("TESTPUB", "ingested")]
    assert doc.id == 7


async def test_publication_with_no_chunks_raises() -> None:
    empty = (
        '<html><body><script id="manual-data" type="application/json">'
        '{"title": "x", "modules": []}</script></body></html>'
    )
    pipeline, repo, registry, _ = _pipeline(empty)
    with pytest.raises(IngestionError):
        await pipeline.ingest_publication(_PUB)
    assert repo.inserted == []
    assert registry.statuses == []


async def test_fetch_failure_is_wrapped_in_ingestion_error() -> None:
    class BoomFetcher:
        async def get(self, url: str) -> str:
            raise ConnectionError("network down")

    pipeline = HtmlIngestionPipeline(
        repository=FakeRepo(),
        registry=FakeRegistry(),
        fetcher=BoomFetcher(),
        embedder=FakeEmbedder(),
    )
    with pytest.raises(IngestionError) as excinfo:
        await pipeline.ingest_publication(_PUB)
    assert isinstance(excinfo.value.__cause__, ConnectionError)


async def test_embed_count_mismatch_raises_ingestion_error() -> None:
    class ShortEmbedder:
        async def embed_documents(self, texts: list[str]) -> list[list[float]]:
            return [[0.0] * 1024]  # always one vector, regardless of input

    repo, registry = FakeRepo(), FakeRegistry()
    pipeline = HtmlIngestionPipeline(
        repository=repo,
        registry=registry,
        fetcher=FakeFetcher(_HTML),
        embedder=ShortEmbedder(),
    )
    with pytest.raises(IngestionError):
        await pipeline.ingest_publication(_PUB)
    assert repo.inserted == []
    assert registry.statuses == []
