"""In-memory store for analysis jobs, which run as background asyncio tasks.

One process, one store: run uvicorn with a single worker (the default). That is
plenty for a demo capped at a few hundred API calls a day.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from .models import AnalysisResult

JobStatus = Literal["running", "done", "failed"]


@dataclass
class Job:
    id: str
    filename: str
    total_reviews: int
    status: JobStatus = "running"
    done: int = 0
    total: int = 1
    message: str = "Starting"
    error: str | None = None
    result: AnalysisResult | None = None
    report: bytes | None = None  # Cached .xlsx, built on first download.
    created_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None
    task: asyncio.Task[None] | None = field(default=None, repr=False)

    def progress(self, done: int, total: int, message: str) -> None:
        self.done, self.total, self.message = done, total, message

    def to_public(self) -> dict[str, Any]:
        """The JSON the frontend polls."""
        return {
            "id": self.id,
            "status": self.status,
            "filename": self.filename,
            "total_reviews": self.total_reviews,
            "progress": {"done": self.done, "total": self.total, "message": self.message},
            "error": self.error,
            "result": self.result.model_dump(mode="json") if self.result else None,
        }


class JobStore:
    def __init__(self, ttl_seconds: float) -> None:
        self.ttl = ttl_seconds
        self._jobs: dict[str, Job] = {}

    def create(self, filename: str, total_reviews: int) -> Job:
        self.cleanup()
        job = Job(id=secrets.token_urlsafe(16), filename=filename, total_reviews=total_reviews)
        self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def running_count(self) -> int:
        return sum(job.status == "running" for job in self._jobs.values())

    def cleanup(self) -> None:
        """Forget finished jobs older than the TTL so memory stays bounded."""
        now = time.monotonic()
        expired = [j.id for j in self._jobs.values()
                   if j.finished_at is not None and now - j.finished_at > self.ttl]
        for job_id in expired:
            del self._jobs[job_id]

    def all(self) -> list[Job]:
        return list(self._jobs.values())
