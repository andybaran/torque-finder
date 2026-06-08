"""Command-line entry point for ingestion.

Exposed via ``[project.scripts] parts-lookup = "parts_lookup.ingestion.cli:main"``.
Subcommands:

* ``parts-lookup ingest <pdf_path>`` — ingest a single PDF.
* ``parts-lookup ingest-dir <dir>``  — ingest every ``*.pdf`` in a directory
  (non-recursive; sort by filename for deterministic order).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

from parts_lookup.assets.r2_client import R2Client
from parts_lookup.config import Settings, get_settings
from parts_lookup.domain.errors import IngestionError
from parts_lookup.indexing.repository import Repository
from parts_lookup.indexing.session import async_session_factory
from parts_lookup.ingestion.pipeline import IngestionPipeline
from parts_lookup.retrieval.embedder import VoyageEmbedder


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "ingest":
        return asyncio.run(_run_ingest_one(Path(args.pdf_path)))
    if args.command == "ingest-dir":
        return asyncio.run(_run_ingest_dir(Path(args.directory)))

    parser.print_help()
    return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="parts-lookup")
    sub = parser.add_subparsers(dest="command", required=False)

    ingest = sub.add_parser("ingest", help="Ingest one PDF.")
    ingest.add_argument("pdf_path", help="Path to a PDF file.")

    ingest_dir = sub.add_parser("ingest-dir", help="Ingest every *.pdf in DIR.")
    ingest_dir.add_argument("directory", help="Directory containing PDFs.")

    return parser


async def _run_ingest_one(pdf_path: Path) -> int:
    settings = get_settings()
    async with _PipelineScope(settings) as scope:
        rc = await _ingest_one(scope.pipeline, pdf_path)
        if rc == 0:
            await scope.commit()
        else:
            await scope.rollback()
        return rc


async def _run_ingest_dir(directory: Path) -> int:
    if not directory.is_dir():
        print(f"error: not a directory: {directory}", file=sys.stderr)
        return 2

    pdfs = sorted(p for p in directory.glob("*.pdf") if p.is_file())
    if not pdfs:
        print(f"warning: no PDFs found in {directory}", file=sys.stderr)
        return 0

    settings = get_settings()
    rc = 0
    async with _PipelineScope(settings) as scope:
        for pdf in pdfs:
            pdf_rc = await _ingest_one(scope.pipeline, pdf)
            # Commit per-PDF so a later crash can't roll back earlier
            # successes; rollback on per-PDF failure so a half-flushed Pdf
            # row doesn't poison the next iteration.
            if pdf_rc == 0:
                await scope.commit()
            else:
                await scope.rollback()
            rc = max(rc, pdf_rc)
    return rc


async def _ingest_one(pipeline: IngestionPipeline, pdf_path: Path) -> int:
    try:
        doc = await pipeline.ingest(pdf_path)
    except IngestionError as exc:
        cause = f" (cause: {exc.__cause__!r})" if exc.__cause__ else ""
        print(f"FAIL {pdf_path}: {exc}{cause}", file=sys.stderr)
        return 1
    print(f"OK   {pdf_path} -> id={doc.id} sha256={doc.sha256[:12]}… pages={doc.page_count}")
    return 0


class _PipelineScope:
    """Per-PDF transactional scope that owns one session for the whole batch.

    The session lives for the duration of the ``async with`` block (so
    multi-PDF runs share a connection), but the *caller* commits or rolls
    back after each PDF. That makes successful ingestions durable as soon
    as they finish — a native crash on a later PDF can no longer take
    previous successes with it. Re-running on the same directory then
    resumes via SHA-256 dedup as designed.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._session_cm = None  # type: ignore[assignment]
        self._session = None  # type: ignore[assignment]
        self._pipeline: IngestionPipeline | None = None

    @property
    def pipeline(self) -> IngestionPipeline:
        assert self._pipeline is not None, "_PipelineScope used outside async with"
        return self._pipeline

    async def commit(self) -> None:
        await self._session.commit()  # type: ignore[union-attr]

    async def rollback(self) -> None:
        await self._session.rollback()  # type: ignore[union-attr]

    async def __aenter__(self) -> _PipelineScope:
        factory = async_session_factory(self._settings)
        self._session_cm = factory()
        self._session = await self._session_cm.__aenter__()
        repo = Repository(self._session)
        embedder = VoyageEmbedder(self._settings)
        r2 = R2Client(self._settings)
        self._pipeline = IngestionPipeline(
            repository=repo, r2_client=r2, embedder=embedder
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        try:
            if exc_type is not None:
                # Outer-scope failure (not a per-PDF one) — drop any
                # uncommitted work on the way out.
                await self._session.rollback()  # type: ignore[union-attr]
        finally:
            await self._session_cm.__aexit__(exc_type, exc, tb)  # type: ignore[union-attr]


if __name__ == "__main__":
    raise SystemExit(main())
