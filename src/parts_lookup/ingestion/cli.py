"""Command-line entry point for ingestion.

Exposed via ``[project.scripts] parts-lookup = "parts_lookup.ingestion.cli:main"``.
Subcommands:

* ``parts-lookup ingest <pdf_path>`` — ingest a single PDF.
* ``parts-lookup ingest-dir <dir>``  — ingest every ``*.pdf`` in a directory
  (non-recursive; sort by filename for deterministic order).
* ``parts-lookup ingest-html [pub_id ...]`` — ingest HTML publications from
  the discovery registry (omit ids to ingest every 'discovered'/'stale' one).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

from parts_lookup.assets.r2_client import R2Client
from parts_lookup.config import Settings, get_settings
from parts_lookup.discovery.fetcher import Fetcher
from parts_lookup.discovery.registry import PublicationRegistry
from parts_lookup.domain.errors import IngestionError
from parts_lookup.indexing.repository import Repository
from parts_lookup.indexing.session import async_session_factory
from parts_lookup.ingestion.html_pipeline import HtmlIngestionPipeline
from parts_lookup.ingestion.pipeline import IngestionPipeline
from parts_lookup.retrieval.embedder import VoyageEmbedder


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "ingest":
        return asyncio.run(_run_ingest_one(Path(args.pdf_path)))
    if args.command == "ingest-dir":
        return asyncio.run(_run_ingest_dir(Path(args.directory)))
    if args.command == "ingest-html":
        return asyncio.run(_run_ingest_html(list(args.pub_ids)))

    parser.print_help()
    return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="parts-lookup")
    sub = parser.add_subparsers(dest="command", required=False)

    ingest = sub.add_parser("ingest", help="Ingest one PDF.")
    ingest.add_argument("pdf_path", help="Path to a PDF file.")

    ingest_dir = sub.add_parser("ingest-dir", help="Ingest every *.pdf in DIR.")
    ingest_dir.add_argument("directory", help="Directory containing PDFs.")

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
    for pdf in pdfs:
        # Fresh scope (and DB session) per PDF. Bounding a connection's
        # lifetime to a single PDF keeps a long batch robust against a
        # dropped connection (e.g. a managed-Postgres proxy idle/lifetime
        # cap over a multi-hour run) — a drop now fails one PDF, not the
        # whole batch. pool_pre_ping revalidates the pooled connection on
        # each checkout. SHA-256 dedup makes re-runs skip finished PDFs.
        try:
            async with _PipelineScope(settings) as scope:
                pdf_rc = await _ingest_one(scope.pipeline, pdf)
                if pdf_rc == 0:
                    await scope.commit()
                else:
                    await scope.rollback()
                rc = max(rc, pdf_rc)
        except Exception as exc:
            print(
                f"FAIL {pdf}: unexpected {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            rc = 1
    return rc


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


async def _ingest_one(pipeline: IngestionPipeline, pdf_path: Path) -> int:
    try:
        doc = await pipeline.ingest(pdf_path)
    except IngestionError as exc:
        cause = f" (cause: {exc.__cause__!r})" if exc.__cause__ else ""
        print(f"FAIL {pdf_path}: {exc}{cause}", file=sys.stderr)
        return 1
    print(f"OK   {pdf_path} -> document id={doc.id} ref={doc.source_ref[:12]}…")
    return 0


class _PipelineScope:
    """Transactional scope that owns one DB session for its lifetime.

    Used once per PDF (a fresh scope per file in batch runs), so a session's
    connection lives only as long as a single ingest. The caller commits on
    success / rolls back on failure, so a finished PDF is durable
    immediately and a later failure can't undo it. Re-running resumes via
    SHA-256 dedup as designed.
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
