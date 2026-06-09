# HTML Manual Content Ingestion — Design

**Status:** Approved (brainstorming + user sign-off on migration & API break) — pending implementation plan
**Date:** 2026-06-09
**Issue:** #2
**Scope:** Ingest SRAM digital-manual (HTML) content and answer from it via the same
`/v1/query` as PDFs. Consumes the `publications` registry produced by the discovery
sub-project (see `2026-06-09-sram-publication-discovery-design.md`).

## 1. Background & problem

The PDF pipeline is live: 258 PDFs / 9,818 pages indexed, hybrid retrieval, Claude vision
extraction, deployed on Railway. But SRAM's newest manuals are digital-only, served on
`docs.sram.com`. Discovery has already cataloged three Red AXS publications
(status `discovered`) in the `publications` table:

| pub_id | type |
|---|---|
| `6TmfV97fHWv8kvGXVegoTy` | UM |
| `2wamQedjkGP8QebD5HQiiC` | BM |
| `3AypAkC43AlFBouXgIFsDD` | SM |

### What a publication page contains (investigated 2026-06-08)

`https://docs.sram.com/en-US/publications/<pub_id>` embeds the whole manual as JSON in
`<script id="manual-data" type="application/json">`:

- `modules` (~39) → `children` (blocks) → `content` (HTML `<p>` + images). **Torque values
  live in image `caption2` fields** (e.g. `"40 N·m (354 in-lb)"`). A structured `toolList`
  gives tool sizes verbatim (`Hex: 2, 2.5, 3, 4, 5, 8 mm`, `TORX: T25`). A `legend` maps
  symbols. Top-level `filters` carry languages/models/series/procedure facets.
- Every module and child has a `#hash` → section-precise deep link
  `…/publications/<id>#<hash>`. **No screenshot needed** — the deep link *is* the source
  reference.
- Contentful-backed (space `enhz2tloa31p`); images on `images.ctfassets.net`. No API token
  exposed — we parse the embedded JSON, not Contentful.
- Trimmed real fixtures exist: `tests/fixtures/sram_publication_red_axs.html` and
  `tests/fixtures/sram_model_page_ed_red_e1.html`.

### Goal

Ingest a publication's structured content so a mechanic's natural-language query is answered
from PDF *or* HTML — whichever ranks best — returning the answer plus a `docs.sram.com#hash`
deep link (no screenshot for HTML sources).

## 2. Decisions (user-approved)

1. **Unified query.** One `/v1/query` ranks PDF and HTML candidates together; the best
   answer wins. Retrieval, extraction, and the API become source-agnostic.
2. **Unified storage: `documents` + `chunks`.** Migrate existing `pdfs`/`pages` into the new
   tables, then **drop the old tables**. Explicitly chosen over a parallel HTML-only table;
   the Railway Postgres volume was grown to **250 GB** so the `pages`→`chunks` vector copy +
   HNSW rebuild cannot hit `DiskFullError`.
3. **Chunk granularity: block-level (fine), with small-to-big extraction.** Embed each HTML
   block for sharp retrieval; on a hit, reconstruct the parent module's full text from
   sibling chunks and send *that* to Claude. No module-text duplication in storage.
4. **Extraction branches by source type.** PDF chunk → Claude **vision** on the page PNG
   (unchanged). HTML chunk → Claude **text** on the reconstructed module. Same
   structured-output schema (answer / tool_size / torque / confidence / source).
5. **Breaking API response change** (approved — no frontend exists yet). PDF-specific fields
   are replaced by source-agnostic ones (§6).
6. **Migration safety gate.** The `thoughts.md` ground-truth eval must pass through the new
   model **before** `pdfs`/`pages` are dropped (§7).
7. **Scope: the 3 Red AXS publications** already in the registry. Full-corpus HTML ingest
   and rerankers are out of scope.

## 3. Data model — `documents` + `chunks`

One alembic migration creates:

```
documents(
  id            int pk,
  source_type   text  ('pdf' | 'html'),
  title         text,
  source_url    text,                  -- R2 PDF URL | docs.sram.com publication URL
  source_ref    text,                  -- sha256 (pdf) | pub_id (html); unique
  created_at    timestamptz
)

chunks(
  id            int pk,
  document_id   int → documents(id),
  ordinal       int,                   -- page_no (pdf) | block order within publication (html)
  text          text,
  tsv           tsvector (generated),
  embedding     vector(1024),          -- HNSW, cosine
  png_r2_key    text NULL,             -- pdf only
  anchor        text NULL,             -- html: block #hash (nullable — not every block has one)
  parent_anchor text NULL,             -- html: owning module's #hash
  source_url    text,                  -- pdf: deep link w/ #page=N | html: …/publications/<id>#<anchor or parent_anchor>
  created_at    timestamptz
)
```

- **PDF chunk = a page.** `png_r2_key` set, `ordinal = page_no`, anchors null.
- **HTML chunk = a block.** `png_r2_key` null, `anchor` = block hash (nullable),
  `parent_anchor` = module hash, `source_url` resolves to the most precise hash available.
- Module reconstruction at extraction time:
  `SELECT text FROM chunks WHERE document_id = ? AND parent_anchor = ? ORDER BY ordinal`.
- Indexes mirror today's `pages`: GIN on `tsv`, HNSW (cosine) on `embedding`, plus
  `(document_id, parent_anchor, ordinal)` for module reconstruction.

## 4. Ingestion flow (HTML)

```
read publications WHERE status IN ('discovered','stale')
 → discovery.Fetcher.get(source_url)            (existing fetcher: cache, robots, politeness)
 → parse <script id="manual-data"> JSON
 → per module, per child block:
     chunk text = block <p> text + its images' caption/caption2   (torque is IN the text)
     anchor / parent_anchor / source_url as in §3
 → Voyage-embed chunk texts (batched by token budget, as today)
 → insert document + chunks (one transaction per publication)
 → set publications.status = 'ingested'
```

The PDF ingestion pipeline is refactored to write `documents` + `chunks` instead of
`pdfs`/`pages` — same rasterize/embed steps, new write target.

Parsing (`manual-data` JSON → domain chunk objects) is **pure** — no network or DB — and
unit-tests against the saved fixture, matching the discovery-context layering rule.

## 5. Query flow (unified)

```
POST /v1/query
 → Voyage embeds the question
 → hybrid search over chunks (tsvector + pgvector, RRF) → top-k chunks (PDF + HTML mixed)
 → per candidate:
     pdf  → fetch page PNG from R2 → Claude vision
     html → reconstruct parent module text from sibling chunks → Claude text
 → same structured-output schema → best answer wins
 → response carries the winner's deep link (+ screenshot URL iff pdf)
```

## 6. API response (breaking change — approved)

Removed: `source_page_no`, `source_page_png_url`, `pdf_deep_link`.

Added:

| field | type | meaning |
|---|---|---|
| `source_type` | `'pdf' \| 'html'` | where the answer came from |
| `source_url` | str | deep link — `…#page=N` (pdf) or `…#<hash>` (html) |
| `screenshot_url` | str \| null | page PNG URL; null for HTML sources |

`candidates` become generic: `{source_type, label, score, source_url, screenshot_url?}`
(`label` = `"p. 28"` for PDFs, module title for HTML).

## 7. Migration & safety gate

Order is load-bearing:

1. **Confirm** the resized 250 GB volume backs the `DATABASE_URL` we migrate against.
2. Alembic migration: create `documents`/`chunks`; copy `pdfs`→`documents`,
   `pages`→`chunks` (set-based `INSERT … SELECT`, no row rewrite tricks needed — but heed
   the timestamptz lesson: `SET LOCAL timezone='UTC'`, no `AT TIME ZONE` USING clauses).
   Build the HNSW index after the copy, not before.
3. **Run the `thoughts.md` eval suite** against the new model — page 28 → "40 N·m
   (354 in-lb)", page 51 → "T25 5.5 N·m (49 in-lb)" — through the deployed query path.
4. Only after the eval is green: a **separate** migration drops `pdfs`/`pages`. The drop is
   its own revision so a red eval leaves both schemas intact and rollback is trivial.

## 8. Testing

- **Unit:** `manual-data` parser against the saved Red AXS fixture — module/block walk,
  caption folding, anchor/parent_anchor assignment, `toolList` handling. Chunk-text
  assembly is pure functions.
- **Integration (real Postgres):** documents/chunks insert + module reconstruction query;
  hybrid search returns mixed-source candidates; migration copy preserves row counts and
  embeddings (`pages` count == pdf-chunk count).
- **Eval (gate):** existing `thoughts.md` ground-truth suite must pass through
  `documents`/`chunks` before the drop migration runs. New eval cases: torque/tool queries
  answerable only from the Red AXS HTML publications return the correct value **and** a
  working `docs.sram.com…#hash` deep link.

## 9. Out of scope (explicit)

- Full-corpus HTML ingest (all ~1.4k models' publications) — registry + same code, later.
- Rerankers / retrieval-quality work beyond existing hybrid RRF.
- Surfacing Contentful image URLs in responses.
- Scheduled re-crawl / re-ingest on `stale` (the status flip exists; the cron does not).

## 10. Rollout

1. Migration revisions (create+copy, then gated drop) land with the code PR.
2. Deploy → `scripts/start.sh` runs `alembic upgrade` to the create+copy revision.
3. Run eval against production → green → upgrade to the drop revision.
4. `uv run parts-lookup ingest-html` (new CLI verb) for the 3 Red AXS publications.
5. Verify an HTML-sourced answer end-to-end via `POST /v1/query`.
