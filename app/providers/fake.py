"""An offline stand-in for a real model, used by the test suite and by `AI_PROVIDER=fake`.

It uses keyword rules, so it is fast, free and deterministic. The results are
plausible but much cruder than a real model's; never present them as AI output.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any

from ..prompts import extract_data
from .base import JSONSchema

POSITIVE = {"love", "loved", "great", "amazing", "best", "friendly", "delicious", "perfect",
            "excellent", "cozy", "fantastic", "recommend", "favorite", "favourite", "good",
            "nice", "fast", "lovely", "awesome", "fresh", "wonderful", "kind", "quick"}
NEGATIVE = {"slow", "rude", "cold", "dirty", "expensive", "overpriced", "bad", "terrible",
            "worst", "burnt", "bitter", "wrong", "waited", "wait", "loud", "never", "awful",
            "disappointed", "disappointing", "stale", "crowded", "broken", "lukewarm", "sticky"}

# First matching rule wins.
TOPICS: list[tuple[str, tuple[str, ...]]] = [
    ("service speed", ("wait", "waited", "slow", "line", "queue", "minutes", "fast", "quick")),
    ("staff friendliness", ("staff", "barista", "rude", "friendly", "smile", "kind", "service")),
    ("price", ("price", "expensive", "overpriced", "$", "cheap", "value", "cost")),
    ("food", ("croissant", "pastry", "pastries", "muffin", "sandwich", "food", "cake", "bagel")),
    ("wifi & workspace", ("wifi", "wi-fi", "outlet", "laptop", "work", "study")),
    ("cleanliness", ("dirty", "clean", "sticky", "bathroom", "restroom", "tables")),
    ("atmosphere", ("music", "cozy", "vibe", "atmosphere", "loud", "seating", "decor")),
    ("coffee quality", ("coffee", "latte", "espresso", "cappuccino", "brew", "beans", "mocha")),
]


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z$'\-]+", text.lower())


def classify(text: str, rating: float | None) -> tuple[str, str]:
    words = _words(text)
    score: float = sum(w in POSITIVE for w in words) - sum(w in NEGATIVE for w in words)
    if rating is not None:
        score += (rating - 3) * 2
    sentiment = "positive" if score > 0 else "negative" if score < 0 else "neutral"
    topic = next((name for name, keys in TOPICS if any(k in words or k in text for k in keys)),
                 "general experience")
    return sentiment, topic


def _first_sentence(text: str) -> str:
    sentence = re.split(r"(?<=[.!?])\s", text.strip(), maxsplit=1)[0]
    words = sentence.split()
    return " ".join(words[:20]) + ("..." if len(words) > 20 else "")


class FakeProvider:
    name = "fake"

    def __init__(self) -> None:
        self.calls = 0

    async def generate_json(self, *, system: str, prompt: str, schema: JSONSchema) -> Any:
        self.calls += 1
        data = extract_data(prompt)
        if "reviews" in schema["properties"]:
            return {"reviews": [self._classify_one(item) for item in data]}
        return self._summarise(data)

    @staticmethod
    def _classify_one(item: dict[str, Any]) -> dict[str, Any]:
        sentiment, topic = classify(item["text"], item.get("rating"))
        return {"id": item["id"], "sentiment": sentiment, "topic": topic,
                "summary": _first_sentence(item["text"])}

    @staticmethod
    def _summarise(analyses: list[dict[str, Any]]) -> dict[str, Any]:
        by_topic: dict[tuple[str, str], list[int]] = defaultdict(list)
        for a in analyses:
            by_topic[(a["topic"], a["sentiment"])].append(a["id"])

        def top(sentiment: str, verb: str) -> list[dict[str, Any]]:
            ranked = sorted(((t, ids) for (t, s), ids in by_topic.items() if s == sentiment),
                            key=lambda x: -len(x[1]))[:5]
            return [{"title": t.capitalize(), "review_ids": ids,
                     "detail": f"{len(ids)} review(s) {verb} {t}."} for t, ids in ranked]

        complaints = top("negative", "criticised")
        praise = top("positive", "praised")
        actions = [{"title": f"Fix {c['title'].lower()}",
                    "detail": f"It is the most common complaint ({len(c['review_ids'])} reviews)."}
                   for c in complaints[:2]]
        if praise:
            actions.append({"title": f"Promote {praise[0]['title'].lower()}",
                            "detail": "Customers already love it; feature it in marketing."})
        while len(actions) < 3:
            actions.append({"title": "Ask for more reviews",
                            "detail": "More feedback makes these trends more reliable."})
        topics = Counter(a["topic"] for a in analyses)
        return {"top_complaints": complaints, "top_praise": praise,
                "suggested_actions": actions[:3],
                "topic_groups": [{"name": t, "raw_topics": [t]} for t in topics]}

    async def aclose(self) -> None:
        return None
