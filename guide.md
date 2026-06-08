# parts-lookup — build guide

This guide lists the skills and agents to consult as you hand-type the code for parts-lookup, plus a suggested first session. Stack decisions and architecture live in `CLAUDE.md`.

## Curated skills for this project

All of these are already available via Claude Code's Skill tool. Invoke via `/<skill-name>` or ask Claude to use one. They're grouped by what phase of the work they help with.

### Foundational — return to these throughout

- **`fullstack-dev-skills:python-pro`** — Python 3.11+ best practices. Type hints, async, pytest, dataclasses. Main "how do I do this idiomatically in modern Python?" reference.
- **`code-craftsmanship:clean-code`** — naming, small functions, comment discipline. Use when a module is starting to feel messy.
- **`code-craftsmanship:domain-driven-design`** — ubiquitous language, bounded contexts, aggregates. Relevant every time you add a new type to `domain/`.
- **`systems-architecture:clean-architecture`** — the dependency rule (api → application → domain). Use when deciding where a new thing belongs.

### By bounded context

- **`fullstack-dev-skills:fastapi-expert`** — async routes, dependency injection, lifespan, OpenAPI. Owns `api/`.
- **`fullstack-dev-skills:postgres-pro`** — pgvector index tuning, EXPLAIN, VACUUM. Owns `indexing/` and `retrieval/`.
- **`fullstack-dev-skills:sql-pro`** — when writing the hybrid-search query and it needs to be both fast and correct.
- **`fullstack-dev-skills:rag-architect`** — retrieval quality, chunking, reranking, RRF fusion strategy. Owns `retrieval/` design questions.
- **`claude-api`** — Anthropic SDK, prompt caching, structured output, vision inputs. Owns `extraction/`. Invoke any time the Claude client is touched.
- **`fullstack-dev-skills:api-designer`** — REST conventions, pagination, error shapes. Use once when locking the `POST /v1/query` contract.

### Quality and operations

- **`fullstack-dev-skills:test-master`** — pytest patterns, fixtures, test pyramids. Relevant from day one but especially for the eval harness.
- **`fullstack-dev-skills:monitoring-expert`** — OTel instrumentation, span design, Grafana dashboards. Use when wiring `observability.py`.
- **`fullstack-dev-skills:devops-engineer`** — Railway deploy, CI/CD, Docker/Nixpacks. Use when taking it live.
- **`fullstack-dev-skills:secure-code-guardian`** / **`VibeSec-Skill`** — API security, input validation, secrets handling. Use before exposing the API publicly.
- **`fullstack-dev-skills:database-optimizer`** — when Postgres queries get slow.
- **`fullstack-dev-skills:debugging-wizard`** — stack-trace / weird-bug investigation partner.
- **`review`** / **`fullstack-dev-skills:code-reviewer`** — self-review before committing. Cheap and catches a lot.

## How to use these productively

The goal is to type the code yourself, so the pattern is:

1. **Read + sketch** — pick a bounded context, read the relevant skill (e.g., `/rag-architect` before writing `retrieval/`).
2. **Type the code yourself** — no agent-generated implementations.
3. **Ask the skill questions as you go** — "why does pgvector need an HNSW index here?" or "how should I structure this Pydantic model?"
4. **Invoke `/review` when done** — it reviews what was written.

Claude will hold the three-alternatives interview discipline for any new infrastructure choice that comes up (reranker, background-job runner, caching layer, etc.).

## Suggested immediate next steps

A sensible first session:

1. `uv init` the project, set up `pyproject.toml` with the dependencies listed in `CLAUDE.md`.
2. Stand up Railway: create project, add managed Postgres, add a Volume if needed, set env vars.
3. Enable `pgvector` on the Postgres: `CREATE EXTENSION vector;`
4. Write the first alembic migration for the `pdfs` and `pages` tables.
5. Seed one hand-crafted `pages` row (using one page from `pdf.pdf`) so `retrieval/` can be tested before `ingestion/` exists.
