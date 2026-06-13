"""One-off FAIL-SAFE backfill of the #28 ``product_family`` / ``brand`` facet.

Re-derives the product facet for EXISTING documents from their ``title`` using
the shared deterministic ``retrieval.product_match.derive_facet`` — the SAME
normalizer ingest uses, so the backfilled column and freshly-ingested rows agree.

FAIL-SAFE by construction (reviewer-required): ``derive_facet`` only emits a
``product_family`` when a product is CONFIDENTLY identified; a typo'd / generic /
unrecognizable title yields ``None`` rather than a guess. A wrong stored family
would mis-boost retrieval (#28) and trip the contamination guard (#32) on correct
answers, so "don't know → NULL" is the deliberate default.

Reads the live store ONLY through ``Repository`` (SQLAlchemy), respecting the
DDD boundary (CLAUDE.md) — same pattern as ``tests/eval/mine_specs.py``. It needs
``DATABASE_URL`` but makes no paid Anthropic/Voyage calls.

Two-phase, by design — the backfill is a PROPOSAL a human signs off on, not an
authority:

    # 1. AUDIT (default): print every (title -> family/brand) derivation +
    #    summary; writes NOTHING. Review the family / brand / NULL split.
    set -a && . ./.env && set +a
    uv run python -m scripts.backfill_product_family

    # 2. APPLY: after reviewing the audit, write the facet to the DB.
    uv run python -m scripts.backfill_product_family --apply

Idempotent: re-running derives the same values, so it is safe to re-run. The
migration 0006 must have been applied first (the columns must exist).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from parts_lookup.retrieval.product_match import derive_facet


def _database_url() -> str:
    url = os.environ["DATABASE_URL"]
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


async def backfill(apply: bool) -> int:
    """Derive + (optionally) write the facet for every document. Returns rc."""
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from parts_lookup.indexing.repository import Repository

    engine = create_async_engine(_database_url(), echo=False)
    with_family = brand_only = null_both = 0
    try:
        async with AsyncSession(engine) as session:
            repo = Repository(session)
            documents = await repo.list_documents()
            for doc in documents:
                family, brand = derive_facet(doc.title)
                if family is not None:
                    with_family += 1
                    tag = f"family={family} brand={brand}"
                elif brand is not None:
                    brand_only += 1
                    tag = f"BRAND-ONLY brand={brand}"
                else:
                    null_both += 1
                    tag = "NULL (product-blind, fail-safe)"
                print(f"[{doc.source_type.value:4}] {doc.title:70} -> {tag}")
                if apply:
                    await repo.set_product_facet(doc.id, family, brand)
            if apply:
                await session.commit()
            else:
                await session.rollback()
    finally:
        await engine.dispose()

    total = with_family + brand_only + null_both
    verb = "WROTE" if apply else "would write (dry-run; nothing written)"
    print(
        f"\n# {verb}: family={with_family} brand_only={brand_only} "
        f"null={null_both} total={total}",
        file=sys.stderr,
    )
    if not apply:
        print(
            "# audit only — re-run with --apply to write after reviewing the table above",
            file=sys.stderr,
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="backfill_product_family")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the derived facet to the DB (default: audit/dry-run only).",
    )
    args = parser.parse_args(argv)
    return asyncio.run(backfill(apply=args.apply))


if __name__ == "__main__":
    raise SystemExit(main())
