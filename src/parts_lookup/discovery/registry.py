"""publications-table ORM model + registry repository (discovery context)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, String, func, select
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from parts_lookup.domain.errors import DiscoveryError
from parts_lookup.domain.models import DiscoveredPublication, RegisteredPublication

# Share the declarative Base + metadata used by alembic's env.py.
from parts_lookup.indexing.repository import Base


class Publication(Base):
    __tablename__ = "publications"

    id: Mapped[int] = mapped_column(primary_key=True)
    pub_id: Mapped[str] = mapped_column(String, unique=True, index=True, nullable=False)
    pub_type: Mapped[str] = mapped_column(String, nullable=False, default="")
    title: Mapped[str] = mapped_column(String, nullable=False, default="")
    locale: Mapped[str] = mapped_column(String, nullable=False, default="")
    source_url: Mapped[str] = mapped_column(String, nullable=False)
    series: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    models: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    procedures: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    referenced_by_models: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, default=list
    )
    content_hash: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="discovered")
    discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


def _row_to_domain(row: Publication) -> RegisteredPublication:
    """Map an ORM row to the pure domain read model (ORM rows never escape)."""
    return RegisteredPublication(
        pub_id=row.pub_id,
        pub_type=row.pub_type,
        title=row.title,
        locale=row.locale,
        source_url=row.source_url,
        series=tuple(row.series),
        models=tuple(row.models),
        procedures=tuple(row.procedures),
        referenced_by_models=tuple(row.referenced_by_models),
        content_hash=row.content_hash,
        status=row.status,
        discovered_at=row.discovered_at,
        last_seen_at=row.last_seen_at,
    )


class PublicationRegistry:
    """Upsert/read access to the publications registry."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(
        self, pub: DiscoveredPublication, referenced_by_models: list[str]
    ) -> str:
        """Insert or update a publication. Returns 'inserted' | 'unchanged' | 'stale'."""
        row = await self._get_row(pub.pub_id)
        if row is None:
            row = Publication(
                pub_id=pub.pub_id,
                pub_type=pub.pub_type,
                title=pub.title,
                locale=pub.locale,
                source_url=pub.source_url,
                series=list(pub.series),
                models=list(pub.models),
                procedures=list(pub.procedures),
                referenced_by_models=sorted(set(referenced_by_models)),
                content_hash=pub.content_hash,
                status="discovered",
            )
            self._session.add(row)
            await self._session.flush()
            await self._session.refresh(row)
            return "inserted"

        row.last_seen_at = datetime.now(UTC)
        row.referenced_by_models = sorted(
            set(row.referenced_by_models) | set(referenced_by_models)
        )
        if row.content_hash != pub.content_hash:
            row.content_hash = pub.content_hash
            row.title = pub.title
            row.source_url = pub.source_url
            row.locale = pub.locale
            row.pub_type = pub.pub_type
            row.series = list(pub.series)
            row.models = list(pub.models)
            row.procedures = list(pub.procedures)
            row.status = "stale"
            await self._session.flush()
            return "stale"
        await self._session.flush()
        return "unchanged"

    async def get(self, pub_id: str) -> RegisteredPublication | None:
        row = await self._get_row(pub_id)
        return _row_to_domain(row) if row is not None else None

    async def set_status(self, pub_id: str, status: str) -> None:
        """Flip a publication's lifecycle status (e.g. 'discovered' → 'ingested')."""
        row = await self._get_row(pub_id)
        if row is None:
            raise DiscoveryError(f"unknown publication: {pub_id}")
        row.status = status
        await self._session.flush()

    async def list_all(self) -> list[RegisteredPublication]:
        result = await self._session.execute(select(Publication).order_by(Publication.id))
        return [_row_to_domain(row) for row in result.scalars().all()]

    async def _get_row(self, pub_id: str) -> Publication | None:
        result = await self._session.execute(
            select(Publication).where(Publication.pub_id == pub_id)
        )
        return result.scalar_one_or_none()
