
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class IntelligenceLink(Base):
    __tablename__ = "company_intelligence_links"

    company_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("companies.id", ondelete="CASCADE"),
        primary_key=True,
    )

    crawler_company_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
    )

    crawler_job_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
    )
