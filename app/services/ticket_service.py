"""Ticket queries and background processing."""

import asyncio
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.runner import start_run
from app.core.config import get_settings
from app.core.db import session_scope
from app.core.errors import NotFoundError
from app.core.logging import get_logger
from app.models.agent import AgentRun, AgentTrace
from app.models.enums import RunStatus, TicketStatus
from app.models.ticket import Ticket

logger = get_logger(__name__)

_run_slots: asyncio.Semaphore | None = None
_run_slots_loop: asyncio.AbstractEventLoop | None = None


def run_slots() -> asyncio.Semaphore:
    """Bound on agent runs executing at once.

    Ticket submission returns as soon as the row is stored and hands the run to
    a background task, so without this N accepted requests become N concurrent
    graph runs — each holding a database session, an ONNX embedding pass, and a
    model call. On a small single-instance deployment that is how the box falls
    over. Work above the bound waits its turn rather than being rejected.

    Cached per event loop for the same reason as the compiled graph in
    `agents.graph`: an asyncio primitive must not be shared across loops, and a
    test suite creates a fresh loop per test.
    """
    global _run_slots, _run_slots_loop

    loop = asyncio.get_running_loop()
    if _run_slots is None or _run_slots_loop is not loop:
        _run_slots = asyncio.Semaphore(get_settings().max_concurrent_runs)
        _run_slots_loop = loop
    return _run_slots


async def get_ticket(db: AsyncSession, ticket_id: uuid.UUID) -> Ticket:
    ticket = await db.get(Ticket, ticket_id)
    if ticket is None:
        raise NotFoundError(f"Ticket {ticket_id} not found")
    return ticket


async def latest_run(db: AsyncSession, ticket_id: uuid.UUID) -> AgentRun | None:
    return (
        await db.execute(
            select(AgentRun)
            .where(AgentRun.ticket_id == ticket_id)
            .order_by(AgentRun.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


def _apply_filters(
    stmt: Select,
    *,
    status: TicketStatus | None,
    category: str | None,
    search: str | None,
) -> Select:
    if status:
        stmt = stmt.where(Ticket.status == status)
    if category:
        stmt = stmt.where(Ticket.category == category)
    if search:
        pattern = f"%{search.strip()}%"
        stmt = stmt.where(
            Ticket.subject.ilike(pattern)
            | Ticket.customer_email.ilike(pattern)
            | Ticket.order_ref.ilike(pattern)
        )
    return stmt


async def list_tickets(
    db: AsyncSession,
    *,
    status: TicketStatus | None = None,
    category: str | None = None,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Ticket], int]:
    filters = {"status": status, "category": category, "search": search}

    total = (
        await db.execute(_apply_filters(select(func.count(Ticket.id)), **filters))
    ).scalar_one()

    stmt = _apply_filters(select(Ticket), **filters)
    # Awaiting-approval first, then most urgent, then newest: the order a
    # support lead actually works the queue in.
    stmt = stmt.order_by(
        (Ticket.status != TicketStatus.AWAITING_APPROVAL),
        Ticket.priority.desc().nullslast(),
        Ticket.created_at.desc(),
    ).limit(limit).offset(offset)

    rows = list((await db.execute(stmt)).scalars().all())
    return rows, total


async def process_ticket_in_background(ticket_id: uuid.UUID) -> None:
    """Run the agent for a ticket outside the request lifecycle.

    Opens its own session because the request's session is closed by the time
    a FastAPI background task runs. Never raises: a failure is recorded on the
    run and the ticket, and re-raising here would only crash a detached task.

    Waits for a slot before opening the session, so queued work is not also
    holding a connection from the pool while it waits.
    """
    try:
        async with run_slots(), session_scope() as db:
            await start_run(db, ticket_id)
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("background_processing_failed", ticket_id=str(ticket_id), error=str(exc))


async def trace_for_ticket(
    db: AsyncSession, ticket_id: uuid.UUID
) -> tuple[AgentRun, list[AgentTrace]]:
    run = await latest_run(db, ticket_id)
    if run is None:
        raise NotFoundError(f"No agent run exists for ticket {ticket_id}")

    steps = list(
        (
            await db.execute(
                select(AgentTrace)
                .where(AgentTrace.run_id == run.id)
                .order_by(AgentTrace.created_at, AgentTrace.step_index)
            )
        )
        .scalars()
        .all()
    )
    return run, steps


async def compute_stats(db: AsyncSession) -> dict[str, Any]:
    """Aggregate counts and spend for the dashboard."""
    tickets_total = (await db.execute(select(func.count(Ticket.id)))).scalar_one()

    by_status = {
        status.value: count
        for status, count in (
            await db.execute(select(Ticket.status, func.count(Ticket.id)).group_by(Ticket.status))
        ).all()
    }
    by_category = {
        (category.value if category else "UNCLASSIFIED"): count
        for category, count in (
            await db.execute(
                select(Ticket.category, func.count(Ticket.id)).group_by(Ticket.category)
            )
        ).all()
    }

    runs_total = (await db.execute(select(func.count(AgentRun.id)))).scalar_one()
    escalated = (
        await db.execute(
            select(func.count(AgentRun.id)).where(AgentRun.status == RunStatus.ESCALATED)
        )
    ).scalar_one()
    completed = (
        await db.execute(
            select(func.count(AgentRun.id)).where(AgentRun.status == RunStatus.COMPLETED)
        )
    ).scalar_one()

    totals = (
        await db.execute(
            select(
                func.coalesce(func.sum(AgentRun.total_cost_usd), 0),
                func.coalesce(func.sum(AgentRun.total_input_tokens), 0),
                func.coalesce(func.sum(AgentRun.total_output_tokens), 0),
            )
        )
    ).one()
    total_cost, input_tokens, output_tokens = totals

    return {
        "tickets_total": tickets_total,
        "by_status": by_status,
        "by_category": by_category,
        "runs_total": runs_total,
        "escalation_rate": round(escalated / runs_total, 4) if runs_total else 0.0,
        "auto_resolution_rate": round(completed / runs_total, 4) if runs_total else 0.0,
        "total_cost_usd": Decimal(total_cost),
        "avg_cost_per_ticket_usd": (
            Decimal(total_cost) / runs_total if runs_total else Decimal("0")
        ).quantize(Decimal("0.000001")),
        "total_input_tokens": int(input_tokens),
        "total_output_tokens": int(output_tokens),
    }
