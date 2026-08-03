"""Graph state.

Every value must be JSON-serialisable: the state is checkpointed to Postgres so
a run can be suspended for human approval and resumed in a different process
(or after a restart). That rules out ORM objects, Decimals, and datetimes —
money is carried as a string and timestamps as ISO strings.
"""

from typing import Any, Literal, TypedDict

Decision = Literal["approve", "reject"]


class Approval(TypedDict, total=False):
    decision: Decision
    edited_draft: str | None
    approver: str | None
    note: str | None


class AgentState(TypedDict, total=False):
    # -- identity ----------------------------------------------------------
    ticket_id: str
    run_id: str
    thread_id: str

    # -- ticket input ------------------------------------------------------
    subject: str
    body: str
    customer_email: str
    customer_name: str | None
    order_ref: str | None

    # -- classification ----------------------------------------------------
    category: str | None
    sentiment: str | None
    priority: int | None
    confidence: float | None
    classification_reasoning: str | None

    # -- retrieval ---------------------------------------------------------
    policy_chunks: list[dict[str, Any]]
    policy_context: str
    # None until retrieval runs. The escalation gate treats "no score yet" as
    # "not applicable" — a 0.0 default made the pre-retrieval gate escalate
    # every ticket for having no matching policy.
    retrieval_top_score: float | None

    # -- order & eligibility ----------------------------------------------
    order: dict[str, Any] | None
    order_error: str | None
    eligibility: dict[str, Any] | None

    # -- drafting ----------------------------------------------------------
    # `draft` is always what the model wrote. The human's edit lands in
    # `final_text` instead of overwriting it, so the two stay comparable —
    # which is the signal for judging how often the agent needed correcting.
    draft: str | None
    final_text: str | None
    proposed_actions: list[dict[str, Any]]

    # -- guardrails & control ---------------------------------------------
    guardrail_flags: list[dict[str, Any]]
    escalate: bool
    escalation_reason: str | None
    revision_count: int

    # -- human approval ----------------------------------------------------
    approval: Approval | None
    executed_actions: list[dict[str, Any]]

    # -- bookkeeping -------------------------------------------------------
    prompt_versions: dict[str, str]
    step_index: int
    outcome: str | None
    error: str | None


def initial_state(
    *,
    ticket_id: str,
    run_id: str,
    thread_id: str,
    subject: str,
    body: str,
    customer_email: str,
    customer_name: str | None = None,
    order_ref: str | None = None,
) -> AgentState:
    return AgentState(
        ticket_id=ticket_id,
        run_id=run_id,
        thread_id=thread_id,
        subject=subject,
        body=body,
        customer_email=customer_email,
        customer_name=customer_name,
        order_ref=order_ref,
        policy_chunks=[],
        policy_context="",
        retrieval_top_score=None,
        order=None,
        order_error=None,
        eligibility=None,
        draft=None,
        final_text=None,
        proposed_actions=[],
        guardrail_flags=[],
        escalate=False,
        escalation_reason=None,
        revision_count=0,
        approval=None,
        executed_actions=[],
        prompt_versions={},
        step_index=0,
        outcome=None,
        error=None,
    )
