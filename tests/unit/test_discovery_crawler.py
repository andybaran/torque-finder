# tests/unit/test_discovery_crawler.py
from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.asyncio

MODEL_HTML = (
    '<a href="https://docs.sram.com/en-US/publications/PUB1/UM">UM</a>'
    '<a href="https://docs.sram.com/en-US/publications/PUB2/SM">SM</a>'
)
PUB_HTML_TMPL = (
    '<script id="manual-data" type="application/json">{json}</script>'
)


def _pub_html(title: str) -> str:
    data = {"title": title, "locale": "en-US", "filters": []}
    return PUB_HTML_TMPL.format(json=json.dumps(data))


class FakeFetcher:
    def __init__(self, pages: dict[str, str]) -> None:
        self._pages = pages

    async def get(self, url: str) -> str:
        return self._pages[url]


class FakeRegistry:
    def __init__(self) -> None:
        self.upserts: list[tuple[str, list[str]]] = []

    async def upsert(self, pub, referenced_by_models):  # noqa: ANN001
        self.upserts.append((pub.pub_id, referenced_by_models))
        return "inserted"


async def test_discover_seed_visits_models_and_upserts_unique_pubs():
    from parts_lookup.discovery.crawler import DiscoveryCrawler

    pages = {
        "https://www.sram.com/en/service/models/ed-red-e1": MODEL_HTML,
        "https://www.sram.com/en/service/models/cn-red-e1": MODEL_HTML,  # same pubs
        "https://docs.sram.com/en-US/publications/PUB1": _pub_html("Pub One"),
        "https://docs.sram.com/en-US/publications/PUB2": _pub_html("Pub Two"),
    }
    reg = FakeRegistry()
    crawler = DiscoveryCrawler(
        fetcher=FakeFetcher(pages),
        registry=reg,
        base_url="https://www.sram.com",
    )

    summary = await crawler.discover_seed(["ed-red-e1", "cn-red-e1"])

    # Two models, but only two unique publications upserted.
    upserted_ids = {pid for pid, _ in reg.upserts}
    assert upserted_ids == {"PUB1", "PUB2"}
    assert summary["models_crawled"] == 2
    assert summary["publications_upserted"] == 2
    # PUB1 was referenced by both models.
    refs = dict(reg.upserts)
    assert set(refs["PUB1"]) == {"ed-red-e1", "cn-red-e1"}
