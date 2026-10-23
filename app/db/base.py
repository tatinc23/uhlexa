"""Declarative base, shared column types and mixins. Everything here is portable across Postgres and SQLite."""

import enum
import uuid
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import MetaData
from sqlalchemy.ext.mutable import MutableDict, MutableList
from sqlalchemy.orm import DeclarativeBase, Mapped, MappedColumn, mapped_column
from sqlalchemy.types import TypeDecorator

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """Timezone-aware UTC datetimes on every backend (SQLite drops tzinfo; this puts it back). Naive input is a bug."""

    impl = sa.DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: sa.Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime: use timezone-aware UTC (app.db.base.utcnow)")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: sa.Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


# One shared instance per type so every column using it gets in-place mutation tracking
# (`task.meta["items"] = [...]` is persisted). Nested mutation is still not tracked: reassign the value.
JSONDict = MutableDict.as_mutable(sa.JSON())
JSONList = MutableList.as_mutable(sa.JSON())


def enum_type(enum_class: type[enum.StrEnum]) -> sa.Enum:
    """Enum stored as its string *values* in a VARCHAR(32) (no native enum, no CHECK); bad strings raise LookupError."""
    return sa.Enum(
        enum_class,
        native_enum=False,
        create_constraint=False,
        length=32,
        values_callable=lambda cls: [member.value for member in cls],
        validate_strings=True,
    )


def fk(target: str, *, ondelete: str | None = None, index: bool = False) -> MappedColumn[Any]:
    """Foreign key column; nullability comes from the `Mapped[...]` annotation."""
    return mapped_column(sa.ForeignKey(target, ondelete=ondelete), index=index)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class UUIDPkMixin:
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, server_default=sa.func.now())
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow, server_default=sa.func.now()
    )
