# SRAM Publication Discovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a `discovery/` bounded context that crawls SRAM (sitemap → model pages → publication links) and catalogs every digital-manual publication into a Postgres `publications` registry — metadata only, no content ingestion.

**Architecture:** Pure parsers (sitemap, model page, publication metadata) take strings in and return domain objects with no I/O, so they unit-test against compact fixtures. A throttled, disk-cached `Fetcher` owns HTTP; a `PublicationRegistry` owns the new table; a `DiscoveryCrawler` orchestrates them via dependency injection (so it tests with fakes). A standalone `parts-lookup-discover` CLI wires real implementations from `Settings`.

**Tech Stack:** Python 3.14, httpx (async + `MockTransport` for tests), SQLAlchemy 2.0 async ORM (shares the `Base` in `indexing/repository.py`), alembic, pydantic v2, pytest (`asyncio_mode=auto`).

Reference spec: `docs/superpowers/specs/2026-06-09-sram-publication-discovery-design.md`.

---

## File Structure

- Create `src/parts_lookup/discovery/__init__.py` — package marker.
- Create `src/parts_lookup/discovery/sitemap.py` — pure sitemap XML parsers.
- Create `src/parts_lookup/discovery/model_page.py` — pure model-page HTML → publication refs.
- Create `src/parts_lookup/discovery/publication_probe.py` — pure publication HTML → metadata + content hash.
- Create `src/parts_lookup/discovery/fetcher.py` — async HTTP client with throttle + disk cache.
- Create `src/parts_lookup/discovery/registry.py` — `Publication` ORM model + `PublicationRegistry`.
- Create `src/parts_lookup/discovery/crawler.py` — orchestration.
- Create `src/parts_lookup/discovery/cli.py` — argparse CLI + `main()`.
- Modify `src/parts_lookup/domain/errors.py` — add `DiscoveryError`.
- Modify `src/parts_lookup/domain/models.py` — add `PublicationRef`, `DiscoveredPublication`.
- Modify `src/parts_lookup/config.py` — add discovery settings.
- Modify `pyproject.toml` — add `parts-lookup-discover` console script.
- Create `infra/migrations/versions/0002_publications.py` — `publications` table.
- Create tests under `tests/unit/` (parsers, fetcher, crawler) and `tests/integration/` (registry).

**Canonical types (used across tasks — keep names/fields identical):**

- `PublicationRef(pub_id: str, pub_type: str, source_url: str)` — `pub_type` is `""`/`UM`/`SM`/`BM`.
- `DiscoveredPublication(pub_id, pub_type, title, locale, source_url, series, models, procedures, content_hash)` — the three filter fields are `tuple[str, ...]`.
- Registry method: `async upsert(pub: DiscoveredPublication, referenced_by_models: list[str]) -> str` returning one of `"inserted"`, `"unchanged"`, `"stale"`.

---

## Task 1: Domain error + config settings

**Files:**
- Modify: `src/parts_lookup/domain/errors.py`
- Modify: `src/parts_lookup/config.py`
- Test: `tests/unit/test_discovery_config.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_discovery_config.py
from __future__ import annotations


def test_discovery_error_is_parts_lookup_error():
    from parts_lookup.domain.errors import DiscoveryError, PartsLookupError

    assert issubclass(DiscoveryError, PartsLookupError)


def test_settings_have_discovery_defaults(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    from parts_lookup.config import Settings

    s = Settings()  # type: ignore[call-arg]
    assert s.sram_base_url == "https://www.sram.com"
    assert s.sram_docs_base_url == "https://docs.sram.com"
    assert s.discovery_max_concurrency >= 1
    assert s.discovery_request_delay_seconds >= 0
    assert "discovery" in s.discovery_cache_dir or s.discovery_cache_dir.endswith("cache")
    assert s.discovery_user_agent  # non-empty
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --extra dev pytest tests/unit/test_discovery_config.py -v`
Expected: FAIL — `ImportError: cannot import name 'DiscoveryError'`.

- [ ] **Step 3: Add the error class**

In `src/parts_lookup/domain/errors.py`, append:

```python
class DiscoveryError(PartsLookupError):
    """Discovery/crawl failed (fetch, parse, or registry write)."""
```

- [ ] **Step 4: Add the config settings**

In `src/parts_lookup/config.py`, inside `class Settings`, after the `# --- App ---` block add:

```python
    # --- Discovery (SRAM crawl) ---
    sram_base_url: str = "https://www.sram.com"
    sram_docs_base_url: str = "https://docs.sram.com"
    discovery_user_agent: str = (
        "parts-lookup-discovery/0.1 (+https://github.com/andybaran/torque-finder)"
    )
    discovery_max_concurrency: int = Field(default=4, ge=1, le=16)
    discovery_request_delay_seconds: float = Field(default=0.5, ge=0.0)
    discovery_cache_dir: str = ".cache/discovery"
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run --extra dev pytest tests/unit/test_discovery_config.py -v`
Expected: PASS (2 passed).

- [ ] **Step 6: Commit**

```bash
git add src/parts_lookup/domain/errors.py src/parts_lookup/config.py tests/unit/test_discovery_config.py
git commit -m "feat(discovery): add DiscoveryError + discovery settings"
```

---

## Task 2: Domain models — PublicationRef + DiscoveredPublication

**Files:**
- Modify: `src/parts_lookup/domain/models.py`
- Test: `tests/unit/test_discovery_models.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_discovery_models.py
from __future__ import annotations


def test_publication_ref_is_frozen():
    from parts_lookup.domain.models import PublicationRef

    ref = PublicationRef(pub_id="abc", pub_type="UM", source_url="https://x/abc")
    assert ref.pub_id == "abc"
    import pytest

    with pytest.raises(Exception):
        ref.pub_id = "zzz"  # frozen


def test_discovered_publication_holds_filter_tuples():
    from parts_lookup.domain.models import DiscoveredPublication

    pub = DiscoveredPublication(
        pub_id="abc",
        pub_type="UM",
        title="Road AXS",
        locale="en-US",
        source_url="https://docs.sram.com/en-US/publications/abc",
        series=("red-axs",),
        models=("ed-red-e1",),
        procedures=("installation-axs",),
        content_hash="deadbeef",
    )
    assert pub.series == ("red-axs",)
    assert pub.models == ("ed-red-e1",)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --extra dev pytest tests/unit/test_discovery_models.py -v`
Expected: FAIL — `ImportError: cannot import name 'PublicationRef'`.

- [ ] **Step 3: Add the models**

In `src/parts_lookup/domain/models.py`, append (the `_Frozen` base already exists in this file):

```python
class PublicationRef(_Frozen):
    """A publication link found on a model page. ``pub_type`` ∈ {'', 'UM', 'SM', 'BM'}."""

    pub_id: str
    pub_type: str = ""
    source_url: str


class DiscoveredPublication(_Frozen):
    """Metadata for one publication, parsed from its embedded manual-data JSON."""

    pub_id: str
    pub_type: str = ""
    title: str = ""
    locale: str = ""
    source_url: str
    series: tuple[str, ...] = ()
    models: tuple[str, ...] = ()
    procedures: tuple[str, ...] = ()
    content_hash: str
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --extra dev pytest tests/unit/test_discovery_models.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/parts_lookup/domain/models.py tests/unit/test_discovery_models.py
git commit -m "feat(discovery): add PublicationRef + DiscoveredPublication domain models"
```

---

## Task 3: Sitemap parser

**Files:**
- Create: `src/parts_lookup/discovery/__init__.py`
- Create: `src/parts_lookup/discovery/sitemap.py`
- Test: `tests/unit/test_discovery_sitemap.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_discovery_sitemap.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --extra dev pytest tests/unit/test_discovery_sitemap.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'parts_lookup.discovery'`.

- [ ] **Step 3: Create the package marker and parser**

`src/parts_lookup/discovery/__init__.py`:

```python
"""Discovery bounded context: enumerate manufacturer publications."""
```

`src/parts_lookup/discovery/sitemap.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --extra dev pytest tests/unit/test_discovery_sitemap.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/parts_lookup/discovery/__init__.py src/parts_lookup/discovery/sitemap.py tests/unit/test_discovery_sitemap.py
git commit -m "feat(discovery): sitemap parser (index + model-page URLs)"
```

---

## Task 4: Model-page parser → publication refs

**Files:**
- Create: `src/parts_lookup/discovery/model_page.py`
- Test: `tests/unit/test_discovery_model_page.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_discovery_model_page.py
from __future__ import annotations

MODEL_HTML = """
<html><body>
<a href="https://docs.sram.com/en-US/publications/6TmfV97fHWv8kvGXVegoTy/UM">User Manual</a>
<a href="https://docs.sram.com/en-US/publications/3AypAkC43AlFBouXgIFsDD/SM">Service Manual</a>
<a href="https://docs.sram.com/en-US/publications/2wamQedjkGP8QebD5HQiiC/BM">Bleed</a>
<a href="https://docs.sram.com/en-US/publications/6TmfV97fHWv8kvGXVegoTy">dup, no type</a>
<a href="https://www.sram.com/en/other">unrelated</a>
</body></html>
"""


def test_parse_publication_refs_dedups_and_keeps_types():
    from parts_lookup.discovery.model_page import parse_publication_refs

    refs = parse_publication_refs(MODEL_HTML)
    by_id = {r.pub_id: r for r in refs}

    assert set(by_id) == {
        "6TmfV97fHWv8kvGXVegoTy",
        "3AypAkC43AlFBouXgIFsDD",
        "2wamQedjkGP8QebD5HQiiC",
    }
    # the typed variant wins over the bare duplicate
    assert by_id["6TmfV97fHWv8kvGXVegoTy"].pub_type == "UM"
    assert by_id["6TmfV97fHWv8kvGXVegoTy"].source_url == (
        "https://docs.sram.com/en-US/publications/6TmfV97fHWv8kvGXVegoTy"
    )
    assert by_id["3AypAkC43AlFBouXgIFsDD"].pub_type == "SM"


def test_parse_publication_refs_empty_when_none():
    from parts_lookup.discovery.model_page import parse_publication_refs

    assert parse_publication_refs("<html>nothing here</html>") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --extra dev pytest tests/unit/test_discovery_model_page.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'parts_lookup.discovery.model_page'`.

- [ ] **Step 3: Write the parser**

`src/parts_lookup/discovery/model_page.py`:

```python
"""Pure model-page HTML parser → publication references. No I/O."""

from __future__ import annotations

import re

from parts_lookup.domain.models import PublicationRef

# docs.sram.com/<locale>/publications/<pub_id>[/<TYPE>]
_PUB_RE = re.compile(
    r"https?://docs\.sram\.com/(?P<locale>[A-Za-z-]+)/publications/"
    r"(?P<pub_id>[A-Za-z0-9]+)(?:/(?P<type>[A-Z]{2}))?",
)


def parse_publication_refs(html: str) -> list[PublicationRef]:
    """Extract unique publication refs from a model page, preferring typed links.

    Order is stable: refs appear in first-seen order of their pub_id.
    """
    order: list[str] = []
    found: dict[str, PublicationRef] = {}

    for m in _PUB_RE.finditer(html):
        pub_id = m.group("pub_id")
        locale = m.group("locale")
        pub_type = m.group("type") or ""
        source_url = f"https://docs.sram.com/{locale}/publications/{pub_id}"
        ref = PublicationRef(pub_id=pub_id, pub_type=pub_type, source_url=source_url)

        if pub_id not in found:
            found[pub_id] = ref
            order.append(pub_id)
        elif pub_type and not found[pub_id].pub_type:
            # Upgrade a bare ref to a typed one.
            found[pub_id] = ref

    return [found[pid] for pid in order]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --extra dev pytest tests/unit/test_discovery_model_page.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/parts_lookup/discovery/model_page.py tests/unit/test_discovery_model_page.py
git commit -m "feat(discovery): model-page parser → deduped publication refs"
```

---

## Task 5: Publication metadata probe

**Files:**
- Create: `src/parts_lookup/discovery/publication_probe.py`
- Test: `tests/unit/test_discovery_publication_probe.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_discovery_publication_probe.py
from __future__ import annotations

import hashlib
import json

import pytest

MANUAL_DATA = {
    "title": "Road AXS and XPLR AXS",
    "locale": "en-US",
    "filters": [
        {"key": "languages", "options": [{"value": "en-US", "text": "English"}]},
        {"key": "models", "options": [
            {"value": "ed-red-e1", "text": "ED-RED-E1"},
            {"value": "cn-red-e1", "text": "CN-RED-E1"},
        ]},
        {"key": "series", "options": [{"value": "red-axs", "text": "RED AXS"}]},
        {"key": "procedure", "options": [{"value": "installation-axs", "text": "Install"}]},
    ],
}
PUB_HTML = (
    "<html><head><title>x</title></head><body>"
    '<script id="manual-data" type="application/json">' + json.dumps(MANUAL_DATA) + "</script>"
    "</body></html>"
)


def test_extract_manual_data_json_roundtrips():
    from parts_lookup.discovery.publication_probe import extract_manual_data_json

    raw = extract_manual_data_json(PUB_HTML)
    assert json.loads(raw)["title"] == "Road AXS and XPLR AXS"


def test_extract_manual_data_json_missing_raises():
    from parts_lookup.discovery.publication_probe import extract_manual_data_json
    from parts_lookup.domain.errors import DiscoveryError

    with pytest.raises(DiscoveryError):
        extract_manual_data_json("<html>no script</html>")


def test_build_publication_pulls_filters_and_hash():
    from parts_lookup.domain.models import PublicationRef
    from parts_lookup.discovery.publication_probe import build_publication

    ref = PublicationRef(
        pub_id="6TmfV97fHWv8kvGXVegoTy",
        pub_type="UM",
        source_url="https://docs.sram.com/en-US/publications/6TmfV97fHWv8kvGXVegoTy",
    )
    pub = build_publication(PUB_HTML, ref)

    assert pub.pub_id == "6TmfV97fHWv8kvGXVegoTy"
    assert pub.pub_type == "UM"
    assert pub.title == "Road AXS and XPLR AXS"
    assert pub.locale == "en-US"
    assert pub.series == ("red-axs",)
    assert pub.models == ("ed-red-e1", "cn-red-e1")
    assert pub.procedures == ("installation-axs",)
    raw = json.dumps(MANUAL_DATA)
    assert pub.content_hash == hashlib.sha256(raw.encode("utf-8")).hexdigest()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --extra dev pytest tests/unit/test_discovery_publication_probe.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'parts_lookup.discovery.publication_probe'`.

- [ ] **Step 3: Write the probe**

`src/parts_lookup/discovery/publication_probe.py`:

```python
"""Pure publication HTML → metadata. Extracts the embedded manual-data JSON. No I/O."""

from __future__ import annotations

import hashlib
import json
import re

from parts_lookup.domain.errors import DiscoveryError
from parts_lookup.domain.models import DiscoveredPublication, PublicationRef

_MANUAL_DATA_RE = re.compile(
    r'<script[^>]*id="manual-data"[^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)


def extract_manual_data_json(html: str) -> str:
    """Return the raw JSON text of the <script id="manual-data"> block."""
    m = _MANUAL_DATA_RE.search(html)
    if not m:
        raise DiscoveryError("publication HTML has no <script id='manual-data'> block")
    return m.group(1).strip()


def _filter_values(data: dict, key: str) -> tuple[str, ...]:
    for f in data.get("filters", []):
        if f.get("key") == key:
            return tuple(o["value"] for o in f.get("options", []) if "value" in o)
    return ()


def build_publication(html: str, ref: PublicationRef) -> DiscoveredPublication:
    """Parse a publication page into a DiscoveredPublication metadata object."""
    raw = extract_manual_data_json(html)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DiscoveryError(f"manual-data JSON for {ref.pub_id} is invalid") from exc

    return DiscoveredPublication(
        pub_id=ref.pub_id,
        pub_type=ref.pub_type,
        title=str(data.get("title", "")).strip(),
        locale=str(data.get("locale", "")),
        source_url=ref.source_url,
        series=_filter_values(data, "series"),
        models=_filter_values(data, "models"),
        procedures=_filter_values(data, "procedure"),
        content_hash=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --extra dev pytest tests/unit/test_discovery_publication_probe.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/parts_lookup/discovery/publication_probe.py tests/unit/test_discovery_publication_probe.py
git commit -m "feat(discovery): publication metadata probe (manual-data JSON)"
```

---

## Task 6: Throttled, disk-cached Fetcher

**Files:**
- Create: `src/parts_lookup/discovery/fetcher.py`
- Test: `tests/unit/test_discovery_fetcher.py`

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --extra dev pytest tests/unit/test_discovery_fetcher.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'parts_lookup.discovery.fetcher'`.

- [ ] **Step 3: Write the fetcher**

`src/parts_lookup/discovery/fetcher.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --extra dev pytest tests/unit/test_discovery_fetcher.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/parts_lookup/discovery/fetcher.py tests/unit/test_discovery_fetcher.py
git commit -m "feat(discovery): throttled, disk-cached async fetcher"
```

---

## Task 7: publications table — migration + ORM + registry

**Files:**
- Create: `infra/migrations/versions/0002_publications.py`
- Create: `src/parts_lookup/discovery/registry.py`
- Test: `tests/integration/test_discovery_registry.py`

- [ ] **Step 1: Write the failing (integration) test**

```python
# tests/integration/test_discovery_registry.py
from __future__ import annotations

import os
import uuid

import pytest

if not os.environ.get("DATABASE_URL"):
    pytest.skip("DATABASE_URL not set; skipping registry test", allow_module_level=True)

pytest.importorskip("parts_lookup.discovery.registry")

pytestmark = pytest.mark.asyncio


async def test_upsert_insert_then_change_detection():
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from parts_lookup.domain.models import DiscoveredPublication
    from parts_lookup.discovery.registry import PublicationRegistry

    url = os.environ["DATABASE_URL"]
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)

    pub_id = f"test-{uuid.uuid4().hex[:12]}"
    pub = DiscoveredPublication(
        pub_id=pub_id,
        pub_type="UM",
        title="Test",
        locale="en-US",
        source_url=f"https://docs.sram.com/en-US/publications/{pub_id}",
        series=("red-axs",),
        models=("ed-red-e1",),
        procedures=("installation-axs",),
        content_hash="hash-v1",
    )

    engine = create_async_engine(url, echo=False)
    try:
        async with AsyncSession(engine) as session:
            reg = PublicationRegistry(session)

            assert await reg.upsert(pub, ["ed-red-e1"]) == "inserted"
            assert await reg.upsert(pub, ["ed-red-e1"]) == "unchanged"

            changed = pub.model_copy(update={"content_hash": "hash-v2"})
            assert await reg.upsert(changed, ["cn-red-e1"]) == "stale"

            row = await reg.get(pub_id)
            assert row is not None
            assert row.status == "stale"
            assert set(row.referenced_by_models) == {"ed-red-e1", "cn-red-e1"}

            # Clean up so the test DB isn't polluted.
            await session.delete(row)
            await session.commit()
    finally:
        await engine.dispose()
```

- [ ] **Step 2: Write the alembic migration**

`infra/migrations/versions/0002_publications.py`:

```python
"""publications registry table

Revision ID: 0002_publications
Revises: 0001_initial
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY

revision = "0002_publications"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "publications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("pub_id", sa.String(), nullable=False, unique=True, index=True),
        sa.Column("pub_type", sa.String(), nullable=False, server_default=""),
        sa.Column("title", sa.String(), nullable=False, server_default=""),
        sa.Column("locale", sa.String(), nullable=False, server_default=""),
        sa.Column("source_url", sa.String(), nullable=False),
        sa.Column("series", ARRAY(sa.String()), nullable=False, server_default="{}"),
        sa.Column("models", ARRAY(sa.String()), nullable=False, server_default="{}"),
        sa.Column("procedures", ARRAY(sa.String()), nullable=False, server_default="{}"),
        sa.Column(
            "referenced_by_models",
            ARRAY(sa.String()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="discovered"),
        sa.Column(
            "discovered_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("publications")
```

- [ ] **Step 3: Write the ORM model + registry**

`src/parts_lookup/discovery/registry.py`:

```python
"""publications-table ORM model + registry repository (discovery context)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, func, select
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from parts_lookup.domain.models import DiscoveredPublication

# Share the declarative Base + metadata used by alembic's env.py.
from parts_lookup.indexing.repository import Base


class Publication(Base):
    __tablename__ = "publications"

    id: Mapped[int] = mapped_column(primary_key=True)
    pub_id: Mapped[str] = mapped_column(String, unique=True, index=True, nullable=False)
    pub_type: Mapped[str] = mapped_column(String, nullable=False, default="")
    title: Mapped[str] = mapped_column(String, nullable=False, default="")
    locale: Mapped[str] = mapped_column(String, nullable=False, default="")
    source_url: Mapped[str] = mapped_column(String, nullable=False)
    series: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    models: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    procedures: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    referenced_by_models: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, default=list
    )
    content_hash: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="discovered")
    discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PublicationRegistry:
    """Upsert/read access to the publications registry."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(
        self, pub: DiscoveredPublication, referenced_by_models: list[str]
    ) -> str:
        """Insert or update a publication. Returns 'inserted' | 'unchanged' | 'stale'."""
        row = await self._get_row(pub.pub_id)
        if row is None:
            self._session.add(
                Publication(
                    pub_id=pub.pub_id,
                    pub_type=pub.pub_type,
                    title=pub.title,
                    locale=pub.locale,
                    source_url=pub.source_url,
                    series=list(pub.series),
                    models=list(pub.models),
                    procedures=list(pub.procedures),
                    referenced_by_models=sorted(set(referenced_by_models)),
                    content_hash=pub.content_hash,
                    status="discovered",
                )
            )
            await self._session.flush()
            return "inserted"

        row.last_seen_at = datetime.now().astimezone()
        row.referenced_by_models = sorted(
            set(row.referenced_by_models) | set(referenced_by_models)
        )
        if row.content_hash != pub.content_hash:
            row.content_hash = pub.content_hash
            row.title = pub.title
            row.series = list(pub.series)
            row.models = list(pub.models)
            row.procedures = list(pub.procedures)
            row.status = "stale"
            await self._session.flush()
            return "stale"
        await self._session.flush()
        return "unchanged"

    async def get(self, pub_id: str) -> Publication | None:
        return await self._get_row(pub_id)

    async def list_all(self) -> list[Publication]:
        result = await self._session.execute(select(Publication).order_by(Publication.id))
        return list(result.scalars().all())

    async def _get_row(self, pub_id: str) -> Publication | None:
        result = await self._session.execute(
            select(Publication).where(Publication.pub_id == pub_id)
        )
        return result.scalar_one_or_none()
```

- [ ] **Step 4: Apply the migration against the dev DB**

Run: `uv run alembic -c alembic.ini upgrade head`
Expected: applies `0002_publications`; `\d publications` shows the table with `timestamptz` columns.

- [ ] **Step 5: Run the integration test**

Run: `uv run --extra dev pytest tests/integration/test_discovery_registry.py -v`
Expected: PASS (1 passed). (Skips if `DATABASE_URL` unset.)

- [ ] **Step 6: Commit**

```bash
git add infra/migrations/versions/0002_publications.py src/parts_lookup/discovery/registry.py tests/integration/test_discovery_registry.py
git commit -m "feat(discovery): publications registry table + ORM + upsert"
```

---

## Task 8: Crawler orchestration

**Files:**
- Create: `src/parts_lookup/discovery/crawler.py`
- Test: `tests/unit/test_discovery_crawler.py`

- [ ] **Step 1: Write the failing test (fakes for fetcher + registry)**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --extra dev pytest tests/unit/test_discovery_crawler.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'parts_lookup.discovery.crawler'`.

- [ ] **Step 3: Write the crawler**

`src/parts_lookup/discovery/crawler.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --extra dev pytest tests/unit/test_discovery_crawler.py -v`
Expected: PASS (1 passed).

- [ ] **Step 5: Commit**

```bash
git add src/parts_lookup/discovery/crawler.py tests/unit/test_discovery_crawler.py
git commit -m "feat(discovery): crawler orchestration (seed + sitemap)"
```

---

## Task 9: CLI + console script + acceptance run

**Files:**
- Create: `src/parts_lookup/discovery/cli.py`
- Modify: `pyproject.toml`
- Test: `tests/unit/test_discovery_cli.py`

- [ ] **Step 1: Write the failing test (argument parsing only — no network)**

```python
# tests/unit/test_discovery_cli.py
from __future__ import annotations


def test_build_parser_seed_command():
    from parts_lookup.discovery.cli import build_parser

    args = build_parser().parse_args(["seed", "ed-red-e1", "cn-red-e1"])
    assert args.command == "seed"
    assert args.model_ids == ["ed-red-e1", "cn-red-e1"]


def test_build_parser_sitemap_command():
    from parts_lookup.discovery.cli import build_parser

    args = build_parser().parse_args(["sitemap"])
    assert args.command == "sitemap"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --extra dev pytest tests/unit/test_discovery_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'parts_lookup.discovery.cli'`.

- [ ] **Step 3: Write the CLI**

`src/parts_lookup/discovery/cli.py`:

```python
"""CLI for the discovery context: `parts-lookup-discover seed|sitemap`."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from parts_lookup.config import Settings, get_settings
from parts_lookup.discovery.crawler import DiscoveryCrawler
from parts_lookup.discovery.fetcher import Fetcher
from parts_lookup.discovery.registry import PublicationRegistry
from parts_lookup.indexing.session import async_session_factory


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="parts-lookup-discover")
    sub = parser.add_subparsers(dest="command", required=True)

    seed = sub.add_parser("seed", help="Crawl specific model IDs and their publications.")
    seed.add_argument("model_ids", nargs="+", help="Model IDs, e.g. ed-red-e1")

    sub.add_parser("sitemap", help="Full crawl from www.sram.com/sitemap.xml.")
    return parser


def _fetcher(settings: Settings) -> Fetcher:
    return Fetcher(
        user_agent=settings.discovery_user_agent,
        cache_dir=settings.discovery_cache_dir,
        max_concurrency=settings.discovery_max_concurrency,
        delay_seconds=settings.discovery_request_delay_seconds,
    )


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    fetcher = _fetcher(settings)
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
```

- [ ] **Step 4: Register the console script**

In `pyproject.toml`, change the `[project.scripts]` section to:

```toml
[project.scripts]
parts-lookup = "parts_lookup.ingestion.cli:main"
parts-lookup-discover = "parts_lookup.discovery.cli:main"
```

- [ ] **Step 5: Run tests + re-sync the entry point**

Run: `uv run --extra dev pytest tests/unit/test_discovery_cli.py -v`
Expected: PASS (2 passed).

Run: `uv sync`
Expected: re-installs the project so `parts-lookup-discover` is on PATH.

- [ ] **Step 6: Commit**

```bash
git add src/parts_lookup/discovery/cli.py pyproject.toml tests/unit/test_discovery_cli.py
git commit -m "feat(discovery): CLI + parts-lookup-discover console script"
```

- [ ] **Step 7: Acceptance run (Red AXS seed, live)**

Run: `uv run parts-lookup-discover seed ed-red-e1`
Expected: prints a summary like `{'models_crawled': 1, 'publications_found': 3, 'publications_upserted': 3}`.

Verify the registry rows:

```bash
uv run python - <<'PY'
import asyncio, asyncpg, urllib.parse as up
from parts_lookup.config import get_settings
s = get_settings(); url = s.database_url.replace("postgresql+asyncpg://", "postgresql://")
p = up.urlsplit(url); q = p.query or ""
ssl = "require" if ("ssl=require" in q or "sslmode=require" in q) else None
clean = up.urlunsplit((p.scheme, p.netloc, p.path, "", ""))
async def main():
    c = await asyncpg.connect(clean, ssl=ssl)
    for r in await c.fetch("select pub_id, pub_type, title, series, status from publications order by id"):
        print(dict(r))
    await c.close()
asyncio.run(main())
PY
```
Expected: rows for the Red AXS UM/SM/BM publications with `pub_type` and `series` populated, `status='discovered'`.

- [ ] **Step 8: Full unit suite green**

Run: `uv run --extra dev pytest tests/unit -v`
Expected: all discovery unit tests + the pre-existing 15 pass.

---

## Self-Review

**Spec coverage:**
- §4 bounded context / file layout → Tasks 3–9 create every listed module. ✓
- §5 `publications` table (all columns, `timestamptz`, unique `pub_id`, `status`) → Task 7 migration + ORM. ✓
- §6 first-run seed flow → Task 8 `discover_seed`; acceptance run Task 9 Step 7. ✓ Full-crawl flow → Task 8 `discover_sitemap`. ✓
- §7 politeness/cache/resumable/change-detection → Task 6 (throttle + disk cache), Task 7 (`content_hash`→`stale`, `last_seen_at`, idempotent upsert). ✓
- §8 testing (fixtures, no live network in unit; integration on `DATABASE_URL`) → Tasks 3–6, 8 unit; Task 7 integration. ✓
- §9 out-of-scope (no content ingest) → respected; probe is metadata-only. ✓
- Opportunistic Contentful (C): intentionally **not** a task — it's an investigation note in the spec, no code. Documented here so it isn't mistaken for a gap.

**Placeholder scan:** none — every step has runnable code/commands.

**Type consistency:** `PublicationRef(pub_id, pub_type, source_url)` and `DiscoveredPublication(... series/models/procedures as tuples ...)` are defined in Task 2 and used identically in Tasks 4, 5, 7, 8. `Fetcher.get`, `PublicationRegistry.upsert(pub, referenced_by_models) -> str`, and `DiscoveryCrawler(fetcher=, registry=, base_url=)` signatures match across Tasks 6–9. Registry status strings `inserted|unchanged|stale` consistent (Task 7 ↔ test). ✓
