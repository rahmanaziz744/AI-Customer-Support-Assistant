"""Rolling ceiling on model spend.

Cost is already recorded per node in `agent_traces` (see `core.pricing` and
`agents.tracing`); this module reads it back and refuses to start new agent
work once a rolling 24-hour window exceeds the configured cap. That makes the
existing accounting load-bearing rather than merely informational, which is the
difference between knowing what a public demo costs and bounding it.

Traces rather than `agent_runs.total_cost_usd`: a trace row lands as each node
finishes, so a run still in flight already counts against the budget. Summing
the run totals would only see spend after the whole run completed, which is
exactly when it is too late.

This is the innermost of three limits, and the weakest. Outside it sit
`MAX_CONCURRENT_RUNS` (how much can be in flight at once) and, the only one an
application bug cannot bypass, a spend limit set on the Anthropic workspace the
deployed key belongs to. Configure that one too.
"""

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import BudgetExceededError
from app.core.logging import get_logger
from app.models.agent import AgentTrace

logger = get_logger(__name__)

WINDOW = timedelta(hours=24)


@dataclass(frozen=True)
class BudgetStatus:
    """A spend reading against the configured ceiling."""

    spend_usd: Decimal
    limit_usd: Decimal

    @property
    def enforced(self) -> bool:
        """False when no ceiling is configured, which disables enforcement."""
        return self.limit_usd > 0

    @property
    def exhausted(self) -> bool:
        return self.enforced and self.spend_usd >= self.limit_usd

    @property
    def ratio(self) -> float:
        """Fraction of the ceiling consumed; 0.0 when unenforced."""
        if not self.enforced:
            return 0.0
        return float(self.spend_usd / self.limit_usd)

    @property
    def remaining_usd(self) -> Decimal:
        if not self.enforced:
            return Decimal("0")
        return max(self.limit_usd - self.spend_usd, Decimal("0"))


async def window_spend_usd(db: AsyncSession, *, now: datetime | None = None) -> Decimal:
    """Total model spend over the trailing window, straight from the database."""
    since = (now or datetime.now(UTC)) - WINDOW
    total = (
        await db.execute(
            select(func.coalesce(func.sum(AgentTrace.cost_usd), 0)).where(
                AgentTrace.created_at >= since
            )
        )
    ).scalar_one()
    return Decimal(total)


# (monotonic_deadline, reading). Re-read rather than invalidated, so a burst of
# requests costs one aggregate instead of one per request. The cost of the
# staleness is bounded overshoot: at most one cache window of runs can start
# after the ceiling is actually reached.
_cache: tuple[float, Decimal] | None = None
_cache_lock: asyncio.Lock | None = None
_cache_lock_loop: asyncio.AbstractEventLoop | None = None


def _lock() -> asyncio.Lock:
    """One lock per event loop.

    Same constraint as the graph cache in `agents.graph`: an asyncio primitive
    must not be shared across loops, and a test suite creates a fresh loop per
    test.
    """
    global _cache_lock, _cache_lock_loop

    loop = asyncio.get_running_loop()
    if _cache_lock is None or _cache_lock_loop is not loop:
        _cache_lock = asyncio.Lock()
        _cache_lock_loop = loop
    return _cache_lock


def reset_cache() -> None:
    """Drop the cached reading. For tests and for forcing a fresh snapshot."""
    global _cache
    _cache = None


async def budget_status(db: AsyncSession, *, use_cache: bool = True) -> BudgetStatus:
    """Current spend against the ceiling, reusing a recent reading by default."""
    global _cache

    settings = get_settings()
    limit = Decimal(str(settings.daily_budget_usd))

    if not use_cache:
        spend = await window_spend_usd(db)
        _cache = (time.monotonic() + settings.budget_cache_seconds, spend)
        return BudgetStatus(spend_usd=spend, limit_usd=limit)

    async with _lock():
        cached = _cache
        now = time.monotonic()
        if cached is not None and now < cached[0]:
            return BudgetStatus(spend_usd=cached[1], limit_usd=limit)

        spend = await window_spend_usd(db)
        _cache = (now + settings.budget_cache_seconds, spend)

    return BudgetStatus(spend_usd=spend, limit_usd=limit)


async def assert_budget_available(db: AsyncSession) -> BudgetStatus:
    """Raise `BudgetExceededError` if the ceiling is spent, else return status.

    Call this before anything that will issue model calls. Read-only routes
    deliberately do not call it: a tripped budget should still leave the
    dashboard, the ticket list, and the traces browsable.
    """
    status = await budget_status(db)
    if status.exhausted:
        logger.warning(
            "budget_exhausted",
            spend_usd=str(status.spend_usd),
            limit_usd=str(status.limit_usd),
        )
        raise BudgetExceededError(
            "The demo's model spending cap for the last 24 hours has been reached. "
            "It frees up as older activity ages out of the window.",
            detail={
                "spend_usd": str(status.spend_usd),
                "limit_usd": str(status.limit_usd),
                "window_hours": int(WINDOW.total_seconds() // 3600),
            },
        )
    return status
