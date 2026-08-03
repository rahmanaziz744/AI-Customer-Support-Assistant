"""Graph nodes.

Each node takes the state and returns a partial update. Nodes open their own
short-lived database sessions rather than sharing the request's session, so the
graph can run in a background task or resume in a different process after a
human approval without holding a connection open across the interrupt.
"""

import uuid
from decimal import Decimal
from typing import Any

from langgraph.types import interrupt
from pydantic import BaseModel, Field

from app.agents.eligibility import evaluate_refund, evaluate_replacement
from app.agents.escalation import evaluate_escalation
from app.agents.guardrails import GuardrailFlag, screen_input, screen_output, truncate_body
from app.agents.llm import get_chat_model, model_name_of, response_text, usage_of
from app.agents.prompts import active_versions, load_prompt
from app.agents.state import AgentState
from app.agents.tools import ACTION_TOOLS, clamp_to_eligibility, normalise_tool_calls
from app.agents.tracing import TRACE_KEY, traced_node
from app.core.config import get_settings
from app.core.db import session_scope
from app.core.logging import get_logger
from app.rag.retriever import format_citations_for_prompt, retrieve_policy
from app.services.order_client import OrderAPIError, OrderClient, OrderNotFound

logger = get_logger(__name__)

# Categories where a refund/replacement decision is meaningful.
ACTIONABLE_CATEGORIES = {"REFUND_REQUEST", "REPLACEMENT_REQUEST", "SHIPPING_ISSUE"}

# Wrapping untrusted text in a named block, and saying so in the system prompt,
# gives the model a clear boundary between data and instructions.
TICKET_BLOCK = """<customer_ticket>
Subject: {subject}
From: {name} <{email}>
Order reference: {order_ref}

{body}
</customer_ticket>"""


class Classification(BaseModel):
    """Structured output schema for the triage step."""

    category: str = Field(description="One of the documented category values")
    sentiment: str = Field(description="POSITIVE, NEUTRAL, NEGATIVE, or VERY_NEGATIVE")
    priority: int = Field(ge=1, le=5, description="SLA priority, 5 is most urgent")
    confidence: float = Field(ge=0.0, le=1.0, description="Confidence in the category")
    reasoning: str = Field(description="One or two sentences of justification")


VALID_CATEGORIES = {
    "REFUND_REQUEST", "REPLACEMENT_REQUEST", "ORDER_STATUS", "SHIPPING_ISSUE",
    "BILLING", "TECHNICAL_SUPPORT", "ACCOUNT", "COMPLAINT", "GENERAL_INQUIRY",
}
VALID_SENTIMENTS = {"POSITIVE", "NEUTRAL", "NEGATIVE", "VERY_NEGATIVE"}


def _ticket_block(state: AgentState) -> str:
    return TICKET_BLOCK.format(
        subject=state.get("subject", ""),
        name=state.get("customer_name") or "Unknown",
        email=state.get("customer_email", ""),
        order_ref=state.get("order_ref") or "(none given)",
        body=truncate_body(state.get("body", "")),
    )


# ---------------------------------------------------------------------------
# 1. Input guardrails
# ---------------------------------------------------------------------------
@traced_node("input_guardrails")
async def input_guardrails_node(state: AgentState) -> dict[str, Any]:
    flags = screen_input(state.get("subject", ""), state.get("body", ""))
    serialised = [f.to_dict() for f in flags]
    blocking = [f for f in flags if f.blocking]

    return {
        "guardrail_flags": serialised,
        "prompt_versions": active_versions(),
        TRACE_KEY: {
            "input_summary": f"subject={state.get('subject', '')[:120]!r}",
            "output_summary": (
                f"{len(flags)} flag(s), {len(blocking)} blocking"
                if flags
                else "no guardrail flags"
            ),
            "meta": {"flags": [f.rule for f in flags]},
        },
    }


# ---------------------------------------------------------------------------
# 2. Classification
# ---------------------------------------------------------------------------
@traced_node("classify")
async def classify_node(state: AgentState) -> dict[str, Any]:
    model = get_chat_model("classify", max_tokens=1024)
    structured = model.with_structured_output(Classification, include_raw=True)

    result = await structured.ainvoke(
        [
            {"role": "system", "content": load_prompt("classify")},
            {"role": "user", "content": _ticket_block(state)},
        ]
    )

    # include_raw gives us the parsed object and the raw message, so usage and
    # model name are still available for the trace.
    parsed: Classification | None = result.get("parsed") if isinstance(result, dict) else result
    raw = result.get("raw") if isinstance(result, dict) else None

    if parsed is None:
        # Structured output failed; a low confidence value routes to a human
        # rather than letting the run continue on a guess.
        return {
            "category": "GENERAL_INQUIRY",
            "sentiment": "NEUTRAL",
            "priority": 3,
            "confidence": 0.0,
            "classification_reasoning": "Classifier returned no parsable result.",
            TRACE_KEY: {
                "status": "error",
                "model": model_name_of(raw, get_settings().model_for("classify")),
                "usage": usage_of(raw),
                "output_summary": "structured output parse failed",
            },
        }

    category = parsed.category.upper().strip()
    if category not in VALID_CATEGORIES:
        category = "GENERAL_INQUIRY"
    sentiment = parsed.sentiment.upper().strip()
    if sentiment not in VALID_SENTIMENTS:
        sentiment = "NEUTRAL"

    priority = _apply_sla_rules(parsed.priority, category, sentiment, state)

    return {
        "category": category,
        "sentiment": sentiment,
        "priority": priority,
        "confidence": float(parsed.confidence),
        "classification_reasoning": parsed.reasoning,
        TRACE_KEY: {
            "model": model_name_of(raw, get_settings().model_for("classify")),
            "usage": usage_of(raw),
            "input_summary": state.get("subject"),
            "output_summary": (
                f"{category} / {sentiment} / P{priority} / conf={parsed.confidence:.2f}"
            ),
            "meta": {"model_priority": parsed.priority, "applied_priority": priority},
        },
    }


def _apply_sla_rules(
    model_priority: int, category: str, sentiment: str, state: AgentState
) -> int:
    """Blend the model's priority with deterministic SLA floors.

    The model reads tone well but has no stake in the SLA, so hard business
    rules are applied in code and can only ever raise urgency, never lower it.
    """
    priority = max(1, min(5, int(model_priority)))

    # A blocking input flag means policy already requires a human.
    for flag in state.get("guardrail_flags") or []:
        if flag.get("severity") == "block":
            priority = max(priority, 5)

    if sentiment == "VERY_NEGATIVE":
        priority = max(priority, 4)
    if category == "BILLING":
        # Money already left the customer's account.
        priority = max(priority, 3)
    return priority


# ---------------------------------------------------------------------------
# 3. Policy retrieval
# ---------------------------------------------------------------------------
@traced_node("retrieve_policy")
async def retrieve_policy_node(state: AgentState) -> dict[str, Any]:
    query = f"{state.get('subject', '')}\n{state.get('body', '')}"

    async with session_scope() as db:
        chunks = await retrieve_policy(db, query, category=state.get("category"))

    top_score = chunks[0].score if chunks else 0.0
    return {
        "policy_chunks": [c.as_citation() for c in chunks],
        "policy_context": format_citations_for_prompt(chunks),
        "retrieval_top_score": top_score,
        TRACE_KEY: {
            "input_summary": f"category={state.get('category')} query={query[:150]!r}",
            "output_summary": (
                f"{len(chunks)} chunk(s), top score {top_score:.3f}: "
                + ", ".join(c.document_slug for c in chunks[:4])
            ),
            "meta": {"scores": [round(c.score, 4) for c in chunks]},
        },
    }


# ---------------------------------------------------------------------------
# 4. Order lookup
# ---------------------------------------------------------------------------
@traced_node("fetch_order")
async def fetch_order_node(state: AgentState) -> dict[str, Any]:
    order_ref = state.get("order_ref")
    if not order_ref:
        return {
            "order": None,
            TRACE_KEY: {"output_summary": "no order reference on the ticket"},
        }

    client = OrderClient()
    try:
        order = await client.get_order(order_ref)
    except OrderNotFound:
        return {
            "order": None,
            "order_error": f"Order {order_ref} does not exist",
            TRACE_KEY: {"status": "error", "output_summary": f"{order_ref} not found"},
        }
    except OrderAPIError as exc:
        return {
            "order": None,
            "order_error": str(exc),
            TRACE_KEY: {"status": "error", "output_summary": f"order API error: {exc}"},
        }

    return {
        "order": order,
        "order_error": None,
        TRACE_KEY: {
            "input_summary": order_ref,
            "output_summary": (
                f"{order['order_ref']} {order['status']} total={order['total_amount']} "
                f"refunded={order['refunded_amount']}"
            ),
        },
    }


# ---------------------------------------------------------------------------
# 5. Eligibility (deterministic)
# ---------------------------------------------------------------------------
@traced_node("check_eligibility")
async def check_eligibility_node(state: AgentState) -> dict[str, Any]:
    order = state.get("order")
    category = state.get("category") or ""

    if not order or category not in ACTIONABLE_CATEGORIES:
        return {
            "eligibility": None,
            TRACE_KEY: {
                "output_summary": (
                    "skipped: no order on file"
                    if not order
                    else f"skipped: {category} needs no order action"
                )
            },
        }

    rules = _rules_for(state, category)

    if category == "REPLACEMENT_REQUEST":
        text = f"{state.get('subject', '')} {state.get('body', '')}".lower()
        damaged = any(
            word in text
            for word in ("damaged", "broken", "cracked", "shattered", "defective", "smashed")
        )
        # The engine caps replacements per order, but only if it is told how
        # many already went out. Without this the second replacement on the
        # same order was approved automatically.
        prior = await _count_prior_replacements(state.get("order_ref"))
        decision = evaluate_replacement(
            order, rules, damaged_on_arrival=damaged, prior_replacements=prior
        )
    else:
        decision = evaluate_refund(order, rules)

    return {
        "eligibility": decision.to_dict(),
        TRACE_KEY: {
            "input_summary": f"{category} on {order.get('order_ref')}",
            "output_summary": (
                f"{decision.action}: eligible={decision.eligible} "
                f"approved={decision.approved_amount} escalate={decision.requires_escalation}"
            ),
            "meta": {"checks": decision.checks},
        },
    }


async def _count_prior_replacements(order_ref: str | None) -> int:
    """How many replacements this order has already received.

    Returns 0 if the order system cannot be reached — the ticket still needs a
    human either way, and failing the lookup should not block the decision.
    """
    if not order_ref:
        return 0
    try:
        actions = await OrderClient().list_actions(order_ref)
    except (OrderAPIError, OrderNotFound):
        return 0
    return sum(1 for a in actions if a.get("action_type") == "REPLACEMENT")


def _rules_for(state: AgentState, category: str) -> dict[str, Any]:
    """Pull the machine-readable rules from the retrieved policy documents.

    Retrieval already selected the governing policy, so its frontmatter rules
    are the ones that apply. Falling back to `{}` lets the engine use its
    documented defaults rather than failing closed on a retrieval miss.
    """
    wanted = "replacement-policy" if category == "REPLACEMENT_REQUEST" else "refund-policy"
    for chunk in state.get("policy_chunks") or []:
        if chunk.get("slug") == wanted:
            return chunk.get("rules") or {}
    return {}


# ---------------------------------------------------------------------------
# 6. Drafting
# ---------------------------------------------------------------------------
@traced_node("draft_response")
async def draft_response_node(state: AgentState) -> dict[str, Any]:
    revision = int(state.get("revision_count") or 0)
    model = get_chat_model("draft", max_tokens=4096)
    bound = model.bind_tools(ACTION_TOOLS)

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": load_prompt("draft")},
        {"role": "user", "content": _build_draft_context(state)},
    ]

    if revision:
        problems = "\n".join(
            f"- {f.get('detail')}"
            for f in (state.get("guardrail_flags") or [])
            if f.get("severity") == "block" and f.get("layer") == "output"
        )
        messages.append({"role": "assistant", "content": state.get("draft") or ""})
        messages.append(
            {
                "role": "user",
                "content": f"{load_prompt('revise')}\n\nProblems found:\n{problems}",
            }
        )

    response = await bound.ainvoke(messages)

    draft = response_text(response)
    raw_calls = getattr(response, "tool_calls", None) or []
    actions = normalise_tool_calls(raw_calls)
    actions, notes = clamp_to_eligibility(actions, state.get("eligibility"))

    # A clamp means the model tried to act beyond its authority — propose a
    # refund eligibility refused, or exceed the approved amount. Clamping alone
    # would silently sanitise that away, so record it as a blocking flag: the
    # ticket goes to a human, and the attempt stays visible in the trace.
    # Layer "tool", not "output": the output pass replaces its own flags on each
    # revision, and an authority breach must survive that. It also routes
    # straight to a human rather than earning a polite retry.
    overreach_flags = [
        GuardrailFlag(
            layer="tool",
            rule="proposal_exceeded_authority",
            severity="block",
            detail=note,
        ).to_dict()
        for note in notes
    ]
    prior_flags = state.get("guardrail_flags") or []

    return {
        "draft": draft,
        "proposed_actions": actions,
        "revision_count": revision,
        "guardrail_flags": prior_flags + overreach_flags,
        TRACE_KEY: {
            "model": model_name_of(response, get_settings().model_for("draft")),
            "usage": usage_of(response),
            "input_summary": (
                f"revision={revision} "
                f"policy_chunks={len(state.get('policy_chunks') or [])}"
            ),
            "output_summary": (
                f"{len(draft)} chars, actions="
                + (", ".join(a["type"] for a in actions) or "none")
                + (f" | {'; '.join(notes)}" if notes else "")
            ),
            "meta": {"clamp_notes": notes, "raw_tool_calls": [c.get("name") for c in raw_calls]},
        },
    }


def _build_draft_context(state: AgentState) -> str:
    parts = [_ticket_block(state)]

    parts.append(
        "<retrieved_policy>\n"
        + (state.get("policy_context") or "(none retrieved)")
        + "\n</retrieved_policy>"
    )

    order = state.get("order")
    if order:
        items = ", ".join(
            f"{i.get('quantity', 1)}x {i.get('name')}" for i in order.get("items", [])
        )
        parts.append(
            "<order_record>\n"
            f"Reference: {order.get('order_ref')}\n"
            f"Status: {order.get('status')}\n"
            f"Items: {items}\n"
            f"Total: {order.get('total_amount')} {order.get('currency', 'USD')}\n"
            f"Already refunded: {order.get('refunded_amount')}\n"
            f"Placed: {order.get('placed_at')}\n"
            f"Delivered: {order.get('delivered_at') or 'not yet delivered'}\n"
            f"Tracking: {order.get('tracking_number') or 'none'}\n"
            f"Final sale: {order.get('is_final_sale')}\n"
            "</order_record>"
        )
    elif state.get("order_error"):
        parts.append(f"<order_record>Lookup failed: {state['order_error']}</order_record>")

    eligibility = state.get("eligibility")
    if eligibility:
        approved = eligibility.get("approved_amount")
        parts.append(
            "<eligibility_decision>\n"
            "This decision is binding. Do not contradict it or exceed the approved amount.\n"
            f"Action considered: {eligibility.get('action')}\n"
            f"Eligible: {eligibility.get('eligible')}\n"
            f"Approved amount: {approved if approved else 'n/a'}\n"
            f"Reason: {eligibility.get('reason')}\n"
            "</eligibility_decision>"
        )
    else:
        parts.append(
            "<eligibility_decision>\n"
            "No refund or replacement decision applies to this ticket. Answer the "
            "question and call propose_no_action.\n"
            "</eligibility_decision>"
        )

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# 7. Output guardrails
# ---------------------------------------------------------------------------
@traced_node("output_guardrails")
async def output_guardrails_node(state: AgentState) -> dict[str, Any]:
    flags = screen_output(
        state.get("draft") or "",
        eligibility=state.get("eligibility"),
        proposed_actions=state.get("proposed_actions") or [],
        original_body=state.get("body") or "",
    )
    new_flags = [f.to_dict() for f in flags]
    blocking = [f for f in flags if f.blocking]

    # Input-layer flags are kept; output-layer flags are replaced each pass so a
    # successful revision clears the problems that triggered it.
    prior = [f for f in (state.get("guardrail_flags") or []) if f.get("layer") != "output"]

    return {
        "guardrail_flags": prior + new_flags,
        TRACE_KEY: {
            "input_summary": f"draft {len(state.get('draft') or '')} chars",
            "output_summary": (
                f"{len(blocking)} blocking, {len(flags) - len(blocking)} advisory"
                if flags
                else "draft passed all checks"
            ),
            "meta": {"rules": [f.rule for f in flags]},
        },
    }


# ---------------------------------------------------------------------------
# 8. Escalation gate
# ---------------------------------------------------------------------------
@traced_node("escalation_gate")
async def escalation_gate_node(state: AgentState) -> dict[str, Any]:
    verdict = evaluate_escalation(state)
    return {
        "escalate": verdict.escalate,
        "escalation_reason": verdict.reason,
        TRACE_KEY: {
            "output_summary": (
                f"escalate: {verdict.reason}" if verdict.escalate else "cleared for human review"
            ),
            "meta": {"rule": verdict.rule},
        },
    }


# ---------------------------------------------------------------------------
# 9. Human approval (interrupt)
# ---------------------------------------------------------------------------
@traced_node("await_approval")
async def await_approval_node(state: AgentState) -> dict[str, Any]:
    """Suspend the run until a human approves or rejects.

    `interrupt` checkpoints the state to Postgres and unwinds. The run resumes
    from this point — possibly in another process — when the approval endpoint
    sends a Command(resume=...).
    """
    decision = interrupt(
        {
            "type": "approval_required",
            "ticket_id": state.get("ticket_id"),
            "draft": state.get("draft"),
            "proposed_actions": state.get("proposed_actions") or [],
            "eligibility": state.get("eligibility"),
            "policy_citations": state.get("policy_chunks") or [],
        }
    )

    approval = decision if isinstance(decision, dict) else {"decision": str(decision)}
    return {
        "approval": approval,
        TRACE_KEY: {
            "output_summary": f"human decision: {approval.get('decision')}",
            "meta": {"approver": approval.get("approver")},
        },
    }


# ---------------------------------------------------------------------------
# 10. Execute approved actions
# ---------------------------------------------------------------------------
@traced_node("execute_actions")
async def execute_actions_node(state: AgentState) -> dict[str, Any]:
    approval = state.get("approval") or {}
    order_ref = state.get("order_ref")
    ticket_id = state.get("ticket_id")
    approver = approval.get("approver")

    client = OrderClient()
    executed: list[dict[str, Any]] = []

    for index, action in enumerate(state.get("proposed_actions") or []):
        if action.get("type") == "none" or action.get("invalid"):
            continue
        if not order_ref:
            executed.append({**action, "status": "skipped", "error": "no order reference"})
            continue

        # Deterministic key: re-approving the same run cannot double-charge.
        idempotency_key = f"run:{state.get('run_id')}:action:{index}"

        try:
            if action["type"] == "refund":
                result = await client.issue_refund(
                    order_ref,
                    Decimal(str(action.get("amount") or "0")),
                    action.get("reason") or "Approved by support",
                    ticket_id=ticket_id,
                    approved_by=approver,
                    idempotency_key=idempotency_key,
                )
            else:
                result = await client.issue_replacement(
                    order_ref,
                    action.get("reason") or "Approved by support",
                    ticket_id=ticket_id,
                    approved_by=approver,
                    idempotency_key=idempotency_key,
                )
            executed.append({**action, "status": "executed", "result": result})
        except (OrderAPIError, OrderNotFound) as exc:
            logger.error("action_execution_failed", action=action.get("type"), error=str(exc))
            executed.append({**action, "status": "failed", "error": str(exc)})

    failed = [a for a in executed if a["status"] == "failed"]
    return {
        "executed_actions": executed,
        TRACE_KEY: {
            "status": "error" if failed else "ok",
            "input_summary": f"{len(state.get('proposed_actions') or [])} proposed",
            "output_summary": (
                f"{len(executed)} executed, {len(failed)} failed"
                if executed
                else "no order actions to execute"
            ),
        },
    }


# ---------------------------------------------------------------------------
# 11. Terminal nodes
# ---------------------------------------------------------------------------
@traced_node("send_reply")
async def send_reply_node(state: AgentState) -> dict[str, Any]:
    """Mock delivery of the approved reply.

    A real deployment swaps this for an email/helpdesk call. It is a node rather
    than a side effect elsewhere so the send is traced like every other step.
    """
    approval = state.get("approval") or {}
    final = approval.get("edited_draft") or state.get("draft") or ""
    edited = bool(approval.get("edited_draft"))

    logger.info(
        "reply_sent",
        ticket_id=state.get("ticket_id"),
        to=state.get("customer_email"),
        edited_by_human=edited,
        chars=len(final),
    )
    return {
        # Deliberately not writing back to `draft`: the model's original text
        # must survive so it can be compared against what a human sent.
        "final_text": final,
        "outcome": "resolved",
        TRACE_KEY: {
            "output_summary": (
                f"sent {len(final)} chars to {state.get('customer_email')}"
                + (" (edited by approver)" if edited else "")
            ),
            "meta": {"edited_by_human": edited, "channel": "mock-email"},
        },
    }


@traced_node("escalated")
async def escalated_node(state: AgentState) -> dict[str, Any]:
    return {
        "outcome": "escalated",
        TRACE_KEY: {"output_summary": state.get("escalation_reason") or "escalated to a human"},
    }


@traced_node("rejected")
async def rejected_node(state: AgentState) -> dict[str, Any]:
    approval = state.get("approval") or {}
    return {
        "outcome": "rejected",
        TRACE_KEY: {
            "output_summary": f"rejected by {approval.get('approver') or 'reviewer'}",
            "meta": {"note": approval.get("note")},
        },
    }


def new_thread_id() -> str:
    return str(uuid.uuid4())
