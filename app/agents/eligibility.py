"""Deterministic refund/replacement eligibility.

The model never decides whether money moves. It receives this engine's verdict
as a binding input and writes prose around it. Everything here is pure — dates,
amounts, and policy rules in; a decision out — so it is exhaustively testable
and cannot be talked out of a verdict by a persuasive ticket.
"""

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.core.config import get_settings

# Fallbacks used only if a policy document omits the rule; the policy
# frontmatter is the real source (see data/policies/*.md).
DEFAULT_REFUND_WINDOW_DAYS = 30
DEFAULT_REPLACEMENT_WINDOW_DAYS = 45
DEFAULT_DAMAGED_ON_ARRIVAL_DAYS = 7


@dataclass
class EligibilityDecision:
    action: str  # "refund" | "replacement"
    eligible: bool
    reason: str
    # Ceiling the drafting step may propose. Serialised as a string: state is
    # JSON-checkpointed and Decimal is not JSON-native.
    approved_amount: str | None = None
    requires_escalation: bool = False
    escalation_reason: str | None = None
    # Every rule evaluated, for the trace and the UI.
    checks: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _check(name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"check": name, "passed": passed, "detail": detail}


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _money(value: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value if value is not None else default))
    except (InvalidOperation, TypeError):
        return Decimal(default)


def _days_since(moment: datetime, now: datetime) -> int:
    return (now - moment).days


def evaluate_refund(
    order: dict[str, Any],
    rules: dict[str, Any] | None = None,
    *,
    requested_amount: Decimal | str | None = None,
    now: datetime | None = None,
) -> EligibilityDecision:
    """Decide whether a refund may be issued, and for how much."""
    settings = get_settings()
    rules = rules or {}
    now = now or datetime.now(UTC)
    checks: list[dict[str, Any]] = []

    window_days = int(rules.get("return_window_days", DEFAULT_REFUND_WINDOW_DAYS))
    cap = _money(rules.get("max_auto_refund_usd", settings.max_auto_refund_usd))

    total = _money(order.get("total_amount"))
    already_refunded = _money(order.get("refunded_amount"))
    refundable = total - already_refunded
    status = str(order.get("status", "")).upper()
    delivered_at = _parse_dt(order.get("delivered_at"))
    is_final_sale = bool(order.get("is_final_sale"))

    def deny(reason: str) -> EligibilityDecision:
        return EligibilityDecision(
            action="refund", eligible=False, reason=reason, checks=checks
        )

    # 1. Cancelled orders are refunded at cancellation; nothing left to do.
    if status == "CANCELLED":
        checks.append(_check("order_status", False, "Order was cancelled"))
        return deny("This order was cancelled and refunded at the time of cancellation.")
    checks.append(_check("order_status", True, f"Order status is {status}"))

    # 2. Something must remain to refund.
    if refundable <= 0:
        checks.append(
            _check("refundable_balance", False, f"Refunded {already_refunded} of {total}")
        )
        return deny("This order has already been refunded in full.")
    checks.append(_check("refundable_balance", True, f"{refundable} remains refundable"))

    # 3. Final sale items are never refundable (replacement may still apply).
    if is_final_sale:
        checks.append(_check("final_sale", False, "Item was sold as final sale"))
        return deny(
            "This item was sold as final sale, which policy excludes from refunds. "
            "A replacement may still be possible if it arrived damaged or defective."
        )
    checks.append(_check("final_sale", True, "Not a final sale item"))

    # 4. The refund window runs from delivery, so undelivered orders are a
    #    cancellation question, not a refund one.
    if delivered_at is None:
        checks.append(_check("delivered", False, f"Not delivered (status {status})"))
        return deny(
            "This order has not been delivered yet, so the refund window has not started. "
            "An order that has not shipped can be cancelled for a full refund instead."
        )
    days_since_delivery = _days_since(delivered_at, now)
    checks.append(
        _check("delivered", True, f"Delivered {days_since_delivery} day(s) ago")
    )

    # 5. The window itself.
    if days_since_delivery > window_days:
        checks.append(
            _check(
                "within_window",
                False,
                f"{days_since_delivery} days elapsed, window is {window_days}",
            )
        )
        return EligibilityDecision(
            action="refund",
            eligible=False,
            reason=(
                f"This order was delivered {days_since_delivery} days ago, outside the "
                f"{window_days}-day refund window. A human agent may grant a goodwill "
                "exception, which cannot be promised automatically."
            ),
            requires_escalation=True,
            escalation_reason="Refund requested outside the policy window",
            checks=checks,
        )
    checks.append(
        _check("within_window", True, f"{days_since_delivery} of {window_days} days used")
    )

    # 6. Amount: never above the remaining balance.
    approved = refundable
    if requested_amount is not None:
        requested = _money(requested_amount)
        if requested <= 0:
            return deny("The requested refund amount must be greater than zero.")
        if requested > refundable:
            checks.append(
                _check(
                    "requested_amount",
                    False,
                    f"Requested {requested} exceeds refundable {refundable}",
                )
            )
            return EligibilityDecision(
                action="refund",
                eligible=False,
                reason=(
                    f"The requested amount ({requested}) is more than the remaining "
                    f"refundable balance on this order ({refundable})."
                ),
                requires_escalation=True,
                escalation_reason="Requested refund exceeds the order balance",
                checks=checks,
            )
        approved = requested
        checks.append(_check("requested_amount", True, f"Requested {requested} is available"))

    # 7. Value cap: eligible, but a human with more authority must approve.
    if approved > cap:
        checks.append(_check("auto_approval_cap", False, f"{approved} exceeds cap {cap}"))
        return EligibilityDecision(
            action="refund",
            eligible=True,
            reason=(
                f"This refund of {approved} is within policy but above the {cap} "
                "auto-approval limit, so it needs senior approval."
            ),
            approved_amount=str(approved),
            requires_escalation=True,
            escalation_reason=f"Refund of {approved} exceeds the {cap} auto-approval limit",
            checks=checks,
        )
    checks.append(_check("auto_approval_cap", True, f"{approved} is within cap {cap}"))

    return EligibilityDecision(
        action="refund",
        eligible=True,
        reason=(
            f"Delivered {days_since_delivery} days ago, inside the {window_days}-day "
            f"window, with {approved} refundable."
        ),
        approved_amount=str(approved),
        checks=checks,
    )


def evaluate_replacement(
    order: dict[str, Any],
    rules: dict[str, Any] | None = None,
    *,
    damaged_on_arrival: bool = False,
    prior_replacements: int = 0,
    now: datetime | None = None,
) -> EligibilityDecision:
    """Decide whether a replacement may be issued."""
    rules = rules or {}
    now = now or datetime.now(UTC)
    checks: list[dict[str, Any]] = []

    window_days = int(rules.get("replacement_window_days", DEFAULT_REPLACEMENT_WINDOW_DAYS))
    doa_days = int(
        rules.get("damaged_on_arrival_window_days", DEFAULT_DAMAGED_ON_ARRIVAL_DAYS)
    )
    max_replacements = int(rules.get("max_replacements_per_order", 1))

    status = str(order.get("status", "")).upper()
    delivered_at = _parse_dt(order.get("delivered_at"))

    def deny(reason: str, *, escalate: bool = False, why: str | None = None):
        return EligibilityDecision(
            action="replacement",
            eligible=False,
            reason=reason,
            requires_escalation=escalate,
            escalation_reason=why,
            checks=checks,
        )

    if status == "CANCELLED":
        checks.append(_check("order_status", False, "Order was cancelled"))
        return deny("This order was cancelled, so there is nothing to replace.")
    checks.append(_check("order_status", True, f"Order status is {status}"))

    # Repeat replacements may signal a fulfilment problem or abuse; a human looks.
    if prior_replacements >= max_replacements:
        checks.append(
            _check(
                "replacement_limit",
                False,
                f"{prior_replacements} replacement(s) already issued",
            )
        )
        return deny(
            "A replacement has already been issued for this order, so a second one "
            "needs review by a human agent.",
            escalate=True,
            why="Second replacement requested on the same order",
        )
    checks.append(_check("replacement_limit", True, f"{prior_replacements} prior replacement(s)"))

    if delivered_at is None:
        checks.append(_check("delivered", False, f"Not delivered (status {status})"))
        return deny(
            "This order has not been delivered yet. If it is delayed or lost, that is "
            "handled as a shipping issue rather than a replacement."
        )

    days_since_delivery = _days_since(delivered_at, now)
    checks.append(_check("delivered", True, f"Delivered {days_since_delivery} day(s) ago"))

    if days_since_delivery > window_days:
        checks.append(
            _check(
                "within_window",
                False,
                f"{days_since_delivery} days elapsed, window is {window_days}",
            )
        )
        return deny(
            f"This order was delivered {days_since_delivery} days ago, outside the "
            f"{window_days}-day replacement window. If the item is faulty it may still "
            "be covered by warranty.",
            escalate=True,
            why="Replacement requested outside the policy window",
        )
    checks.append(
        _check("within_window", True, f"{days_since_delivery} of {window_days} days used")
    )

    # Final sale is refund-exempt but replaceable when it arrived damaged — the
    # single documented exception to the final-sale rule.
    if bool(order.get("is_final_sale")) and not damaged_on_arrival:
        checks.append(
            _check("final_sale", False, "Final sale item, no damage-on-arrival reported")
        )
        return deny(
            "This item was sold as final sale. Final sale items can only be replaced "
            "if they arrived damaged or defective."
        )
    checks.append(_check("final_sale", True, "Final sale rule does not block this replacement"))

    if damaged_on_arrival:
        within_doa = days_since_delivery <= doa_days
        checks.append(
            _check(
                "damaged_on_arrival",
                True,
                (
                    f"Reported {days_since_delivery} day(s) after delivery; "
                    f"{'inside' if within_doa else 'outside'} the {doa_days}-day window"
                    f"{'' if within_doa else ', so the original must be returned first'}"
                ),
            )
        )

    return EligibilityDecision(
        action="replacement",
        eligible=True,
        reason=(
            f"Delivered {days_since_delivery} days ago, inside the {window_days}-day "
            "replacement window."
        ),
        checks=checks,
    )
