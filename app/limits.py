"""Cost and abuse protection: a per-IP rate limiter and a hard daily API-call budget."""

from __future__ import annotations

import sqlite3
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path


class RateLimiter:
    """Sliding-window limiter: at most ``limit`` hits per ``window`` seconds per key.

    Kept in memory: a restart forgets recent hits, which is acceptable because the
    daily budget below (persisted to disk) is the real cost ceiling.
    """

    def __init__(self, limit: int, window: float, clock: Callable[[], float] = time.monotonic):
        self.limit = limit
        self.window = window
        self._clock = clock
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> deque[float]:
        hits = self._hits[key]
        while hits and hits[0] <= now - self.window:
            hits.popleft()
        return hits

    def hit(self, key: str) -> float | None:
        """Record a hit. Returns ``None`` if allowed, else seconds until the next slot."""
        with self._lock:
            now = self._clock()
            hits = self._prune(key, now)
            if len(hits) >= self.limit:
                return hits[0] + self.window - now
            hits.append(now)
            if len(self._hits) > 10_000:  # Bound memory under a flood of distinct IPs.
                for k in [k for k, v in self._hits.items() if not v]:
                    del self._hits[k]
            return None

    def remaining(self, key: str) -> int:
        with self._lock:
            return max(0, self.limit - len(self._prune(key, self._clock())))


class BudgetExceeded(Exception):
    """The daily API-call limit has been reached."""


class DailyBudget:
    """Counts AI API calls per UTC day in SQLite, so the cap survives restarts.

    ``consume()`` is called right before every single HTTP request to the AI
    provider, retries included, so the limit can never be overshot.
    """

    def __init__(self, path: Path, limit: int,
                 now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self.limit = limit
        self._now = now
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("CREATE TABLE IF NOT EXISTS usage (day TEXT PRIMARY KEY, calls INTEGER)")
        self._lock = threading.Lock()

    def _today(self) -> str:
        return self._now().date().isoformat()

    def used(self) -> int:
        with self._lock:
            row = self._db.execute("SELECT calls FROM usage WHERE day = ?",
                                   (self._today(),)).fetchone()
        return int(row[0]) if row else 0

    def remaining(self) -> int:
        return max(0, self.limit - self.used())

    def consume(self, n: int = 1) -> None:
        """Atomically reserve ``n`` calls or raise ``BudgetExceeded``."""
        with self._lock:
            day = self._today()
            row = self._db.execute("SELECT calls FROM usage WHERE day = ?", (day,)).fetchone()
            used = int(row[0]) if row else 0
            if used + n > self.limit:
                raise BudgetExceeded()
            self._db.execute(
                "INSERT INTO usage (day, calls) VALUES (?, ?) "
                "ON CONFLICT(day) DO UPDATE SET calls = calls + excluded.calls",
                (day, n),
            )

    def resets_at(self) -> datetime:
        """The next UTC midnight."""
        tomorrow = self._now().date() + timedelta(days=1)
        return datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=UTC)

    def close(self) -> None:
        self._db.close()
