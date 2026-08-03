"""Run orchestration.

Owns the boundary between the graph (in-memory state, checkpointed by LangGraph)
and the database (tickets, runs, traces that the API and UI read). The graph
never writes ticket rows; this module translates a graph result into them.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from langgraph.types import Command
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.graph import get_compiled_graph
from app.agents.state import AgentState, initial_state
from app.core.errors import ConflictError, NotFoundError
from app.core.logging import get_logger
from app.models.agent import AgentRun
from app.models.enums import RunStatus, Sentiment, TicketCategory, TicketStatus
from app.models.ticket import Ticket

logger = get_logger(__name__)


def _interrupt_payload(result: dict[str, Any]) -> dict[str, Any] | None:
    """Return the interrupt payload if the graph suspended, else None."""
    raw = result.get("__interrupt__")
    if not raw:
        return None
    first = raw[0] if isinstance(raw, (list, tuple)) else raw
    value = getattr(first, "value", first)
    return value if isinstance(value, dict) else {"value": value}


def _sync_run_from_state(run: AgentRun, state: dict[str, Any]) -> None:
    run.draft_response = state.get("draft")
    if state.get("final_text"):
        run.final_response = state["final_text"]
    run.proposed_actions = state.get("proposed_actions") or []
    run.executed_actions = state.get("executed_actions") or []
    run.policy_citations = state.get("policy_chunks") or []
    run.guardrail_flags = state.get("guardrail_flags") or []
    run.eligibility = state.get("eligibility")
    run.escalation_reason = state.get("escalation_reason")
    run.prompt_versions = state.get("prompt_versions") or {}


def _sync_ticket_from_state(ticket: Ticket, state: dict[str, Any]) -> None:
    if state.get("category"):
        ticket.category = TicketCategory(state["category"])
    if state.get("sentiment"):
        ticket.sentiment = Sentiment(state["sentiment"])
    if state.get("priority") is not None:
        ticket.priority = int(state["priority"])
    if state.get("confidence") is not None:
        ticket.confidence = float(state["confidence"])
    ticket.classification_meta = {
        "reasoning": state.get("classification_reasoning"),
        "retrieval_top_score": state.get("retrieval_top_score"),
    }


async def _load_ticket(db: AsyncSession, ticket_id: uuid.UUID | str) -> Ticket:
    ticket = await db.get(Ticket, uuid.UUID(str(ticket_id)))
    if ticket is None:
        raise NotFoundError(f"Ticket {ticket_id} not found")
    return ticket


async def start_run(db: AsyncSession, ticket_id: uuid.UUID | str) -> AgentRun:
    """Run the agent over a ticket, stopping at the human-approval interrupt."""
    ticket = await _load_ticket(db, ticket_id)

    if ticket.status not in (TicketStatus.NEW, TicketStatus.FAILED):
        raise ConflictError(
            f"Ticket is {ticket.status.value}; only NEW or FAILED tickets can be processed"
        )

    thread_id = str(uuid.uuid4())
    run = AgentRun(ticket_id=ticket.id, thread_id=thread_id, status=RunStatus.RUNNING)
    db.add(run)
    ticket.status = TicketStatus.PROCESSING
    await db.commit()
    await db.refresh(run)
    # `refresh` opens a fresh transaction that would otherwise stay open for the
    # entire graph run — holding a pooled connection idle-in-transaction and
    # blocking any concurrent DDL. Close it before the long-running work starts.
    await db.commit()

    state: AgentState = initial_state(
        ticket_id=str(ticket.id),
        run_id=str(run.id),
        thread_id=thread_id,
        subject=ticket.subject,
        body=ticket.body,
        customer_email=ticket.customer_email,
        customer_name=ticket.customer_name,
        order_ref=ticket.order_ref,
    )

    graph = await get_compiled_graph()
    config = {"configurable": {"thread_id": thread_id}}

    try:
        result = await graph.ainvoke(state, config=config)
    except Exception as exc:
        logger.exception("run_failed", ticket_id=str(ticket.id), error=str(exc))
        run.status = RunStatus.FAILED
        run.error = f"{type(exc).__name__}: {exc}"
        run.completed_at = datetime.now(UTC)
        ticket.status = TicketStatus.FAILED
        await db.commit()
        await db.refresh(run)
        return run

    await _apply_result(db, ticket, run, result)
    return run


async def _apply_result(
    db: AsyncSession, ticket: Ticket, run: AgentRun, result: dict[str, Any]
) -> None:
    """Translate a graph result into ticket and run rows."""
    _sync_run_from_state(run, result)
    _sync_ticket_from_state(ticket, result)

    interrupt = _interrupt_payload(result)
    outcome = result.get("outcome")

    if interrupt is not None:
        # Suspended at the approval gate: the draft is ready for a human.
        run.status = RunStatus.AWAITING_APPROVAL
        ticket.status = TicketStatus.AWAITING_APPROVAL
        if interrupt.get("draft"):
            run.draft_response = interrupt["draft"]
        if interrupt.get("proposed_actions"):
            run.proposed_actions = interrupt["proposed_actions"]
    elif outcome == "escalated" or result.get("escalate"):
        run.status = RunStatus.ESCALATED
        ticket.status = TicketStatus.ESCALATED
        run.completed_at = datetime.now(UTC)
    elif outcome == "rejected":
        run.status = RunStatus.REJECTED
        ticket.status = TicketStatus.REJECTED
        run.completed_at = datetime.now(UTC)
    elif outcome == "resolved":
        run.status = RunStatus.COMPLETED
        ticket.status = TicketStatus.RESOLVED
        ticket.resolved_at = datetime.now(UTC)
        run.completed_at = datetime.now(UTC)
    else:
        # The graph ended without a recognised outcome — treat as a failure
        # rather than silently marking the ticket resolved.
        run.status = RunStatus.FAILED
        run.error = "Graph finished without an outcome"
        ticket.status = TicketStatus.FAILED
        run.completed_at = datetime.now(UTC)

    await db.commit()
    await db.refresh(run)
    logger.info(
        "run_state",
        ticket_id=str(ticket.id),
        run_id=str(run.id),
        run_status=run.status.value,
        ticket_status=ticket.status.value,
    )


async def resume_run(
    db: AsyncSession,
    ticket_id: uuid.UUID | str,
    *,
    decision: str,
    edited_draft: str | None = None,
    approver: str | None = None,
    note: str | None = None,
) -> AgentRun:
    """Resume a suspended run with a human's decision.

    On approval the graph continues into `execute_actions`, which is the only
    place a refund or replacement is actually issued.
    """
    ticket = await _load_ticket(db, ticket_id)

    run = (
        await db.execute(
            select(AgentRun)
            .where(AgentRun.ticket_id == ticket.id)
            .order_by(AgentRun.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    if run is None:
        raise NotFoundError(f"No agent run exists for ticket {ticket_id}")
    if run.status != RunStatus.AWAITING_APPROVAL:
        raise ConflictError(
            f"Run is {run.status.value}; only a run awaiting approval can be decided"
        )
    if decision not in ("approve", "reject"):
        raise ConflictError(f"Unknown decision {decision!r}; expected 'approve' or 'reject'")

    run.approved_by = approver
    if edited_draft:
        run.final_response = edited_draft
    await db.commit()

    graph = await get_compiled_graph()
    config = {"configurable": {"thread_id": run.thread_id}}
    payload = {
        "decision": decision,
        "edited_draft": edited_draft,
        "approver": approver,
        "note": note,
    }

    try:
        result = await graph.ainvoke(Command(resume=payload), config=config)
    except Exception as exc:
        logger.exception("resume_failed", ticket_id=str(ticket.id), error=str(exc))
        run.status = RunStatus.FAILED
        run.error = f"{type(exc).__name__}: {exc}"
        run.completed_at = datetime.now(UTC)
        ticket.status = TicketStatus.FAILED
        await db.commit()
        await db.refresh(run)
        return run

    await _apply_result(db, ticket, run, result)

    # The human's edit is what actually went out; keep it distinct from the
    # model's original draft so the two can be compared later.
    if decision == "approve":
        run.final_response = edited_draft or run.draft_response
        await db.commit()
        await db.refresh(run)

    return run
