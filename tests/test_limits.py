from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.limits import BudgetExceeded, DailyBudget, RateLimiter


def test_rate_limiter_sliding_window() -> None:
    now = [0.0]
    limiter = RateLimiter(limit=2, window=60, clock=lambda: now[0])
    assert limiter.hit("a") is None
    assert limiter.hit("a") is None
    assert limiter.hit("a") == pytest.approx(60)
    assert limiter.hit("b") is None  # Other IPs are unaffected.
    now[0] = 30
    assert limiter.remaining("a") == 0
    now[0] = 60.5
    assert limiter.remaining("a") == 2
    assert limiter.hit("a") is None


def test_budget_persists_and_resets_daily(tmp_path: Path) -> None:
    day = [datetime(2026, 10, 17, 23, 0, tzinfo=UTC)]
    budget = DailyBudget(tmp_path / "u.db", limit=3, now=lambda: day[0])
    budget.consume(2)
    budget.close()

    reopened = DailyBudget(tmp_path / "u.db", limit=3, now=lambda: day[0])
    assert reopened.remaining() == 1
    reopened.consume()
    with pytest.raises(BudgetExceeded):
        reopened.consume()
    assert reopened.resets_at() == datetime(2026, 10, 18, tzinfo=UTC)

    day[0] += timedelta(hours=2)  # Past midnight UTC.
    assert reopened.remaining() == 3


def test_budget_never_partially_consumes(tmp_path: Path) -> None:
    budget = DailyBudget(tmp_path / "u.db", limit=5)
    budget.consume(4)
    with pytest.raises(BudgetExceeded):
        budget.consume(2)
    assert budget.used() == 4
