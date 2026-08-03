"""Agent execution records: one AgentRun per graph invocation, one AgentTrace per node."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, uuid_pk
from app.models.enums import RunStatus


class AgentRun(Base, TimestampMixin):
    __tablename__ = "agent_runs"

    id: Mapped[uuid.UUID] = uuid_pk()
    ticket_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # LangGraph checkpoint thread; how an approval resumes the right run.
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)

    status: Mapped[RunStatus] = mapped_column(
        SAEnum(RunStatus, native_enum=False, length=32, name="run_status"),
        default=RunStatus.RUNNING,
        nullable=False,
        index=True,
    )

    draft_response: Mapped[str | None] = mapped_column(Text)
    # Draft as edited by the human approver, when they changed it before sending.
    final_response: Mapped[str | None] = mapped_column(Text)

    proposed_actions: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, nullable=False
    )
    executed_actions: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, nullable=False
    )
    policy_citations: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, nullable=False
    )
    guardrail_flags: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, nullable=False
    )
    eligibility: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    escalation_reason: Mapped[str | None] = mapped_column(Text)
    # Which prompt file version each node ran, so a result can be reproduced.
    prompt_versions: Mapped[dict[str, str]] = mapped_column(JSONB, default=dict, nullable=False)

    total_input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), default=Decimal("0"), nullable=False
    )

    approved_by: Mapped[str | None] = mapped_column(String(200))
    error: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    ticket: Mapped["Ticket"] = relationship(back_populates="runs")  # noqa: F821
    traces: Mapped[list["AgentTrace"]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="AgentTrace.step_index",
        lazy="selectin",
    )


class AgentTrace(Base):
    """One row per graph node execution — the observability backbone.

    Written even when a node fails, so a stuck run can be diagnosed from the
    trace alone without replaying the graph.
    """

    __tablename__ = "agent_traces"

    id: Mapped[uuid.UUID] = uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ticket_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False, index=True
    )

    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    node_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), default="ok", nullable=False)

    model: Mapped[str | None] = mapped_column(String(64))
    input_summary: Mapped[str | None] = mapped_column(Text)
    output_summary: Mapped[str | None] = mapped_column(Text)

    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Null (not zero) when the model has no pricing entry — see core.pricing.
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))

    latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    run: Mapped[AgentRun] = relationship(back_populates="traces")
