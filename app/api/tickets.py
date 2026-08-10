"""Ticket, approval, and trace endpoints."""

import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.runner import resume_run, start_run
from app.core.budget import assert_budget_available
from app.core.config import get_settings
from app.core.db import get_db
from app.core.logging import get_logger
from app.core.rate_limit import expensive_rate_limit, limiter, ticket_rate_limit
from app.core.security import require_demo_token
from app.models.enums import TicketStatus
from app.models.ticket import Ticket
from app.schemas.ticket import (
    ApprovalRequest,
    RejectionRequest,
    RunRead,
    StatsResponse,
    TicketCreate,
    TicketListResponse,
    TicketRead,
    TicketSummary,
    TraceResponse,
)
from app.services import ticket_service

logger = get_logger(__name__)
settings = get_settings()

router = APIRouter(prefix="/api", tags=["tickets"])


async def _to_detail(db: AsyncSession, ticket: Ticket) -> TicketRead:
    run = await ticket_service.latest_run(db, ticket.id)
    detail = TicketRead.model_validate(ticket)
    detail.latest_run = RunRead.model_validate(run) if run else None
    return detail


@router.post("/tickets", response_model=TicketRead, status_code=status.HTTP_201_CREATED)
@limiter.limit(ticket_rate_limit)
async def create_ticket(
    request: Request,
    payload: TicketCreate,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> TicketRead:
    """Submit a ticket and (by default) start the agent on it.

    Returns as soon as the ticket is stored; the agent runs in the background
    so a slow model call never holds the connection open. Poll the ticket or
    its trace to watch progress.

    `request` is required by slowapi's decorator, which reads the client
    address off it to key the limit.
    """
    # Checked before the ticket is stored, so a refused submission leaves
    # nothing behind. Only when the agent would actually run: storing a ticket
    # for later costs nothing and stays available with the budget spent.
    if payload.process:
        await assert_budget_available(db)

    ticket = Ticket(
        channel=payload.channel,
        customer_email=str(payload.customer_email).lower(),
        customer_name=payload.customer_name,
        subject=payload.subject,
        body=payload.body,
        order_ref=payload.order_ref,
        status=TicketStatus.NEW,
    )
    db.add(ticket)
    await db.commit()
    await db.refresh(ticket)

    logger.info("ticket_created", ticket_id=str(ticket.id), process=payload.process)

    if payload.process:
        background.add_task(ticket_service.process_ticket_in_background, ticket.id)

    return await _to_detail(db, ticket)


@router.get("/tickets", response_model=TicketListResponse)
async def list_tickets(
    status_filter: TicketStatus | None = Query(default=None, alias="status"),
    category: str | None = Query(default=None),
    search: str | None = Query(default=None, description="Match subject, email, or order ref"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> TicketListResponse:
    rows, total = await ticket_service.list_tickets(
        db, status=status_filter, category=category, search=search, limit=limit, offset=offset
    )
    return TicketListResponse(
        items=[TicketSummary.model_validate(t) for t in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/tickets/{ticket_id}", response_model=TicketRead)
async def get_ticket(ticket_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> TicketRead:
    ticket = await ticket_service.get_ticket(db, ticket_id)
    return await _to_detail(db, ticket)


@router.post("/tickets/{ticket_id}/process", response_model=RunRead)
@limiter.limit(expensive_rate_limit)
async def process_ticket(
    request: Request, ticket_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> RunRead:
    """Run the agent synchronously and return the resulting run.

    The blocking counterpart to the background run started at creation —
    handy for scripted demos and tests that need the result in one call.
    """
    await assert_budget_available(db)
    run = await start_run(db, ticket_id)
    return RunRead.model_validate(run)


@router.post("/tickets/{ticket_id}/approve", response_model=RunRead)
@limiter.limit(expensive_rate_limit)
async def approve_ticket(
    request: Request,
    ticket_id: uuid.UUID,
    payload: ApprovalRequest,
    db: AsyncSession = Depends(get_db),
) -> RunRead:
    """Approve the drafted reply, releasing the run to execute and send.

    This is the only path that issues a refund or replacement.
    """
    # Resuming the graph runs more nodes, so it spends. Rejection deliberately
    # does not check: stopping a run must stay possible with the budget spent.
    await assert_budget_available(db)
    run = await resume_run(
        db,
        ticket_id,
        decision="approve",
        edited_draft=payload.edited_draft,
        approver=payload.approver,
        note=payload.note,
    )
    logger.info("ticket_approved", ticket_id=str(ticket_id), approver=payload.approver)
    return RunRead.model_validate(run)


@router.post("/tickets/{ticket_id}/reject", response_model=RunRead)
@limiter.limit(expensive_rate_limit)
async def reject_ticket(
    request: Request,
    ticket_id: uuid.UUID,
    payload: RejectionRequest,
    db: AsyncSession = Depends(get_db),
) -> RunRead:
    """Reject the drafted reply. Nothing is sent and no action is executed."""
    run = await resume_run(
        db,
        ticket_id,
        decision="reject",
        approver=payload.approver,
        note=payload.note,
    )
    logger.info("ticket_rejected", ticket_id=str(ticket_id), approver=payload.approver)
    return RunRead.model_validate(run)


@router.get("/tickets/{ticket_id}/trace", response_model=TraceResponse)
async def get_trace(ticket_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> TraceResponse:
    """Per-node execution trace with token counts, cost, and latency."""
    run, steps = await ticket_service.trace_for_ticket(db, ticket_id)
    return TraceResponse(
        ticket_id=ticket_id,
        run_id=run.id,
        run_status=run.status,
        total_input_tokens=run.total_input_tokens,
        total_output_tokens=run.total_output_tokens,
        total_cost_usd=run.total_cost_usd,
        total_latency_ms=sum(s.latency_ms for s in steps),
        prompt_versions=run.prompt_versions,
        steps=steps,
    )


@router.get("/stats", response_model=StatsResponse)
async def get_stats(db: AsyncSession = Depends(get_db)) -> StatsResponse:
    """Queue counts, escalation/resolution rates, and cumulative model spend."""
    return StatsResponse(**await ticket_service.compute_stats(db))


@router.delete(
    "/tickets/{ticket_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_demo_token)],
)
@limiter.limit(expensive_rate_limit)
async def delete_ticket(
    request: Request, ticket_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> Response:
    """Delete a ticket and its runs.

    Gated by `X-Demo-Token` when `DEMO_ADMIN_TOKEN` is set — the one route on a
    public demo that can destroy what other visitors are looking at.
    """
    ticket = await ticket_service.get_ticket(db, ticket_id)
    await db.delete(ticket)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
