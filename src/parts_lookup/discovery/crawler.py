"""Discovery orchestration: model pages → publication refs → registry upserts.

Collaborators are injected (fetcher with `async get(url)`, registry with
`async upsert(pub, referenced_by_models)`) so this is unit-testable with fakes.
"""

from __future__ import annotations

from typing import Any

from parts_lookup.discovery.model_page import parse_publication_refs
from parts_lookup.discovery.publication_probe import build_publication
from parts_lookup.discovery.sitemap import model_page_urls, parse_sitemap_index


class DiscoveryCrawler:
    def __init__(self, *, fetcher: Any, registry: Any, base_url: str) -> None:
        self._fetcher = fetcher
        self._registry = registry
        self._base_url = base_url.rstrip("/")

    def _model_url(self, model_id: str) -> str:
        return f"{self._base_url}/en/service/models/{model_id}"

    async def discover_seed(self, model_ids: list[str]) -> dict[str, int]:
        """Crawl the given model pages, dedup their publications, upsert each."""
        # pub_id -> (PublicationRef, set of referencing model_ids)
        refs: dict[str, Any] = {}
        referenced_by: dict[str, set[str]] = {}

        for model_id in model_ids:
            html = await self._fetcher.get(self._model_url(model_id))
            for ref in parse_publication_refs(html):
                refs.setdefault(ref.pub_id, ref)
                # Prefer a typed ref if a later page provides one.
                if ref.pub_type and not refs[ref.pub_id].pub_type:
                    refs[ref.pub_id] = ref
                referenced_by.setdefault(ref.pub_id, set()).add(model_id)

        upserted = 0
        for pub_id, ref in refs.items():
            pub_html = await self._fetcher.get(ref.source_url)
            pub = build_publication(pub_html, ref)
            await self._registry.upsert(pub, sorted(referenced_by[pub_id]))
            upserted += 1

        return {
            "models_crawled": len(model_ids),
            "publications_found": len(refs),
            "publications_upserted": upserted,
        }

    async def discover_sitemap(self, sitemap_index_url: str) -> dict[str, int]:
        """Full crawl: sitemap index → en urlset → every model page → discover_seed."""
        index_xml = await self._fetcher.get(sitemap_index_url)
        child_sitemaps = parse_sitemap_index(index_xml)
        en_sitemaps = [u for u in child_sitemaps if ".en." in u] or child_sitemaps

        model_ids: list[str] = []
        seen: set[str] = set()
        for sm in en_sitemaps:
            urlset = await self._fetcher.get(sm)
            for url in model_page_urls(urlset):
                model_id = url.rstrip("/").rsplit("/", 1)[-1]
                if model_id not in seen:
                    seen.add(model_id)
                    model_ids.append(model_id)

        return await self.discover_seed(model_ids)
