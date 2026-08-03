"""The escalation gate.

Deliberately deterministic and separate from the model: whether a human must
look at a ticket is a policy decision, so it is decided by rules over the run's
state rather than by asking the model to judge its own work. A wrong escalation
costs an agent a minute; a wrong auto-resolution costs a customer.
"""

from dataclasses import dataclass
from typing import Any

from app.agents.state import AgentState
from app.core.config import get_settings


@dataclass
class EscalationVerdict:
    escalate: bool
    reason: str | None = None
    rule: str | None = None


def evaluate_escalation(state: AgentState) -> EscalationVerdict:
    """Decide whether this run must go to a human instead of producing a draft."""
    settings = get_settings()

    # 1. A blocking guardrail flag always wins — legal threats, safety issues,
    #    fraud, and drafts that contradict the eligibility verdict.
    for flag in state.get("guardrail_flags") or []:
        if flag.get("severity") == "block":
            return EscalationVerdict(
                escalate=True,
                reason=flag.get("detail") or "A safety guardrail blocked automated handling.",
                rule=f"guardrail:{flag.get('rule')}",
            )

    # 2. The eligibility engine can demand a human even when it says "eligible"
    #    (for example, a refund above the auto-approval cap).
    eligibility = state.get("eligibility")
    if eligibility and eligibility.get("requires_escalation"):
        return EscalationVerdict(
            escalate=True,
            reason=eligibility.get("escalation_reason") or eligibility.get("reason"),
            rule="eligibility:requires_escalation",
        )

    # 3. The classifier does not know what the ticket is about.
    confidence = state.get("confidence")
    if confidence is not None and confidence < settings.escalation_confidence_threshold:
        return EscalationVerdict(
            escalate=True,
            reason=(
                f"Classification confidence {confidence:.2f} is below the "
                f"{settings.escalation_confidence_threshold:.2f} threshold."
            ),
            rule="confidence_below_threshold",
        )

    # 4. Nothing in the corpus actually addresses this ticket, so a draft would
    #    be ungrounded — the failure mode most likely to invent a policy.
    top_score = state.get("retrieval_top_score")
    if top_score is not None and top_score < settings.escalation_retrieval_threshold:
        return EscalationVerdict(
            escalate=True,
            reason=(
                f"No policy document closely matches this ticket "
                f"(best similarity {top_score:.2f})."
            ),
            rule="retrieval_below_threshold",
        )

    # 5. A furious customer with a high-priority problem gets a person.
    if state.get("priority") == 5 and state.get("sentiment") == "VERY_NEGATIVE":
        return EscalationVerdict(
            escalate=True,
            reason="Highest-priority ticket from a very negative customer.",
            rule="priority_sentiment",
        )

    # 6. The ticket names an order we could not read; drafting would be guesswork.
    if state.get("order_error"):
        return EscalationVerdict(
            escalate=True,
            reason=f"Could not read the referenced order: {state['order_error']}",
            rule="order_lookup_failed",
        )

    # 7. The draft failed output guardrails twice; stop retrying.
    if (state.get("revision_count") or 0) > 1:
        return EscalationVerdict(
            escalate=True,
            reason="The drafted reply failed automated review after a revision attempt.",
            rule="revision_limit_reached",
        )

    return EscalationVerdict(escalate=False)


def summarise_for_human(state: AgentState, verdict: EscalationVerdict) -> dict[str, Any]:
    """Context a human needs to pick the ticket up cold."""
    return {
        "reason": verdict.reason,
        "rule": verdict.rule,
        "category": state.get("category"),
        "priority": state.get("priority"),
        "sentiment": state.get("sentiment"),
        "confidence": state.get("confidence"),
        "order_ref": state.get("order_ref"),
        "eligibility": state.get("eligibility"),
        "guardrail_flags": state.get("guardrail_flags") or [],
    }
