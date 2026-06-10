# parts-lookup

Natural-language lookup API over manufacturer PDFs for bicycle-shop mechanics.
Ask "what torque for the headset top-cap bolt?" and get back the answer, a
screenshot of the page it came from, and a deep-link to that page in the
original PDF.

Architecture, design rationale, and bounded-context layout live in
[`CLAUDE.md`](./CLAUDE.md). This README is just the operator-facing quickstart.

## Stack

- Python 3.14, [uv](https://docs.astral.sh/uv/) for env + deps
- FastAPI + Pydantic v2 (async)
- Postgres + [pgvector](https://github.com/pgvector/pgvector) on Railway
- SQLAlchemy 2.0 / asyncpg, alembic migrations
- Voyage AI `voyage-3` embeddings (1024 dims)
- Anthropic Claude Sonnet 4.6 (vision) for answer extraction
- Cloudflare R2 for PDFs + rendered page PNGs
- structlog + OpenTelemetry (Grafana Cloud) + Sentry

## Bounded contexts

```
src/parts_lookup/
├── api/         # FastAPI app, thin HTTP adapter
├── domain/      # Pure types (Pydantic v2), shared across contexts
├── indexing/    # Postgres ORM + Repository (writes/reads)
├── retrieval/   # Voyage embedder + hybrid keyword/vector search (RRF)
├── extraction/  # Claude vision client + prompt
├── ingestion/   # docling + pypdfium2 + R2 upload (offline CLI only)
├── assets/      # Cross-cutting R2 client
├── config.py    # pydantic-settings env loader
└── observability.py
```

## First-time setup

```bash
# 1. Install Python 3.14 + dependencies
uv sync --extra dev

# 2. Configure env
cp .env.example .env
# Fill in API keys + DATABASE_URL

# 3. Apply DB schema (creates pgvector extension + tables)
# NOTE: against a shared/production DATABASE_URL, use the pinned revision from
# scripts/start.sh instead of `head` — migration 0005 (drop of legacy tables)
# is gated and must only run via docs/runbooks/2026-06-09-0005-drop-rollout.md.
uv run alembic -c alembic.ini upgrade 0004_documents_chunks
```

## Ingest a PDF

The ingestion pipeline runs offline (it pulls in docling/PyTorch — install
the optional extra first):

```bash
uv sync --extra ingestion
uv run parts-lookup ingest ./pdf.pdf
# or
uv run parts-lookup ingest-dir ./manuals/
```

Ingestion is idempotent — re-running on the same PDF (matched by sha256) is
a no-op.

## Run the API

```bash
uv run uvicorn --factory parts_lookup.api.main:create_app --reload
```

Then visit:
- `POST http://localhost:8000/v1/query` — the lookup endpoint
- `http://localhost:8000/docs` — interactive OpenAPI docs
- `http://localhost:8000/healthz` — liveness probe

Sample request:

```bash
curl -s http://localhost:8000/v1/query \
  -H 'content-type: application/json' \
  -d '{"question": "what torque for the seatpost clamp?", "top_k": 3}' \
  | jq
```

## Tests

```bash
uv run pytest tests/unit            # fast, no network
uv run pytest tests/integration     # requires DATABASE_URL
uv run pytest -m eval               # opt-in: requires all live API keys
```

The `eval` suite encodes the ground-truth examples from `thoughts.md` and is
the canonical regression check for retrieval+extraction quality.

## Deploy

Railway picks up `infra/railway.toml`. Set the env vars from `.env.example`
in the project's Variables panel; the start command runs migrations then
boots uvicorn. The runtime container does NOT install the `ingestion` extra,
keeping PyTorch off the request path.
