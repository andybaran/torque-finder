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
uv run alembic -c alembic.ini upgrade head
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
uv run pytest tests/unit            # fast, no network ($0 — incl. the eval grader/metrics gate)
uv run pytest tests/integration     # requires DATABASE_URL
uv run pytest -m eval               # opt-in PAID live suites — see the gate below
```

### Quality eval gate (`tests/eval/`)

The fitness function for retrieval+extraction quality. It has two layers, split
so CI never spends money:

- **Offline ($0, the CI gate):** the deterministic grader (`grading.py`) and
  metric aggregation (`metrics.py`) plus the frozen corpora
  (`probes_out_of_corpus.py`, `ground_truth_sampled.py`) are exercised by
  `tests/unit/test_eval_grading_offline.py` + `test_eval_metrics_offline.py` —
  no network, no API keys, no DB. They prove value-matching (notation +
  unit-conversion equivalence), provenance, abstention detection, and the
  metric math. Run them with the normal `uv run --extra dev pytest tests/unit`.
- **Live (PAID, double-gated):** the source-grounded sampled suite
  (`test_eval_sampled.py`), the 39-probe adversarial out-of-corpus suite
  (`test_eval_adversarial.py`), and the canonical `thoughts.md` suites
  (`test_eval_smoke.py`, `test_eval_html.py`) hit real Voyage + Claude
  (~$0.02/query, ~$2.5 a full run — a run once drained the Anthropic balance).
  They skip unless **both** the live env vars are present **and**
  `PARTS_EVAL_LIVE=1` is set.

One command prints the full metric set (pass rate, recall@k, contamination %,
hallucination %, tool-size completeness) and the deltas vs. the recorded
baselines:

```bash
uv run python -m tests.eval.run_eval          # DRY: prints baselines + est. cost, $0
set -a && . ./.env && set +a
PARTS_EVAL_LIVE=1 uv run python -m tests.eval.run_eval   # PAID live run
```

Recorded baselines live in `tests/eval/baselines/`: round-1 source-grounded
**68/121 (~56%)**, round-2 out-of-corpus hallucination **6/39 (~15%)**.
Refreshing them from a fresh live run is a separate, explicitly user-greenlit
step (paste the printed numbers into the JSON). The frozen sampled questions
are deterministic templates, so the first live run re-records
`round1_sampled.json → frozen_sampled` rather than reproducing 68/121 exactly.
Regenerate the mined corpus with `uv run python -m tests.eval.mine_specs`
(reads the live DB through the `Repository` layer; needs `DATABASE_URL`, no
paid calls).

## Deploy

Railway picks up `infra/railway.toml`. Set the env vars from `.env.example`
in the project's Variables panel; the start command runs migrations then
boots uvicorn. The runtime container does NOT install the `ingestion` extra,
keeping PyTorch off the request path.

## Health probes & alerting

Two probes (see `api/routes/health.py`):

- `GET /healthz` — **liveness**, static 200 while the process is up. Used by
  the platform to decide whether to restart the container.
- `GET /readyz` — **deep readiness**. Returns 200 when the most recent
  Anthropic (answer-service) call succeeded, 503 when it failed — a cheap,
  cached signal, so probing never spends a vision call. It can lag a
  just-started outage by one request window (acceptable for an uptime
  monitor). Point uptime monitors / load-balancer draining here, not at
  `/healthz`.

Every Anthropic call failure is classified (`extraction/claude_client.py`):
transient (429/529/conn/timeout/5xx) → HTTP **503 + `Retry-After`**;
operator-fault (billing-400 / 401 / 403 / 413) → **503** + a Sentry capture
(pages on-call); a genuine parse failure stays **502**. Each upstream failure
emits a structured log event **`extraction.upstream_failure`** carrying
`error_class`, `status_code`, `request_id`, `retry_after`, and the verbatim
`upstream_message`.

### Grafana Cloud alert (configure manually — not in this repo)

The alert rule lives in Grafana Cloud, not version control. Create a **log
alert** on the structured-log stream (Loki) keyed off the event above, e.g.:

```logql
sum(count_over_time({service="parts-lookup"}
  | json | event="extraction.upstream_failure" [5m]))
```

Suggested rule: **fire when this count is > 0 sustained over 10m** (any
operator-fault — exhausted credit, bad key — is already paged via Sentry; this
catches a sustained transient-failure rate that Sentry won't). Split by
`error_class` for a per-cause breakdown. Tune the threshold to the ~10
queries/day volume; even a single billing-class failure warrants a page.
