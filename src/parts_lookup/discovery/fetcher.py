"""Async HTTP fetcher with politeness throttle, robots.txt, + on-disk cache."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from parts_lookup.domain.errors import DiscoveryError


class Fetcher:
    """Fetch URLs as text. Caches responses to disk; throttles live requests.

    The cache is keyed by sha256(url); a cache hit skips the network, the
    delay, and the robots check, so parser iteration and re-runs are cheap and
    polite. Pass ``force_refresh=True`` to bypass cache reads (re-fetch and
    overwrite). ``respect_robots`` (default True) consults each host's
    robots.txt once and refuses disallowed URLs.
    """

    def __init__(
        self,
        *,
        user_agent: str,
        cache_dir: str,
        max_concurrency: int = 4,
        delay_seconds: float = 0.5,
        force_refresh: bool = False,
        respect_robots: bool = True,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._ua = user_agent
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._delay = delay_seconds
        self._force_refresh = force_refresh
        self._respect_robots = respect_robots
        self._robots: dict[str, RobotFileParser] = {}
        self._sem = asyncio.Semaphore(max_concurrency)
        self._client = client or httpx.AsyncClient(
            headers={"User-Agent": user_agent},
            follow_redirects=True,
            timeout=30.0,
        )

    def _cache_path(self, url: str) -> Path:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return self._cache_dir / f"{digest}.txt"

    async def _robots_for(self, origin: str) -> RobotFileParser:
        """Fetch + parse a host's robots.txt once; cache the parser per origin.

        Lenient on fetch failure / non-200 (parse empty rules = allow all),
        matching common crawler behaviour when robots.txt is absent.
        """
        rp = self._robots.get(origin)
        if rp is None:
            rp = RobotFileParser()
            try:
                resp = await self._client.get(
                    f"{origin}/robots.txt", headers={"User-Agent": self._ua}
                )
                rp.parse(resp.text.splitlines() if resp.status_code == 200 else [])
            except httpx.HTTPError:
                rp.parse([])
            self._robots[origin] = rp
        return rp

    async def _allowed(self, url: str) -> bool:
        if not self._respect_robots:
            return True
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        rp = await self._robots_for(origin)
        return rp.can_fetch(self._ua, url)

    async def get(self, url: str) -> str:
        cached = self._cache_path(url)
        if cached.exists() and not self._force_refresh:
            return cached.read_text(encoding="utf-8")

        if not await self._allowed(url):
            raise DiscoveryError(f"blocked by robots.txt: {url}")

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
