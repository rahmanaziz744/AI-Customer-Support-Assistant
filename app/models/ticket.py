"""Support ticket."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Float, Integer, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, uuid_pk
from app.models.enums import Channel, Sentiment, TicketCategory, TicketStatus


def _enum(enum_cls: type, name: str) -> SAEnum:
    return SAEnum(enum_cls, native_enum=False, length=32, validate_strings=True, name=name)


class Ticket(Base, TimestampMixin):
    __tablename__ = "tickets"

    id: Mapped[uuid.UUID] = uuid_pk()

    channel: Mapped[Channel] = mapped_column(
        _enum(Channel, "channel"), default=Channel.EMAIL, nullable=False
    )
    customer_email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    customer_name: Mapped[str | None] = mapped_column(String(200))
    subject: Mapped[str] = mapped_column(String(500), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    order_ref: Mapped[str | None] = mapped_column(String(64), index=True)

    status: Mapped[TicketStatus] = mapped_column(
        _enum(TicketStatus, "ticket_status"),
        default=TicketStatus.NEW,
        nullable=False,
        index=True,
    )

    # Classification results are flattened into columns so the queue can filter
    # and sort on them without unpacking JSON on every row.
    category: Mapped[TicketCategory | None] = mapped_column(
        _enum(TicketCategory, "ticket_category"), index=True
    )
    sentiment: Mapped[Sentiment | None] = mapped_column(_enum(Sentiment, "sentiment"))
    priority: Mapped[int | None] = mapped_column(Integer, index=True)
    confidence: Mapped[float | None] = mapped_column(Float)
    classification_meta: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, nullable=False
    )

    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    runs: Mapped[list["AgentRun"]] = relationship(  # noqa: F821
        back_populates="ticket",
        cascade="all, delete-orphan",
        order_by="AgentRun.created_at",
        lazy="selectin",
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Ticket {self.id} {self.status} {self.subject[:40]!r}>"
