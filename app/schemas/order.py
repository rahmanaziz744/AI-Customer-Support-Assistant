"""DTOs for the mock order/fulfilment system."""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import ActionType, OrderStatus


class OrderItem(BaseModel):
    sku: str
    name: str
    quantity: int = 1
    unit_price: Decimal


class OrderRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    order_ref: str
    customer_email: str
    status: OrderStatus
    items: list[OrderItem]
    total_amount: Decimal
    currency: str
    placed_at: datetime
    delivered_at: datetime | None
    tracking_number: str | None
    refunded_amount: Decimal
    is_final_sale: bool

    @property
    def refundable_amount(self) -> Decimal:
        return self.total_amount - self.refunded_amount


class RefundRequest(BaseModel):
    amount: Decimal = Field(gt=0, description="Refund amount in the order currency")
    reason: str = Field(min_length=1, max_length=1000)
    ticket_id: str | None = None
    approved_by: str | None = None
    # Replaying the same key returns the original action instead of double-refunding.
    idempotency_key: str | None = Field(default=None, max_length=120)


class ReplacementRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)
    ticket_id: str | None = None
    approved_by: str | None = None
    idempotency_key: str | None = Field(default=None, max_length=120)


class ActionResult(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    action_id: str
    order_ref: str
    action_type: ActionType
    amount: Decimal | None
    status: str
    reason: str | None
    created_at: datetime
    # True when an existing action was returned rather than a new one created.
    replayed: bool = False
