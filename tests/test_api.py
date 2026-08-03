"""API and graph integration tests.

These drive the real graph against the real database with a scripted model, so
they cover the wiring that unit tests cannot: routing, the approval interrupt,
resume, and the fact that a refund only happens after a human approves.
"""

import pytest

from app.agents.nodes import Classification
from tests.fakes import ScriptedResponse, use_scripted_models

pytestmark = pytest.mark.db


def refund_script(amount: float = 50.0, confidence: float = 0.95) -> dict:
    return {
        "classify": ScriptedResponse(
            structured=Classification(
                category="REFUND_REQUEST",
                sentiment="NEGATIVE",
                priority=3,
                confidence=confidence,
                reasoning="Customer asks for a refund.",
            )
        ),
        "draft": ScriptedResponse(
            text=f"Hello,\n\nA refund of ${amount:.2f} is on its way.\n\nNorthwind Goods Support",
            tool_calls=[
                {"name": "ProposeRefund", "args": {"amount": amount, "reason": "Within window"}}
            ],
        ),
    }


async def create_ticket(client, **overrides) -> dict:
    payload = {
        "customer_email": "test@example.com",
        "subject": "Refund please",
        "body": "The item is faulty and I would like a refund.",
        "process": False,
        **overrides,
    }
    response = await client.post("/api/tickets", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


class TestHealth:
    async def test_liveness(self, client):
        response = await client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    async def test_readiness_reports_the_database(self, client):
        response = await client.get("/health/ready")
        assert response.json()["checks"]["database"] == "ok"


class TestTicketCrud:
    async def test_create_returns_a_new_ticket(self, client):
        ticket = await create_ticket(client)
        assert ticket["status"] == "NEW"
        assert ticket["latest_run"] is None

    async def test_email_is_normalised(self, client):
        ticket = await create_ticket(client, customer_email="MiXeD@Example.COM")
        assert ticket["customer_email"] == "mixed@example.com"

    async def test_order_ref_is_upper_cased(self, client):
        ticket = await create_ticket(client, order_ref="ord-1001")
        assert ticket["order_ref"] == "ORD-1001"

    async def test_invalid_email_is_rejected(self, client):
        response = await client.post(
            "/api/tickets",
            json={"customer_email": "not-an-email", "subject": "s", "body": "b"},
        )
        assert response.status_code == 422

    async def test_missing_fields_are_rejected(self, client):
        assert (await client.post("/api/tickets", json={"subject": "x"})).status_code == 422

    async def test_unknown_ticket_is_404(self, client):
        response = await client.get("/api/tickets/00000000-0000-0000-0000-000000000000")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"

    async def test_list_filters_by_status(self, client):
        await create_ticket(client, subject="Filterable ticket")
        response = await client.get("/api/tickets", params={"status": "NEW", "limit": 100})
        assert response.status_code == 200
        assert all(item["status"] == "NEW" for item in response.json()["items"])

    async def test_search_matches_the_subject(self, client):
        await create_ticket(client, subject="Unique-Needle-Subject")
        response = await client.get("/api/tickets", params={"search": "Unique-Needle"})
        assert response.json()["total"] >= 1

    async def test_delete_removes_the_ticket(self, client):
        ticket = await create_ticket(client)
        assert (await client.delete(f"/api/tickets/{ticket['id']}")).status_code == 204
        assert (await client.get(f"/api/tickets/{ticket['id']}")).status_code == 404


class TestApprovalFlow:
    async def test_run_suspends_awaiting_approval(self, client, seeded_order):
        use_scripted_models(refund_script())
        ticket = await create_ticket(client, order_ref=seeded_order.order_ref)

        run = (await client.post(f"/api/tickets/{ticket['id']}/process")).json()

        assert run["status"] == "AWAITING_APPROVAL"
        assert run["draft_response"]
        assert run["eligibility"]["eligible"] is True
        assert run["proposed_actions"][0]["type"] == "refund"

    async def test_nothing_is_refunded_before_approval(self, client, seeded_order):
        """The whole point of the gate: a proposed refund must not have moved money."""
        use_scripted_models(refund_script())
        ticket = await create_ticket(client, order_ref=seeded_order.order_ref)
        await client.post(f"/api/tickets/{ticket['id']}/process")

        order = (await client.get(f"/mock-api/orders/{seeded_order.order_ref}")).json()
        assert order["refunded_amount"] == "0.00"

    async def test_approval_executes_the_refund_and_resolves(self, client, seeded_order):
        use_scripted_models(refund_script())
        ticket = await create_ticket(client, order_ref=seeded_order.order_ref)
        await client.post(f"/api/tickets/{ticket['id']}/process")

        run = (
            await client.post(
                f"/api/tickets/{ticket['id']}/approve", json={"approver": "a@b.test"}
            )
        ).json()

        assert run["status"] == "COMPLETED"
        assert run["executed_actions"][0]["status"] == "executed"

        order = (await client.get(f"/mock-api/orders/{seeded_order.order_ref}")).json()
        assert order["refunded_amount"] == "50.00"

        detail = (await client.get(f"/api/tickets/{ticket['id']}")).json()
        assert detail["status"] == "RESOLVED"

    async def test_edited_draft_is_what_gets_sent(self, client, seeded_order):
        use_scripted_models(refund_script())
        ticket = await create_ticket(client, order_ref=seeded_order.order_ref)
        await client.post(f"/api/tickets/{ticket['id']}/process")

        run = (
            await client.post(
                f"/api/tickets/{ticket['id']}/approve",
                json={"approver": "a@b.test", "edited_draft": "Rewritten by a human."},
            )
        ).json()

        assert run["final_response"] == "Rewritten by a human."
        assert run["draft_response"] != run["final_response"], "the original is preserved"

    async def test_rejection_sends_nothing_and_refunds_nothing(self, client, seeded_order):
        use_scripted_models(refund_script())
        ticket = await create_ticket(client, order_ref=seeded_order.order_ref)
        await client.post(f"/api/tickets/{ticket['id']}/process")

        run = (
            await client.post(
                f"/api/tickets/{ticket['id']}/reject", json={"approver": "a@b.test"}
            )
        ).json()

        assert run["status"] == "REJECTED"
        order = (await client.get(f"/mock-api/orders/{seeded_order.order_ref}")).json()
        assert order["refunded_amount"] == "0.00"

    async def test_deciding_twice_conflicts(self, client, seeded_order):
        use_scripted_models(refund_script())
        ticket = await create_ticket(client, order_ref=seeded_order.order_ref)
        await client.post(f"/api/tickets/{ticket['id']}/process")
        await client.post(f"/api/tickets/{ticket['id']}/approve", json={})

        again = await client.post(f"/api/tickets/{ticket['id']}/approve", json={})
        assert again.status_code == 409

    async def test_approving_an_unprocessed_ticket_is_404(self, client):
        ticket = await create_ticket(client)
        response = await client.post(f"/api/tickets/{ticket['id']}/approve", json={})
        assert response.status_code == 404

    async def test_processing_twice_conflicts(self, client, seeded_order):
        use_scripted_models(refund_script())
        ticket = await create_ticket(client, order_ref=seeded_order.order_ref)
        await client.post(f"/api/tickets/{ticket['id']}/process")
        again = await client.post(f"/api/tickets/{ticket['id']}/process")
        assert again.status_code == 409


class TestEscalation:
    async def test_legal_threat_escalates_without_drafting(self, client, seeded_order):
        use_scripted_models(
            {
                **refund_script(),
                "draft": ScriptedResponse(text="THIS MUST NOT BE REACHED"),
            }
        )
        ticket = await create_ticket(
            client,
            body="I am contacting my lawyer about taking legal action.",
            order_ref=seeded_order.order_ref,
        )

        run = (await client.post(f"/api/tickets/{ticket['id']}/process")).json()

        assert run["status"] == "ESCALATED"
        assert not run["draft_response"]
        assert "legal action" in run["escalation_reason"]

    async def test_over_refund_attempt_is_blocked_and_escalated(self, client, seeded_order):
        """The model proposes far more than the engine approved."""
        use_scripted_models(refund_script(amount=10_000.0))
        ticket = await create_ticket(client, order_ref=seeded_order.order_ref)

        run = (await client.post(f"/api/tickets/{ticket['id']}/process")).json()

        assert run["status"] == "ESCALATED"
        assert all(
            a["type"] == "none" or a.get("amount") != "10000.00"
            for a in run["proposed_actions"]
        )
        order = (await client.get(f"/mock-api/orders/{seeded_order.order_ref}")).json()
        assert order["refunded_amount"] == "0.00"

    async def test_low_confidence_escalates(self, client, seeded_order):
        use_scripted_models(refund_script(confidence=0.2))
        ticket = await create_ticket(client, order_ref=seeded_order.order_ref)

        run = (await client.post(f"/api/tickets/{ticket['id']}/process")).json()

        assert run["status"] == "ESCALATED"
        assert "confidence" in (run["escalation_reason"] or "").lower()

    async def test_unknown_order_escalates(self, client):
        use_scripted_models(refund_script())
        ticket = await create_ticket(client, order_ref="ORD-DOES-NOT-EXIST")

        run = (await client.post(f"/api/tickets/{ticket['id']}/process")).json()

        assert run["status"] == "ESCALATED"


class TestTraceAndStats:
    async def test_trace_records_every_node_with_cost(self, client, seeded_order):
        use_scripted_models(refund_script())
        ticket = await create_ticket(client, order_ref=seeded_order.order_ref)
        await client.post(f"/api/tickets/{ticket['id']}/process")

        trace = (await client.get(f"/api/tickets/{ticket['id']}/trace")).json()

        nodes = [step["node_name"] for step in trace["steps"]]
        assert nodes[0] == "input_guardrails"
        for expected in ("classify", "retrieve_policy", "check_eligibility", "draft_response"):
            assert expected in nodes
        assert float(trace["total_cost_usd"]) > 0
        assert trace["prompt_versions"]["draft"] == "v1"

    async def test_trace_is_404_before_any_run(self, client):
        ticket = await create_ticket(client)
        assert (await client.get(f"/api/tickets/{ticket['id']}/trace")).status_code == 404

    async def test_stats_are_well_formed(self, client):
        stats = (await client.get("/api/stats")).json()
        assert stats["tickets_total"] >= 0
        assert 0.0 <= stats["escalation_rate"] <= 1.0
        assert 0.0 <= stats["auto_resolution_rate"] <= 1.0


class TestMockOrderApi:
    async def test_refund_is_idempotent(self, client, seeded_order):
        body = {"amount": "10.00", "reason": "test", "idempotency_key": "k-1"}
        first = await client.post(f"/mock-api/orders/{seeded_order.order_ref}/refund", json=body)
        second = await client.post(f"/mock-api/orders/{seeded_order.order_ref}/refund", json=body)

        assert first.json()["replayed"] is False
        assert second.json()["replayed"] is True

        order = (await client.get(f"/mock-api/orders/{seeded_order.order_ref}")).json()
        assert order["refunded_amount"] == "10.00", "the replay must not double-charge"

    async def test_over_refund_is_rejected(self, client, seeded_order):
        response = await client.post(
            f"/mock-api/orders/{seeded_order.order_ref}/refund",
            json={"amount": "99999.00", "reason": "too much"},
        )
        assert response.status_code == 422

    async def test_unknown_order_is_404(self, client):
        assert (await client.get("/mock-api/orders/NOPE")).status_code == 404
