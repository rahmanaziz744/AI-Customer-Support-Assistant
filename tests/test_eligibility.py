"""Eligibility engine.

The most safety-critical unit in the project: it decides whether money moves.
Pure functions, so every branch is covered without a database or a model.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.agents.eligibility import evaluate_refund, evaluate_replacement

REFUND_RULES = {"return_window_days": 30, "max_auto_refund_usd": 500}
REPLACEMENT_RULES = {
    "replacement_window_days": 45,
    "damaged_on_arrival_window_days": 7,
    "max_replacements_per_order": 1,
}


def order(**overrides) -> dict:
    base = {
        "order_ref": "ORD-1",
        "status": "DELIVERED",
        "total_amount": "100.00",
        "refunded_amount": "0.00",
        "delivered_at": (datetime.now(UTC) - timedelta(days=5)).isoformat(),
        "is_final_sale": False,
    }
    base.update(overrides)
    return base


class TestRefund:
    def test_inside_window_is_eligible_for_the_full_balance(self):
        decision = evaluate_refund(order(), REFUND_RULES)
        assert decision.eligible
        assert decision.approved_amount == "100.00"
        assert not decision.requires_escalation

    def test_outside_window_is_declined_and_escalated(self):
        decision = evaluate_refund(
            order(delivered_at=(datetime.now(UTC) - timedelta(days=45)).isoformat()),
            REFUND_RULES,
        )
        assert not decision.eligible
        assert decision.requires_escalation
        assert "45 days ago" in decision.reason

    def test_boundary_day_is_still_inside_the_window(self):
        decision = evaluate_refund(
            order(delivered_at=(datetime.now(UTC) - timedelta(days=30)).isoformat()),
            REFUND_RULES,
        )
        assert decision.eligible, "day 30 of a 30-day window must still qualify"

    def test_final_sale_is_never_refundable(self):
        decision = evaluate_refund(order(is_final_sale=True), REFUND_RULES)
        assert not decision.eligible
        assert "final sale" in decision.reason.lower()

    def test_undelivered_order_has_not_started_its_window(self):
        decision = evaluate_refund(order(status="SHIPPED", delivered_at=None), REFUND_RULES)
        assert not decision.eligible
        assert "not been delivered" in decision.reason

    def test_cancelled_order_was_already_refunded(self):
        decision = evaluate_refund(order(status="CANCELLED"), REFUND_RULES)
        assert not decision.eligible

    def test_fully_refunded_order_has_nothing_left(self):
        decision = evaluate_refund(order(refunded_amount="100.00"), REFUND_RULES)
        assert not decision.eligible
        assert "in full" in decision.reason

    def test_partial_refund_caps_at_the_remaining_balance(self):
        decision = evaluate_refund(order(refunded_amount="70.00"), REFUND_RULES)
        assert decision.eligible
        assert decision.approved_amount == "30.00"

    def test_request_above_remaining_balance_is_refused(self):
        decision = evaluate_refund(
            order(refunded_amount="70.00"), REFUND_RULES, requested_amount=Decimal("50.00")
        )
        assert not decision.eligible
        assert decision.requires_escalation

    def test_request_below_balance_is_honoured_exactly(self):
        decision = evaluate_refund(order(), REFUND_RULES, requested_amount=Decimal("25.00"))
        assert decision.eligible
        assert decision.approved_amount == "25.00"

    def test_above_cap_is_eligible_but_needs_a_senior_human(self):
        decision = evaluate_refund(order(total_amount="1200.00"), REFUND_RULES)
        assert decision.eligible, "still within policy"
        assert decision.requires_escalation, "but above the auto-approval cap"
        assert "auto-approval limit" in (decision.escalation_reason or "")

    @pytest.mark.parametrize("amount", ["0", "-5"])
    def test_non_positive_request_is_rejected(self, amount):
        decision = evaluate_refund(order(), REFUND_RULES, requested_amount=Decimal(amount))
        assert not decision.eligible

    def test_missing_rules_fall_back_to_documented_defaults(self):
        # 40 days out: outside the default 30-day window even with no rules given.
        decision = evaluate_refund(
            order(delivered_at=(datetime.now(UTC) - timedelta(days=40)).isoformat()), {}
        )
        assert not decision.eligible

    def test_every_check_is_recorded_for_the_reviewer(self):
        decision = evaluate_refund(order(), REFUND_RULES)
        names = {c["check"] for c in decision.checks}
        assert {"order_status", "refundable_balance", "final_sale", "delivered",
                "within_window", "auto_approval_cap"} <= names


class TestReplacement:
    def test_inside_window_is_eligible(self):
        decision = evaluate_replacement(order(), REPLACEMENT_RULES)
        assert decision.eligible

    def test_outside_window_is_declined_and_escalated(self):
        decision = evaluate_replacement(
            order(delivered_at=(datetime.now(UTC) - timedelta(days=60)).isoformat()),
            REPLACEMENT_RULES,
        )
        assert not decision.eligible
        assert decision.requires_escalation

    def test_final_sale_is_replaceable_only_when_damaged(self):
        undamaged = evaluate_replacement(
            order(is_final_sale=True), REPLACEMENT_RULES, damaged_on_arrival=False
        )
        damaged = evaluate_replacement(
            order(is_final_sale=True), REPLACEMENT_RULES, damaged_on_arrival=True
        )
        assert not undamaged.eligible
        assert damaged.eligible, "the documented exception to the final-sale rule"

    def test_second_replacement_escalates(self):
        decision = evaluate_replacement(
            order(), REPLACEMENT_RULES, prior_replacements=1
        )
        assert not decision.eligible
        assert decision.requires_escalation

    def test_undelivered_order_is_a_shipping_matter(self):
        decision = evaluate_replacement(
            order(status="SHIPPED", delivered_at=None), REPLACEMENT_RULES
        )
        assert not decision.eligible
        assert "not been delivered" in decision.reason

    def test_damage_reported_late_still_qualifies_but_is_noted(self):
        decision = evaluate_replacement(
            order(delivered_at=(datetime.now(UTC) - timedelta(days=20)).isoformat()),
            REPLACEMENT_RULES,
            damaged_on_arrival=True,
        )
        assert decision.eligible
        doa = next(c for c in decision.checks if c["check"] == "damaged_on_arrival")
        assert "returned first" in doa["detail"]


class TestMalformedInput:
    """The engine reads data from an external system, so it must not crash on it."""

    def test_unparsable_date_is_treated_as_undelivered(self):
        decision = evaluate_refund(order(delivered_at="not-a-date"), REFUND_RULES)
        assert not decision.eligible

    def test_unparsable_amount_does_not_raise(self):
        decision = evaluate_refund(order(total_amount="abc"), REFUND_RULES)
        assert not decision.eligible

    def test_naive_datetime_is_assumed_utc(self):
        naive = (datetime.now(UTC) - timedelta(days=5)).replace(tzinfo=None).isoformat()
        decision = evaluate_refund(order(delivered_at=naive), REFUND_RULES)
        assert decision.eligible
