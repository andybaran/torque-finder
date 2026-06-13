# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

Implemented and deployed. The full `parts_lookup` package, alembic migrations, tests, and a live Railway deployment all exist. A functional test instance runs at **https://parts-api-production.up.railway.app** (`/docs`, `/healthz`, `POST /v1/query`), backed by Railway Postgres (pgvector), Cloudflare R2 (bucket `manuals`), and live Anthropic + Voyage APIs. Validated end-to-end against the `thoughts.md` ground truth (e.g. page 28 → "40 N-m (354 in-lb)", page 51 → "T25 5.5 N-m (49 in-lb)"). Stack decisions are complete (see below); any *new* infrastructure still goes through the three-alternatives interview.

## What we're building

An **API** that lets bicycle-shop mechanics look up component specifications (e.g., tool size + torque values) from manufacturer PDFs using **natural-language** queries. A separate frontend will be built later by the user.

Each API response must include:
- The answer (e.g., tool size, torque spec)
- A **citation of the source document** (always), with a **screenshot of the PDF region** and a **page deep-link (`#page=N`)** when the answer is pinned to a specific PDF page (the common case). When no exact page can be honestly pinned, the response falls back to an **interim best-effort document-level citation** (the original PDF, no `#page`, no screenshot) per #49. Returning the exact answer page reliably is a tracked later feature (Refs #49, #48): #48 measured the bottleneck as within-document page localization, and #49 turned retrieval document-level (leading-doc page coverage) as the first step toward it.

The ground-truth examples in `thoughts.md` (pages 27, 28, 31, 50, 51 of `pdf.pdf`) are the canonical eval set — new retrieval/extraction work should be validated against them before anything else.

Language: **Python**. Anthropic models are available for use; other infrastructure (vector DBs, OCR, etc.) can be acquired.

## Collaboration rules (from thoughts.md)

These are hard constraints set by the user — treat them as standing instructions, not suggestions:

1. **Three-alternatives rule.** Before adopting any new piece of infrastructure (vector DB, OCR tool, PDF parser, embedding model, framework, hosting, etc.), present **at least 3 alternatives** and interview the user on pros/cons before a decision is made. Do not silently pick one.
2. **Fortune-100-grade practices.** Apply domain-driven design and patterns expected in enterprise software (clear bounded contexts, layering, typed interfaces, observability, testability) — even at small scale. Document the reasoning behind architectural choices.
3. **API-first.** No frontend work in this repo. The frontend lives in the sibling repo `torque-finder-web`; this repo stays a clean HTTP API surface that the web client calls cross-origin (see the CORS allowlist under Decisions made). Keep that boundary — no frontend code here.

## Architecture notes

### Decisions made

- **Deployment target:** Railway (cloud PaaS). Runtime stays CPU-light; Claude handles heavy lifting via API.
  - **Build:** a repo-root `Dockerfile` on `ghcr.io/astral-sh/uv:python3.14-bookworm-slim`. Nixpacks cannot provide Python 3.14, so `railway.toml` sets `builder = "DOCKERFILE"`. The runtime image installs only the default deps (`uv sync --no-dev --frozen`) — the `ingestion` extra (docling/PyTorch) is excluded, keeping it off the request path.
  - **Startup:** `scripts/start.sh` runs `alembic upgrade head` then boots uvicorn. It lives in a script (not the `railway.toml` `startCommand`) because Railway does **not** POSIX-expand `${PORT:-8080}` in `startCommand` — it would pass the literal string to uvicorn. The container uses `uv run --no-sync` so it never re-syncs deps on boot.
  - **Secrets:** local `.env` (gitignored) for the offline ingest job; Railway's Variables panel for the cloud service. The cloud `DATABASE_URL` uses the internal Postgres host; local ingest uses the public proxy URL (`*.proxy.rlwy.net`, `?ssl=require`).
- **Expected scale:** ~1,000 PDFs, ~10 mechanic queries/day initially.
- **Project tooling:** **uv** (Astral). One tool for Python-version management, virtualenvs, dependency install, lockfile, and script running. Uses `pyproject.toml` + `uv.lock`. Common commands: `uv sync` (install deps), `uv add <pkg>`, `uv run <cmd>`.
- **API framework:** **FastAPI** + **Pydantic v2**. Async handlers so a single worker can serve concurrent Claude calls without blocking. Typed request/response models are the API contract — OpenAPI docs served at `/docs` for the future frontend.
- **CORS:** Starlette `CORSMiddleware` with an env-driven allowlist (`CORS_ALLOW_ORIGINS`, comma-separated; empty default = no cross-origin access). Lets the sibling `torque-finder-web` frontend call the API from the browser. `allow_credentials=False` for now (auth is out of scope; flip to `True` with an explicit non-`*` origin list once cookie auth lands).
- **PDF processing:** **docling** (IBM Research, MIT) for text extraction, layout detection, and table-structure recognition at ingest time. **pypdfium2** (Apache-2.0) for rendering pages to PNG for screenshots and Claude vision input. docling is an **ingestion-only** dependency — runtime container on Railway does not import it. Ingestion runs offline (on your machine or a batch job), so docling's PyTorch/ML weight does not bloat the runtime.
- **Indexing / search stack:** **Postgres with `pgvector`** on Railway's managed Postgres. Hybrid retrieval: Postgres full-text search (tsvector + BM25-ish ranking) combined with cosine-similarity vector search via pgvector, merged via reciprocal-rank fusion. Single store for page metadata, structured extracted specs, text index, and embeddings. Python access via SQLAlchemy 2.0 + asyncpg. Unified `documents`/`chunks` store (PDF pages and HTML blocks); legacy `pdfs`/`pages` dropped by gated migration 0005.
- **Embedding model:** **Voyage AI `voyage-3`** (1024 dims) via REST API, used at both ingest and query time. Cloud-API choice is deliberate: keeps PyTorch out of the Railway runtime container. Cost is trivial at this scale (~$3 one-time ingest, <$0.02/month steady state). `voyage-3-large` is the upgrade path if retrieval quality falls short on the ground-truth eval from `thoughts.md`.
- **Object storage:** **Cloudflare R2** (S3-compatible, $0 egress). Stores original PDFs and rendered page PNGs. Accessed via `boto3` or `aiobotocore` with S3-compatible endpoint. Decouples durable file storage from the Railway compute container — Railway can redeploy without touching files. Served via custom domain or signed URLs to the frontend.
- **Observability:** three-piece stack. **structlog** for structured JSON application logs (→ Railway stdout). **OpenTelemetry** with auto-instrumentation for FastAPI, HTTPX, and SQLAlchemy, plus manual spans around retrieval and Claude calls, exported to **Grafana Cloud** (user's existing account). **Sentry** free tier for unhandled-exception capture.
- **Retrieval architecture:** **Option C — page-level hybrid.** Index at the page level (keyword and/or embeddings over per-page text). Retrieval returns top-k candidate pages (k=3 initial). Send those pages **as rendered images** to Claude, which reads them with vision and returns a structured answer + the source page. Chosen because:
  - Bike-manual answers live in diagrams and torque tables — text-chunk RAG (Option B) would mangle these.
  - Direct full-PDF vision (Option A) doesn't scale past a handful of docs.
  - The screenshot + deep-link requirement falls out naturally: the retrieval unit *is* the page, which is already rendered as an image and trivially maps to `#page=N`.
- **Within-doc page expansion (#30):** the "right manual, wrong page" failure — the correct manual is retrieved but the answer-bearing page lands just outside the candidate set. After RRF + the title rerank (#29), `hybrid_search` expands **only the single top-RRF document** (the doc owning the rank-1 fused chunk; tiebreak: higher score, then lower `chunk_id`), anchored on its highest-fused page, adding `±retrieval_page_window` (W=1) neighbor pages (`ordinal ± W`; PDF only — HTML uses small-to-big module reconstruction). This **decouples two knobs**: `retrieval_top_k` is now *document-selection breadth* (which fused pages seed the base set), while `retrieval_max_candidates` (default 6) is the *hard page budget* sent to Claude vision. Base fused-winner pages are never reordered or evicted; neighbors are appended closest-to-anchor-first and dropped farthest-first when over budget — keeping cost bounded (≤ 6 vision pages/query) and the `source_index` 1:1 contract (`query.py` strict-`zip` + `retrieved[source_index-1]`) stable. A deterministic, bounded within-doc rerank (torque-regex + fastener-token boost) breaks near-ties inside the leader doc only; a model-based reranker decision is **deferred to #28** (no second reranker introduced here).
- **Budget target:** ~$10/month Claude (Sonnet 4.6) + ~$15/month Railway at the 10 queries/day volume.

### Bounded contexts

1. **Ingestion** — accept a PDF, extract per-page text, render per-page images. Batch/slow; must not block queries.
2. **Indexing** — build the page-level searchable index from ingested docs. Swappable strategy (BM25, vector, hybrid).
3. **Retrieval** — given an NL query, return top-k candidate pages with scores. The "recall" layer.
4. **Answer extraction** — send candidate page images + query to Claude, parse structured answer. LLM cost and caching live here.
5. **API** — thin HTTP adapter. Validates input, composes response (answer + screenshot URL + PDF deep link).
6. **PDF asset service** (cross-cutting) — serves rendered page images and original PDFs with `#page=N` links.

### Query flow

`API receives query → Retrieval returns top-k pages → Extraction sends those page images to Claude → API composes response with answer + screenshot URL + PDF deep link.`

### Decisions still open

None at the moment. Stack decisions are complete. Future infrastructure choices (e.g., reranker model, background-job runner, admin UI) must still go through the three-alternatives interview process.

## Project layout

(Repo dir is `torque-finder/`; the Python package is `parts_lookup`.)

```
torque-finder/
├── pyproject.toml              # uv + project metadata
├── uv.lock
├── Dockerfile                  # Railway runtime image (uv + Python 3.14)
├── .dockerignore
├── railway.toml                # root copy so `railway up` auto-detects it
├── scripts/start.sh            # container entrypoint: alembic + uvicorn
├── scripts/ingest-pass1.sh     # bulk ingest, non-catalog manuals
├── scripts/ingest-pass2.sh     # bulk ingest, large spare-parts catalogs
├── .env.example                # ANTHROPIC_API_KEY, VOYAGE_API_KEY, R2_*, DATABASE_URL, OTEL_*, SENTRY_DSN
├── CLAUDE.md
├── README.md
├── src/parts_lookup/
│   ├── api/                    # FastAPI app — thin HTTP adapter (bounded context: API)
│   │   ├── main.py             # app factory, middleware wiring, OTel init
│   │   ├── routes/
│   │   │   ├── query.py        # POST /v1/query
│   │   │   └── health.py
│   │   └── schemas.py          # Pydantic request/response models
│   ├── ingestion/              # bounded context: Ingestion
│   │   ├── docling_parser.py   # PDF → structured per-page text + tables
│   │   ├── rasterizer.py       # pypdfium2 page → PNG, uploads to R2
│   │   ├── pipeline.py         # orchestrates ingestion of one PDF
│   │   ├── html_parser.py      # manual-data JSON → block chunks
│   │   ├── html_pipeline.py    # registry → fetch → embed → documents/chunks
│   │   └── cli.py              # `uv run parts-lookup ingest <path>` / `ingest-html`
│   ├── indexing/               # bounded context: Indexing
│   │   └── repository.py       # SQLAlchemy models + write APIs
│   ├── retrieval/              # bounded context: Retrieval
│   │   ├── embedder.py         # Voyage client (async)
│   │   ├── keyword.py          # tsvector query
│   │   ├── vector.py           # pgvector cosine query
│   │   └── hybrid.py           # reciprocal-rank fusion
│   ├── extraction/             # bounded context: Answer extraction
│   │   ├── claude_client.py    # Anthropic SDK wrapper, retries, caching
│   │   └── prompt.py           # system prompt + output schema
│   ├── assets/                 # cross-cutting: R2 client, signed URLs
│   │   └── r2_client.py
│   ├── domain/                 # pure business types, no I/O
│   │   ├── models.py           # Page, Spec, AnswerCandidate, Query, etc.
│   │   └── errors.py
│   ├── config.py               # pydantic-settings, env-driven config
│   └── observability.py        # structlog + OTel tracer setup
├── tests/
│   ├── unit/                   # domain, pure-function tests
│   ├── integration/            # real Postgres, mocked external APIs
│   └── eval/                   # thoughts.md ground-truth regression suite
└── infra/
    ├── railway.toml
    └── migrations/             # alembic
```

Every top-level folder under `src/parts_lookup/` is a bounded context. `domain/` holds pure types that flow across them. External dependencies (Postgres, Voyage, Claude, R2) are only touched from their owning context — no ORM calls in `api/`, no HTTP calls in `domain/`. This is the key Fortune-100-style separation that makes each context independently testable.

## Data flows

### Ingestion (one-time per PDF, runs offline or as a CLI job)

`CLI accepts PDF path → upload original to R2 → docling parses structure → pypdfium2 renders each page to PNG → upload PNGs to R2 → for each page: Voyage embeds the text → insert page row (pdf_id, page_no, text, tsvector, embedding, png_url) into Postgres.`

HTML publications: `uv run parts-lookup ingest-html` reads the `publications` registry, parses each publication's embedded manual-data JSON into block-level chunks, and writes `documents`/`chunks` (no PNGs — deep links via `#hash`).

Run it locally (it needs the `ingestion` extra) — it writes to the **same Railway Postgres + R2** the deployed API reads, via the local `.env`:

```bash
uv sync --extra ingestion
uv run parts-lookup ingest <one.pdf>        # single PDF
uv run parts-lookup ingest-dir <dir>        # every *.pdf in DIR (non-recursive)
```

`ingest-dir` commits per-PDF and continues past per-PDF failures; SHA-256 dedup makes re-runs resume safely. For the bundled ~260-PDF corpus, `scripts/ingest-pass1.sh` (non-catalog manuals) and `scripts/ingest-pass2.sh` (the large spare-parts catalogs, one at a time) wrap this. **Voyage's free tier is capped at 10K tokens/min — add a payment method before bulk ingest** (the 200M free-token allowance still applies, so it stays ~free at this scale). Embedding is batched by token budget to respect Voyage's per-request limit.

### Query (per request, on Railway)

`POST /v1/query with NL question → Voyage embeds question → Postgres hybrid search over chunks (tsvector + pgvector, RRF) → #29 title rerank + #28 product boost → #49 doc-level aggregation picks the leading document → that doc's pages (base winners + #30 neighbor expansion) are sent to Claude, capped by the page budget (`retrieval_max_candidates`, default 10) → PDF candidates: fetch page PNGs from R2 for Claude vision; HTML candidates: reconstruct the parent module text from sibling chunks for Claude text → parse structured JSON answer → API returns {answer, source_type, source_url, screenshot_url, candidates}. The citation is doc-level best-effort (#49): `source_url` carries a `#page=N` deep link + `screenshot_url` when Claude pins a PDF page, falls back to a page-less document-level PDF link (screenshot null) otherwise, and all source links are null on a #32 abstention.`

Every step is a span in the OTel trace — when something is slow or wrong, the waterfall in Grafana tells you which context owns the problem.

## Recommended development order

Build the bounded contexts from the inside out, so each one is demoable before the next depends on it:

1. **`domain/` + `config.py`** — pure types and env loading. Sets a foundation the rest imports.
2. **`indexing/` + alembic migrations** — get the Postgres schema right. Seed with a couple of hand-crafted rows so retrieval can be tested without ingestion.
3. **`retrieval/`** — hybrid search over seeded data. Evaluate against `thoughts.md` examples once ingestion is wired.
4. **`ingestion/` + `assets/`** — docling + pypdfium2 + R2 upload. Run on `pdf.pdf` first. Validate text + PNGs end up correct.
5. **`extraction/`** — Claude client with canned page images. Get the structured-output parsing rock-solid *before* connecting it to live retrieval.
6. **`api/`** — wire everything together. Thin layer; adds almost no logic of its own.
7. **`observability.py`** + OTel instrumentation — add late enough that the shape is clear, early enough to be useful for debugging the above.
8. **Eval harness** in `tests/eval/` — codify the `thoughts.md` ground truth as a pytest suite. Run on every change.

### Quality eval gate (the fitness function)

`tests/eval/` is the repeatable quality gate (issue #34): source-grounded grading + a 39-probe adversarial out-of-corpus suite, with recorded baselines (`tests/eval/baselines/`) so fixes show deltas. It is split so CI never spends money:

- **Offline ($0, the CI gate):** the deterministic grader (`grading.py` — notation + unit-conversion equivalence, provenance, abstention) and metric aggregation (`metrics.py`) plus the frozen corpora are unit-tested in `tests/unit/test_eval_grading_offline.py` + `test_eval_metrics_offline.py`. No network, no keys, no DB. The grader is deliberately **deterministic, not an LLM judge** — adding an LLM judge would be a new-infrastructure decision requiring the three-alternatives interview (CLAUDE.md rule 1).
- **Live (PAID, double-gated):** the sampled / adversarial / `thoughts.md` suites hit real Voyage + Claude (~$0.02/query, ~$2.5 a full run — once drained the Anthropic balance). They skip unless **both** the live env vars are present **and** `PARTS_EVAL_LIVE=1`. One command: `uv run python -m tests.eval.run_eval` (DRY by default; `PARTS_EVAL_LIVE=1` to actually pay). **Watch the Anthropic balance before any live eval; refreshing baselines is a separate, explicitly user-greenlit step.**

## Key external credentials (env vars)

- `ANTHROPIC_API_KEY` — Claude Sonnet 4.6
- `VOYAGE_API_KEY` — voyage-3 embeddings
- `R2_ACCOUNT_ID` / `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` / `R2_BUCKET`
- `DATABASE_URL` — Railway-provided Postgres URL (includes pgvector)
- `OTEL_EXPORTER_OTLP_ENDPOINT` / `OTEL_EXPORTER_OTLP_HEADERS` — Grafana Cloud OTLP
- `SENTRY_DSN` — Sentry free tier
