import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPkMixin


class User(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "users"

    handle: Mapped[str] = mapped_column(sa.String(64), unique=True)
    display_name: Mapped[str] = mapped_column(sa.String(120))
    email: Mapped[str | None] = mapped_column(sa.String(255), unique=True)
    is_demo: Mapped[bool] = mapped_column(default=False)
