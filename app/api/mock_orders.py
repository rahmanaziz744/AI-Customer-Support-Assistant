"""Mock order/fulfilment system.

Stands in for the commerce backend a real deployment would call. It enforces
only what a payment system would enforce — the order exists, the money is
available, the same request is not applied twice — and deliberately does *not*
enforce company policy. Policy lives in `app.agents.eligibility`, so a bug there
cannot hide behind the API silently doing the right thing.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.models.enums import ActionStatus, ActionType, OrderStatus
from app.models.order import Order, OrderAction
from app.schemas.order import ActionResult, OrderRead, RefundRequest, ReplacementRequest

logger = get_logger(__name__)

router = APIRouter(prefix="/mock-api/orders", tags=["mock order api"])


async def _get_order(db: AsyncSession, order_ref: str) -> Order:
    order = (
        await db.execute(select(Order).where(Order.order_ref == order_ref.upper()))
    ).scalar_one_or_none()
    if order is None:
        raise NotFoundError(f"Order {order_ref} not found", detail={"order_ref": order_ref})
    return order


async def _find_replay(db: AsyncSession, key: str | None) -> OrderAction | None:
    if not key:
        return None
    return (
        await db.execute(select(OrderAction).where(OrderAction.idempotency_key == key))
    ).scalar_one_or_none()


def _to_result(action: OrderAction, order_ref: str, *, replayed: bool = False) -> ActionResult:
    return ActionResult(
        action_id=str(action.id),
        order_ref=order_ref,
        action_type=action.action_type,
        amount=action.amount,
        status=action.status.value,
        reason=action.reason,
        created_at=action.created_at,
        replayed=replayed,
    )


@router.get("", response_model=list[OrderRead])
async def list_orders(
    customer_email: str | None = Query(default=None),
    limit: int = Query(default=50, le=200),
    db: AsyncSession = Depends(get_db),
) -> list[Order]:
    stmt = select(Order).order_by(Order.placed_at.desc()).limit(limit)
    if customer_email:
        stmt = stmt.where(Order.customer_email == customer_email.lower())
    return list((await db.execute(stmt)).scalars().all())


@router.get("/{order_ref}", response_model=OrderRead)
async def get_order(order_ref: str, db: AsyncSession = Depends(get_db)) -> Order:
    return await _get_order(db, order_ref)


@router.post("/{order_ref}/refund", response_model=ActionResult)
async def refund_order(
    order_ref: str, payload: RefundRequest, db: AsyncSession = Depends(get_db)
) -> ActionResult:
    """Issue a refund. Enforces financial invariants only, not company policy."""
    if replay := await _find_replay(db, payload.idempotency_key):
        logger.info("refund_replayed", order_ref=order_ref, key=payload.idempotency_key)
        return _to_result(replay, order_ref, replayed=True)

    order = await _get_order(db, order_ref)

    if order.status == OrderStatus.CANCELLED:
        raise ConflictError(
            f"Order {order.order_ref} is cancelled and was already refunded at cancellation"
        )

    amount = Decimal(payload.amount).quantize(Decimal("0.01"))
    if amount > order.refundable_amount:
        raise ValidationError(
            "Refund exceeds the remaining refundable balance",
            detail={
                "requested": str(amount),
                "refundable": str(order.refundable_amount),
                "order_total": str(order.total_amount),
                "already_refunded": str(order.refunded_amount),
            },
        )

    action = OrderAction(
        order_id=order.id,
        ticket_id=uuid.UUID(payload.ticket_id) if payload.ticket_id else None,
        action_type=ActionType.REFUND,
        status=ActionStatus.EXECUTED,
        amount=amount,
        reason=payload.reason,
        approved_by=payload.approved_by,
        idempotency_key=payload.idempotency_key,
        created_at=datetime.now(UTC),
    )
    order.refunded_amount = order.refunded_amount + amount
    if order.refunded_amount >= order.total_amount:
        order.status = OrderStatus.RETURNED

    db.add(action)
    await db.commit()
    await db.refresh(action)

    logger.info(
        "refund_executed",
        order_ref=order.order_ref,
        amount=str(amount),
        approved_by=payload.approved_by,
    )
    return _to_result(action, order.order_ref)


@router.post("/{order_ref}/replacement", response_model=ActionResult)
async def replace_order(
    order_ref: str, payload: ReplacementRequest, db: AsyncSession = Depends(get_db)
) -> ActionResult:
    if replay := await _find_replay(db, payload.idempotency_key):
        logger.info("replacement_replayed", order_ref=order_ref, key=payload.idempotency_key)
        return _to_result(replay, order_ref, replayed=True)

    order = await _get_order(db, order_ref)

    if order.status == OrderStatus.CANCELLED:
        raise ConflictError(f"Order {order.order_ref} is cancelled; nothing to replace")

    action = OrderAction(
        order_id=order.id,
        ticket_id=uuid.UUID(payload.ticket_id) if payload.ticket_id else None,
        action_type=ActionType.REPLACEMENT,
        status=ActionStatus.EXECUTED,
        reason=payload.reason,
        approved_by=payload.approved_by,
        idempotency_key=payload.idempotency_key,
        created_at=datetime.now(UTC),
    )
    db.add(action)
    await db.commit()
    await db.refresh(action)

    logger.info(
        "replacement_executed", order_ref=order.order_ref, approved_by=payload.approved_by
    )
    return _to_result(action, order.order_ref)


@router.get("/{order_ref}/actions", response_model=list[ActionResult])
async def list_actions(order_ref: str, db: AsyncSession = Depends(get_db)) -> list[ActionResult]:
    order = await _get_order(db, order_ref)
    actions = (
        await db.execute(
            select(OrderAction)
            .where(OrderAction.order_id == order.id)
            .order_by(OrderAction.created_at)
        )
    ).scalars().all()
    return [_to_result(a, order.order_ref) for a in actions]
