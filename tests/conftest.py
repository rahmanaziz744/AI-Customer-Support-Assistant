"""Shared fixtures.

Unit tests need no database and no API key. The API and graph tests use the
real Postgres from docker-compose, wrapped so each test starts from a known
state; they skip cleanly when the database is not running, which keeps
`pytest` useful on a laptop with nothing started.
"""

import asyncio
import os
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

# Offline embeddings so no test downloads the ONNX model.
os.environ.setdefault("EMBEDDING_PROVIDER", "hash")
# The hash embedder is lexical, not semantic, so its similarity scores carry no
# meaning and would trip the retrieval-quality gate on every ticket. That gate
# is covered directly in test_guardrails.py; here it is switched off so the
# integration tests exercise the paths after retrieval.
os.environ.setdefault("ESCALATION_RETRIEVAL_THRESHOLD", "0.0")

import app.core.runtime  # noqa: F401,E402  (event loop policy, must precede any loop)
from app.core.db import SessionLocal  # noqa: E402
from app.models.enums import Channel, OrderStatus  # noqa: E402
from app.models.order import Order  # noqa: E402
from app.models.ticket import Ticket  # noqa: E402
from tests.fakes import restore_real_models  # noqa: E402


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "db: requires a running Postgres")


async def _database_available() -> bool:
    """Probe Postgres with a short timeout.

    A dedicated engine rather than the app's: the default connect timeout makes
    "no database running" take minutes to discover, which turns a fast unit-test
    run into a coffee break.
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.core.config import get_settings

    probe = create_async_engine(
        get_settings().database_url,
        connect_args={"connect_timeout": 2},
        poolclass=None,
    )
    try:
        async with probe.connect():
            return True
    except Exception:
        return False
    finally:
        await probe.dispose()


@pytest.fixture(scope="session")
def db_available() -> bool:
    return asyncio.run(_database_available())


@pytest.fixture
async def db(db_available: bool) -> AsyncGenerator:
    """A session against the real database, skipping if it is not up."""
    if not db_available:
        pytest.skip("Postgres is not running (docker compose up -d db)")
    async with SessionLocal() as session:
        yield session


@pytest.fixture(autouse=True)
def _restore_models():
    """Undo any scripted model installed by a test."""
    yield
    restore_real_models()


@pytest.fixture
def order_payload() -> dict:
    """A delivered order inside every window, as the order API would return it."""
    return {
        "order_ref": "ORD-TEST",
        "customer_email": "test@example.com",
        "status": "DELIVERED",
        "items": [{"sku": "X", "name": "Thing", "quantity": 1, "unit_price": "50.00"}],
        "total_amount": "50.00",
        "currency": "USD",
        "placed_at": (datetime.now(UTC) - timedelta(days=10)).isoformat(),
        "delivered_at": (datetime.now(UTC) - timedelta(days=5)).isoformat(),
        "tracking_number": "T1",
        "refunded_amount": "0.00",
        "is_final_sale": False,
    }


@pytest.fixture
async def seeded_order(db) -> AsyncGenerator[Order, None]:
    order = Order(
        order_ref=f"ORD-T{uuid.uuid4().hex[:8].upper()}",
        customer_email="test@example.com",
        status=OrderStatus.DELIVERED,
        items=[{"sku": "X", "name": "Thing", "quantity": 1, "unit_price": "50.00"}],
        total_amount=Decimal("50.00"),
        placed_at=datetime.now(UTC) - timedelta(days=10),
        delivered_at=datetime.now(UTC) - timedelta(days=5),
    )
    db.add(order)
    await db.commit()
    await db.refresh(order)
    yield order
    await db.delete(order)
    await db.commit()


@pytest.fixture
async def seeded_ticket(db, seeded_order: Order) -> AsyncGenerator[Ticket, None]:
    ticket = Ticket(
        channel=Channel.EMAIL,
        customer_email="test@example.com",
        customer_name="Test Person",
        subject="Refund please",
        body="The thing I ordered is faulty and I would like a refund.",
        order_ref=seeded_order.order_ref,
    )
    db.add(ticket)
    await db.commit()
    await db.refresh(ticket)
    yield ticket
    await db.delete(ticket)
    await db.commit()


@pytest.fixture
async def client(db_available: bool) -> AsyncGenerator[httpx.AsyncClient, None]:
    """HTTP client bound to the ASGI app, with no socket bound."""
    if not db_available:
        pytest.skip("Postgres is not running (docker compose up -d db)")

    from app.main import app
    from app.services import order_client

    transport = httpx.ASGITransport(app=app)
    # The agent reaches the order API over HTTP even though it is mounted in
    # this same app, so a graph run would otherwise need a real listening
    # socket on ORDER_API_BASE_URL. Pointing the default transport at the same
    # ASGI app keeps the suite socket-free.
    order_client.set_default_transport(transport)
    try:
        async with (
            httpx.AsyncClient(transport=transport, base_url="http://test") as c,
            app.router.lifespan_context(app),
        ):
            yield c
    finally:
        order_client.set_default_transport(None)
