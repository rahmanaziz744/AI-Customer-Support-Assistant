"""The spend ceiling: policy, caching, and the refusal it raises.

The database query is one line and needs Postgres; the decisions built on top
of it are what can go wrong quietly, so those are tested here against a stub
session.
"""

from decimal import Decimal

import pytest

from app.core import budget
from app.core.budget import BudgetStatus, assert_budget_available, budget_status
from app.core.config import get_settings
from app.core.errors import BudgetExceededError


class _Result:
    def __init__(self, value: Decimal) -> None:
        self._value = value

    def scalar_one(self) -> Decimal:
        return self._value


class FakeSession:
    """Counts queries so the caching behaviour is observable."""

    def __init__(self, spend: str = "0") -> None:
        self.spend = Decimal(spend)
        self.queries = 0

    async def execute(self, *_args, **_kwargs) -> _Result:
        self.queries += 1
        return _Result(self.spend)


@pytest.fixture(autouse=True)
def _clean_cache():
    budget.reset_cache()
    yield
    budget.reset_cache()


@pytest.fixture
def limit(monkeypatch):
    def _set(value: float) -> None:
        monkeypatch.setattr(get_settings(), "daily_budget_usd", value)

    return _set


class TestBudgetStatus:
    def test_zero_limit_is_unenforced(self):
        status = BudgetStatus(spend_usd=Decimal("100"), limit_usd=Decimal("0"))
        assert not status.enforced
        assert not status.exhausted
        assert status.ratio == 0.0

    def test_under_limit(self):
        status = BudgetStatus(spend_usd=Decimal("2"), limit_usd=Decimal("5"))
        assert status.enforced
        assert not status.exhausted
        assert status.ratio == pytest.approx(0.4)
        assert status.remaining_usd == Decimal("3")

    def test_exactly_at_limit_is_exhausted(self):
        """Reaching the cap spends it; the next run would go over."""
        status = BudgetStatus(spend_usd=Decimal("5"), limit_usd=Decimal("5"))
        assert status.exhausted
        assert status.remaining_usd == Decimal("0")

    def test_over_limit_clamps_remaining(self):
        status = BudgetStatus(spend_usd=Decimal("7.5"), limit_usd=Decimal("5"))
        assert status.exhausted
        assert status.remaining_usd == Decimal("0")


class TestAssertBudgetAvailable:
    async def test_disabled_never_raises(self, limit):
        limit(0.0)
        db = FakeSession("999999")
        status = await assert_budget_available(db)
        assert not status.enforced

    async def test_under_limit_passes(self, limit):
        limit(5.0)
        status = await assert_budget_available(FakeSession("1.25"))
        assert status.spend_usd == Decimal("1.25")

    async def test_exhausted_raises_429(self, limit):
        limit(5.0)
        with pytest.raises(BudgetExceededError) as exc:
            await assert_budget_available(FakeSession("5.00"))
        assert exc.value.status_code == 429
        assert exc.value.code == "budget_exceeded"

    async def test_refusal_reports_the_numbers(self, limit):
        limit(5.0)
        with pytest.raises(BudgetExceededError) as exc:
            await assert_budget_available(FakeSession("6.00"))
        assert exc.value.detail["spend_usd"] == "6.00"
        assert exc.value.detail["limit_usd"] == "5.0"
        assert exc.value.detail["window_hours"] == 24


class TestCaching:
    async def test_repeated_reads_hit_the_database_once(self, limit):
        limit(5.0)
        db = FakeSession("1.00")
        for _ in range(10):
            await budget_status(db)
        assert db.queries == 1

    async def test_reset_forces_a_fresh_read(self, limit):
        limit(5.0)
        db = FakeSession("1.00")
        await budget_status(db)
        budget.reset_cache()
        await budget_status(db)
        assert db.queries == 2

    async def test_expiry_forces_a_fresh_read(self, limit, monkeypatch):
        limit(5.0)
        monkeypatch.setattr(get_settings(), "budget_cache_seconds", 0.0)
        db = FakeSession("1.00")
        await budget_status(db)
        await budget_status(db)
        assert db.queries == 2

    async def test_uncached_read_always_queries(self, limit):
        limit(5.0)
        db = FakeSession("1.00")
        await budget_status(db, use_cache=False)
        await budget_status(db, use_cache=False)
        assert db.queries == 2

    async def test_stale_cache_still_refuses_once_over(self, limit):
        """A cached over-limit reading keeps refusing without re-querying."""
        limit(5.0)
        db = FakeSession("9.00")
        with pytest.raises(BudgetExceededError):
            await assert_budget_available(db)
        with pytest.raises(BudgetExceededError):
            await assert_budget_available(db)
        assert db.queries == 1
