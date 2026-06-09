# SRAM Publication Discovery — Design

**Status:** Approved (brainstorming) — pending implementation plan
**Date:** 2026-06-09
**Scope:** Discovery / crawl feasibility only. Ingestion of HTML manual *content* is a separate, deferred sub-project.

## 1. Background & problem

SRAM's newest manuals are no longer PDFs — they're served as "digital manuals" on
`docs.sram.com`. We need a way to find them so we can later index the same kind of
information the PDF pipeline already handles (tool sizes, torque specs, procedures) and
return a link to the manufacturer's page.

### What these pages actually are (investigated 2026-06-08)

A publication page such as
`https://docs.sram.com/en-US/publications/6TmfV97fHWv8kvGXVegoTy`
("Road AXS and XPLR AXS User Manual") is **not HTML to scrape and screenshot** — it embeds
the entire manual as structured JSON in a `<script id="manual-data" type="application/json">`
block (~384 KB). Key facts:

- **Content is already structured.** `modules` (39) → `children` → `content` (HTML blocks +
  images). Torque values live in fields (e.g. an image's `caption2` = `"40 N·m (354 In-lb)"`).
  A structured `toolList` gives tool sizes verbatim (`Hex Wrenches: 2, 2.5, 3, 4, 5, 8 mm`,
  `TORX: T25`). There is a `legend` of symbols.
- **The Models/Series/Procedure "combinations" are filter facets over ONE document, not
  separate fetches.** Top-level `filters` = `languages`(15), `models`(53), `series`(6),
  `procedure`(5). 22 of 39 modules carry a `filters` tag (e.g.
  `{"key":"procedure","values":["installation-axs"]}`). One fetch yields every combination.
- **Section-precise deep links already exist.** Every module/child has a `hash`
  (e.g. `safety-and-precautions`), so `…/publications/<id>#<hash>` jumps to the exact
  section — this is the "link to the manufacturer's page" we must return.
- **Contentful-backed.** Space `enhz2tloa31p`; content images on `images.ctfassets.net`.
  No Contentful delivery token is exposed in the page.

This means *parsing a known publication is easy*. The hard, separable problem — and the
subject of this spec — is **discovery**: enumerating which publications exist.

## 2. Feasibility verdict

**Feasible using public, server-rendered artifacts only — no headless browser.** Confirmed
chain:

```
www.sram.com/sitemap.xml  (index)
  → sitemap.en.xml  (1 MB, 5,090 URLs; 1,442 unique /service/models/<id> pages)
    → each model page (static HTML) lists its publication links:
        docs.sram.com/en-US/publications/<pub_id>/<TYPE>   TYPE ∈ {UM, SM, BM}
      → dedup pub_ids (many models share one publication)
```

Verified on the real `ed-red-e1` model page: it links `…/6TmfV97fHWv8kvGXVegoTy/UM`,
`…/3AypAkC43AlFBouXgIFsDD/SM`, `…/2wamQedjkGP8QebD5HQiiC/BM`. The links are in the static
HTML — no SPA rendering required.

### Approaches considered

| | Approach | Verdict |
|---|---|---|
| **A** | Sitemap → model pages → publication links → dedup (static HTML only) | **Chosen.** Public/stable artifacts, no JS rendering, bounded (~1.4k polite fetches), resumable. |
| **B** | Reverse-engineer the browse-by-product SPA search API | Rejected — undocumented private API, not in static HTML, fragile, ToS risk. |
| **C** | Query Contentful Delivery API directly | Not viable now — no public delivery token exposed. **Probe opportunistically** during implementation; adopt later only if a token is legitimately obtainable. |

## 3. Decisions

- **Approach A**, with **C probed opportunistically** (note a token if one surfaces in JS
  bundles; otherwise drop it — no dependency).
- **Targeted subset first:** seed the first crawl from the **Red AXS road family** (e.g.
  `ed-red-e1` and siblings). A single Red AXS UM publication's `filters.models` already lists
  all 53 served models, so one seed fans out to the whole family's publication set cheaply.
- **Catalog into a Postgres `publications` registry table** (one alembic migration). PDF
  tables (`pdfs`/`pages`) are untouched.
- **Metadata-only.** Discovery stops at "this publication exists + its metadata + where to
  get it." No content parsing, embeddings, or answering.

## 4. Architecture — new `discovery/` bounded context

Sits beside `ingestion/`. Discovery catalogs *what* exists; ingestion (deferred) turns a
known publication into indexed content and reads from the registry discovery populates.

```
src/parts_lookup/discovery/
├── sitemap.py            # fetch sitemap index → en sitemap → extract /service/models/<id> URLs
├── model_page.py         # parse a model page's HTML → publication refs (pub_id, type) + brand/series text
├── publication_probe.py  # fetch a publication → extract #manual-data JSON *metadata*
│                         #   (title, locale, filters: models/series/procedure, content_hash) — NOT content
├── crawler.py            # orchestration: seed → fetch (throttled, cached, resumable) → dedup → upsert
├── registry.py           # publications-table read/write (upsert by pub_id, last_seen, content_hash)
└── cli.py                # `parts-lookup discover ...`
```

**Layering / testability rule (matches the existing DDD layout):** the three parsers
(`sitemap`, `model_page`, `publication_probe`) are pure — HTML/JSON in, domain objects out,
**no network or DB inside them** — so they unit-test against saved fixtures. `crawler` owns
HTTP + throttling; `registry` owns DB. Domain types live in `domain/` (`DiscoveredPublication`,
`ModelRef`). Config gains a SRAM base URL + politeness knobs; the async DB session is reused.

## 5. Data model — `publications` registry table

One alembic migration adds:

| column | type | purpose |
|---|---|---|
| `id` | int pk | surrogate key |
| `pub_id` | text, unique | Contentful hash, e.g. `6TmfV97fHWv8kvGXVegoTy` |
| `pub_type` | text | `UM` / `SM` / `BM` |
| `title` | text | from `manual-data.title` |
| `locale` | text | e.g. `en-US` |
| `source_url` | text | `https://docs.sram.com/en-US/publications/<pub_id>` |
| `series` | text[] | from the publication's own `filters` (series) |
| `models` | text[] | from `filters` (models) |
| `procedures` | text[] | from `filters` (procedure) |
| `referenced_by_models` | text[] | which model-page IDs linked to it (crawl evidence) |
| `content_hash` | text | hash of the `manual-data` JSON → change detection |
| `status` | text | `discovered` → (later) `ingested` / `stale` — the ingestion handoff |
| `discovered_at` | timestamptz | first seen |
| `last_seen_at` | timestamptz | most recent crawl that saw it |

Unique constraint on `pub_id`. Upsert semantics: insert on first sight; on re-crawl update
`last_seen_at`, and if `content_hash` changed set `status = stale` (signals "re-ingest later").

## 6. Data flow

### First run (Red AXS seed)
```
seed = {ed-red-e1, …Red AXS siblings}
 → fetch each model page → parse publication refs (pub_id, type)
 → dedup pub_ids
 → for each pub: fetch page, extract manual-data metadata (title, locale, filters, content_hash)
 → upsert into publications registry
```

### Full crawl (later, same code, broader seed)
```
fetch sitemap index → sitemap.en.xml → 1,442 model URLs
 → (throttled) fetch each model page → publication refs
 → dedup → probe each unique publication → upsert
```

## 7. Politeness, robustness, change detection

- **Throttle:** small concurrency + delay between requests; honor `robots.txt`; descriptive
  User-Agent.
- **Cache** raw HTML (disk or R2) so re-runs and parser iteration don't re-hit SRAM.
- **Resumable & idempotent:** upsert by `pub_id`, update `last_seen_at`. Safe to re-run.
- **Change detection:** `content_hash` over the `manual-data` JSON; a changed hash flips
  `status = stale`.

## 8. Testing

- **Unit:** `sitemap`, `model_page`, `publication_probe` against **saved fixtures** — capture
  the real `ed-red-e1` model page and the Red AXS publication HTML into `tests/fixtures/`.
  No live network in tests.
- **Integration:** registry upsert / dedup / change-detection against a real Postgres.
- **Acceptance:** a `parts-lookup discover` run on the Red AXS seed produces registry rows
  with correct `pub_type` and filter tags.

## 9. Out of scope (explicit)

- Parsing full procedure content; embeddings; answering; any schema for HTML *content*.
- A retrieval/return contract for HTML answers (deep link instead of screenshot).
- The browse-by-product SPA search API (Approach B).

These belong to the deferred **HTML manual ingestion** sub-project, which will consume the
`publications` registry this spec produces.

## 10. Future / handoff

- Ingestion sub-project reads `publications WHERE status IN ('discovered','stale')`, fetches
  the `manual-data` JSON, and indexes module-level content (with the `#hash` deep link as the
  source link, and optionally the Contentful image URLs in place of a rendered screenshot).
- A scheduled re-crawl keeps the registry current (new models, updated `content_hash`).
