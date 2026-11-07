"""Declarative base and reusable mixins (timestamps, soft delete)."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, MetaData
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utcnow() -> datetime:
    """Naive UTC datetime, truncated to whole seconds.

    DATETIME columns have second precision and MySQL *rounds* fractions, which could store a value
    up to half a second in the future; truncating keeps comparisons with "now" exact.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(), default=utcnow, onupdate=utcnow, nullable=False)


class SoftDeleteMixin:
    """Rows are never physically removed; `deleted_at IS NULL` means active."""

    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True, index=True)

    @declared_attr
    def deleted_by_id(cls) -> Mapped[int | None]:  # noqa: N805
        return mapped_column(Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True)

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    def soft_delete(self, by_user_id: int | None) -> None:
        self.deleted_at = utcnow()
        self.deleted_by_id = by_user_id

    def restore(self) -> None:
        self.deleted_at = None
        self.deleted_by_id = None
