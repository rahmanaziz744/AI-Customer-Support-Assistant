"""DTOs for tickets, agent runs, and traces."""

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models.enums import Channel, RunStatus, Sentiment, TicketCategory, TicketStatus


class TicketCreate(BaseModel):
    customer_email: EmailStr
    subject: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=1, max_length=20_000)
    customer_name: str | None = Field(default=None, max_length=200)
    order_ref: str | None = Field(default=None, max_length=64)
    channel: Channel = Channel.EMAIL
    # Whether to start the agent immediately. False is useful for seeding a
    # queue, or for replaying a ticket later with a different prompt version.
    process: bool = True

    @field_validator("order_ref")
    @classmethod
    def _upper(cls, value: str | None) -> str | None:
        return value.strip().upper() if value else None


class TraceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    step_index: int
    node_name: str
    status: str
    model: str | None
    input_summary: str | None
    output_summary: str | None
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_usd: Decimal | None
    latency_ms: int
    error: str | None
    meta: dict[str, Any]
    created_at: datetime


class RunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    status: RunStatus
    thread_id: str
    draft_response: str | None
    final_response: str | None
    proposed_actions: list[dict[str, Any]]
    executed_actions: list[dict[str, Any]]
    policy_citations: list[dict[str, Any]]
    guardrail_flags: list[dict[str, Any]]
    eligibility: dict[str, Any] | None
    escalation_reason: str | None
    prompt_versions: dict[str, str]
    total_input_tokens: int
    total_output_tokens: int
    total_cost_usd: Decimal
    approved_by: str | None
    error: str | None
    created_at: datetime
    completed_at: datetime | None


class TicketSummary(BaseModel):
    """Row shape for the queue list — no draft or trace payload."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    subject: str
    customer_email: str
    customer_name: str | None
    order_ref: str | None
    status: TicketStatus
    category: TicketCategory | None
    sentiment: Sentiment | None
    priority: int | None
    confidence: float | None
    created_at: datetime
    updated_at: datetime


class TicketRead(TicketSummary):
    body: str
    channel: Channel
    classification_meta: dict[str, Any]
    resolved_at: datetime | None
    latest_run: RunRead | None = None


class TicketListResponse(BaseModel):
    items: list[TicketSummary]
    total: int
    limit: int
    offset: int


class ApprovalRequest(BaseModel):
    # Supplying edited_draft is how a reviewer corrects the model before it goes
    # out; the original draft is preserved for comparison.
    edited_draft: str | None = Field(default=None, max_length=20_000)
    approver: str | None = Field(default=None, max_length=200)
    note: str | None = Field(default=None, max_length=2000)


class RejectionRequest(BaseModel):
    approver: str | None = Field(default=None, max_length=200)
    note: str | None = Field(default=None, max_length=2000)


class TraceResponse(BaseModel):
    ticket_id: UUID
    run_id: UUID
    run_status: RunStatus
    total_input_tokens: int
    total_output_tokens: int
    total_cost_usd: Decimal
    total_latency_ms: int
    prompt_versions: dict[str, str]
    steps: list[TraceRead]


class StatsResponse(BaseModel):
    tickets_total: int
    by_status: dict[str, int]
    by_category: dict[str, int]
    runs_total: int
    escalation_rate: float
    auto_resolution_rate: float
    total_cost_usd: Decimal
    avg_cost_per_ticket_usd: Decimal
    total_input_tokens: int
    total_output_tokens: int
