"""The analysis pipeline: batch the reviews, call the AI with retries, validate what
comes back, then aggregate everything into an `AnalysisResult`."""

from __future__ import annotations

import asyncio
import math
import random
from collections import Counter
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from pydantic import ValidationError

from . import prompts
from .limits import BudgetExceeded, DailyBudget
from .models import (
    SENTIMENTS,
    AnalysisResult,
    Insight,
    OverallInsights,
    Review,
    ReviewAnalysis,
    TopicStat,
)
from .providers import LLMProvider, PermanentProviderError, TransientProviderError

T = TypeVar("T")
ProgressCallback = Callable[[int, int, str], None]  # (done, total, message)

MAX_TOPICS_SHOWN = 8
MAX_RETRY_DELAY = 30.0


class AnalysisError(Exception):
    """A failure with a message that is safe to show to the user."""


def estimate_calls(n_reviews: int, batch_size: int) -> int:
    """API calls a clean run needs: one per batch plus one for the overall summary."""
    return math.ceil(n_reviews / batch_size) + 1


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def validate_batch(raw: Any, batch: list[Review]) -> list[ReviewAnalysis]:
    """Check that the model returned one well-formed result for every review in the batch.
    Raises ``ValueError`` (which triggers a retry) if anything is missing or malformed."""
    if not isinstance(raw, dict) or not isinstance(raw.get("reviews"), list):
        raise ValueError("response has no 'reviews' list")
    expected = {r.id for r in batch}
    found: dict[int, ReviewAnalysis] = {}
    for item in raw["reviews"]:
        if isinstance(item, dict) and isinstance(item.get("sentiment"), str):
            item = {**item, "sentiment": item["sentiment"].strip().lower()}
        try:
            analysis = ReviewAnalysis.model_validate(item)
        except ValidationError:
            continue  # A bad item counts as missing; the check below decides.
        if analysis.id in expected and analysis.id not in found:
            analysis.topic = _clip(analysis.topic.lower().strip(" ."), 40) or "general"
            analysis.summary = _clip(analysis.summary, 200)
            found[analysis.id] = analysis
    missing = expected - found.keys()
    if missing:
        raise ValueError(f"no valid result for review ids {sorted(missing)[:10]}")
    return [found[r.id] for r in batch]


def validate_summary(raw: Any, valid_ids: set[int]) -> OverallInsights:
    """Parse the overall insights, dropping review ids the model invented and any
    insight left with no supporting reviews."""
    try:
        insights = OverallInsights.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(f"summary failed validation: {exc.error_count()} errors") from exc
    if len(insights.suggested_actions) < 3:
        raise ValueError("fewer than 3 suggested actions")
    insights.suggested_actions = insights.suggested_actions[:3]

    def clean(items: list[Insight]) -> list[Insight]:
        for item in items:
            item.review_ids = sorted({i for i in item.review_ids if i in valid_ids})
        kept = [i for i in items if i.review_ids]
        return sorted(kept, key=lambda i: -i.mentions)[:5]

    insights.top_complaints = clean(insights.top_complaints)
    insights.top_praise = clean(insights.top_praise)
    return insights


def apply_topic_groups(analyses: list[ReviewAnalysis], insights: OverallInsights) -> None:
    """Rename each review's free-form topic to its canonical group (in place)."""
    mapping: dict[str, str] = {}
    for group in insights.topic_groups:
        name = _clip(group.name.lower().strip(), 40)
        for raw in group.raw_topics:
            mapping.setdefault(raw.lower().strip(), name)
    for a in analyses:
        a.topic = mapping.get(a.topic, a.topic)


def topic_stats(analyses: list[ReviewAnalysis]) -> list[TopicStat]:
    """Sentiment counts per topic, largest first; the long tail folds into "other"."""
    counts: dict[str, Counter[str]] = {}
    for a in analyses:
        counts.setdefault(a.topic, Counter())[a.sentiment] += 1
    ranked = sorted(counts.items(), key=lambda kv: (-sum(kv[1].values()), kv[0]))
    if len(ranked) > MAX_TOPICS_SHOWN:
        head, tail = ranked[: MAX_TOPICS_SHOWN - 1], ranked[MAX_TOPICS_SHOWN - 1:]
        other: Counter[str] = Counter()
        for _, c in tail:
            other.update(c)
        ranked = [*head, ("other", other)]
    return [TopicStat(topic=t, **{s: c[s] for s in SENTIMENTS}) for t, c in ranked]


class Analyzer:
    def __init__(self, provider: LLMProvider, budget: DailyBudget, *, batch_size: int = 25,
                 concurrency: int = 2, max_retries: int = 4, base_delay: float = 1.5,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self.provider = provider
        self.budget = budget
        self.batch_size = batch_size
        self.concurrency = concurrency
        self.max_retries = max_retries
        self.base_delay = base_delay
        self._sleep = sleep

    async def _call(self, prompt: str, schema: dict[str, Any], validate: Callable[[Any], T],
                    counter: list[int]) -> T:
        """One logical AI request, retried on transient errors and invalid output.
        Every attempt is charged to the daily budget before it is sent."""
        last_error = "unknown error"
        for attempt in range(self.max_retries):
            try:
                self.budget.consume()
            except BudgetExceeded as exc:
                raise AnalysisError(
                    "The demo's daily AI limit was reached part-way through. "
                    "Please try again after midnight UTC."
                ) from exc
            counter[0] += 1
            retry_after: float | None = None
            try:
                raw = await self.provider.generate_json(
                    system=prompts.SYSTEM_PROMPT, prompt=prompt, schema=schema)
                return validate(raw)
            except PermanentProviderError as exc:
                raise AnalysisError(f"The AI service rejected the request: {exc}") from exc
            except TransientProviderError as exc:
                last_error, retry_after = str(exc), exc.retry_after
            except ValueError as exc:
                last_error = f"invalid AI output ({exc})"
            if attempt < self.max_retries - 1:
                delay = retry_after or self.base_delay * 2**attempt * random.uniform(0.8, 1.2)
                await self._sleep(min(delay, MAX_RETRY_DELAY))
        raise AnalysisError(
            f"The AI service kept failing after {self.max_retries} attempts ({last_error}). "
            "Please try again in a few minutes."
        )

    async def analyze(self, reviews: list[Review], *, filename: str,
                      on_progress: ProgressCallback | None = None) -> AnalysisResult:
        progress = on_progress or (lambda done, total, msg: None)
        batches = [reviews[i:i + self.batch_size] for i in range(0, len(reviews), self.batch_size)]
        total_steps = len(batches) + 1
        calls = [0]  # Mutable counter shared by the concurrent batch tasks.
        done = 0
        semaphore = asyncio.Semaphore(self.concurrency)
        progress(0, total_steps, f"Analyzing {len(reviews)} reviews in {len(batches)} batches")

        async def run_batch(batch: list[Review]) -> list[ReviewAnalysis]:
            nonlocal done
            async with semaphore:
                result = await self._call(prompts.build_classify_prompt(batch),
                                          prompts.CLASSIFY_SCHEMA,
                                          lambda raw: validate_batch(raw, batch), calls)
            done += 1
            progress(done, total_steps, f"Analyzed batch {done} of {len(batches)}")
            return result

        tasks = [asyncio.create_task(run_batch(b)) for b in batches]
        try:
            per_batch = await asyncio.gather(*tasks)
        except BaseException:
            for t in tasks:
                t.cancel()  # Stop spending API calls once any batch has failed.
            raise
        analyses = [a for batch in per_batch for a in batch]

        progress(done, total_steps, "Writing the overall summary")
        valid_ids = {r.id for r in reviews}
        insights = await self._call(prompts.build_summary_prompt(analyses),
                                    prompts.SUMMARY_SCHEMA,
                                    lambda raw: validate_summary(raw, valid_ids), calls)
        apply_topic_groups(analyses, insights)
        progress(total_steps, total_steps, "Done")

        sentiment_counts = Counter(a.sentiment for a in analyses)
        return AnalysisResult(
            filename=filename,
            reviews=reviews,
            analyses=analyses,
            insights=insights,
            sentiment_counts={s: sentiment_counts.get(s, 0) for s in SENTIMENTS},
            topic_stats=topic_stats(analyses),
            api_calls_used=calls[0],
            provider=self.provider.name,
        )
