"""Seed the database: policy corpus + mock orders.

    python scripts/seed_db.py [--reset]

The order set is chosen so every branch of the eligibility engine has a
reachable fixture: inside/outside the refund window, final sale, undelivered,
above the auto-approval cap, partially refunded, and so on. The eval set and
the demo script both reference these order refs by name.
"""

import argparse
import asyncio
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import delete, select  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.db import session_scope  # noqa: E402
from app.core.logging import configure_logging, get_logger  # noqa: E402
from app.models.enums import ActionStatus, ActionType, OrderStatus  # noqa: E402
from app.models.order import Order, OrderAction  # noqa: E402
from app.rag.ingest import ingest_policies  # noqa: E402

logger = get_logger("seed")

NOW = datetime.now(UTC)


def _days_ago(days: int) -> datetime:
    return NOW - timedelta(days=days)


# Each entry documents the case it exists to exercise.
SEED_ORDERS: list[dict] = [
    {
        # Happy path: comfortably inside the 30-day refund window.
        "order_ref": "ORD-1001",
        "customer_email": "maria.lopez@example.com",
        "status": OrderStatus.DELIVERED,
        "items": [
            {"sku": "NW-KTL-01", "name": "Stovetop Kettle, 1.5L", "quantity": 1,
             "unit_price": "89.99"}
        ],
        "total_amount": Decimal("89.99"),
        "placed_at": _days_ago(12),
        "delivered_at": _days_ago(5),
        "tracking_number": "NW7742110045",
    },
    {
        # Outside the 30-day window: refund must be declined and escalated.
        "order_ref": "ORD-1002",
        "customer_email": "james.okoro@example.com",
        "status": OrderStatus.DELIVERED,
        "items": [
            {"sku": "NW-RUG-14", "name": "Wool Runner Rug 2x6", "quantity": 1,
             "unit_price": "129.50"}
        ],
        "total_amount": Decimal("129.50"),
        "placed_at": _days_ago(52),
        "delivered_at": _days_ago(45),
        "tracking_number": "NW7742118820",
    },
    {
        # Final sale: not refundable, but replaceable if it arrived damaged.
        "order_ref": "ORD-1003",
        "customer_email": "sarah.chen@example.com",
        "status": OrderStatus.DELIVERED,
        "items": [
            {"sku": "NW-LMP-07", "name": "Ceramic Table Lamp (clearance)", "quantity": 1,
             "unit_price": "59.00"}
        ],
        "total_amount": Decimal("59.00"),
        "placed_at": _days_ago(16),
        "delivered_at": _days_ago(10),
        "tracking_number": "NW7742120031",
        "is_final_sale": True,
    },
    {
        # Shipped but not delivered: refund window has not started.
        "order_ref": "ORD-1004",
        "customer_email": "daniel.kim@example.com",
        "status": OrderStatus.SHIPPED,
        "items": [
            {"sku": "NW-CHR-22", "name": "Oak Dining Chair", "quantity": 2,
             "unit_price": "105.00"}
        ],
        "total_amount": Decimal("210.00"),
        "placed_at": _days_ago(4),
        "delivered_at": None,
        "tracking_number": "NW7742130987",
    },
    {
        # Above the $500 auto-approval cap: eligible, but must escalate.
        "order_ref": "ORD-1005",
        "customer_email": "priya.nair@example.com",
        "status": OrderStatus.DELIVERED,
        "items": [
            {"sku": "NW-SFA-03", "name": "Linen Two-Seat Sofa", "quantity": 1,
             "unit_price": "1250.00"}
        ],
        "total_amount": Decimal("1250.00"),
        "placed_at": _days_ago(9),
        "delivered_at": _days_ago(3),
        "tracking_number": "NW7742131442",
    },
    {
        # Partially refunded already: only the remaining balance is refundable.
        "order_ref": "ORD-1006",
        "customer_email": "maria.lopez@example.com",
        "status": OrderStatus.DELIVERED,
        "items": [
            {"sku": "NW-TWL-09", "name": "Cotton Bath Towel Set", "quantity": 3,
             "unit_price": "25.00"}
        ],
        "total_amount": Decimal("75.00"),
        "refunded_amount": Decimal("30.00"),
        "placed_at": _days_ago(26),
        "delivered_at": _days_ago(20),
        "tracking_number": "NW7742109003",
    },
    {
        # Not yet shipped: cancellable for a full refund.
        "order_ref": "ORD-1007",
        "customer_email": "tom.becker@example.com",
        "status": OrderStatus.PLACED,
        "items": [
            {"sku": "NW-MUG-02", "name": "Speckled Stoneware Mug", "quantity": 4,
             "unit_price": "11.25"}
        ],
        "total_amount": Decimal("45.00"),
        "placed_at": _days_ago(1),
        "delivered_at": None,
    },
    {
        # Electronics past both windows: warranty territory.
        "order_ref": "ORD-1008",
        "customer_email": "aisha.rahman@example.com",
        "status": OrderStatus.DELIVERED,
        "items": [
            {"sku": "NW-HDP-11", "name": "Over-Ear Headphones", "quantity": 1,
             "unit_price": "299.00"}
        ],
        "total_amount": Decimal("299.00"),
        "placed_at": _days_ago(70),
        "delivered_at": _days_ago(62),
        "tracking_number": "NW7742090012",
    },
    {
        # Shipped 15 days ago, tracking stale: lost-package case.
        "order_ref": "ORD-1009",
        "customer_email": "luis.fernandez@example.com",
        "status": OrderStatus.SHIPPED,
        "items": [
            {"sku": "NW-BLK-05", "name": "Merino Throw Blanket", "quantity": 1,
             "unit_price": "64.99"}
        ],
        "total_amount": Decimal("64.99"),
        "placed_at": _days_ago(17),
        "delivered_at": None,
        "tracking_number": "NW7742101777",
    },
    {
        # Delivered 2 days ago: inside the 7-day damaged-on-arrival window.
        "order_ref": "ORD-1010",
        "customer_email": "sarah.chen@example.com",
        "status": OrderStatus.DELIVERED,
        "items": [
            {"sku": "NW-VSE-08", "name": "Hand-Blown Glass Vase", "quantity": 1,
             "unit_price": "180.00"}
        ],
        "total_amount": Decimal("180.00"),
        "placed_at": _days_ago(7),
        "delivered_at": _days_ago(2),
        "tracking_number": "NW7742140556",
    },
    {
        # Already had one replacement (see SEED_ACTIONS): a further request must
        # escalate. Kept separate from ORD-1010 so the first-replacement case
        # still has an order with a clean history.
        "order_ref": "ORD-1011",
        "customer_email": "sarah.chen@example.com",
        "status": OrderStatus.DELIVERED,
        "items": [
            {"sku": "NW-VSE-08", "name": "Hand-Blown Glass Vase", "quantity": 1,
             "unit_price": "180.00"}
        ],
        "total_amount": Decimal("180.00"),
        "placed_at": _days_ago(12),
        "delivered_at": _days_ago(6),
        "tracking_number": "NW7742140998",
    },
]


# Replacements already issued, so the "second replacement" path has a fixture.
SEED_ACTIONS: list[dict] = [
    {
        "order_ref": "ORD-1011",
        "action_type": ActionType.REPLACEMENT,
        "reason": "Vase arrived cracked; first replacement issued",
        "approved_by": "seed@northwind.test",
        "days_ago": 1,
    },
]


async def seed_actions(db) -> int:
    created = 0
    for spec in SEED_ACTIONS:
        order = (
            await db.execute(select(Order).where(Order.order_ref == spec["order_ref"]))
        ).scalar_one_or_none()
        if order is None:
            continue
        key = f"seed:{spec['order_ref']}:{spec['action_type'].value}"
        exists = (
            await db.execute(select(OrderAction).where(OrderAction.idempotency_key == key))
        ).scalar_one_or_none()
        if exists:
            continue
        db.add(
            OrderAction(
                order_id=order.id,
                action_type=spec["action_type"],
                status=ActionStatus.EXECUTED,
                reason=spec["reason"],
                approved_by=spec["approved_by"],
                idempotency_key=key,
                created_at=_days_ago(spec["days_ago"]),
            )
        )
        created += 1
    await db.flush()
    return created


async def seed_orders(db, reset: bool) -> tuple[int, int]:
    if reset:
        await db.execute(delete(OrderAction))
        await db.execute(delete(Order))
        logger.info("orders_cleared")

    created = skipped = 0
    for spec in SEED_ORDERS:
        existing = (
            await db.execute(select(Order).where(Order.order_ref == spec["order_ref"]))
        ).scalar_one_or_none()
        if existing:
            skipped += 1
            continue

        db.add(
            Order(
                order_ref=spec["order_ref"],
                customer_email=spec["customer_email"],
                status=spec["status"],
                items=spec["items"],
                total_amount=spec["total_amount"],
                currency="USD",
                placed_at=spec["placed_at"],
                delivered_at=spec.get("delivered_at"),
                tracking_number=spec.get("tracking_number"),
                refunded_amount=spec.get("refunded_amount", Decimal("0.00")),
                is_final_sale=spec.get("is_final_sale", False),
            )
        )
        created += 1

    await db.flush()
    return created, skipped


async def main(reset: bool) -> int:
    configure_logging()
    settings = get_settings()

    # Create the LangGraph checkpoint tables here, while nothing else holds a
    # transaction. Its CREATE INDEX CONCURRENTLY waits on every open
    # transaction, so doing it during a request would hang.
    from app.agents.graph import ensure_checkpointer_schema

    await ensure_checkpointer_schema()

    async with session_scope() as db:
        report = await ingest_policies(db, settings.policies_dir, force=reset)
        created, skipped = await seed_orders(db, reset)
        actions = await seed_actions(db)

    print(f"Policies: {report.summary()}")
    print(f"Orders:   created={created} skipped={skipped}")
    print(f"Actions:  created={actions}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reset", action="store_true", help="delete existing orders and re-embed all policies"
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.reset)))
