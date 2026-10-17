"""Data shapes shared by the CSV parser, the analyzer, the API and the Excel report."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, computed_field

Sentiment = Literal["positive", "neutral", "negative"]
SENTIMENTS: tuple[Sentiment, ...] = ("positive", "neutral", "negative")


class Review(BaseModel):
    """One row from the uploaded CSV."""

    id: int = Field(description="1-based row number in the upload, used to match AI output")
    text: str
    rating: float | None = None
    date: str | None = None


class ReviewAnalysis(BaseModel):
    """The AI's verdict on a single review."""

    id: int
    sentiment: Sentiment
    topic: str
    summary: str


class Insight(BaseModel):
    """A recurring complaint or point of praise, backed by the reviews that mention it."""

    title: str
    detail: str
    review_ids: list[int] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def mentions(self) -> int:
        return len(self.review_ids)


class SuggestedAction(BaseModel):
    title: str
    detail: str


class TopicGroup(BaseModel):
    """Maps the free-form per-review topics onto a short list of canonical topics."""

    name: str
    raw_topics: list[str]


class OverallInsights(BaseModel):
    top_complaints: list[Insight]
    top_praise: list[Insight]
    suggested_actions: list[SuggestedAction]
    topic_groups: list[TopicGroup] = Field(default_factory=list)


class TopicStat(BaseModel):
    topic: str
    positive: int = 0
    neutral: int = 0
    negative: int = 0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total(self) -> int:
        return self.positive + self.neutral + self.negative


class AnalysisResult(BaseModel):
    """Everything the frontend and the Excel report need."""

    filename: str
    reviews: list[Review]
    analyses: list[ReviewAnalysis]
    insights: OverallInsights
    sentiment_counts: dict[str, int]
    topic_stats: list[TopicStat]
    api_calls_used: int
    provider: str
