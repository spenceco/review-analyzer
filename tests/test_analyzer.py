from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from app.analyzer import (
    AnalysisError,
    Analyzer,
    estimate_calls,
    topic_stats,
    validate_batch,
    validate_summary,
)
from app.limits import DailyBudget
from app.models import ReviewAnalysis
from app.providers import PermanentProviderError, TransientProviderError
from app.providers.fake import FakeProvider

from .conftest import make_reviews


class ScriptedProvider(FakeProvider):
    """A fake provider that fails in scripted ways before behaving normally."""

    def __init__(self, failures: list[Exception | str]) -> None:
        super().__init__()
        self.failures = failures

    async def generate_json(self, *, system: str, prompt: str, schema: dict[str, Any]) -> Any:
        if self.failures:
            self.calls += 1
            failure = self.failures.pop(0)
            if failure == "drop-one":  # Valid JSON, but one review missing.
                good = await FakeProvider().generate_json(system=system, prompt=prompt,
                                                          schema=schema)
                good["reviews"] = good["reviews"][1:]
                return good
            assert isinstance(failure, Exception)
            raise failure
        return await super().generate_json(system=system, prompt=prompt, schema=schema)


def run(analyzer: Analyzer, n: int):  # type: ignore[no-untyped-def]
    return asyncio.run(analyzer.analyze(make_reviews(n), filename="t.csv"))


def make(provider: FakeProvider, tmp_path: Path, limit: int = 100, **kw: Any) -> Analyzer:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    analyzer = Analyzer(provider, DailyBudget(tmp_path / "u.db", limit), sleep=fake_sleep, **kw)
    analyzer.sleeps = sleeps  # type: ignore[attr-defined]
    return analyzer


def test_estimate_calls() -> None:
    assert estimate_calls(1, 25) == 2
    assert estimate_calls(25, 25) == 2
    assert estimate_calls(26, 25) == 3
    assert estimate_calls(200, 25) == 9


def test_batches_and_counts_calls(tmp_path: Path) -> None:
    provider = FakeProvider()
    progress: list[tuple[int, int]] = []
    analyzer = make(provider, tmp_path, batch_size=25)
    result = asyncio.run(analyzer.analyze(make_reviews(60), filename="t.csv",
                                          on_progress=lambda d, t, m: progress.append((d, t))))
    assert provider.calls == 4  # 3 batches + 1 summary
    assert result.api_calls_used == 4
    assert [a.id for a in result.analyses] == list(range(1, 61))
    assert progress[-1] == (4, 4)
    assert sum(result.sentiment_counts.values()) == 60
    assert len(result.insights.suggested_actions) == 3


def test_retries_transient_errors_with_backoff(tmp_path: Path) -> None:
    provider = ScriptedProvider([TransientProviderError("503"),
                                 TransientProviderError("429", retry_after=7)])
    analyzer = make(provider, tmp_path, batch_size=50)
    result = run(analyzer, 10)
    assert result.api_calls_used == 4  # 2 failures + batch + summary
    assert analyzer.sleeps[1] == 7  # type: ignore[attr-defined]  # Honours Retry-After.


def test_retries_when_a_review_is_missing(tmp_path: Path) -> None:
    provider = ScriptedProvider(["drop-one"])
    result = run(make(provider, tmp_path), 5)
    assert len(result.analyses) == 5 and result.api_calls_used == 3


def test_gives_up_after_max_retries(tmp_path: Path) -> None:
    provider = ScriptedProvider([TransientProviderError("503")] * 10)
    with pytest.raises(AnalysisError, match="after 3 attempts"):
        run(make(provider, tmp_path, max_retries=3), 5)
    assert provider.calls == 3


def test_permanent_error_is_not_retried(tmp_path: Path) -> None:
    provider = ScriptedProvider([PermanentProviderError("bad key")])
    with pytest.raises(AnalysisError, match="rejected"):
        run(make(provider, tmp_path), 5)
    assert provider.calls == 1


def test_daily_budget_is_a_hard_cap_even_with_retries(tmp_path: Path) -> None:
    provider = ScriptedProvider([TransientProviderError("503")] * 3)
    analyzer = make(provider, tmp_path, limit=4)
    with pytest.raises(AnalysisError, match="daily AI limit"):
        run(analyzer, 5)
    assert provider.calls == 4
    assert analyzer.budget.remaining() == 0


def test_validate_batch_normalises_fields() -> None:
    raw = {"reviews": [{"id": 1, "sentiment": "Positive", "topic": "  Coffee Quality. ",
                        "summary": "x" * 500}]}
    [a] = validate_batch(raw, make_reviews(1))
    assert a.sentiment == "positive" and a.topic == "coffee quality" and len(a.summary) == 200


def test_validate_batch_rejects_bad_shapes() -> None:
    with pytest.raises(ValueError):
        validate_batch({"oops": []}, make_reviews(1))
    with pytest.raises(ValueError, match=r"\[1\]"):
        validate_batch({"reviews": [{"id": 1, "sentiment": "angry", "topic": "t",
                                     "summary": "s"}]}, make_reviews(1))


def test_validate_summary_drops_invented_ids() -> None:
    raw = {
        "top_complaints": [{"title": "Slow", "detail": "d", "review_ids": [1, 2, 99]},
                           {"title": "Ghost", "detail": "d", "review_ids": [404]}],
        "top_praise": [],
        "suggested_actions": [{"title": f"A{i}", "detail": "d"} for i in range(4)],
        "topic_groups": [],
    }
    insights = validate_summary(raw, {1, 2, 3})
    assert [(c.title, c.review_ids) for c in insights.top_complaints] == [("Slow", [1, 2])]
    assert len(insights.suggested_actions) == 3


def test_validate_summary_requires_three_actions() -> None:
    raw = {"top_complaints": [], "top_praise": [], "topic_groups": [],
           "suggested_actions": [{"title": "Only one", "detail": "d"}]}
    with pytest.raises(ValueError, match="3"):
        validate_summary(raw, {1})


def test_topic_stats_fold_long_tail_into_other() -> None:
    analyses = [ReviewAnalysis(id=i, sentiment="positive", topic=f"t{i % 12}", summary="s")
                for i in range(120)]
    stats = topic_stats(analyses)
    assert len(stats) == 8 and stats[-1].topic == "other"
    assert sum(s.total for s in stats) == 120


def test_topic_groups_are_applied(tmp_path: Path) -> None:
    class Grouping(FakeProvider):
        async def generate_json(self, *, system: str, prompt: str,
                                schema: dict[str, Any]) -> Any:
            out = await super().generate_json(system=system, prompt=prompt, schema=schema)
            if "topic_groups" in out:
                out["topic_groups"] = [{"name": "Drinks", "raw_topics": ["coffee quality"]}]
            return out

    result = run(make(Grouping(), tmp_path), 3)
    assert {a.topic for a in result.analyses} == {"drinks"}
