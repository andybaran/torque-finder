from __future__ import annotations

INDEX_XML = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://www.sram.com/sitemap.en.xml</loc></sitemap>
  <sitemap><loc>https://www.sram.com/sitemap.de.xml</loc></sitemap>
</sitemapindex>"""

URLSET_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://www.sram.com/en/service/models/ed-red-e1</loc></url>
  <url><loc>https://www.sram.com/en/sram/red-axs</loc></url>
  <url><loc>https://www.sram.com/en/service/models/cn-red-e1</loc></url>
</urlset>"""


def test_parse_sitemap_index_returns_child_sitemaps():
    from parts_lookup.discovery.sitemap import parse_sitemap_index

    assert parse_sitemap_index(INDEX_XML) == [
        "https://www.sram.com/sitemap.en.xml",
        "https://www.sram.com/sitemap.de.xml",
    ]


def test_model_page_urls_filters_to_service_models():
    from parts_lookup.discovery.sitemap import model_page_urls

    assert model_page_urls(URLSET_XML) == [
        "https://www.sram.com/en/service/models/ed-red-e1",
        "https://www.sram.com/en/service/models/cn-red-e1",
    ]
