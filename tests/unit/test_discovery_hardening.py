from __future__ import annotations

import httpx


# --- #16: sitemap English selection (pure) ---
def test_is_english_sitemap():
    from parts_lookup.discovery.sitemap import is_english_sitemap

    assert is_english_sitemap("https://www.sram.com/sitemap.en.xml")
    assert not is_english_sitemap("https://www.sram.com/sitemap.de.xml")
    # No false positive on '.en.' elsewhere in the path.
    assert not is_english_sitemap("https://www.sram.com/foo.en.bar/sitemap.fr.xml")


# --- #16: cli --refresh flag ---
def test_cli_refresh_flag():
    from parts_lookup.discovery.cli import build_parser

    assert build_parser().parse_args(["--refresh", "seed", "ed-red-e1"]).refresh is True
    assert build_parser().parse_args(["seed", "ed-red-e1"]).refresh is False


def _counting_client(calls: list[str]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, text="BODY")

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _robots_client(robots_body: str) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=robots_body)
        return httpx.Response(200, text="PAGE")

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --- #16: force_refresh bypasses the disk cache ---
async def test_force_refresh_bypasses_cache(tmp_path):
    from parts_lookup.discovery.fetcher import Fetcher

    calls: list[str] = []
    f = Fetcher(
        user_agent="t",
        cache_dir=str(tmp_path),
        delay_seconds=0.0,
        force_refresh=True,
        respect_robots=False,
        client=_counting_client(calls),
    )
    await f.get("https://example.com/a")
    await f.get("https://example.com/a")
    assert len(calls) == 2  # cache bypassed on both calls
    await f.aclose()


# --- #15: robots.txt is honored ---
async def test_robots_blocks_disallowed(tmp_path):
    from parts_lookup.discovery.fetcher import Fetcher
    from parts_lookup.domain.errors import DiscoveryError

    f = Fetcher(
        user_agent="t",
        cache_dir=str(tmp_path),
        delay_seconds=0.0,
        client=_robots_client("User-agent: *\nDisallow: /blocked"),
    )
    assert await f.get("https://example.com/allowed") == "PAGE"
    import pytest

    with pytest.raises(DiscoveryError):
        await f.get("https://example.com/blocked/x")
    await f.aclose()


async def test_respect_robots_false_ignores_rules(tmp_path):
    from parts_lookup.discovery.fetcher import Fetcher

    f = Fetcher(
        user_agent="t",
        cache_dir=str(tmp_path),
        delay_seconds=0.0,
        respect_robots=False,
        client=_robots_client("User-agent: *\nDisallow: /"),
    )
    assert await f.get("https://example.com/anything") == "PAGE"
    await f.aclose()
