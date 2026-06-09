"""Async HTTP fetcher with politeness throttle + on-disk response cache."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import httpx

from parts_lookup.domain.errors import DiscoveryError


class Fetcher:
    """Fetch URLs as text. Caches responses to disk; throttles live requests.

    The cache is keyed by sha256(url); a cache hit skips both the network and
    the delay, so parser iteration and re-runs are cheap and polite.
    """

    def __init__(
        self,
        *,
        user_agent: str,
        cache_dir: str,
        max_concurrency: int = 4,
        delay_seconds: float = 0.5,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._ua = user_agent
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._delay = delay_seconds
        self._sem = asyncio.Semaphore(max_concurrency)
        self._client = client or httpx.AsyncClient(
            headers={"User-Agent": user_agent},
            follow_redirects=True,
            timeout=30.0,
        )

    def _cache_path(self, url: str) -> Path:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return self._cache_dir / f"{digest}.txt"

    async def get(self, url: str) -> str:
        cached = self._cache_path(url)
        if cached.exists():
            return cached.read_text(encoding="utf-8")

        async with self._sem:
            if self._delay:
                await asyncio.sleep(self._delay)
            try:
                resp = await self._client.get(url, headers={"User-Agent": self._ua})
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                raise DiscoveryError(f"fetch failed for {url}: {exc}") from exc

        body = resp.text
        cached.write_text(body, encoding="utf-8")
        return body

    async def aclose(self) -> None:
        await self._client.aclose()
