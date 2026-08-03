"""Mock commerce data the agent reads and (post-approval) writes."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Numeric, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, uuid_pk
from app.models.enums import ActionStatus, ActionType, OrderStatus


class Order(Base, TimestampMixin):
    __tablename__ = "orders"

    id: Mapped[uuid.UUID] = uuid_pk()
    order_ref: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    customer_email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)

    status: Mapped[OrderStatus] = mapped_column(
        SAEnum(OrderStatus, native_enum=False, length=32, name="order_status"),
        default=OrderStatus.PLACED,
        nullable=False,
    )
    items: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, nullable=False)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)

    placed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tracking_number: Mapped[str | None] = mapped_column(String(64))

    refunded_amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("0.00"), nullable=False
    )
    # Final-sale items are refund-exempt regardless of the return window.
    is_final_sale: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    actions: Mapped[list["OrderAction"]] = relationship(
        back_populates="order", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def refundable_amount(self) -> Decimal:
        return self.total_amount - self.refunded_amount


class OrderAction(Base):
    """Audit trail of refunds/replacements actually executed against an order."""

    __tablename__ = "order_actions"

    id: Mapped[uuid.UUID] = uuid_pk()
    order_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ticket_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tickets.id", ondelete="SET NULL"), index=True
    )

    action_type: Mapped[ActionType] = mapped_column(
        SAEnum(ActionType, native_enum=False, length=32, name="action_type"), nullable=False
    )
    status: Mapped[ActionStatus] = mapped_column(
        SAEnum(ActionStatus, native_enum=False, length=32, name="action_status"),
        default=ActionStatus.EXECUTED,
        nullable=False,
    )
    amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    reason: Mapped[str | None] = mapped_column(Text)
    approved_by: Mapped[str | None] = mapped_column(String(200))
    # Guards against a double-approval replaying the same refund.
    idempotency_key: Mapped[str | None] = mapped_column(String(120), unique=True, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    order: Mapped[Order] = relationship(back_populates="actions")
