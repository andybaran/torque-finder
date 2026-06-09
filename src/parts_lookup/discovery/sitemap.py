"""Pure sitemap XML parsers. No I/O."""

from __future__ import annotations

import re

_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.IGNORECASE)


def _locs(xml: str) -> list[str]:
    return [m.group(1) for m in _LOC_RE.finditer(xml)]


def parse_sitemap_index(xml: str) -> list[str]:
    """Child sitemap URLs from a <sitemapindex> document."""
    return _locs(xml)


def model_page_urls(xml: str) -> list[str]:
    """Model-page URLs (…/service/models/<id>) from a <urlset> document."""
    return [u for u in _locs(xml) if "/service/models/" in u]
