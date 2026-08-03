"""Graph topology.

    input_guardrails → classify → triage_gate
                                     ├── escalate ──────────────► escalated → END
                                     └── continue
                                          ↓
                    retrieve_policy → fetch_order → check_eligibility → review_gate
                                     ├── escalate ──────────────► escalated → END
                                     └── continue
                                          ↓
                     draft_response → output_guardrails → post_draft_gate
                                     ├── revise ──► draft_response (once)
                                     ├── escalate ►  escalated → END
                                     └── approve
                                          ↓
                                  await_approval  ⟵ INTERRUPT (human decides)
                                     ├── reject ──► rejected → END
                                     └── approve ─► execute_actions → send_reply → END

The gate appears twice on purpose. The first catches tickets that must never
reach a model-written draft (legal threats, safety issues); the second catches
what only becomes knowable after retrieval and eligibility ran.
"""

import asyncio
import contextlib
from typing import Literal

from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph

from app.agents import nodes
from app.agents.state import AgentState
from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

MAX_REVISIONS = 1


def _route_after_triage(state: AgentState) -> Literal["escalated", "retrieve_policy"]:
    return "escalated" if state.get("escalate") else "retrieve_policy"


def _route_after_review(state: AgentState) -> Literal["escalated", "draft_response"]:
    return "escalated" if state.get("escalate") else "draft_response"


def _route_after_draft(
    state: AgentState,
) -> Literal["revise", "escalated", "await_approval"]:
    """Decide what to do with a freshly checked draft.

    Runs after `post_draft_gate`, so `escalate` already reflects every blocking
    flag. The split below decides *who* fixes the problem: a wording failure
    the model can retry, versus a breach that a human must see.
    """
    if not state.get("escalate"):
        return "await_approval"

    blocking = [
        f for f in (state.get("guardrail_flags") or []) if f.get("severity") == "block"
    ]
    # An authority breach or an always-escalate topic is never retried — asking
    # the model to try again is the wrong response to it overstepping.
    if any(f.get("layer") != "output" for f in blocking):
        return "escalated"

    # A phrasing failure earns exactly one chance to fix itself.
    if blocking and int(state.get("revision_count") or 0) < MAX_REVISIONS:
        return "revise"
    return "escalated"


async def _bump_revision(state: AgentState) -> dict:
    """Increment the revision counter between the two drafting attempts."""
    return {"revision_count": int(state.get("revision_count") or 0) + 1}


def _route_after_approval(state: AgentState) -> Literal["rejected", "execute_actions"]:
    approval = state.get("approval") or {}
    return "rejected" if approval.get("decision") == "reject" else "execute_actions"


def build_graph() -> StateGraph:
    """Assemble the graph. Compilation (and the checkpointer) happens separately."""
    graph = StateGraph(AgentState)

    graph.add_node("input_guardrails", nodes.input_guardrails_node)
    graph.add_node("classify", nodes.classify_node)
    graph.add_node("triage_gate", nodes.escalation_gate_node)
    graph.add_node("retrieve_policy", nodes.retrieve_policy_node)
    graph.add_node("fetch_order", nodes.fetch_order_node)
    graph.add_node("check_eligibility", nodes.check_eligibility_node)
    graph.add_node("review_gate", nodes.escalation_gate_node)
    graph.add_node("draft_response", nodes.draft_response_node)
    graph.add_node("output_guardrails", nodes.output_guardrails_node)
    graph.add_node("post_draft_gate", nodes.escalation_gate_node)
    graph.add_node("revise", _bump_revision)
    graph.add_node("await_approval", nodes.await_approval_node)
    graph.add_node("execute_actions", nodes.execute_actions_node)
    graph.add_node("send_reply", nodes.send_reply_node)
    graph.add_node("escalated", nodes.escalated_node)
    graph.add_node("rejected", nodes.rejected_node)

    graph.add_edge(START, "input_guardrails")
    graph.add_edge("input_guardrails", "classify")
    graph.add_edge("classify", "triage_gate")
    graph.add_conditional_edges("triage_gate", _route_after_triage)

    graph.add_edge("retrieve_policy", "fetch_order")
    graph.add_edge("fetch_order", "check_eligibility")
    graph.add_edge("check_eligibility", "review_gate")
    graph.add_conditional_edges("review_gate", _route_after_review)

    graph.add_edge("draft_response", "output_guardrails")
    graph.add_edge("output_guardrails", "post_draft_gate")
    graph.add_conditional_edges("post_draft_gate", _route_after_draft)
    graph.add_edge("revise", "draft_response")

    graph.add_conditional_edges("await_approval", _route_after_approval)
    graph.add_edge("execute_actions", "send_reply")

    graph.add_edge("send_reply", END)
    graph.add_edge("escalated", END)
    graph.add_edge("rejected", END)

    return graph


_compiled = None
_compiled_loop: asyncio.AbstractEventLoop | None = None
_saver_cm = None
_schema_ready = False


async def ensure_checkpointer_schema() -> None:
    """Create the checkpoint tables, once, before any request is served.

    This must not happen lazily inside a request. `setup()` issues
    `CREATE INDEX CONCURRENTLY`, which waits for every open transaction in the
    database to finish — including the request's own session. On a fresh
    database that is a permanent hang, and it hides until the first deploy
    because an already-migrated database makes `setup()` a no-op.

    Call it from application startup and from the migration step.
    """
    global _schema_ready
    if _schema_ready:
        return

    settings = get_settings()
    async with AsyncPostgresSaver.from_conn_string(settings.sync_database_url) as saver:
        await saver.setup()
    _schema_ready = True
    logger.info("checkpointer_schema_ready")


async def get_compiled_graph():
    """Compile the graph against the Postgres checkpointer.

    The checkpointer is what makes the approval interrupt durable: state is
    persisted per thread, so a run can be suspended, the process restarted, and
    the run resumed by whichever worker handles the approval.

    The compiled graph is cached per event loop. psycopg's async connection is
    bound to the loop that opened it, so reusing a cached graph on a different
    loop fails with "the connection is closed" — which is what happens to a
    test suite that creates a fresh loop per test, and to any script that calls
    `asyncio.run` more than once.
    """
    global _compiled, _compiled_loop, _saver_cm

    loop = asyncio.get_running_loop()
    if _compiled is not None and _compiled_loop is loop:
        return _compiled

    if _saver_cm is not None:
        # Abandon the previous loop's connection rather than leaking it.
        await _close_saver()

    # Schema creation is separate (see ensure_checkpointer_schema); doing it
    # here would run DDL while a caller's transaction is open.
    await ensure_checkpointer_schema()

    settings = get_settings()
    _saver_cm = AsyncPostgresSaver.from_conn_string(settings.sync_database_url)
    saver = await _saver_cm.__aenter__()

    _compiled = build_graph().compile(checkpointer=saver)
    _compiled_loop = loop
    logger.info("graph_compiled", checkpointer="postgres")
    return _compiled


async def _close_saver() -> None:
    global _saver_cm
    # The previous loop is usually already closed, which makes teardown raise;
    # dropping the reference is what actually matters.
    with contextlib.suppress(Exception):
        await _saver_cm.__aexit__(None, None, None)  # type: ignore[union-attr]
    _saver_cm = None


def build_in_memory_graph():
    """Compile against an in-memory checkpointer, for tests that need no durability."""
    return build_graph().compile(checkpointer=MemorySaver())


def reset_compiled_graph() -> None:
    """Drop the cached graph. Used between tests."""
    global _compiled, _compiled_loop, _saver_cm
    _compiled = None
    _compiled_loop = None
    _saver_cm = None
