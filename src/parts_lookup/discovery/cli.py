"""CLI for the discovery context: `parts-lookup-discover seed|sitemap`."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence

from parts_lookup.config import Settings, get_settings
from parts_lookup.discovery.crawler import DiscoveryCrawler
from parts_lookup.discovery.fetcher import Fetcher
from parts_lookup.discovery.registry import PublicationRegistry
from parts_lookup.indexing.session import async_session_factory


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="parts-lookup-discover")
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Bypass the on-disk cache and re-fetch every URL.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    seed = sub.add_parser("seed", help="Crawl specific model IDs and their publications.")
    seed.add_argument("model_ids", nargs="+", help="Model IDs, e.g. ed-red-e1")

    sub.add_parser("sitemap", help="Full crawl from www.sram.com/sitemap.xml.")
    return parser


def _fetcher(settings: Settings, *, force_refresh: bool = False) -> Fetcher:
    return Fetcher(
        user_agent=settings.discovery_user_agent,
        cache_dir=settings.discovery_cache_dir,
        max_concurrency=settings.discovery_max_concurrency,
        delay_seconds=settings.discovery_request_delay_seconds,
        force_refresh=force_refresh,
    )


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    fetcher = _fetcher(settings, force_refresh=args.refresh)
    factory = async_session_factory(settings)
    try:
        async with factory() as session:  # type: AsyncSession
            crawler = DiscoveryCrawler(
                fetcher=fetcher,
                registry=PublicationRegistry(session),
                base_url=settings.sram_base_url,
            )
            if args.command == "seed":
                summary = await crawler.discover_seed(args.model_ids)
            else:
                summary = await crawler.discover_sitemap(
                    f"{settings.sram_base_url}/sitemap.xml"
                )
            await session.commit()
        print(f"discovery complete: {summary}")
        return 0
    finally:
        await fetcher.aclose()


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
