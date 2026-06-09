# tests/unit/test_discovery_fetcher.py
from __future__ import annotations

import httpx
import pytest

pytestmark = pytest.mark.asyncio


def _client(counter: list[int]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        counter.append(1)
        return httpx.Response(200, text=f"BODY {request.url}")

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_fetcher_returns_body(tmp_path):
    from parts_lookup.discovery.fetcher import Fetcher

    calls: list[int] = []
    f = Fetcher(
        user_agent="test-agent",
        cache_dir=str(tmp_path),
        delay_seconds=0.0,
        client=_client(calls),
    )
    body = await f.get("https://example.com/a")
    assert body == "BODY https://example.com/a"
    await f.aclose()


async def test_fetcher_caches_second_request(tmp_path):
    from parts_lookup.discovery.fetcher import Fetcher

    calls: list[int] = []
    f = Fetcher(
        user_agent="test-agent",
        cache_dir=str(tmp_path),
        delay_seconds=0.0,
        client=_client(calls),
    )
    await f.get("https://example.com/a")
    await f.get("https://example.com/a")
    assert len(calls) == 1  # second served from disk cache
    await f.aclose()
