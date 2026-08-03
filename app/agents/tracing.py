"""Per-node tracing.

Wraps every graph node so each execution writes an `agent_traces` row with its
latency, token usage, and cost. Traces are written even when a node raises, so
a failed run can be diagnosed from the trace table alone.

A node opts into usage reporting by returning a `__trace__` key in its state
update; the wrapper strips it before the value reaches the graph state.
"""

import functools
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from langgraph.errors import GraphInterrupt

from app.agents.state import AgentState
from app.core.db import session_scope
from app.core.logging import get_logger
from app.core.pricing import TokenUsage, safe_calculate_cost, usage_from_anthropic
from app.models.agent import AgentRun, AgentTrace

logger = get_logger(__name__)

TRACE_KEY = "__trace__"
MAX_SUMMARY_CHARS = 2000

NodeFn = Callable[[AgentState], Awaitable[dict[str, Any]]]


def _clip(text: Any) -> str | None:
    if text is None:
        return None
    s = str(text)
    return s if len(s) <= MAX_SUMMARY_CHARS else s[:MAX_SUMMARY_CHARS] + " […]"


async def _write_trace(
    *,
    run_id: str,
    ticket_id: str,
    step_index: int,
    node_name: str,
    status: str,
    latency_ms: int,
    model: str | None = None,
    usage: TokenUsage | None = None,
    input_summary: Any = None,
    output_summary: Any = None,
    error: str | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    usage = usage or TokenUsage()
    cost = safe_calculate_cost(model, usage) if model and usage.total_tokens else None

    try:
        async with session_scope() as db:
            db.add(
                AgentTrace(
                    run_id=run_id,
                    ticket_id=ticket_id,
                    step_index=step_index,
                    node_name=node_name,
                    status=status,
                    model=model,
                    input_summary=_clip(input_summary),
                    output_summary=_clip(output_summary),
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cache_read_tokens=usage.cache_read_tokens,
                    cache_write_tokens=usage.cache_write_tokens,
                    cost_usd=cost,
                    latency_ms=latency_ms,
                    error=error,
                    meta=meta or {},
                    created_at=datetime.now(UTC),
                )
            )
            if usage.total_tokens or cost:
                run = await db.get(AgentRun, run_id)
                if run is not None:
                    run.total_input_tokens += usage.input_tokens + usage.cache_read_tokens
                    run.total_output_tokens += usage.output_tokens
                    if cost:
                        run.total_cost_usd = run.total_cost_usd + cost
    except Exception as exc:  # pragma: no cover - tracing must never break a run
        logger.error("trace_write_failed", node=node_name, error=str(exc))


def traced_node(name: str) -> Callable[[NodeFn], NodeFn]:
    """Decorate a graph node so its execution is timed, costed, and recorded."""

    def decorator(fn: NodeFn) -> NodeFn:
        @functools.wraps(fn)
        async def wrapper(state: AgentState) -> dict[str, Any]:
            step_index = int(state.get("step_index") or 0)
            run_id = state.get("run_id")
            ticket_id = state.get("ticket_id")
            started = time.perf_counter()

            try:
                update = await fn(state) or {}
            except GraphInterrupt:
                # Not a failure: the approval node suspends the run this way.
                # LangGraph checkpoints and re-enters the node on resume, which
                # is when the real trace row gets written.
                logger.info("node_suspended", node=name, step=step_index)
                raise
            except Exception as exc:
                latency_ms = int((time.perf_counter() - started) * 1000)
                logger.exception("node_failed", node=name, error=str(exc))
                if run_id and ticket_id:
                    await _write_trace(
                        run_id=run_id,
                        ticket_id=ticket_id,
                        step_index=step_index,
                        node_name=name,
                        status="error",
                        latency_ms=latency_ms,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                raise

            latency_ms = int((time.perf_counter() - started) * 1000)
            trace_info: dict[str, Any] = update.pop(TRACE_KEY, {}) or {}

            usage_raw = trace_info.get("usage")
            usage = (
                usage_raw
                if isinstance(usage_raw, TokenUsage)
                else usage_from_anthropic(usage_raw)
            )

            if run_id and ticket_id:
                await _write_trace(
                    run_id=run_id,
                    ticket_id=ticket_id,
                    step_index=step_index,
                    node_name=name,
                    status=trace_info.get("status", "ok"),
                    latency_ms=latency_ms,
                    model=trace_info.get("model"),
                    usage=usage,
                    input_summary=trace_info.get("input_summary"),
                    output_summary=trace_info.get("output_summary"),
                    meta=trace_info.get("meta"),
                )

            logger.info("node_complete", node=name, step=step_index, latency_ms=latency_ms)
            update["step_index"] = step_index + 1
            return update

        return wrapper

    return decorator
