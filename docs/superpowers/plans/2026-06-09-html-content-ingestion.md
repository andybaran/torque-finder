# HTML Manual Content Ingestion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ingest SRAM digital-manual (HTML) content from the 3 Red AXS publications in the `publications` registry into a unified `documents`/`chunks` store, so `POST /v1/query` answers from PDF *or* HTML — returning a `docs.sram.com…#hash` deep link for HTML sources — with the legacy `pdfs`/`pages` tables migrated in and (after an eval gate) dropped.

**Architecture:** One alembic revision creates `documents` + `chunks` and copies `pdfs`/`pages` into them set-based; a *separate, gated* revision drops the old tables. A pure parser turns the publication's embedded `manual-data` JSON into block-level chunks (module headings + tool lists are their own chunks, so a module's title is always the first sibling). Retrieval (tsvector + pgvector + RRF) moves to `chunks`. Extraction branches by source type: PDF chunk → Claude vision on the page PNG; HTML chunk → Claude text on the parent module reconstructed from sibling chunks. The API response becomes source-agnostic (`source_type`/`source_url`/`screenshot_url`) — an approved breaking change.

**Tech Stack:** Python 3.14, uv, FastAPI, SQLAlchemy 2.0 async + asyncpg, pgvector, alembic, Voyage `voyage-3`, Anthropic SDK, httpx, pytest (`asyncio_mode=auto`).

Reference spec (authoritative design): `docs/superpowers/specs/2026-06-09-html-content-ingestion-design.md`. Issue: #2.

**DANGER ZONE — read before executing anything:**
- Integration/eval tests and alembic run against the **REAL Railway production Postgres** (`set -a && . ./.env && set +a` loads it). Migration 0004 is additive (create + copy). Migration 0005 (drop) is **NEVER run by this plan** — it lands as code only; Task 13 documents the manual, user-confirmed gate.
- **Production crash-loop window (Task 1 → feature deploy).** Task 1 Step 4 moves the prod `alembic_version` to `0004_documents_chunks`, but the *currently deployed* Railway image was built from `main` and only ships migrations 0001–0003; its `scripts/start.sh` runs `alembic upgrade head` under `set -e`, and alembic exits non-zero with `Can't locate revision identified by '0004_documents_chunks'` when the DB's stored revision isn't in its script directory. So **any Railway restart or redeploy of the old image between Task 1 Step 4 and the deploy of this feature branch's image fails to boot — a hard API outage.** Mitigation: do not trigger Railway redeploys/restarts during the execution window, and prefer executing Tasks 1→13 in one sitting. Recovery commands if the old image must boot anyway are documented in Task 1 Step 4.
- Integration tests must only insert-then-rollback. Never drop/truncate/delete existing rows.
- If `alembic upgrade` fails complaining about a sync driver, the `.env` `DATABASE_URL` lacks the asyncpg scheme — re-run with `DATABASE_URL="${DATABASE_URL/postgresql:\/\//postgresql+asyncpg://}"` prefixed to the command.

---

## File Structure

- Create: `infra/migrations/versions/0004_documents_chunks.py` — create + copy (Task 1).
- Modify: `src/parts_lookup/domain/models.py`, `src/parts_lookup/domain/__init__.py` — new source-agnostic types (Tasks 2, 5, 12).
- Modify: `src/parts_lookup/indexing/repository.py` — `Document`/`Chunk` ORM + repo methods (Task 3); legacy `Pdf`/`Page` removed in Task 12.
- Modify: `src/parts_lookup/retrieval/keyword.py`, `vector.py`, `hybrid.py` — search `chunks` (Task 4).
- Modify: `src/parts_lookup/extraction/prompt.py`, `claude_client.py` — source-aware candidates, `source_index` schema (Task 5).
- Modify: `src/parts_lookup/ingestion/pipeline.py`, `cli.py` — PDF pipeline writes documents/chunks; new `ingest-html` verb (Tasks 6, 9).
- Create: `src/parts_lookup/ingestion/html_parser.py` — pure manual-data JSON → chunks (Task 7).
- Create: `src/parts_lookup/ingestion/html_pipeline.py` — fetch → parse → embed → store → status flip (Task 8).
- Modify: `src/parts_lookup/discovery/registry.py` — `set_status` (Task 8).
- Modify: `src/parts_lookup/api/schemas.py`, `src/parts_lookup/api/routes/query.py`, `src/parts_lookup/api/main.py` — breaking response change (Task 10, 12).
- Create: `tests/fixtures/sram_manual_data_red_axs_trimmed.json` — captured real fixture (Task 7).
- Create: `tests/unit/test_html_parser.py`, `tests/unit/test_extraction.py`, `tests/unit/test_html_pipeline.py`, `tests/integration/test_documents_chunks.py`, `tests/eval/ground_truth_html.py`, `tests/eval/test_eval_html.py`.
- Modify: `tests/unit/test_rrf.py` — `page_id` → `chunk_id` (Task 4).
- Modify: `tests/unit/test_domain.py`, `tests/eval/test_eval_smoke.py`; Delete: `tests/integration/test_repository_smoke.py` (Task 12).
- Create: `infra/migrations/versions/0005_drop_pdfs_pages.py`; Modify: `scripts/start.sh` (pin) (Task 13).

**Canonical types (identical names/fields across all tasks):**

- `SourceType(StrEnum)`: `PDF = "pdf"`, `HTML = "html"`.
- `IndexedDocument(id: int, source_type: SourceType, title: str, source_url: str, source_ref: str, created_at: datetime)` — `source_ref` = sha256 (pdf) | pub_id (html); for pdf docs `source_url` holds the **R2 key** of the original PDF (resolved to a URL at response time).
- `HtmlChunk(ordinal: int, text: str, anchor: str | None, parent_anchor: str | None, source_url: str)` — parser output.
- `ParsedPublication(title: str, chunks: tuple[HtmlChunk, ...])`.
- `RetrievedChunk(chunk_id, document_id, source_type, document_title, document_source_url, ordinal, text, png_r2_key, anchor, parent_anchor, source_url, score, source)`.
- `Answer(text, tool_size, torque, confidence, source_index)` — `source_index` is the 1-based index of the cited `ExtractionCandidate`.
- `ExtractionCandidate(index: int, source_type: SourceType, label: str, png_bytes: bytes | None = None, text: str | None = None)`.
- Repository methods: `upsert_document`, `get_document_by_source_ref`, `insert_chunk`, `delete_chunks`, `fetch_module_text`.
- Registry method: `set_status(pub_id: str, status: str)`.

---

## Task 1: Migration 0004 — create `documents`/`chunks`, copy `pdfs`/`pages`

**Files:**
- Create: `infra/migrations/versions/0004_documents_chunks.py`

- [ ] **Step 1: Confirm the 250 GB volume (spec §7.1) — STOP if unconfirmed**

Ask the user (or check the Railway dashboard / `railway volume list`) that the Postgres behind `.env`'s `DATABASE_URL` is the instance whose volume was grown to 250 GB. Do not run the upgrade until confirmed.

- [ ] **Step 2: Write the migration**

```python
# infra/migrations/versions/0004_documents_chunks.py
"""create documents + chunks; copy pdfs/pages into them (set-based)

Revision ID: 0004_documents_chunks
Revises: 0003_timestamptz

Unified source-agnostic store (spec §3). The copy is INSERT … SELECT (no row
rewrites); the session timezone is pinned to UTC per the 0003 lesson (no
AT TIME ZONE USING clauses anywhere). The GIN/HNSW indexes are built AFTER
the bulk copy so the vector index is constructed in one pass instead of
per-row. The legacy tables are NOT touched here — dropping them is a
separate, eval-gated revision (spec §7).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import TSVECTOR

revision = "0004_documents_chunks"
down_revision = "0003_timestamptz"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL timezone TO 'UTC'")

    op.create_table(
        "documents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_type", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("source_url", sa.String(), nullable=False),
        sa.Column("source_ref", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("source_ref", name="uq_documents_source_ref"),
        sa.CheckConstraint(
            "source_type IN ('pdf', 'html')", name="ck_documents_source_type"
        ),
    )
    # NOTE: no separate ix_documents_source_ref index — the UniqueConstraint
    # above already creates a backing unique index; a second one is redundant.

    op.create_table(
        "chunks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "document_id",
            sa.Integer(),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "tsv",
            TSVECTOR(),
            sa.Computed("to_tsvector('english', text)", persisted=True),
            nullable=False,
        ),
        sa.Column("embedding", Vector(1024), nullable=False),
        sa.Column("png_r2_key", sa.String(), nullable=True),
        sa.Column("anchor", sa.String(), nullable=True),
        sa.Column("parent_anchor", sa.String(), nullable=True),
        sa.Column("source_url", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("document_id", "ordinal", name="uq_chunks_document_ordinal"),
    )

    # --- set-based copy (pdf rows only exist today) ---
    op.execute(
        """
        INSERT INTO documents (source_type, title, source_url, source_ref, created_at)
        SELECT 'pdf', f.filename, f.r2_key, f.sha256, f.created_at
        FROM pdfs AS f
        ORDER BY f.id
        """
    )
    op.execute(
        """
        INSERT INTO chunks
            (document_id, ordinal, text, embedding, png_r2_key,
             anchor, parent_anchor, source_url, created_at)
        SELECT d.id, p.page_no, p.text, p.embedding, p.png_r2_key,
               NULL, NULL, f.r2_key || '#page=' || p.page_no, p.created_at
        FROM pages AS p
        JOIN pdfs AS f ON f.id = p.pdf_id
        JOIN documents AS d ON d.source_ref = f.sha256
        """
    )

    # --- indexes AFTER the copy ---
    op.create_index("ix_chunks_document_id", "chunks", ["document_id"])
    op.create_index("ix_chunks_tsv", "chunks", ["tsv"], postgresql_using="gin")
    # Module reconstruction: WHERE document_id = ? AND parent_anchor = ? ORDER BY ordinal
    op.create_index(
        "ix_chunks_module", "chunks", ["document_id", "parent_anchor", "ordinal"]
    )
    op.create_index(
        "ix_chunks_embedding_hnsw",
        "chunks",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )


def downgrade() -> None:
    op.drop_index("ix_chunks_embedding_hnsw", table_name="chunks")
    op.drop_index("ix_chunks_module", table_name="chunks")
    op.drop_index("ix_chunks_tsv", table_name="chunks")
    op.drop_index("ix_chunks_document_id", table_name="chunks")
    op.drop_table("chunks")
    op.drop_table("documents")
```

- [ ] **Step 3: Lint + confirm unit tests still pass**

Run: `uv run --extra dev ruff check && uv run --extra dev pytest tests/unit -q`
Expected: ruff clean; all unit tests pass (no failures).

- [ ] **Step 4: Apply to production (additive only)**

```bash
set -a && . ./.env && set +a
uv run alembic -c alembic.ini upgrade 0004_documents_chunks
```

Expected: `Running upgrade 0003_timestamptz -> 0004_documents_chunks`. The HNSW build over ~9.8k rows takes seconds-to-minutes. The target is **pinned to `0004_documents_chunks`, never `head`**: when first executed `head` *is* 0004, but a re-run after Task 13 lands would silently apply the gated 0005 drop. Pinning makes re-execution safe. (Pinned upgrades to an ancestor of the DB's current revision are a clean no-op, exit 0.)

**⚠️ This step opens the production crash-loop window (see DANGER ZONE).** From this moment until the feature branch's image is deployed, the deployed `main` image cannot boot: its migrations dir tops out at `0003_timestamptz`, so its `start.sh` (`alembic upgrade head`, `set -e`) dies with `Can't locate revision identified by '0004_documents_chunks'`. Rules for the window:

1. **Do not trigger Railway redeploys or restarts** of the parts-api service until the feature PR's image (which contains 0004) is deployed.
2. **If the old image must boot anyway** (Railway restarted it on its own, or an urgent rollback is needed), stamp the DB back so its `upgrade head` is a no-op — this only rewrites the `alembic_version` row; the `documents`/`chunks` tables and all copied data stay intact:
   ```bash
   set -a && . ./.env && set +a
   uv run alembic -c alembic.ini stamp 0003_timestamptz
   ```
3. **Before resuming plan execution — and strictly before any image containing 0004 boots —** re-stamp forward (also metadata-only):
   ```bash
   uv run alembic -c alembic.ini stamp 0004_documents_chunks
   ```
   This re-stamp is mandatory, not optional: if the DB is still stamped `0003` when a 0004-aware image runs `alembic upgrade`, alembic will try to *execute* the 0004 upgrade and fail on `CREATE TABLE documents` (already exists). The stamp pair is safe to repeat; neither command touches schema or data.

- [ ] **Step 5: Verify the copy preserved every row**

```bash
set -a && . ./.env && set +a
uv run python - <<'EOF'
import asyncio, os
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

async def main():
    url = os.environ["DATABASE_URL"]
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    engine = create_async_engine(url)
    async with engine.connect() as conn:
        pdfs = (await conn.execute(text("SELECT count(*) FROM pdfs"))).scalar_one()
        docs = (await conn.execute(text("SELECT count(*) FROM documents"))).scalar_one()
        pages = (await conn.execute(text("SELECT count(*) FROM pages"))).scalar_one()
        chunks = (await conn.execute(text(
            "SELECT count(*) FROM chunks c JOIN documents d ON d.id = c.document_id "
            "WHERE d.source_type = 'pdf'"))).scalar_one()
        same = (await conn.execute(text(
            "SELECT count(*) FROM pages p "
            "JOIN pdfs f ON f.id = p.pdf_id "
            "JOIN documents d ON d.source_ref = f.sha256 "
            "JOIN chunks c ON c.document_id = d.id AND c.ordinal = p.page_no "
            "WHERE c.text = p.text AND c.embedding = p.embedding"
        ))).scalar_one()
    await engine.dispose()
    print(f"pdfs={pdfs} documents={docs} pages={pages} pdf_chunks={chunks} identical={same}")
    assert pdfs == docs, "document count mismatch"
    assert pages == chunks == same, "chunk copy mismatch"
    print("copy verified OK")

asyncio.run(main())
EOF
```

Expected: `pdfs=258 documents=258 pages=9818 pdf_chunks=9818 identical=9818` (counts per spec §1) then `copy verified OK`. If the assert fails, `uv run alembic -c alembic.ini downgrade 0003_timestamptz` and debug before proceeding.

- [ ] **Step 6: Commit**

```bash
git add infra/migrations/versions/0004_documents_chunks.py
git commit -m "feat(indexing): documents/chunks unified store + set-based copy from pdfs/pages (refs #2)" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 2: Source-agnostic domain types

**Files:**
- Modify: `src/parts_lookup/domain/models.py`
- Modify: `src/parts_lookup/domain/__init__.py`
- Test: `tests/unit/test_domain.py`

- [ ] **Step 1: Write the failing tests** (append to `tests/unit/test_domain.py`)

```python
class TestSourceAgnosticTypes:
    def test_source_type_values(self) -> None:
        from parts_lookup.domain.models import SourceType

        assert SourceType.PDF.value == "pdf"
        assert SourceType.HTML.value == "html"

    def test_indexed_document(self) -> None:
        from parts_lookup.domain.models import IndexedDocument, SourceType

        d = IndexedDocument(
            id=1,
            source_type=SourceType.HTML,
            title="Road AXS",
            source_url="https://docs.sram.com/en-US/publications/6TmfV97fHWv8kvGXVegoTy",
            source_ref="6TmfV97fHWv8kvGXVegoTy",
            created_at=datetime(2026, 6, 9, tzinfo=UTC),
        )
        assert d.source_type is SourceType.HTML

    def test_html_chunk_defaults_and_parsed_publication(self) -> None:
        from parts_lookup.domain.models import HtmlChunk, ParsedPublication

        c = HtmlChunk(
            ordinal=1,
            text="Crank arm bolts 40 N·m (354 in-lb)",
            anchor="abc123",
            parent_anchor="mod9",
            source_url="https://docs.sram.com/en-US/publications/X#abc123",
        )
        p = ParsedPublication(title="Road AXS", chunks=(c,))
        assert p.chunks[0].anchor == "abc123"

    def test_retrieved_chunk_pdf_shape(self) -> None:
        from parts_lookup.domain.models import RetrievalSource, RetrievedChunk, SourceType

        r = RetrievedChunk(
            chunk_id=10,
            document_id=2,
            source_type=SourceType.PDF,
            document_title="manual.pdf",
            document_source_url="pdfs/abc.pdf",
            ordinal=28,
            text="40 N-m (354 in-lb)",
            png_r2_key="pages/abc/0028.png",
            anchor=None,
            parent_anchor=None,
            source_url="pdfs/abc.pdf#page=28",
            score=0.03,
            source=RetrievalSource.HYBRID,
        )
        assert r.png_r2_key is not None
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run --extra dev pytest tests/unit/test_domain.py -q`
Expected: FAIL — `ImportError: cannot import name 'SourceType'`.

- [ ] **Step 3: Add the types** (append to `src/parts_lookup/domain/models.py`, after `RetrievalSource`)

```python
class SourceType(StrEnum):
    """Where indexed content came from: a manufacturer PDF or a digital (HTML) manual."""

    PDF = "pdf"
    HTML = "html"


class IndexedDocument(_Frozen):
    """A source document in the unified store (one PDF or one HTML publication).

    ``source_ref`` is the stable dedupe key: the PDF's sha256 or the
    publication's pub_id. For PDFs, ``source_url`` holds the R2 object key of
    the original file (the API resolves it to a public/presigned URL); for
    HTML it is the docs.sram.com publication URL.
    """

    id: int
    source_type: SourceType
    title: str
    source_url: str
    source_ref: str
    created_at: datetime


class HtmlChunk(_Frozen):
    """One block-level chunk parsed from a publication's manual-data JSON.

    ``anchor`` is the block's own #hash (not every block has one);
    ``parent_anchor`` is the owning module's #hash. ``source_url`` already
    resolves to the most precise hash available.
    """

    ordinal: int = Field(gt=0)
    text: str = Field(min_length=1)
    anchor: str | None = None
    parent_anchor: str | None = None
    source_url: str


class ParsedPublication(_Frozen):
    """Pure parser output for one publication: title + ordered chunks."""

    title: str
    chunks: tuple[HtmlChunk, ...]


class RetrievedChunk(_Frozen):
    """A retrieval hit from the unified chunks index. ``score`` is the fused score."""

    chunk_id: int
    document_id: int
    source_type: SourceType
    document_title: str
    document_source_url: str
    ordinal: int
    text: str
    png_r2_key: str | None
    anchor: str | None
    parent_anchor: str | None
    source_url: str
    score: float
    source: RetrievalSource
```

Then update `src/parts_lookup/domain/__init__.py` to export the new names (keep the existing exports for now — legacy types are removed in Task 12):

```python
"""Pure domain types and errors. No I/O, no framework imports."""

from parts_lookup.domain.errors import (
    ExtractionError,
    IngestionError,
    PartsLookupError,
    PdfNotFoundError,
    RetrievalError,
)
from parts_lookup.domain.models import (
    Answer,
    HtmlChunk,
    IndexedDocument,
    PageContent,
    ParsedPublication,
    PdfDocument,
    Query,
    RetrievalSource,
    RetrievedChunk,
    RetrievedPage,
    SourceType,
)

__all__ = [
    "Answer",
    "ExtractionError",
    "HtmlChunk",
    "IndexedDocument",
    "IngestionError",
    "PageContent",
    "ParsedPublication",
    "PartsLookupError",
    "PdfDocument",
    "PdfNotFoundError",
    "Query",
    "RetrievalError",
    "RetrievalSource",
    "RetrievedChunk",
    "RetrievedPage",
    "SourceType",
]
```

- [ ] **Step 4: Run tests + lint**

Run: `uv run --extra dev pytest tests/unit/test_domain.py -q && uv run --extra dev ruff check`
Expected: PASS, ruff clean.

- [ ] **Step 5: Commit**

```bash
git add src/parts_lookup/domain/models.py src/parts_lookup/domain/__init__.py tests/unit/test_domain.py
git commit -m "feat(domain): SourceType, IndexedDocument, HtmlChunk, RetrievedChunk types (refs #2)" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 3: Repository — `Document`/`Chunk` ORM + write/read methods

**Files:**
- Modify: `src/parts_lookup/indexing/repository.py`
- Test: `tests/integration/test_documents_chunks.py`

- [ ] **Step 1: Write the failing integration test**

```python
# tests/integration/test_documents_chunks.py
"""documents/chunks repository smoke vs the live Postgres (insert + rollback only).

NEVER commits: every test inserts inside a transaction and rolls back, so the
production database is untouched.
"""

from __future__ import annotations

import os
import uuid

import pytest

if not os.environ.get("DATABASE_URL"):
    pytest.skip("DATABASE_URL not set; skipping documents/chunks tests", allow_module_level=True)

pytestmark = pytest.mark.asyncio

_STUB_VEC = [0.0] * 1023 + [1.0]


def _engine():  # type: ignore[no-untyped-def]
    from sqlalchemy.ext.asyncio import create_async_engine

    url = os.environ["DATABASE_URL"]
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return create_async_engine(url, echo=False)


async def test_document_chunk_round_trip_and_module_reconstruction() -> None:
    from sqlalchemy.ext.asyncio import AsyncSession

    from parts_lookup.domain.models import SourceType
    from parts_lookup.indexing.repository import Repository

    engine = _engine()
    ref = f"test-{uuid.uuid4().hex}"
    try:
        async with AsyncSession(engine) as session:
            repo = Repository(session)
            doc = await repo.upsert_document(
                source_type=SourceType.HTML,
                title="Test Pub",
                source_url=f"https://docs.sram.com/en-US/publications/{ref}",
                source_ref=ref,
            )
            assert doc.source_type is SourceType.HTML

            # Same source_ref upserts to the same row (re-ingest path).
            again = await repo.upsert_document(
                source_type=SourceType.HTML,
                title="Test Pub v2",
                source_url=doc.source_url,
                source_ref=ref,
            )
            assert again.id == doc.id
            assert again.title == "Test Pub v2"

            base = doc.source_url
            rows = [
                (1, "Crank Installation", "mod1", "mod1"),
                (2, "Tools: TORX: T25", None, "mod1"),
                (3, "Tighten to 40 N·m (354 in-lb)", "blk1", "mod1"),
                (4, "Unrelated module heading", "mod2", "mod2"),
            ]
            for ordinal, text, anchor, parent in rows:
                await repo.insert_chunk(
                    document_id=doc.id,
                    ordinal=ordinal,
                    text=text,
                    embedding=_STUB_VEC,
                    png_r2_key=None,
                    anchor=anchor,
                    parent_anchor=parent,
                    source_url=f"{base}#{anchor or parent}",
                )

            module_text = await repo.fetch_module_text(doc.id, "mod1")
            assert module_text == (
                "Crank Installation\n\nTools: TORX: T25\n\nTighten to 40 N·m (354 in-lb)"
            )

            await repo.delete_chunks(doc.id)
            assert await repo.fetch_module_text(doc.id, "mod1") == ""

            await session.rollback()
    finally:
        await engine.dispose()


async def test_migration_copy_preserved_pages() -> None:
    """Read-only check that 0004's copy kept every page (skips once 0005 dropped pages)."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession

    engine = _engine()
    try:
        async with AsyncSession(engine) as session:
            exists = (
                await session.execute(text("SELECT to_regclass('public.pages')"))
            ).scalar_one()
            if exists is None:
                pytest.skip("legacy pages table already dropped (post-0005)")
            pages = (await session.execute(text("SELECT count(*) FROM pages"))).scalar_one()
            chunks = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM chunks c "
                        "JOIN documents d ON d.id = c.document_id "
                        "WHERE d.source_type = 'pdf'"
                    )
                )
            ).scalar_one()
            assert pages == chunks
    finally:
        await engine.dispose()
```

- [ ] **Step 2: Run to verify failure**

Run: `set -a && . ./.env && set +a && uv run --extra dev pytest tests/integration/test_documents_chunks.py -q`
Expected: FAIL — `AttributeError: 'Repository' object has no attribute 'upsert_document'`.

- [ ] **Step 3: Add ORM models + methods to `src/parts_lookup/indexing/repository.py`**

Add to the imports:

```python
from sqlalchemy import (
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    UniqueConstraint,
    delete,
    func,
    select,
)
```

and extend the domain import line:

```python
from parts_lookup.domain.models import (
    IndexedDocument,
    PageContent,
    PdfDocument,
    SourceType,
)
```

Add after the `Page` class (keep `Pdf`/`Page` untouched — removed in Task 12):

```python
class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_type: Mapped[str] = mapped_column(nullable=False)
    title: Mapped[str] = mapped_column(nullable=False)
    source_url: Mapped[str] = mapped_column(nullable=False)
    source_ref: Mapped[str] = mapped_column(unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    chunks: Mapped[list[Chunk]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        CheckConstraint("source_type IN ('pdf', 'html')", name="ck_documents_source_type"),
    )


class Chunk(Base):
    __tablename__ = "chunks"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    ordinal: Mapped[int] = mapped_column(nullable=False)
    text: Mapped[str] = mapped_column(nullable=False)
    # Generated column maintained by Postgres; never written from Python.
    tsv: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english', text)", persisted=True),
        nullable=False,
    )
    embedding: Mapped[list[float]] = mapped_column(Vector(1024), nullable=False)
    png_r2_key: Mapped[str | None] = mapped_column(nullable=True)
    anchor: Mapped[str | None] = mapped_column(nullable=True)
    parent_anchor: Mapped[str | None] = mapped_column(nullable=True)
    source_url: Mapped[str] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    document: Mapped[Document] = relationship(back_populates="chunks")

    __table_args__ = (
        UniqueConstraint("document_id", "ordinal", name="uq_chunks_document_ordinal"),
        Index("ix_chunks_tsv", "tsv", postgresql_using="gin"),
        Index("ix_chunks_module", "document_id", "parent_anchor", "ordinal"),
        Index(
            "ix_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )


def _document_row_to_domain(row: Document) -> IndexedDocument:
    return IndexedDocument(
        id=row.id,
        source_type=SourceType(row.source_type),
        title=row.title,
        source_url=row.source_url,
        source_ref=row.source_ref,
        created_at=row.created_at,
    )
```

Add these methods to the `Repository` class:

```python
    async def upsert_document(
        self,
        *,
        source_type: SourceType,
        title: str,
        source_url: str,
        source_ref: str,
    ) -> IndexedDocument:
        """Insert a document, or refresh title/source_url if source_ref exists.

        ``source_ref`` (sha256 for PDFs, pub_id for HTML) is the dedupe key,
        so re-ingest is idempotent at the document level.
        """
        existing = await self._get_document_orm_by_source_ref(source_ref)
        if existing is not None:
            existing.title = title
            existing.source_url = source_url
            await self._session.flush()
            return _document_row_to_domain(existing)

        row = Document(
            source_type=source_type.value,
            title=title,
            source_url=source_url,
            source_ref=source_ref,
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return _document_row_to_domain(row)

    async def get_document_by_source_ref(self, source_ref: str) -> IndexedDocument | None:
        row = await self._get_document_orm_by_source_ref(source_ref)
        return _document_row_to_domain(row) if row is not None else None

    async def insert_chunk(
        self,
        *,
        document_id: int,
        ordinal: int,
        text: str,
        embedding: Sequence[float],
        source_url: str,
        png_r2_key: str | None = None,
        anchor: str | None = None,
        parent_anchor: str | None = None,
    ) -> int:
        """Insert one chunk row; returns the new chunk id."""
        row = Chunk(
            document_id=document_id,
            ordinal=ordinal,
            text=text,
            embedding=list(embedding),
            png_r2_key=png_r2_key,
            anchor=anchor,
            parent_anchor=parent_anchor,
            source_url=source_url,
        )
        self._session.add(row)
        await self._session.flush()
        return row.id

    async def delete_chunks(self, document_id: int) -> None:
        """Drop all chunks for a document (re-ingest of a stale publication)."""
        await self._session.execute(delete(Chunk).where(Chunk.document_id == document_id))

    async def fetch_module_text(self, document_id: int, parent_anchor: str) -> str:
        """Reconstruct a module's full text from its sibling chunks, in order.

        Small-to-big extraction (spec §2.3): blocks are embedded individually,
        but Claude reads the whole owning module.
        """
        result = await self._session.execute(
            select(Chunk.text)
            .where(Chunk.document_id == document_id, Chunk.parent_anchor == parent_anchor)
            .order_by(Chunk.ordinal)
        )
        return "\n\n".join(result.scalars().all())

    async def _get_document_orm_by_source_ref(self, source_ref: str) -> Document | None:
        result = await self._session.execute(
            select(Document).where(Document.source_ref == source_ref)
        )
        return result.scalar_one_or_none()
```

Also update the module docstring's first line to mention the unified store:

```python
"""SQLAlchemy ORM models + async write/read repository for the indexing context.

All DB I/O for the indexing bounded context lives here. The unified store is
``documents`` + ``chunks`` (PDF pages and HTML blocks alike); the legacy
``pdfs``/``pages`` models remain only until the gated drop migration lands.
Other contexts call ``Repository`` methods and receive pure domain objects —
ORM rows never escape.
"""
```

- [ ] **Step 4: Run tests + lint**

Run: `set -a && . ./.env && set +a && uv run --extra dev pytest tests/integration/test_documents_chunks.py -q && uv run --extra dev ruff check`
Expected: 2 passed; ruff clean.

- [ ] **Step 5: Commit**

```bash
git add src/parts_lookup/indexing/repository.py tests/integration/test_documents_chunks.py
git commit -m "feat(indexing): Document/Chunk ORM + upsert/insert/module-reconstruction repo methods (refs #2)" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 4: Retrieval over `chunks` (mixed-source hybrid)

**Files:**
- Modify: `src/parts_lookup/retrieval/keyword.py`
- Modify: `src/parts_lookup/retrieval/vector.py`
- Modify: `src/parts_lookup/retrieval/hybrid.py`
- Modify: `src/parts_lookup/domain/models.py` (delete `RetrievedPage`)
- Modify: `src/parts_lookup/domain/__init__.py` (drop the export)
- Modify: `tests/unit/test_rrf.py` (`hit.page_id` → `hit.chunk_id` after the `_FusedHit` rename)
- Test: `tests/integration/test_documents_chunks.py` (add a mixed-source test)

- [ ] **Step 1: Write the failing integration test** (append to `tests/integration/test_documents_chunks.py`)

```python
async def test_hybrid_search_returns_mixed_source_chunks() -> None:
    """PDF and HTML chunks rank in one fused list (insert + rollback, stub embedder)."""
    from sqlalchemy.ext.asyncio import AsyncSession

    from parts_lookup.config import Settings
    from parts_lookup.domain.models import SourceType
    from parts_lookup.indexing.repository import Repository
    from parts_lookup.retrieval.embedder import VoyageEmbedder
    from parts_lookup.retrieval.hybrid import hybrid_search

    settings = Settings(
        database_url=os.environ["DATABASE_URL"],
        stub_external_apis=True,
        _env_file=None,
    )
    embedder = VoyageEmbedder(settings)
    marker = f"zzqxv{uuid.uuid4().hex[:8]}"  # token unique to our rows

    engine = _engine()
    try:
        async with AsyncSession(engine) as session:
            repo = Repository(session)
            texts = [
                f"{marker} crank arm bolt torque 40 N-m pdf page",
                f"{marker} crank arm bolt torque 40 N-m html block",
            ]
            vecs = await embedder.embed_documents(texts)

            pdf_doc = await repo.upsert_document(
                source_type=SourceType.PDF,
                title="mixed-test.pdf",
                source_url="pdfs/mixedtest.pdf",
                source_ref=f"mixed-pdf-{uuid.uuid4().hex}",
            )
            await repo.insert_chunk(
                document_id=pdf_doc.id, ordinal=1, text=texts[0], embedding=vecs[0],
                png_r2_key="pages/mixedtest/0001.png",
                source_url="pdfs/mixedtest.pdf#page=1",
            )
            html_doc = await repo.upsert_document(
                source_type=SourceType.HTML,
                title="Mixed Test Pub",
                source_url="https://docs.sram.com/en-US/publications/mixedtest",
                source_ref=f"mixed-html-{uuid.uuid4().hex}",
            )
            await repo.insert_chunk(
                document_id=html_doc.id, ordinal=1, text=texts[1], embedding=vecs[1],
                anchor="blk1", parent_anchor="mod1",
                source_url="https://docs.sram.com/en-US/publications/mixedtest#blk1",
            )

            hits = await hybrid_search(
                session, embedder, f"{marker} crank arm bolt torque", top_k=10
            )
            ours = [h for h in hits if marker in h.text]
            assert {h.source_type for h in ours} == {SourceType.PDF, SourceType.HTML}
            html_hit = next(h for h in ours if h.source_type is SourceType.HTML)
            assert html_hit.parent_anchor == "mod1"
            assert html_hit.source_url.endswith("#blk1")
            assert html_hit.png_r2_key is None

            await session.rollback()
    finally:
        await engine.dispose()
```

- [ ] **Step 2: Run to verify failure**

Run: `set -a && . ./.env && set +a && uv run --extra dev pytest tests/integration/test_documents_chunks.py::test_hybrid_search_returns_mixed_source_chunks -q`
Expected: FAIL — keyword SQL still queries `pages` (our rows aren't there), so `ours` is empty / `RetrievedPage` shape mismatch.

- [ ] **Step 3: Swap the channel SQL to `chunks`**

Replace the SQL in `src/parts_lookup/retrieval/keyword.py` (docstring: change "pages.tsvector" to "chunks.tsv"):

```python
_KEYWORD_SQL = text(
    """
    SELECT
        c.id AS chunk_id,
        ts_rank_cd(c.tsv, plainto_tsquery('english', :q)) AS score
    FROM chunks AS c
    WHERE c.tsv @@ plainto_tsquery('english', :q)
    ORDER BY score DESC
    LIMIT :top_k
    """
)
```

and in the function body return `[(int(row.chunk_id), float(row.score)) for row in result]` (update the docstring to say ``(chunk_id, score)``).

Replace the SQL in `src/parts_lookup/retrieval/vector.py` (docstring: "chunks.embedding"):

```python
_VECTOR_SQL = text(
    """
    SELECT
        c.id AS chunk_id,
        1 - (c.embedding <=> :embedding) AS score
    FROM chunks AS c
    ORDER BY c.embedding <=> :embedding
    LIMIT :top_k
    """
).bindparams(bindparam("embedding", type_=Vector()))
```

and return `[(int(row.chunk_id), float(row.score)) for row in result]`.

- [ ] **Step 4: Rewrite `src/parts_lookup/retrieval/hybrid.py` hydration**

Keep `RRF_K`, `DEFAULT_TOP_K_PER_CHANNEL`, `_reciprocal_rank_fusion` (rename the field `page_id` → `chunk_id` in `_FusedHit`), and `RetrievalService` exactly as they are apart from types. Full replacement for the load + search section:

```python
from parts_lookup.domain.models import (
    Query,
    RetrievalSource,
    RetrievedChunk,
    SourceType,
)

_LOAD_CHUNKS_SQL = text(
    """
    SELECT
        c.id            AS chunk_id,
        c.document_id   AS document_id,
        c.ordinal       AS ordinal,
        c.text          AS text,
        c.png_r2_key    AS png_r2_key,
        c.anchor        AS anchor,
        c.parent_anchor AS parent_anchor,
        c.source_url    AS source_url,
        d.source_type   AS source_type,
        d.title         AS document_title,
        d.source_url    AS document_source_url
    FROM chunks    AS c
    JOIN documents AS d ON d.id = c.document_id
    WHERE c.id IN :chunk_ids
    """
).bindparams(bindparam("chunk_ids", expanding=True))


@dataclass(frozen=True)
class _FusedHit:
    chunk_id: int
    score: float


def _reciprocal_rank_fusion(
    *channels: list[tuple[int, float]],
    k: int = RRF_K,
) -> list[_FusedHit]:
    """Fuse multiple ranked lists into one. Higher fused score = better."""
    fused: dict[int, float] = {}
    for channel in channels:
        for rank, (chunk_id, _raw_score) in enumerate(channel, start=1):
            fused[chunk_id] = fused.get(chunk_id, 0.0) + 1.0 / (k + rank)
    return sorted(
        (_FusedHit(chunk_id=cid, score=score) for cid, score in fused.items()),
        key=lambda h: h.score,
        reverse=True,
    )


async def hybrid_search(
    session: AsyncSession,
    embedder: VoyageEmbedder,
    query_text: str,
    top_k: int,
    *,
    per_channel_k: int = DEFAULT_TOP_K_PER_CHANNEL,
) -> list[RetrievedChunk]:
    """Run keyword + vector sequentially on the shared session, fuse with RRF, hydrate winners.

    (Sequential on purpose: AsyncSession owns a single connection — see the
    original comment; unchanged behaviour, new unified-chunk shape.)
    """
    query_embedding = await embedder.embed_query(query_text)

    try:
        keyword_hits = await keyword_search(session, query_text, per_channel_k)
        vector_hits = await vector_search(session, query_embedding, per_channel_k)
    except RetrievalError:
        raise
    except Exception as exc:
        raise RetrievalError("Hybrid retrieval channel failed") from exc

    fused = _reciprocal_rank_fusion(keyword_hits, vector_hits)[:top_k]
    if not fused:
        return []

    try:
        result = await session.execute(
            _LOAD_CHUNKS_SQL, {"chunk_ids": [hit.chunk_id for hit in fused]}
        )
    except Exception as exc:
        raise RetrievalError("Failed to load fused chunk rows") from exc

    rows = {int(row.chunk_id): row for row in result}

    retrieved: list[RetrievedChunk] = []
    for hit in fused:
        row = rows.get(hit.chunk_id)
        if row is None:
            continue
        retrieved.append(
            RetrievedChunk(
                chunk_id=int(row.chunk_id),
                document_id=int(row.document_id),
                source_type=SourceType(row.source_type),
                document_title=str(row.document_title),
                document_source_url=str(row.document_source_url),
                ordinal=int(row.ordinal),
                text=str(row.text),
                png_r2_key=None if row.png_r2_key is None else str(row.png_r2_key),
                anchor=None if row.anchor is None else str(row.anchor),
                parent_anchor=None if row.parent_anchor is None else str(row.parent_anchor),
                source_url=str(row.source_url),
                score=hit.score,
                source=RetrievalSource.HYBRID,
            )
        )
    return retrieved
```

Delete the old `_LOAD_PAGES_SQL` / `_load_pages`. `RetrievalService.search` now returns `list[RetrievedChunk]` — update its annotation. Then delete the `RetrievedPage` class from `src/parts_lookup/domain/models.py` and remove `RetrievedPage` from `src/parts_lookup/domain/__init__.py` (both the import and `__all__`).

- [ ] **Step 5: Update `tests/unit/test_rrf.py` for the `_FusedHit` rename**

The existing test reads `hit.page_id` (line 34); after the `page_id` → `chunk_id` rename it raises `AttributeError`. Replace the test function (only the comprehension changes — the fusion math and expected ordering are untouched):

```python
def test_rrf_page_in_both_channels_wins() -> None:
    rrf = _import_rrf()

    keyword: list[tuple[int, float]] = [(1, 9.5), (2, 4.2)]
    vector: list[tuple[int, float]] = [(2, 0.91), (3, 0.83)]

    fused = rrf(keyword, vector)
    fused_ids = [hit.chunk_id for hit in fused]

    assert fused_ids == [2, 1, 3]
```

- [ ] **Step 6: Run tests + lint**

Run: `set -a && . ./.env && set +a && uv run --extra dev pytest tests/integration/test_documents_chunks.py tests/unit -q && uv run --extra dev ruff check`
Expected: integration 3 passed; unit suite passes (`test_rrf.py` passes with the Step 5 `chunk_id` update — fusion math unchanged); ruff clean. (`api/routes/query.py` still compiles; it is rewritten in Task 10.)

- [ ] **Step 7: Commit**

```bash
git add src/parts_lookup/retrieval src/parts_lookup/domain tests/unit/test_rrf.py tests/integration/test_documents_chunks.py
git commit -m "feat(retrieval): hybrid search over unified chunks; RetrievedChunk hits (refs #2)" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 5: Extraction branches by source type (`source_index` contract)

**Files:**
- Modify: `src/parts_lookup/domain/models.py` (rewrite `Answer`)
- Modify: `src/parts_lookup/extraction/prompt.py`
- Modify: `src/parts_lookup/extraction/claude_client.py`
- Modify: `tests/unit/test_domain.py` (Answer kwargs)
- Test: `tests/unit/test_extraction.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_extraction.py
"""Pure tests for extraction block building + response parsing (no network)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from parts_lookup.domain.errors import ExtractionError
from parts_lookup.domain.models import SourceType
from parts_lookup.extraction.claude_client import ClaudeExtractor, ExtractionCandidate


def _pdf_candidate(index: int = 1) -> ExtractionCandidate:
    return ExtractionCandidate(
        index=index,
        source_type=SourceType.PDF,
        label="p. 28 of manual.pdf",
        png_bytes=b"\x89PNG fake",
    )


def _html_candidate(index: int = 2) -> ExtractionCandidate:
    return ExtractionCandidate(
        index=index,
        source_type=SourceType.HTML,
        label="Crank Installation",
        text="Crank Installation\n\nTighten to 40 N·m (354 in-lb)",
    )


def _response(payload: dict) -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=json.dumps(payload))]
    )


class TestBuildUserBlocks:
    def test_pdf_candidate_becomes_image_plus_marker(self) -> None:
        blocks = ClaudeExtractor._build_user_blocks("q?", [_pdf_candidate()])
        assert blocks[0]["type"] == "image"
        assert blocks[1]["type"] == "text"
        assert "source 1" in blocks[1]["text"]
        assert "p. 28 of manual.pdf" in blocks[1]["text"]

    def test_html_candidate_becomes_text_block(self) -> None:
        blocks = ClaudeExtractor._build_user_blocks("q?", [_html_candidate()])
        assert blocks[0]["type"] == "text"
        assert "Source 2: Crank Installation" in blocks[0]["text"]
        assert "40 N·m (354 in-lb)" in blocks[0]["text"]

    def test_pdf_candidate_without_png_rejected(self) -> None:
        bad = ExtractionCandidate(
            index=1, source_type=SourceType.PDF, label="p. 1", png_bytes=None
        )
        with pytest.raises(ExtractionError):
            ClaudeExtractor._build_user_blocks("q?", [bad])

    def test_html_candidate_without_text_rejected(self) -> None:
        bad = ExtractionCandidate(
            index=1, source_type=SourceType.HTML, label="x", text=None
        )
        with pytest.raises(ExtractionError):
            ClaudeExtractor._build_user_blocks("q?", [bad])


class TestParseResponse:
    _CANDIDATES = [_pdf_candidate(1), _html_candidate(2)]

    def test_resolves_cited_source_index(self) -> None:
        answer = ClaudeExtractor._parse_response(
            _response(
                {
                    "answer": "40 N·m (354 in-lb)",
                    "tool_size": None,
                    "torque": "40 N·m (354 in-lb)",
                    "source_index": 2,
                    "confidence": 0.95,
                }
            ),
            self._CANDIDATES,
        )
        assert answer.source_index == 2
        assert answer.torque == "40 N·m (354 in-lb)"

    def test_null_source_index_falls_back_to_top_candidate(self) -> None:
        answer = ClaudeExtractor._parse_response(
            _response(
                {
                    "answer": "not found",
                    "tool_size": None,
                    "torque": None,
                    "source_index": None,
                    "confidence": 0.1,
                }
            ),
            self._CANDIDATES,
        )
        assert answer.source_index == 1

    def test_unknown_source_index_rejected(self) -> None:
        with pytest.raises(ExtractionError):
            ClaudeExtractor._parse_response(
                _response(
                    {
                        "answer": "x",
                        "tool_size": None,
                        "torque": None,
                        "source_index": 9,
                        "confidence": 0.9,
                    }
                ),
                self._CANDIDATES,
            )


async def test_stub_extract_cites_first_candidate() -> None:
    from parts_lookup.config import Settings

    settings = Settings(
        database_url="postgresql+asyncpg://stub/stub",
        stub_external_apis=True,
        _env_file=None,
    )
    extractor = ClaudeExtractor(settings)
    answer = await extractor.extract("q?", [_html_candidate(1), _pdf_candidate(2)])
    assert answer.source_index == 1
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run --extra dev pytest tests/unit/test_extraction.py -q`
Expected: FAIL — `ExtractionCandidate` has no `index`/`source_type`/`label` fields.

- [ ] **Step 3: Rewrite `Answer` in `src/parts_lookup/domain/models.py`**

```python
class Answer(_Frozen):
    """Structured extraction result from Claude.

    ``source_index`` is the 1-based index of the candidate (in the order they
    were supplied to the extractor) the model cited — the caller maps it back
    to the retrieval hit. Tool/torque strings preserve the manual's notation.
    """

    text: str
    tool_size: str | None = None
    torque: str | None = None
    source_index: int = Field(ge=1)
    confidence: float = Field(ge=0.0, le=1.0)
```

Update `tests/unit/test_domain.py` `TestAnswer._kwargs` base dict: replace `"source_pdf_id": 1, "source_page_no": 31,` with `"source_index": 1,`.

- [ ] **Step 4: Rewrite `src/parts_lookup/extraction/prompt.py`**

```python
"""System prompt and structured-output schema for the extraction context.

The schema is the contract Claude is told to follow. Tool sizes and torque
values come straight from manufacturer sources and have no canonical format —
keeping them as free-text strings preserves the original notation
("11 N-m (97 in-lb)", "5mm hex", "T25 Torx") so the API layer can render
exactly what the manual says.
"""

from __future__ import annotations

from typing import Any

SYSTEM_PROMPT: str = """You are a technical reference assistant for bicycle-shop mechanics.

You read excerpts from manufacturer service manuals and answer concrete
mechanical-spec questions. Your users are professionals fixing bikes — they
need precise, unambiguous answers, not prose.

Inputs you will receive:
- A natural-language question from a mechanic.
- A small set of numbered candidate sources from manufacturer manuals.
  Each candidate is either:
  * a PDF page presented as an image, followed by a text marker
    "Above is source {N}: {label}.", or
  * a section of a digital (HTML) manual presented as text, introduced by
    "Source {N}: {label}".

Rules:
1. Use ONLY the information in the supplied sources. Do not draw on outside
   knowledge of bikes, components, or torque conventions.
2. Preserve the manual's notation verbatim. If a torque is written
   "11 N-m (97 in-lb)" or "40 N·m (354 in-lb)", return that whole string —
   do not convert, round, or reformat. Same for tool sizes ("5mm hex key",
   "T25 Torx", "7-8mm").
3. If the answer is in one of the supplied sources, set source_index to that
   source's number.
4. If the supplied sources do not contain the answer, return a low confidence
   (<= 0.3), set tool_size, torque, and source_index to null, and explain in
   the answer field what is missing. Do not guess.
5. Reply with a SINGLE JSON object matching the schema. No prose, no code
   fences, no commentary.
"""


OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["answer", "tool_size", "torque", "source_index", "confidence"],
    "properties": {
        "answer": {
            "type": "string",
            "description": (
                "Short natural-language answer to the mechanic's question, "
                "drawn verbatim from the supplied sources where possible."
            ),
        },
        "tool_size": {
            "type": ["string", "null"],
            "description": (
                "Tool size as written in the source (e.g. '5mm hex key', "
                "'T25 Torx', '7-8mm'). Null if not applicable or not present."
            ),
        },
        "torque": {
            "type": ["string", "null"],
            "description": (
                "Torque spec as written in the source, including units and "
                "any parenthetical conversions (e.g. '11 N-m (97 in-lb)'). "
                "Null if not applicable or not present."
            ),
        },
        "source_index": {
            "type": ["integer", "null"],
            "minimum": 1,
            "description": (
                "The number of the candidate source that contains the answer. "
                "Must match one of the source numbers supplied in the user "
                "turn. Null when no supplied source answers the question."
            ),
        },
        "confidence": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
            "description": (
                "0.0-1.0 self-assessed confidence. Use <= 0.3 when the "
                "supplied sources do not actually answer the question."
            ),
        },
    },
}
```

- [ ] **Step 5: Rewrite `src/parts_lookup/extraction/claude_client.py`**

Replace the dataclass, `_build_user_blocks`, `_parse_response`, and the stub branch (everything else — constructor, fence stripping, the `messages.create` call — stays identical):

```python
from parts_lookup.domain.models import Answer, SourceType


@dataclass(frozen=True)
class ExtractionCandidate:
    """One numbered source being considered as a possible answer origin.

    ``index`` is 1-based and is what Claude cites back as ``source_index``.
    PDF candidates carry ``png_bytes`` (vision); HTML candidates carry
    ``text`` — the parent module reconstructed from sibling chunks.
    """

    index: int
    source_type: SourceType
    label: str
    png_bytes: bytes | None = None
    text: str | None = None
```

```python
    async def extract(
        self,
        query: str,
        candidates: list[ExtractionCandidate],
    ) -> Answer:
        if not candidates:
            raise ExtractionError("extract() requires at least one candidate source")

        if self._stub:
            top = candidates[0]
            return Answer(
                text=(
                    f"[STUB EXTRACTION] Top candidate is source {top.index} "
                    f"({top.label}). Real Claude call skipped because "
                    "STUB_EXTERNAL_APIS=true."
                ),
                tool_size=None,
                torque=None,
                source_index=top.index,
                confidence=0.5,
            )

        assert self._client is not None
        user_blocks = self._build_user_blocks(query, candidates)

        try:
            response = await self._client.messages.create(
                model=self._settings.anthropic_model,
                max_tokens=1024,
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user_blocks}],
            )
        except anthropic.APIError as exc:
            raise ExtractionError("Claude API call failed") from exc

        return self._parse_response(response, candidates)

    @staticmethod
    def _build_user_blocks(
        query: str,
        candidates: list[ExtractionCandidate],
    ) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        for candidate in candidates:
            if candidate.source_type is SourceType.PDF:
                if candidate.png_bytes is None:
                    raise ExtractionError(
                        f"PDF candidate {candidate.index} is missing png_bytes"
                    )
                encoded = base64.standard_b64encode(candidate.png_bytes).decode()
                blocks.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": encoded,
                        },
                    }
                )
                blocks.append(
                    {
                        "type": "text",
                        "text": (
                            f"Above is source {candidate.index}: {candidate.label}."
                        ),
                    }
                )
            else:
                if candidate.text is None:
                    raise ExtractionError(
                        f"HTML candidate {candidate.index} is missing text"
                    )
                blocks.append(
                    {
                        "type": "text",
                        "text": (
                            f"Source {candidate.index}: {candidate.label}\n"
                            f"---\n{candidate.text}\n---"
                        ),
                    }
                )

        blocks.append(
            {
                "type": "text",
                "text": (
                    f"Question: {query}\n\n"
                    "Answer the question using only these sources. "
                    "Reply ONLY with JSON matching the schema."
                ),
            }
        )
        return blocks

    @staticmethod
    def _parse_response(
        response: anthropic.types.Message,
        candidates: list[ExtractionCandidate],
    ) -> Answer:
        if not response.content:
            raise ExtractionError("Claude returned an empty response")

        first = response.content[0]
        if first.type != "text":
            raise ExtractionError(
                f"Expected first content block to be text, got {first.type!r}"
            )

        raw_text = _strip_json_fences(first.text)

        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise ExtractionError(
                "Claude response was not valid JSON after fence stripping"
            ) from exc

        try:
            answer_text = str(payload["answer"])
            confidence = float(payload["confidence"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ExtractionError(
                "Claude response JSON did not match the expected schema"
            ) from exc

        tool_size = payload.get("tool_size")
        torque = payload.get("torque")
        raw_index = payload.get("source_index")

        # Resolve which candidate the model pointed at — guarantees we never
        # surface a source that wasn't actually in the request.
        if raw_index is None:
            # "Not found" path: fall back to the top candidate so the response
            # still carries a deep link; low confidence flags it as best-guess.
            match = candidates[0]
        else:
            try:
                source_index = int(raw_index)
            except (TypeError, ValueError) as exc:
                raise ExtractionError(
                    "Claude response JSON did not match the expected schema"
                ) from exc
            match = next(
                (c for c in candidates if c.index == source_index),
                None,
            )
            if match is None:
                raise ExtractionError(
                    f"Claude cited source_index={source_index}, which was not "
                    f"among the supplied candidates"
                )

        try:
            return Answer(
                text=answer_text,
                tool_size=tool_size if tool_size is None else str(tool_size),
                torque=torque if torque is None else str(torque),
                source_index=match.index,
                confidence=confidence,
            )
        except (ValueError, TypeError) as exc:
            raise ExtractionError(
                "Claude response failed Answer validation"
            ) from exc
```

Also update the module docstring first paragraph: "Takes a query plus a small set of candidate sources (PDF page images and/or HTML manual sections), sends them to Claude, and parses a structured ``Answer``."

- [ ] **Step 6: Run tests + lint**

Run: `uv run --extra dev pytest tests/unit -q && uv run --extra dev ruff check`
Expected: all unit tests pass (including the new `test_extraction.py` and the updated `test_domain.py`); ruff clean. Note: `api/routes/query.py` still references `answer.source_pdf_id` — that is dead code until Task 10 rewrites it; nothing imports it in unit tests.

- [ ] **Step 7: Commit**

```bash
git add src/parts_lookup/domain/models.py src/parts_lookup/extraction tests/unit/test_extraction.py tests/unit/test_domain.py
git commit -m "feat(extraction): source-aware candidates + source_index output contract (refs #2)" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 6: PDF ingestion pipeline writes `documents`/`chunks`

**Files:**
- Modify: `src/parts_lookup/ingestion/pipeline.py`
- Modify: `src/parts_lookup/ingestion/cli.py` (the `_ingest_one` print line)

- [ ] **Step 1: Update the pipeline's write target**

In `src/parts_lookup/ingestion/pipeline.py`:

1. Change the domain import to `from parts_lookup.domain.models import IndexedDocument, SourceType`.
2. Change `ingest` signature to `async def ingest(self, pdf_path: Path) -> IndexedDocument:` and the class/module docstrings to say re-runs short-circuit on the existing **document** (`source_ref` = sha256).
3. Replace the dedupe lookup:

```python
        existing = await self._repo.get_document_by_source_ref(sha256)
        if existing is not None:
            return existing
```

4. Replace the `upsert_pdf` call (delete the now-unused `page_count = max(text_by_page)` only if unused — it is still printed by the CLI, so compute it but pass it through the return path no longer; keep the variable and delete the `pdf_doc = await self._repo.upsert_pdf(...)` block):

```python
        document = await self._repo.upsert_document(
            source_type=SourceType.PDF,
            title=path.name,
            source_url=pdf_key,  # R2 key; the API resolves it to a URL
            source_ref=sha256,
        )
```

5. In `_render_and_persist`, rename the `pdf_doc` parameter to `document: IndexedDocument` and replace the `insert_page` call:

```python
            png_key = f"pages/{sha256}/{page_no:04d}.png"
            await self._r2.upload_bytes(png_key, png_bytes, "image/png")
            await self._repo.insert_chunk(
                document_id=document.id,
                ordinal=page_no,
                text=text,
                embedding=embedding,
                png_r2_key=png_key,
                source_url=f"{pdf_key}#page={page_no}",
            )
```

`_render_and_persist` needs `pdf_key` — add it as a keyword parameter (`pdf_key: str`) and pass it from `ingest` alongside the others. Return `document` at the end of `ingest`.

6. The `page_count` variable is now only used for nothing in the pipeline — delete `page_count = max(text_by_page)` entirely.

- [ ] **Step 2: Update the CLI print** (in `src/parts_lookup/ingestion/cli.py`, `_ingest_one`)

```python
    print(f"OK   {pdf_path} -> document id={doc.id} ref={doc.source_ref[:12]}…")
```

- [ ] **Step 3: Run tests + lint**

Run: `uv run --extra dev pytest tests/unit -q && uv run --extra dev ruff check`
Expected: PASS; ruff clean. (No live re-ingest needed: the migration already copied every PDF, and sha256 dedupe means a re-run would short-circuit anyway.)

- [ ] **Step 4: Commit**

```bash
git add src/parts_lookup/ingestion/pipeline.py src/parts_lookup/ingestion/cli.py
git commit -m "feat(ingestion): pdf pipeline writes documents/chunks (refs #2)" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 7: HTML parser (pure) + real fixture capture

**Files:**
- Create: `src/parts_lookup/ingestion/html_parser.py`
- Create: `tests/fixtures/sram_manual_data_red_axs_trimmed.json`
- Test: `tests/unit/test_html_parser.py`

- [ ] **Step 1: Capture a full manual-data fixture from the live publication**

```bash
curl -sL -A "parts-lookup-discovery/0.1 (+https://github.com/andybaran/torque-finder)" \
  "https://docs.sram.com/en-US/publications/6TmfV97fHWv8kvGXVegoTy" -o /tmp/red_axs_um.html
uv run python - <<'EOF'
import json
from parts_lookup.discovery.publication_probe import extract_manual_data_json

html = open("/tmp/red_axs_um.html", encoding="utf-8").read()
data = json.loads(extract_manual_data_json(html))
modules = data.get("modules", [])
print("modules:", len(modules))
mod = modules[0] if modules else {}
print("module keys:", sorted(mod.keys()))
children = mod.get("children") or []
print("child keys:", sorted(children[0].keys()) if children else "NO CHILDREN")
# Keep the first 2 modules plus the first module mentioning a torque value.
keep = modules[:2]
torquey = next(
    (m for m in modules[2:] if "N·m" in json.dumps(m, ensure_ascii=False)), None
)
if torquey is not None:
    keep.append(torquey)
trimmed = dict(data)
trimmed["modules"] = keep
out = "tests/fixtures/sram_manual_data_red_axs_trimmed.json"
with open(out, "w", encoding="utf-8") as fh:
    json.dump(trimmed, fh, ensure_ascii=False, indent=1)
print("wrote", out, "with", len(keep), "modules;",
      "has torque:" , "N·m" in json.dumps(keep, ensure_ascii=False))
EOF
```

Expected: `modules: ~39`, the key listings, and `wrote tests/fixtures/... with 3 modules; has torque: True`.

**Checkpoint — compare reality to the parser contract.** The parser below reads these keys: module `hash`, `title`, `children`, `toolList`; child `hash`, `title`, `content`, `images`, `toolList`; image `caption`, `caption2`. If the printed key names differ (e.g. `slug` instead of `hash`, captions nested differently), update the accessor names in Step 3's code AND in the synthetic fixture in Step 2 to the real names before continuing. Do not guess — print a sample child (`json.dumps(children[0], indent=2)[:2000]`) if unsure.

- [ ] **Step 2: Write the failing tests**

```python
# tests/unit/test_html_parser.py
"""Pure parser tests: synthetic manual-data (exact assertions) + captured
real fixture (structural assertions). No I/O beyond reading fixtures."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from parts_lookup.domain.errors import IngestionError
from parts_lookup.ingestion.html_parser import parse_publication

_BASE = "https://docs.sram.com/en-US/publications/TESTPUB"
_FIXTURES = Path(__file__).parent.parent / "fixtures"


def _wrap(data: dict) -> str:
    return (
        '<html><body><script id="manual-data" type="application/json">'
        + json.dumps(data, ensure_ascii=False)
        + "</script></body></html>"
    )


_SYNTHETIC = {
    "title": "Road AXS Test Manual ",
    "toolList": [
        {"label": "Hex", "value": "2, 2.5, 3, 4, 5, 8 mm"},
        {"label": "TORX", "value": "T25"},
    ],
    "modules": [
        {
            "title": "Crank Arm Installation",
            "hash": "crank-install",
            "toolList": [{"label": "Hex", "value": "8 mm"}],
            "children": [
                {
                    "title": "Tighten the crank arm bolt",
                    "hash": "crank-bolt",
                    "content": "<p>Grease the spindle threads.</p>",
                    "images": [
                        {"url": "x.png", "caption": "8", "caption2": "40 N·m (354 in-lb)"}
                    ],
                },
                {
                    # No hash on this block → anchor None, link falls back to module.
                    "content": "<p>Check that the arm spins freely.</p>",
                    "images": [],
                },
                {"content": "", "images": []},  # empty → skipped
            ],
        },
        {"title": "", "hash": None, "children": []},  # contributes nothing
    ],
}


class TestSyntheticManual:
    def setup_method(self) -> None:
        self.parsed = parse_publication(_wrap(_SYNTHETIC), base_url=_BASE)

    def test_title_stripped(self) -> None:
        assert self.parsed.title == "Road AXS Test Manual"

    def test_chunk_walk_order_and_ordinals(self) -> None:
        texts = [c.text for c in self.parsed.chunks]
        assert texts == [
            "Tools: Hex: 2, 2.5, 3, 4, 5, 8 mm; TORX: T25",
            "Crank Arm Installation",
            "Tools: Hex: 8 mm",
            "Tighten the crank arm bolt\nGrease the spindle threads.\n8\n40 N·m (354 in-lb)",
            "Check that the arm spins freely.",
        ]
        assert [c.ordinal for c in self.parsed.chunks] == [1, 2, 3, 4, 5]

    def test_module_heading_chunk_anchors(self) -> None:
        heading = self.parsed.chunks[1]
        assert heading.anchor == "crank-install"
        assert heading.parent_anchor == "crank-install"
        assert heading.source_url == f"{_BASE}#crank-install"

    def test_block_with_hash_gets_own_anchor(self) -> None:
        block = self.parsed.chunks[3]
        assert block.anchor == "crank-bolt"
        assert block.parent_anchor == "crank-install"
        assert block.source_url == f"{_BASE}#crank-bolt"

    def test_block_without_hash_falls_back_to_module_anchor(self) -> None:
        block = self.parsed.chunks[4]
        assert block.anchor is None
        assert block.parent_anchor == "crank-install"
        assert block.source_url == f"{_BASE}#crank-install"

    def test_publication_toollist_has_no_anchor(self) -> None:
        tools = self.parsed.chunks[0]
        assert tools.anchor is None
        assert tools.parent_anchor is None
        assert tools.source_url == _BASE


def test_torque_caption_is_searchable_text() -> None:
    parsed = parse_publication(_wrap(_SYNTHETIC), base_url=_BASE)
    assert any("40 N·m (354 in-lb)" in c.text for c in parsed.chunks)


def test_missing_manual_data_raises_ingestion_error() -> None:
    with pytest.raises(IngestionError):
        parse_publication("<html><body>no script</body></html>", base_url=_BASE)


class TestCapturedFixture:
    """Structural invariants against the trimmed real Red AXS publication."""

    @pytest.fixture()
    def parsed(self):  # type: ignore[no-untyped-def]
        raw = (_FIXTURES / "sram_manual_data_red_axs_trimmed.json").read_text(
            encoding="utf-8"
        )
        return parse_publication(_wrap(json.loads(raw)), base_url=_BASE)

    def test_produces_chunks(self, parsed) -> None:  # type: ignore[no-untyped-def]
        assert len(parsed.chunks) >= 3

    def test_every_chunk_links_into_the_publication(self, parsed) -> None:  # type: ignore[no-untyped-def]
        assert all(c.source_url.startswith(_BASE) for c in parsed.chunks)

    def test_some_chunk_has_an_anchor(self, parsed) -> None:  # type: ignore[no-untyped-def]
        assert any(c.anchor for c in parsed.chunks)

    def test_torque_text_survives_if_present_in_fixture(self, parsed) -> None:  # type: ignore[no-untyped-def]
        raw = (_FIXTURES / "sram_manual_data_red_axs_trimmed.json").read_text(
            encoding="utf-8"
        )
        if "N·m" not in raw:
            pytest.skip("trimmed fixture carries no torque caption")
        assert any("N·m" in c.text for c in parsed.chunks)
```

Note: `_wrap(json.loads(raw))` re-wraps the trimmed JSON in a script tag so the same `parse_publication(html, ...)` entry point is exercised.

- [ ] **Step 3: Run to verify failure**

Run: `uv run --extra dev pytest tests/unit/test_html_parser.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'parts_lookup.ingestion.html_parser'`.

- [ ] **Step 4: Write the parser**

```python
# src/parts_lookup/ingestion/html_parser.py
"""Pure parser: SRAM publication HTML → ParsedPublication. No network, no DB.

The publication page embeds the whole manual as JSON in
``<script id="manual-data">`` (Contentful-backed; investigated 2026-06-08):
``modules`` → ``children`` (blocks) → ``content`` (HTML ``<p>``) plus images
whose ``caption``/``caption2`` fields carry torque values; ``toolList`` gives
tool sizes; every module/child has a ``hash`` used for section deep links.

Chunking (spec §2.3, §4): one chunk per block, plus a heading chunk per module
(text = module title, anchor = parent_anchor = module hash) and a chunk per
toolList. The heading chunk means module reconstruction
(``parent_anchor = ?`` ordered by ordinal) always starts with the title —
which the API uses as the candidate label.
"""

from __future__ import annotations

import html as html_module
import json
import re

from parts_lookup.discovery.publication_probe import extract_manual_data_json
from parts_lookup.domain.errors import DiscoveryError, IngestionError
from parts_lookup.domain.models import HtmlChunk, ParsedPublication

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _strip_html(fragment: str) -> str:
    """HTML fragment → plain text: drop tags, unescape entities, collapse spaces."""
    text = _TAG_RE.sub(" ", fragment)
    text = html_module.unescape(text)
    return _WS_RE.sub(" ", text).strip()


def _hash_of(node: dict) -> str | None:
    value = str(node.get("hash") or "").strip().lstrip("#")
    return value or None


def _deep_link(base_url: str, anchor: str | None) -> str:
    return f"{base_url}#{anchor}" if anchor else base_url


def _image_captions(images: object) -> list[str]:
    captions: list[str] = []
    if not isinstance(images, list):
        return captions
    for image in images:
        if not isinstance(image, dict):
            continue
        for key in ("caption", "caption2"):
            value = str(image.get(key) or "").strip()
            if value:
                captions.append(value)
    return captions


def _content_texts(content: object) -> list[str]:
    """Flatten a block's ``content`` into text fragments.

    Tolerates a bare HTML string, a list of strings, or (lists of) dicts
    carrying ``html``/``text``/``value`` plus nested ``images``.
    """
    parts: list[str] = []
    if content is None:
        return parts
    if isinstance(content, str):
        text = _strip_html(content)
        if text:
            parts.append(text)
        return parts
    if isinstance(content, dict):
        for key in ("html", "text", "value"):
            value = content.get(key)
            if isinstance(value, str):
                text = _strip_html(value)
                if text:
                    parts.append(text)
                break
        parts.extend(_image_captions(content.get("images")))
        return parts
    if isinstance(content, list):
        for item in content:
            parts.extend(_content_texts(item))
    return parts


def _tool_list_text(tool_list: object) -> str:
    """toolList → one searchable line, e.g. 'Tools: Hex: 2, 2.5 mm; TORX: T25'."""
    if not tool_list:
        return ""
    if isinstance(tool_list, dict):
        tool_list = [{"label": k, "value": v} for k, v in tool_list.items()]
    entries: list[str] = []
    if isinstance(tool_list, list):
        for item in tool_list:
            if isinstance(item, str):
                text = item.strip()
            elif isinstance(item, dict):
                label = str(
                    item.get("label") or item.get("title") or item.get("type") or ""
                ).strip()
                value = item.get("value") or item.get("text") or item.get("sizes") or ""
                if isinstance(value, list):
                    value = ", ".join(str(v) for v in value)
                value = str(value).strip()
                text = f"{label}: {value}".strip(": ").strip()
            else:
                text = ""
            if text:
                entries.append(text)
    if not entries:
        return ""
    return "Tools: " + "; ".join(entries)


def _block_text(child: dict) -> str:
    """Block title + paragraph text + image captions + tools, de-duplicated."""
    parts: list[str] = []
    title = str(child.get("title") or "").strip()
    if title:
        parts.append(title)
    parts.extend(_content_texts(child.get("content")))
    parts.extend(_image_captions(child.get("images")))
    tool_text = _tool_list_text(child.get("toolList"))
    if tool_text:
        parts.append(tool_text)
    seen: set[str] = set()
    unique = [p for p in parts if not (p in seen or seen.add(p))]
    return "\n".join(unique).strip()


def parse_publication(html: str, base_url: str) -> ParsedPublication:
    """Parse one publication page into title + ordered block-level chunks."""
    try:
        raw = extract_manual_data_json(html)
    except DiscoveryError as exc:
        raise IngestionError(str(exc)) from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise IngestionError("manual-data JSON is invalid") from exc

    title = str(data.get("title") or "").strip()
    chunks: list[HtmlChunk] = []
    ordinal = 0

    def _append(text: str, anchor: str | None, parent_anchor: str | None) -> None:
        nonlocal ordinal
        text = text.strip()
        if not text:
            return
        ordinal += 1
        chunks.append(
            HtmlChunk(
                ordinal=ordinal,
                text=text,
                anchor=anchor,
                parent_anchor=parent_anchor,
                source_url=_deep_link(base_url, anchor or parent_anchor),
            )
        )

    _append(_tool_list_text(data.get("toolList")), None, None)

    for module in data.get("modules") or []:
        if not isinstance(module, dict):
            continue
        module_hash = _hash_of(module)
        _append(str(module.get("title") or "").strip(), module_hash, module_hash)
        _append(_tool_list_text(module.get("toolList")), None, module_hash)
        for child in module.get("children") or []:
            if not isinstance(child, dict):
                continue
            _append(_block_text(child), _hash_of(child), module_hash)

    return ParsedPublication(title=title, chunks=tuple(chunks))
```

- [ ] **Step 5: Run tests + lint**

Run: `uv run --extra dev pytest tests/unit/test_html_parser.py -q && uv run --extra dev ruff check`
Expected: all pass; ruff clean. If a `TestCapturedFixture` test fails, the real JSON shape differs from the contract — fix the parser's accessor names (and synthetic fixture) per the Step 1 checkpoint, not the test.

- [ ] **Step 6: Commit**

```bash
git add src/parts_lookup/ingestion/html_parser.py tests/unit/test_html_parser.py tests/fixtures/sram_manual_data_red_axs_trimmed.json
git commit -m "feat(ingestion): pure manual-data HTML parser + captured Red AXS fixture (refs #2)" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 8: Registry `set_status` + HTML ingestion pipeline

**Files:**
- Modify: `src/parts_lookup/discovery/registry.py`
- Create: `src/parts_lookup/ingestion/html_pipeline.py`
- Test: `tests/unit/test_html_pipeline.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_html_pipeline.py
"""HTML pipeline orchestration with duck-typed fakes — no network, no DB."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from parts_lookup.domain.errors import IngestionError
from parts_lookup.domain.models import (
    IndexedDocument,
    RegisteredPublication,
    SourceType,
)
from parts_lookup.ingestion.html_pipeline import HtmlIngestionPipeline

_PUB = RegisteredPublication(
    pub_id="TESTPUB",
    pub_type="UM",
    title="Registry Title",
    locale="en-US",
    source_url="https://docs.sram.com/en-US/publications/TESTPUB",
    series=(),
    models=(),
    procedures=(),
    referenced_by_models=(),
    content_hash="x",
    status="discovered",
    discovered_at=datetime(2026, 6, 8, tzinfo=UTC),
    last_seen_at=datetime(2026, 6, 8, tzinfo=UTC),
)

_HTML = (
    '<html><body><script id="manual-data" type="application/json">'
    + json.dumps(
        {
            "title": "Road AXS Test Manual",
            "modules": [
                {
                    "title": "Crank Arm Installation",
                    "hash": "crank-install",
                    "children": [
                        {
                            "hash": "crank-bolt",
                            "content": "<p>Tighten the bolt.</p>",
                            "images": [{"caption2": "40 N·m (354 in-lb)"}],
                        }
                    ],
                }
            ],
        },
        ensure_ascii=False,
    )
    + "</script></body></html>"
)


class FakeRepo:
    def __init__(self) -> None:
        self.inserted: list[dict] = []
        self.deleted_for: list[int] = []

    async def upsert_document(self, **kwargs):  # type: ignore[no-untyped-def]
        self.upserted = kwargs
        return IndexedDocument(
            id=7,
            source_type=SourceType.HTML,
            title=kwargs["title"],
            source_url=kwargs["source_url"],
            source_ref=kwargs["source_ref"],
            created_at=datetime(2026, 6, 9, tzinfo=UTC),
        )

    async def delete_chunks(self, document_id: int) -> None:
        self.deleted_for.append(document_id)

    async def insert_chunk(self, **kwargs):  # type: ignore[no-untyped-def]
        self.inserted.append(kwargs)
        return len(self.inserted)


class FakeRegistry:
    def __init__(self) -> None:
        self.statuses: list[tuple[str, str]] = []

    async def set_status(self, pub_id: str, status: str) -> None:
        self.statuses.append((pub_id, status))


class FakeFetcher:
    def __init__(self, body: str) -> None:
        self.body = body
        self.urls: list[str] = []

    async def get(self, url: str) -> str:
        self.urls.append(url)
        return self.body


class FakeEmbedder:
    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * 1024 for _ in texts]


def _pipeline(body: str = _HTML):  # type: ignore[no-untyped-def]
    repo, registry, fetcher = FakeRepo(), FakeRegistry(), FakeFetcher(body)
    pipeline = HtmlIngestionPipeline(
        repository=repo, registry=registry, fetcher=fetcher, embedder=FakeEmbedder()
    )
    return pipeline, repo, registry, fetcher


async def test_ingest_publication_end_to_end() -> None:
    pipeline, repo, registry, fetcher = _pipeline()
    doc = await pipeline.ingest_publication(_PUB)

    assert fetcher.urls == [_PUB.source_url]
    assert repo.upserted["source_type"] is SourceType.HTML
    assert repo.upserted["source_ref"] == "TESTPUB"
    assert repo.upserted["title"] == "Road AXS Test Manual"  # parsed beats registry
    assert repo.deleted_for == [7]  # stale re-ingest wipes old chunks first
    assert len(repo.inserted) == 2  # heading chunk + block chunk
    assert repo.inserted[0]["text"] == "Crank Arm Installation"
    assert "40 N·m (354 in-lb)" in repo.inserted[1]["text"]
    assert repo.inserted[1]["anchor"] == "crank-bolt"
    assert repo.inserted[1]["parent_anchor"] == "crank-install"
    assert repo.inserted[1]["png_r2_key"] is None
    assert registry.statuses == [("TESTPUB", "ingested")]
    assert doc.id == 7


async def test_publication_with_no_chunks_raises() -> None:
    empty = '<html><body><script id="manual-data" type="application/json">{"title": "x", "modules": []}</script></body></html>'
    pipeline, repo, registry, _ = _pipeline(empty)
    with pytest.raises(IngestionError):
        await pipeline.ingest_publication(_PUB)
    assert repo.inserted == []
    assert registry.statuses == []
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run --extra dev pytest tests/unit/test_html_pipeline.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'parts_lookup.ingestion.html_pipeline'`.

- [ ] **Step 3: Add `set_status` to `PublicationRegistry`** (in `src/parts_lookup/discovery/registry.py`; also add `from parts_lookup.domain.errors import DiscoveryError` to the imports)

```python
    async def set_status(self, pub_id: str, status: str) -> None:
        """Flip a publication's lifecycle status (e.g. 'discovered' → 'ingested')."""
        row = await self._get_row(pub_id)
        if row is None:
            raise DiscoveryError(f"unknown publication: {pub_id}")
        row.status = status
        await self._session.flush()
```

- [ ] **Step 4: Write the pipeline**

```python
# src/parts_lookup/ingestion/html_pipeline.py
"""Orchestrates ingestion of one HTML publication: fetch, parse, embed, store.

Mirrors the PDF pipeline's shape: owns no state, touches no I/O directly —
the discovery Fetcher (cache/robots/politeness), Repository, registry, and
VoyageEmbedder are injected. Re-ingest of a 'stale' publication is safe:
the document row is upserted by pub_id and old chunks are deleted first.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from parts_lookup.domain.errors import IngestionError
from parts_lookup.domain.models import (
    IndexedDocument,
    RegisteredPublication,
    SourceType,
)
from parts_lookup.ingestion.html_parser import parse_publication

if TYPE_CHECKING:
    from parts_lookup.discovery.fetcher import Fetcher
    from parts_lookup.discovery.registry import PublicationRegistry
    from parts_lookup.indexing.repository import Repository
    from parts_lookup.retrieval.embedder import VoyageEmbedder


class HtmlIngestionPipeline:
    """End-to-end ingestion of a single registered publication."""

    def __init__(
        self,
        *,
        repository: Repository,
        registry: PublicationRegistry,
        fetcher: Fetcher,
        embedder: VoyageEmbedder,
    ) -> None:
        self._repo = repository
        self._registry = registry
        self._fetcher = fetcher
        self._embedder = embedder

    async def ingest_publication(self, pub: RegisteredPublication) -> IndexedDocument:
        """Ingest one publication; returns the resulting document."""
        try:
            html = await self._fetcher.get(pub.source_url)
        except Exception as exc:
            raise IngestionError(f"fetch failed for publication {pub.pub_id}") from exc

        parsed = parse_publication(html, base_url=pub.source_url)
        if not parsed.chunks:
            raise IngestionError(f"no chunks parsed from publication {pub.pub_id}")

        try:
            embeddings = await self._embedder.embed_documents(
                [chunk.text for chunk in parsed.chunks]
            )
        except Exception as exc:
            raise IngestionError(f"embedding failed for publication {pub.pub_id}") from exc
        if len(embeddings) != len(parsed.chunks):
            raise IngestionError(
                f"embedder returned {len(embeddings)} vectors for "
                f"{len(parsed.chunks)} chunks"
            )

        document = await self._repo.upsert_document(
            source_type=SourceType.HTML,
            title=parsed.title or pub.title,
            source_url=pub.source_url,
            source_ref=pub.pub_id,
        )
        await self._repo.delete_chunks(document.id)
        for chunk, embedding in zip(parsed.chunks, embeddings, strict=True):
            await self._repo.insert_chunk(
                document_id=document.id,
                ordinal=chunk.ordinal,
                text=chunk.text,
                embedding=embedding,
                png_r2_key=None,
                anchor=chunk.anchor,
                parent_anchor=chunk.parent_anchor,
                source_url=chunk.source_url,
            )

        await self._registry.set_status(pub.pub_id, "ingested")
        return document
```

- [ ] **Step 5: Run tests + lint**

Run: `uv run --extra dev pytest tests/unit/test_html_pipeline.py tests/unit -q && uv run --extra dev ruff check`
Expected: PASS; ruff clean.

- [ ] **Step 6: Commit**

```bash
git add src/parts_lookup/discovery/registry.py src/parts_lookup/ingestion/html_pipeline.py tests/unit/test_html_pipeline.py
git commit -m "feat(ingestion): HTML publication pipeline + registry status flip (refs #2)" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 9: `parts-lookup ingest-html` CLI verb

**Files:**
- Modify: `src/parts_lookup/ingestion/cli.py`
- Test: `tests/unit/test_ingest_cli.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_ingest_cli.py
"""Argument-surface tests for the ingestion CLI (no I/O)."""

from __future__ import annotations

from parts_lookup.ingestion.cli import _build_parser


def test_ingest_html_no_args_means_all_pending() -> None:
    args = _build_parser().parse_args(["ingest-html"])
    assert args.command == "ingest-html"
    assert args.pub_ids == []


def test_ingest_html_accepts_pub_ids() -> None:
    args = _build_parser().parse_args(["ingest-html", "A1", "B2"])
    assert args.pub_ids == ["A1", "B2"]
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run --extra dev pytest tests/unit/test_ingest_cli.py -q`
Expected: FAIL — argparse exits with "invalid choice: 'ingest-html'".

- [ ] **Step 3: Add the verb to `src/parts_lookup/ingestion/cli.py`**

Add imports:

```python
from parts_lookup.discovery.fetcher import Fetcher
from parts_lookup.discovery.registry import PublicationRegistry
from parts_lookup.ingestion.html_pipeline import HtmlIngestionPipeline
```

In `_build_parser()` add:

```python
    ingest_html = sub.add_parser(
        "ingest-html",
        help="Ingest HTML publications from the discovery registry.",
    )
    ingest_html.add_argument(
        "pub_ids",
        nargs="*",
        default=[],
        help="Publication ids; omit to ingest every 'discovered'/'stale' publication.",
    )
```

In `main()` add the dispatch branch:

```python
    if args.command == "ingest-html":
        return asyncio.run(_run_ingest_html(list(args.pub_ids)))
```

Add the runner (module docstring: add the new subcommand to the list):

```python
async def _run_ingest_html(pub_ids: list[str]) -> int:
    """Ingest registered publications. One DB session per publication, like ingest-dir."""
    settings = get_settings()
    fetcher = Fetcher(
        user_agent=settings.discovery_user_agent,
        cache_dir=settings.discovery_cache_dir,
        max_concurrency=settings.discovery_max_concurrency,
        delay_seconds=settings.discovery_request_delay_seconds,
        # Discovery may have cached this publication on disk (.cache/discovery);
        # ingest must parse the live page, never a stale cached copy.
        force_refresh=True,
    )
    factory = async_session_factory(settings)
    rc = 0
    try:
        async with factory() as session:
            registry = PublicationRegistry(session)
            all_pubs = await registry.list_all()

        if pub_ids:
            wanted = set(pub_ids)
            targets = [p for p in all_pubs if p.pub_id in wanted]
            missing = wanted - {p.pub_id for p in targets}
            if missing:
                print(f"error: unknown pub_ids: {', '.join(sorted(missing))}", file=sys.stderr)
                return 2
        else:
            targets = [p for p in all_pubs if p.status in ("discovered", "stale")]

        if not targets:
            print("warning: no publications to ingest", file=sys.stderr)
            return 0

        for pub in targets:
            async with factory() as session:
                pipeline = HtmlIngestionPipeline(
                    repository=Repository(session),
                    registry=PublicationRegistry(session),
                    fetcher=fetcher,
                    embedder=VoyageEmbedder(settings),
                )
                try:
                    doc = await pipeline.ingest_publication(pub)
                    await session.commit()
                    print(f"OK   {pub.pub_id} -> document id={doc.id} title={doc.title!r}")
                except IngestionError as exc:
                    await session.rollback()
                    cause = f" (cause: {exc.__cause__!r})" if exc.__cause__ else ""
                    print(f"FAIL {pub.pub_id}: {exc}{cause}", file=sys.stderr)
                    rc = 1
    finally:
        await fetcher.aclose()
    return rc
```

- [ ] **Step 4: Run tests + lint**

Run: `uv run --extra dev pytest tests/unit -q && uv run --extra dev ruff check`
Expected: PASS; ruff clean.

- [ ] **Step 5: Commit**

```bash
git add src/parts_lookup/ingestion/cli.py tests/unit/test_ingest_cli.py
git commit -m "feat(ingestion): ingest-html CLI verb over the publications registry (refs #2)" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 10: API — source-agnostic response (approved breaking change)

**Files:**
- Modify: `src/parts_lookup/api/schemas.py`
- Modify: `src/parts_lookup/api/routes/query.py`

- [ ] **Step 1: Rewrite the response schemas** (replace `CandidatePage` and `AnswerResponse` in `src/parts_lookup/api/schemas.py`; add `from typing import Literal` to imports; `QueryRequest`/`HealthResponse` unchanged)

```python
class Candidate(BaseModel):
    """A retrieval hit returned alongside the answer for transparency/debugging."""

    source_type: Literal["pdf", "html"]
    label: str = Field(..., examples=["p. 28 of mtb-manual.pdf", "Crank Arm Installation"])
    score: float
    source_url: str
    screenshot_url: str | None = Field(
        default=None, description="Rendered page PNG URL; null for HTML sources."
    )


class AnswerResponse(BaseModel):
    answer: str = Field(..., examples=["5 mm hex key, 11 N-m (97 in-lb)"])
    tool_size: str | None = Field(default=None, examples=["5 mm hex key"])
    torque: str | None = Field(default=None, examples=["11 N-m (97 in-lb)"])
    confidence: float = Field(..., ge=0.0, le=1.0)

    source_type: Literal["pdf", "html"] = Field(
        ..., description="Where the answer came from."
    )
    source_url: str = Field(
        ...,
        description=(
            "Deep link to the source: the original PDF with a #page=N fragment, "
            "or the docs.sram.com publication with a #section hash."
        ),
    )
    screenshot_url: str | None = Field(
        default=None,
        description=(
            "URL (public or presigned) to the rendered PNG of the source page. "
            "Null for HTML sources — the deep link is the source reference."
        ),
    )

    candidates: list[Candidate] = Field(
        default_factory=list,
        description="The top-k candidates considered, in fused-rank order.",
    )
```

- [ ] **Step 2: Rewrite the route** (full replacement of `src/parts_lookup/api/routes/query.py`)

```python
"""POST /v1/query — the only real endpoint at v1.

Wires together the four downstream contexts (indexing, retrieval, assets,
extraction) into the single end-to-end mechanic-facing flow described in
CLAUDE.md. Candidates are source-agnostic: PDF chunks go to Claude as page
images; HTML chunks go as the parent module's text reconstructed from
sibling chunks (small-to-big).
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, HTTPException, status

from parts_lookup.api.dependencies import (
    ExtractorDep,
    R2Dep,
    RetrievalDep,
    SessionDep,
)
from parts_lookup.api.schemas import AnswerResponse, Candidate, QueryRequest
from parts_lookup.assets.r2_client import R2Client
from parts_lookup.domain.errors import ExtractionError, RetrievalError
from parts_lookup.domain.models import Query, RetrievedChunk, SourceType
from parts_lookup.extraction.claude_client import ExtractionCandidate
from parts_lookup.indexing.repository import Repository

router = APIRouter(prefix="/v1", tags=["query"])

# Long-lived presigned URLs so a frontend can keep them on screen.
_PRESIGN_TTL_SECONDS = 60 * 60
_LABEL_MAX_CHARS = 80


@router.post("/query", response_model=AnswerResponse)
async def query(
    body: QueryRequest,
    session: SessionDep,
    retrieval: RetrievalDep,
    extractor: ExtractorDep,
    r2: R2Dep,
) -> AnswerResponse:
    domain_query = Query(text=body.question, top_k=body.top_k)

    try:
        retrieved = await retrieval.search(session, domain_query)
    except RetrievalError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Retrieval failed: {exc}",
        ) from exc

    if not retrieved:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No matching content found in the index.",
        )

    repo = Repository(session)
    candidates: list[ExtractionCandidate] = []
    for index, hit in enumerate(retrieved, start=1):
        if hit.source_type is SourceType.PDF:
            if hit.png_r2_key is None:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"PDF chunk {hit.chunk_id} is missing its page PNG key.",
                )
            png = await _fetch_png(r2, hit.png_r2_key)
            candidates.append(
                ExtractionCandidate(
                    index=index,
                    source_type=SourceType.PDF,
                    label=_pdf_label(hit),
                    png_bytes=png,
                )
            )
        else:
            module_text = await _module_text(repo, hit)
            candidates.append(
                ExtractionCandidate(
                    index=index,
                    source_type=SourceType.HTML,
                    label=_html_label(module_text, hit),
                    text=module_text,
                )
            )

    try:
        answer = await extractor.extract(body.question, candidates)
    except ExtractionError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Extraction failed: {exc}",
        ) from exc

    # The extractor guarantees source_index maps to a supplied candidate.
    chosen = retrieved[answer.source_index - 1]
    source_url, screenshot_url = await _source_links(r2, chosen)

    response_candidates: list[Candidate] = []
    for hit, candidate in zip(retrieved, candidates, strict=True):
        hit_source_url, hit_screenshot_url = await _source_links(r2, hit)
        response_candidates.append(
            Candidate(
                source_type=hit.source_type.value,
                label=candidate.label,
                score=hit.score,
                source_url=hit_source_url,
                screenshot_url=hit_screenshot_url,
            )
        )

    return AnswerResponse(
        answer=answer.text,
        tool_size=answer.tool_size,
        torque=answer.torque,
        confidence=answer.confidence,
        source_type=chosen.source_type.value,
        source_url=source_url,
        screenshot_url=screenshot_url,
        candidates=response_candidates,
    )


def _pdf_label(hit: RetrievedChunk) -> str:
    return f"p. {hit.ordinal} of {hit.document_title}"


def _html_label(module_text: str, hit: RetrievedChunk) -> str:
    """Module title = first line of the reconstructed module (the heading chunk)."""
    first_line = module_text.strip().split("\n", 1)[0].strip()
    label = first_line or hit.document_title
    return label[:_LABEL_MAX_CHARS]


async def _module_text(repo: Repository, hit: RetrievedChunk) -> str:
    """Small-to-big: hand Claude the whole owning module, not just the hit block."""
    if hit.parent_anchor is None:
        return hit.text
    text = await repo.fetch_module_text(hit.document_id, hit.parent_anchor)
    return text or hit.text


async def _source_links(r2: R2Client, hit: RetrievedChunk) -> tuple[str, str | None]:
    """(source_url, screenshot_url) for one chunk.

    HTML chunks already carry a complete docs.sram.com#hash deep link and have
    no screenshot. PDF chunks store R2 *keys*; resolve them to URLs here.
    """
    if hit.source_type is SourceType.HTML:
        return hit.source_url, None
    pdf_url = await _resolve_url(r2, hit.document_source_url)
    screenshot = await _resolve_url(r2, hit.png_r2_key) if hit.png_r2_key else None
    return f"{pdf_url}#page={hit.ordinal}", screenshot


async def _fetch_png(r2: R2Client, key: str) -> bytes:
    """Read a PNG from R2 by key via a short-lived presigned GET URL."""
    url = await r2.generate_presigned_url(key, expires_in=300)
    async with httpx.AsyncClient(timeout=30.0) as http:
        response = await http.get(url)
        response.raise_for_status()
        return response.content


async def _resolve_url(r2: R2Client, key: str) -> str:
    """Public URL when R2_PUBLIC_BASE_URL is set, else a presigned URL."""
    try:
        return r2.public_url(key)
    except Exception:
        return await r2.generate_presigned_url(key, expires_in=_PRESIGN_TTL_SECONDS)
```

(The old `_lookup_pdf_r2_key` and the `select`/`Pdf` imports are gone — the PDF's R2 key now travels on the hit as `document_source_url`.)

- [ ] **Step 3: Run tests + lint + import smoke**

Run: `uv run --extra dev pytest tests/unit -q && uv run --extra dev ruff check && uv run python -c "from parts_lookup.api.main import create_app; print('app imports OK')"`
Expected: PASS; ruff clean; `app imports OK`.

- [ ] **Step 4: Commit**

```bash
git add src/parts_lookup/api/schemas.py src/parts_lookup/api/routes/query.py
git commit -m "feat(api)!: source-agnostic answer response (source_type/source_url/screenshot_url) (refs #2)" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 11: Eval suite through the new model + HTML ground truth (+ live ingest)

**Files:**
- Modify: `tests/eval/test_eval_smoke.py`
- Create: `tests/eval/ground_truth_html.py`
- Create: `tests/eval/test_eval_html.py`

- [ ] **Step 1: Rewrite the PDF eval to the unified model** (full replacement of `tests/eval/test_eval_smoke.py`)

```python
"""End-to-end eval harness over the canonical thoughts.md ground truth.

Opt-in regression suite (``pytest -m eval``): each case makes a real Voyage
embed + Postgres hybrid retrieval over the unified ``chunks`` store + a real
Claude call. Roughly ~$0.01/case. This suite is the SAFETY GATE for the
pdfs/pages drop migration (spec §7): 0005 must not run until it is green.
"""

from __future__ import annotations

import os

import httpx
import pytest

from tests.conftest import LIVE_ENV_VARS, missing_env
from tests.eval.ground_truth import GROUND_TRUTH, GroundTruthCase

_missing = missing_env(LIVE_ENV_VARS)
if _missing:
    pytest.skip(
        f"eval suite requires live env vars (missing: {', '.join(_missing)})",
        allow_module_level=True,
    )

pytestmark = [pytest.mark.eval, pytest.mark.asyncio]


def _matches(actual: str | None, expected: str | None) -> bool:
    """Soft substring match; ``expected=None`` is always satisfied."""
    if expected is None:
        return True
    if actual is None:
        return False
    return expected.lower() in actual.lower()


def _database_url() -> str:
    url = os.environ["DATABASE_URL"]
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


async def _fetch_png(r2, key: str) -> bytes:  # type: ignore[no-untyped-def]
    url = await r2.generate_presigned_url(key, expires_in=300)
    async with httpx.AsyncClient(timeout=30.0) as http:
        response = await http.get(url)
        response.raise_for_status()
        return response.content


async def run_query(question: str, top_k: int = 3):  # type: ignore[no-untyped-def]
    """Shared retrieval→extraction runner. Returns (hits, answer, chosen_hit)."""
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from parts_lookup.assets.r2_client import R2Client
    from parts_lookup.config import Settings
    from parts_lookup.domain.models import Query, SourceType
    from parts_lookup.extraction.claude_client import ClaudeExtractor, ExtractionCandidate
    from parts_lookup.indexing.repository import Repository
    from parts_lookup.retrieval.hybrid import RetrievalService

    settings = Settings()  # type: ignore[call-arg]
    engine = create_async_engine(_database_url(), echo=False)
    retrieval = RetrievalService.from_settings(settings)
    extractor = ClaudeExtractor(settings)
    r2 = R2Client(settings)

    try:
        async with AsyncSession(engine) as session:
            hits = await retrieval.search(session, Query(text=question, top_k=top_k))
            assert hits, f"retrieval returned no hits for {question!r}"

            candidates: list[ExtractionCandidate] = []
            repo = Repository(session)
            for index, hit in enumerate(hits, start=1):
                if hit.source_type is SourceType.PDF:
                    assert hit.png_r2_key is not None
                    candidates.append(
                        ExtractionCandidate(
                            index=index,
                            source_type=SourceType.PDF,
                            label=f"p. {hit.ordinal} of {hit.document_title}",
                            png_bytes=await _fetch_png(r2, hit.png_r2_key),
                        )
                    )
                else:
                    module_text = hit.text
                    if hit.parent_anchor is not None:
                        module_text = (
                            await repo.fetch_module_text(hit.document_id, hit.parent_anchor)
                            or hit.text
                        )
                    candidates.append(
                        ExtractionCandidate(
                            index=index,
                            source_type=SourceType.HTML,
                            label=module_text.split("\n", 1)[0][:80],
                            text=module_text,
                        )
                    )

        answer = await extractor.extract(question, candidates)
    finally:
        await engine.dispose()

    return hits, answer, hits[answer.source_index - 1]


@pytest.mark.parametrize("case", GROUND_TRUTH, ids=lambda c: c.case_id)
async def test_ground_truth_case(case: GroundTruthCase) -> None:
    from parts_lookup.domain.models import SourceType

    _hits, answer, chosen = await run_query(case.query)

    assert chosen.source_type is SourceType.PDF, (
        f"[{case.case_id}] expected a PDF source, got {chosen.source_type}"
    )
    assert chosen.ordinal == case.page_no, (
        f"[{case.case_id}] expected page {case.page_no}, got {chosen.ordinal}; "
        f"answer={answer.text!r}"
    )
    assert _matches(answer.tool_size, case.tool_size), (
        f"[{case.case_id}] tool_size {answer.tool_size!r} does not contain {case.tool_size!r}"
    )
    assert _matches(answer.torque, case.torque), (
        f"[{case.case_id}] torque {answer.torque!r} does not contain {case.torque!r}"
    )
```

- [ ] **Step 2: Add the HTML ground truth**

```python
# tests/eval/ground_truth_html.py
"""Ground truth for the Red AXS HTML publications (spec §8).

Expected values come from the 2026-06-08 investigation of
docs.sram.com/en-US/publications/6TmfV97fHWv8kvGXVegoTy: image caption2
fields carry torque values (e.g. "40 N·m (354 in-lb)") and the toolList
gives tool sizes verbatim (Hex: 2, 2.5, 3, 4, 5, 8 mm; TORX: T25).

NOTE for maintainers: after the first real ingest, sanity-check each query
against the live publication and refine the *phrasing* if retrieval misses —
the expected substrings are facts from the manual and must not be loosened.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HtmlGroundTruthCase:
    case_id: str
    query: str
    tool_contains: str | None
    torque_contains: str | None


HTML_GROUND_TRUTH: list[HtmlGroundTruthCase] = [
    HtmlGroundTruthCase(
        case_id="red-axs-crank-arm-torque",
        query=(
            "In the SRAM Road AXS digital user manual, what torque do the "
            "crank arm bolts get?"
        ),
        tool_contains=None,
        torque_contains="40 N",
    ),
    HtmlGroundTruthCase(
        case_id="red-axs-torx-size",
        query=(
            "Which TORX size does the SRAM Road AXS digital user manual's "
            "tool list specify?"
        ),
        tool_contains="T25",
        torque_contains=None,
    ),
]
```

- [ ] **Step 3: Add the HTML eval tests**

```python
# tests/eval/test_eval_html.py
"""HTML-source eval: answers must come from a Red AXS publication with a
working docs.sram.com#hash deep link (spec §8). Opt-in: ``pytest -m eval``."""

from __future__ import annotations

import re

import httpx
import pytest

from tests.conftest import LIVE_ENV_VARS, missing_env
from tests.eval.ground_truth_html import HTML_GROUND_TRUTH, HtmlGroundTruthCase

_missing = missing_env(LIVE_ENV_VARS)
if _missing:
    pytest.skip(
        f"eval suite requires live env vars (missing: {', '.join(_missing)})",
        allow_module_level=True,
    )

pytestmark = [pytest.mark.eval, pytest.mark.asyncio]

_DEEP_LINK_RE = re.compile(r"^https://docs\.sram\.com/.+#.+$")


def _matches(actual: str | None, expected: str | None) -> bool:
    if expected is None:
        return True
    if actual is None:
        return False
    return expected.lower() in actual.lower()


@pytest.mark.parametrize("case", HTML_GROUND_TRUTH, ids=lambda c: c.case_id)
async def test_html_ground_truth_case(case: HtmlGroundTruthCase) -> None:
    from parts_lookup.domain.models import SourceType

    from tests.eval.test_eval_smoke import run_query

    _hits, answer, chosen = await run_query(case.query, top_k=5)

    assert chosen.source_type is SourceType.HTML, (
        f"[{case.case_id}] expected an HTML source, got {chosen.source_type}; "
        f"answer={answer.text!r}"
    )
    assert _DEEP_LINK_RE.match(chosen.source_url), (
        f"[{case.case_id}] source_url is not a docs.sram.com#hash deep link: "
        f"{chosen.source_url!r}"
    )
    assert _matches(answer.tool_size, case.tool_contains), (
        f"[{case.case_id}] tool_size {answer.tool_size!r} missing {case.tool_contains!r}"
    )
    assert _matches(answer.torque, case.torque_contains), (
        f"[{case.case_id}] torque {answer.torque!r} missing {case.torque_contains!r}"
    )

    # The deep link must actually resolve (fragmentless GET).
    page_url = chosen.source_url.split("#", 1)[0]
    async with httpx.AsyncClient(
        timeout=30.0,
        follow_redirects=True,
        headers={"User-Agent": "parts-lookup-eval/0.1"},
    ) as http:
        response = await http.get(page_url)
    assert response.status_code == 200, f"deep link target returned {response.status_code}"
```

- [ ] **Step 4: Ingest the 3 Red AXS publications into production**

```bash
set -a && . ./.env && set +a
uv run parts-lookup ingest-html
```

Expected (order may vary):

```
OK   6TmfV97fHWv8kvGXVegoTy -> document id=259 title='Road AXS and XPLR AXS'
OK   2wamQedjkGP8QebD5HQiiC -> document id=260 title='...'
OK   3AypAkC43AlFBouXgIFsDD -> document id=261 title='...'
```

Then verify:

```bash
uv run python - <<'EOF'
import asyncio, os
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

async def main():
    url = os.environ["DATABASE_URL"].replace("postgresql://", "postgresql+asyncpg://", 1)
    engine = create_async_engine(url)
    async with engine.connect() as conn:
        rows = (await conn.execute(text(
            "SELECT d.source_ref, d.title, count(c.id) AS chunks, "
            "count(*) FILTER (WHERE c.anchor IS NOT NULL) AS anchored "
            "FROM documents d JOIN chunks c ON c.document_id = d.id "
            "WHERE d.source_type = 'html' GROUP BY d.id ORDER BY d.id"
        ))).all()
        for r in rows:
            print(r)
        statuses = (await conn.execute(text(
            "SELECT pub_id, status FROM publications ORDER BY pub_id"))).all()
        print(statuses)
    await engine.dispose()

asyncio.run(main())
EOF
```

Expected: 3 html documents each with dozens-to-hundreds of chunks, `anchored > 0`, and all 3 publications now `status='ingested'`.

- [ ] **Step 5: Run the full eval suite (THE GATE — must be green)**

```bash
set -a && . ./.env && set +a
uv run --extra dev pytest tests/eval -m eval -q
```

Expected: **11 passed** (9 PDF ground-truth cases + 2 HTML cases). Costs ~$0.15. If an HTML case fails on retrieval ranking (a PDF wins), refine the query *phrasing* in `ground_truth_html.py` (never the expected values) and re-run. If a PDF case fails, the unified model has a regression — STOP; do not proceed to Task 13's drop migration until fixed.

- [ ] **Step 6: Commit**

```bash
git add tests/eval
git commit -m "test(eval): unified-model ground truth + Red AXS HTML cases (refs #2)" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 12: Remove the legacy model (code only — tables drop in Task 13)

**Files:**
- Modify: `src/parts_lookup/indexing/repository.py`
- Modify: `src/parts_lookup/domain/models.py`, `src/parts_lookup/domain/errors.py`, `src/parts_lookup/domain/__init__.py`
- Modify: `tests/unit/test_domain.py`, `src/parts_lookup/api/main.py`, `CLAUDE.md`
- Delete: `tests/integration/test_repository_smoke.py`

- [ ] **Step 1: Delete legacy code**

1. `src/parts_lookup/indexing/repository.py`: delete the `Pdf` and `Page` ORM classes, `_pdf_row_to_domain`, `_page_row_to_domain`, and the `Repository` methods `upsert_pdf`, `insert_page`, `get_pdf_by_sha256`, `list_pdfs`, `get_page`, `fetch_pages_by_ids`, and `_get_pdf_orm_by_sha256`. Remove the now-unused imports (`PdfNotFoundError`, `PageContent`, `PdfDocument`).
2. `src/parts_lookup/domain/models.py`: delete `PdfDocument` and `PageContent`.
3. `src/parts_lookup/domain/errors.py`: delete `PdfNotFoundError`.
4. `src/parts_lookup/domain/__init__.py`: remove `PdfDocument`, `PageContent`, `PdfNotFoundError` from imports and `__all__`.
5. `tests/unit/test_domain.py`: delete the `TestPdfDocument` class and drop `PdfDocument` from the import line.
6. Delete `tests/integration/test_repository_smoke.py` (superseded by `test_documents_chunks.py`):

```bash
git rm tests/integration/test_repository_smoke.py
```

- [ ] **Step 2: Verify nothing references the legacy names**

Run:
```bash
grep -rn "PdfDocument\|PageContent\|RetrievedPage\|PdfNotFoundError\|upsert_pdf\|insert_page\|get_pdf_by_sha256\|fetch_pages_by_ids" src tests --include="*.py" | grep -v "pdfium.PdfDocument"
```
Expected: no output (`pdfium.PdfDocument` in `rasterizer.py` is pypdfium2's class, not ours).

- [ ] **Step 3: Update user-facing wording**

In `src/parts_lookup/api/main.py`, change the FastAPI `description` to:

```python
        description=(
            "Natural-language lookup over manufacturer manuals (PDF and "
            "digital/HTML) for bicycle-shop mechanics. Returns a structured "
            "answer plus a source deep link, and a screenshot URL for PDF "
            "sources."
        ),
```

In `CLAUDE.md`, make these minimal edits (keep everything else):
- In **Query (per request, on Railway)**: replace the flow line with: `POST /v1/query with NL question → Voyage embeds question → Postgres hybrid search over chunks (tsvector + pgvector, RRF) returns top-3 chunks (PDF pages and HTML blocks mixed) → PDF candidates: fetch page PNGs from R2 for Claude vision; HTML candidates: reconstruct the parent module text from sibling chunks for Claude text → parse structured JSON answer → API returns {answer, source_type, source_url, screenshot_url (pdf only), candidates}.`
- In the **Project layout** tree under `ingestion/`, add `html_parser.py` ("manual-data JSON → block chunks") and `html_pipeline.py` ("registry → fetch → embed → documents/chunks") lines, and note the `ingest-html` verb next to `cli.py`.
- In **Ingestion** data-flow section, add one sentence: "HTML publications: `uv run parts-lookup ingest-html` reads the `publications` registry, parses each publication's embedded manual-data JSON into block-level chunks, and writes `documents`/`chunks` (no PNGs — deep links via `#hash`)."
- In **Architecture notes → Indexing / search stack** bullet, append: "Unified `documents`/`chunks` store (PDF pages and HTML blocks); legacy `pdfs`/`pages` dropped by gated migration 0005."

- [ ] **Step 4: Run the full local suite + lint**

Run: `uv run --extra dev pytest tests/unit -q && uv run --extra dev ruff check && set -a && . ./.env && set +a && uv run --extra dev pytest tests/integration -q`
Expected: all unit + integration tests pass; ruff clean.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor!: remove legacy pdfs/pages model from code (refs #2)" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 13: Gated drop migration + start.sh pin + rollout runbook

**Files:**
- Create: `infra/migrations/versions/0005_drop_pdfs_pages.py`
- Modify: `scripts/start.sh`

**⚠️ This task writes the drop migration but NEVER runs it. The upgrade to 0005 happens manually, post-merge, only after the user confirms the production eval is green (spec §7).**

- [ ] **Step 1: Write the drop migration**

```python
# infra/migrations/versions/0005_drop_pdfs_pages.py
"""drop legacy pdfs/pages — GATED on the eval suite passing (spec §7)

Revision ID: 0005_drop_pdfs_pages
Revises: 0004_documents_chunks

This revision is deliberately NOT reached by scripts/start.sh (pinned to
0004_documents_chunks). Apply it manually, once the thoughts.md ground-truth
eval is green through documents/chunks in production:

    set -a && . ./.env && set +a
    uv run alembic -c alembic.ini upgrade head

A red eval leaves both schemas intact; downgrade() rebuilds pdfs/pages from
documents/chunks (ids are regenerated, content preserved).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import TSVECTOR

revision = "0005_drop_pdfs_pages"
down_revision = "0004_documents_chunks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_pages_embedding_hnsw", table_name="pages")
    op.drop_index("ix_pages_tsvector", table_name="pages")
    op.drop_index("ix_pages_pdf_id", table_name="pages")
    op.drop_table("pages")
    op.drop_index("ix_pdfs_sha256", table_name="pdfs")
    op.drop_table("pdfs")


def downgrade() -> None:
    op.execute("SET LOCAL timezone TO 'UTC'")

    op.create_table(
        "pdfs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("filename", sa.String(), nullable=False),
        sa.Column("sha256", sa.String(), nullable=False),
        sa.Column("r2_key", sa.String(), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("sha256", name="uq_pdfs_sha256"),
    )
    op.create_index("ix_pdfs_sha256", "pdfs", ["sha256"], unique=True)

    op.create_table(
        "pages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "pdf_id",
            sa.Integer(),
            sa.ForeignKey("pdfs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("page_no", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "tsvector",
            TSVECTOR(),
            sa.Computed("to_tsvector('english', text)", persisted=True),
            nullable=False,
        ),
        sa.Column("embedding", Vector(1024), nullable=False),
        sa.Column("png_r2_key", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("pdf_id", "page_no", name="uq_pages_pdf_page"),
    )

    # Repopulate from the unified store (pdf rows only). page_count uses
    # max(ordinal), not count(*): ordinal carries the original 1-based page_no,
    # so max(ordinal) reproduces the original page_count semantics even if a
    # page row were ever missing (count(*) would silently disagree).
    op.execute(
        """
        INSERT INTO pdfs (filename, sha256, r2_key, page_count, created_at)
        SELECT d.title, d.source_ref, d.source_url,
               (SELECT coalesce(max(c.ordinal), 0) FROM chunks c WHERE c.document_id = d.id),
               d.created_at
        FROM documents AS d
        WHERE d.source_type = 'pdf'
        ORDER BY d.id
        """
    )
    op.execute(
        """
        INSERT INTO pages (pdf_id, page_no, text, embedding, png_r2_key, created_at)
        SELECT f.id, c.ordinal, c.text, c.embedding, c.png_r2_key, c.created_at
        FROM chunks AS c
        JOIN documents AS d ON d.id = c.document_id AND d.source_type = 'pdf'
        JOIN pdfs AS f ON f.sha256 = d.source_ref
        """
    )

    op.create_index("ix_pages_pdf_id", "pages", ["pdf_id"])
    op.create_index("ix_pages_tsvector", "pages", ["tsvector"], postgresql_using="gin")
    op.create_index(
        "ix_pages_embedding_hnsw",
        "pages",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
```

- [ ] **Step 2: Pin `scripts/start.sh` to the pre-drop revision**

```sh
#!/usr/bin/env sh
# Container entrypoint for the Railway runtime.
# Lives in a real shell script (not the railway.toml startCommand) because
# Railway does NOT perform POSIX ${VAR:-default} expansion on startCommand —
# it would pass the literal string to uvicorn. Inside sh, expansion works.
set -e

# Pinned to the create+copy revision: 0005_drop_pdfs_pages is GATED on the
# production eval passing (spec §7) and must be applied manually. Restore to
# `upgrade head` after the drop has been applied.
echo "[start] running database migrations (alembic upgrade 0004_documents_chunks)..."
uv run --no-sync alembic -c alembic.ini upgrade 0004_documents_chunks
echo "[start] migrations complete; booting uvicorn on port ${PORT:-8080}"

exec uv run --no-sync uvicorn --factory parts_lookup.api.main:create_app \
  --host 0.0.0.0 --port "${PORT:-8080}"
```

- [ ] **Step 3: Lint + unit suite**

Run: `uv run --extra dev pytest tests/unit -q && uv run --extra dev ruff check`
Expected: PASS; ruff clean. Do NOT run `alembic upgrade` in this task.

- [ ] **Step 4: Commit**

```bash
git add infra/migrations/versions/0005_drop_pdfs_pages.py scripts/start.sh
git commit -m "feat(indexing): gated 0005 drop migration for pdfs/pages; pin start.sh to 0004 (refs #2)" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

- [ ] **Step 5: Post-merge rollout runbook (manual; requires explicit user go-ahead at each ⚠️)**

This step is documentation for after the PR lands (spec §10) — none of it runs during plan execution:

1. Merge the PR; Railway deploys; `start.sh` upgrades prod to `0004_documents_chunks` (a no-op — Task 1 already applied it) and boots the new code.
2. Smoke the deployed path:
   ```bash
   curl -s https://parts-api-production.up.railway.app/healthz
   curl -s -X POST https://parts-api-production.up.railway.app/v1/query \
     -H 'Content-Type: application/json' \
     -d '{"question": "What torque value applies to the component on page 28?"}' | python3 -m json.tool
   ```
   Expect `"source_type": "pdf"`, `"torque"` containing `40 N-m`, a `source_url` ending `#page=28`, and a non-null `screenshot_url`.
   ```bash
   curl -s -X POST https://parts-api-production.up.railway.app/v1/query \
     -H 'Content-Type: application/json' \
     -d '{"question": "In the SRAM Road AXS digital user manual, what torque do the crank arm bolts get?", "top_k": 5}' | python3 -m json.tool
   ```
   Expect `"source_type": "html"`, a `https://docs.sram.com/...#...` `source_url`, and `"screenshot_url": null`.
3. Re-run the eval gate against production: `set -a && . ./.env && set +a && uv run --extra dev pytest tests/eval -m eval -q` → **11 passed**.
4. ⚠️ **Only after the user confirms the eval is green:** apply the drop —
   ```bash
   set -a && . ./.env && set +a
   uv run alembic -c alembic.ini upgrade head
   ```
   Expected: `Running upgrade 0004_documents_chunks -> 0005_drop_pdfs_pages`.
5. Restore `scripts/start.sh` to `uv run --no-sync alembic -c alembic.ini upgrade head` (and the original echo line) in a small follow-up PR/commit.
6. Re-run step 2's curls once more after the next deploy to confirm nothing depended on the dropped tables.

---

## Self-review checklist (run before finishing)

- Spec §2.1 unified query → Tasks 4, 10. §2.2 unified storage + copy → Tasks 1, 3. §2.3 block chunks + small-to-big → Tasks 7, 10 (`fetch_module_text`). §2.4 extraction branches → Task 5. §2.5 API break → Task 10. §2.6 gate → Tasks 11, 13. §2.7 scope (3 pubs) → Tasks 9, 11. §4 ingestion flow + PDF refactor → Tasks 6–9. §7 ordering → Task 1 (copy) precedes Task 11 (eval) precedes Task 13 (drop, manual). §8 testing → Tasks 3, 4, 7, 8, 11. §10 rollout → Task 13 Step 5.
- No placeholders: every code step carries complete code; the single deliberate judgment call (real manual-data key names) is bounded by Task 7 Step 1's capture-and-verify checkpoint.
- Type consistency: `SourceType/IndexedDocument/HtmlChunk/ParsedPublication/RetrievedChunk/Answer.source_index/ExtractionCandidate(index, source_type, label, png_bytes, text)` and repo methods `upsert_document/get_document_by_source_ref/insert_chunk/delete_chunks/fetch_module_text` are used with identical names in Tasks 2–11.
