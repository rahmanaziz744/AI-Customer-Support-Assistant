"""Action tools offered to the drafting node.

These tools **record an intent**; none of them moves money. A tool call becomes
an entry in `proposed_actions`, which a human sees and approves before
`execute_actions` calls the order API. That separation is what makes the
human-approval gate real rather than advisory — there is no code path from a
model tool call to a refund.
"""

from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, Field


class ProposeRefund(BaseModel):
    """Recommend refunding the customer. Only permitted when the eligibility
    decision says a refund is eligible, and never above its approved amount."""

    amount: float = Field(description="Refund amount in the order currency, e.g. 89.99")
    reason: str = Field(description="Why this refund is warranted, citing the policy")


class ProposeReplacement(BaseModel):
    """Recommend sending a replacement item. Only permitted when the
    eligibility decision says a replacement is eligible."""

    reason: str = Field(description="Why a replacement is warranted, citing the policy")


class ProposeNoAction(BaseModel):
    """Recommend replying without any refund or replacement. Use this for
    informational tickets and for requests that policy does not support."""

    reason: str = Field(description="Why no order action is needed")


ACTION_TOOLS = [ProposeRefund, ProposeReplacement, ProposeNoAction]

TOOL_BY_NAME = {
    "ProposeRefund": "refund",
    "ProposeReplacement": "replacement",
    "ProposeNoAction": "none",
}


def _coerce_amount(value: Any) -> str | None:
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return str(amount) if amount > 0 else None


def normalise_tool_calls(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert raw LangChain tool calls into proposed-action records.

    Amounts are carried as strings because the state is JSON-checkpointed and
    float money would round badly on the way to a payment call.
    """
    actions: list[dict[str, Any]] = []

    for call in tool_calls:
        name = call.get("name") or ""
        args = call.get("args") or {}
        action_type = TOOL_BY_NAME.get(name)
        if action_type is None:
            continue

        action: dict[str, Any] = {
            "type": action_type,
            "reason": str(args.get("reason") or "").strip(),
            "tool_call_id": call.get("id"),
        }
        if action_type == "refund":
            amount = _coerce_amount(args.get("amount"))
            if amount is None:
                # A refund proposal without a usable amount is unexecutable;
                # keep it visible to the human but mark it invalid.
                action["invalid"] = "missing_or_invalid_amount"
            action["amount"] = amount
        actions.append(action)

    return actions


def clamp_to_eligibility(
    actions: list[dict[str, Any]], eligibility: dict[str, Any] | None
) -> tuple[list[dict[str, Any]], list[str]]:
    """Drop or cap proposals the eligibility decision does not permit.

    Belt to the output guardrail's braces: the guardrail *flags* a contradiction
    and escalates, this makes sure nothing over-permissive survives into the
    approval queue in the first place.
    """
    notes: list[str] = []
    if not eligibility:
        # No eligibility decision was computed, so only no-action survives.
        kept = [a for a in actions if a["type"] == "none"]
        if len(kept) != len(actions):
            notes.append("Dropped an order action proposed without an eligibility decision.")
        return kept, notes

    eligible = bool(eligibility.get("eligible"))
    decided_action = str(eligibility.get("action") or "")
    approved_raw = eligibility.get("approved_amount")

    kept: list[dict[str, Any]] = []
    for action in actions:
        if action["type"] == "none":
            kept.append(action)
            continue

        if action["type"] != decided_action or not eligible:
            notes.append(
                f"Dropped proposed {action['type']}: eligibility decided "
                f"{decided_action or 'none'} / eligible={eligible}."
            )
            continue

        if action["type"] == "refund" and approved_raw:
            try:
                approved = Decimal(str(approved_raw))
                proposed = Decimal(str(action.get("amount") or "0"))
            except InvalidOperation:
                notes.append("Dropped proposed refund: amount could not be parsed.")
                continue
            if proposed > approved:
                action["amount"] = str(approved)
                action["clamped_from"] = str(proposed)
                notes.append(f"Capped proposed refund from {proposed} to approved {approved}.")

        kept.append(action)

    return kept, notes
