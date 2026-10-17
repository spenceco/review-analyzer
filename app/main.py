"""FastAPI app: serves the single-page frontend and the JSON API.

Routes
  GET  /                          the web page
  GET  /api/status                limits and remaining daily budget (drives the UI)
  POST /api/analyze               upload a CSV, start a background job
  GET  /api/jobs/{id}             poll progress; includes the result when done
  GET  /api/jobs/{id}/report.xlsx download the Excel report
  GET  /api/sample.csv            the demo coffee-shop CSV
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from .analyzer import AnalysisError, Analyzer, estimate_calls
from .config import Settings, get_settings
from .csv_validation import CSVValidationError, parse_reviews
from .jobs import Job, JobStore
from .limits import DailyBudget, RateLimiter
from .providers import LLMProvider, create_provider
from .report import build_report

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "static"
SAMPLE_CSV = ROOT / "sample_data" / "coffee_shop_reviews.csv"

log = logging.getLogger("review_analyzer")


def _error(status: int, code: str, message: str, **extra: Any) -> HTTPException:
    """Errors are always ``{"detail": {"code", "message", ...}}`` so the UI can show them."""
    return HTTPException(status_code=status, detail={"code": code, "message": message, **extra})


def _client_ip(request: Request) -> str:
    # uvicorn's --proxy-headers (trusting only 127.0.0.1, i.e. Caddy) already put the
    # real visitor address here; we never read X-Forwarded-For ourselves.
    return request.client.host if request.client else "unknown"


def create_app(settings: Settings | None = None, provider: LLMProvider | None = None) -> FastAPI:
    """Build the app. Tests pass their own settings and a fake provider."""
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.budget = DailyBudget(settings.data_dir / "usage.sqlite3",
                                       settings.daily_api_call_limit)
        app.state.provider_error = None
        try:
            app.state.provider = provider or create_provider(settings)
        except RuntimeError as exc:
            # Start anyway so the page loads and explains what's missing.
            log.error("AI provider unavailable: %s", exc)
            app.state.provider, app.state.provider_error = None, str(exc)
        yield
        for job in app.state.jobs.all():
            if job.task and not job.task.done():
                job.task.cancel()
        if app.state.provider is not None:
            await app.state.provider.aclose()
        app.state.budget.close()

    app = FastAPI(title="review-analyzer", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.jobs = JobStore(settings.job_ttl_seconds)
    app.state.limiter = RateLimiter(settings.rate_limit_per_ip, settings.rate_limit_window_seconds)

    def min_calls() -> int:
        return estimate_calls(1, settings.batch_size)

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        response: Response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "frame-ancestors 'none'"
        )
        return response

    @app.get("/api/status")
    async def status(request: Request) -> dict[str, Any]:
        budget: DailyBudget = app.state.budget
        remaining = budget.remaining()
        reason = None
        if app.state.provider_error:
            reason = "The AI service isn't configured on this server yet."
        elif remaining < min_calls():
            reason = "The demo's daily AI limit has been reached. It resets at midnight UTC."
        return {
            "can_analyze": reason is None,
            "reason": reason,
            "provider": app.state.provider.name if app.state.provider else None,
            "calls_remaining_today": remaining,
            "daily_call_limit": budget.limit,
            "resets_at": budget.resets_at().isoformat(),
            "max_rows": settings.max_rows,
            "max_upload_bytes": settings.max_upload_bytes,
            "batch_size": settings.batch_size,
            "uploads_remaining_for_you": app.state.limiter.remaining(_client_ip(request)),
        }

    @app.post("/api/analyze", status_code=202)
    async def analyze(request: Request, file: UploadFile) -> dict[str, Any]:
        if app.state.provider is None:
            raise _error(503, "not_configured", "The AI service isn't configured on this server.")

        # Reject oversized uploads before reading them into memory.
        declared = int(request.headers.get("content-length") or 0)
        if declared > settings.max_upload_bytes + 64 * 1024:
            raise _error(413, "too_large",
                         f"The file is too large. The limit is "
                         f"{settings.max_upload_bytes / 1_048_576:.0f} MB.")
        data = await file.read(settings.max_upload_bytes + 1)
        filename = re.sub(r"[^\w.\- ]", "_", file.filename or "reviews.csv")[:80]
        if not filename.lower().endswith((".csv", ".txt", ".tsv")):
            raise _error(400, "bad_type", "Please upload a .csv file.")

        try:
            reviews = parse_reviews(data, max_rows=settings.max_rows,
                                    max_bytes=settings.max_upload_bytes,
                                    max_review_chars=settings.max_review_chars)
        except CSVValidationError as exc:
            raise _error(422, "invalid_csv", str(exc)) from exc

        needed = estimate_calls(len(reviews), settings.batch_size)
        budget: DailyBudget = app.state.budget
        if budget.remaining() < needed:
            raise _error(429, "daily_limit",
                         "The demo's daily AI limit has been reached. It resets at midnight UTC."
                         if budget.remaining() < min_calls() else
                         f"Only enough AI budget is left today for about "
                         f"{(budget.remaining() - 1) * settings.batch_size} reviews. "
                         "Try a smaller file, or come back after midnight UTC.")
        jobs: JobStore = app.state.jobs
        if jobs.running_count() >= settings.max_concurrent_jobs:
            raise _error(503, "busy", "The server is busy with other analyses. "
                                      "Please try again in a minute.")
        wait = app.state.limiter.hit(_client_ip(request))
        if wait is not None:
            minutes = max(1, round(wait / 60))
            raise _error(429, "rate_limited",
                         f"You've reached the limit of {settings.rate_limit_per_ip} analyses "
                         f"per hour. Please try again in about {minutes} minute(s).",
                         retry_after=round(wait))

        job = jobs.create(filename, len(reviews))
        analyzer = Analyzer(app.state.provider, budget, batch_size=settings.batch_size,
                            concurrency=settings.batch_concurrency,
                            max_retries=settings.max_retries,
                            base_delay=settings.retry_base_delay_seconds)

        async def run() -> None:
            try:
                job.result = await analyzer.analyze(reviews, filename=filename,
                                                    on_progress=job.progress)
                job.status = "done"
            except AnalysisError as exc:
                job.status, job.error = "failed", str(exc)
            except Exception:
                log.exception("Job %s crashed", job.id)
                job.status, job.error = "failed", "Something went wrong on our side. Sorry!"
            finally:
                job.finished_at = time.monotonic()

        job.task = asyncio.create_task(run())
        return {"job_id": job.id, "total_reviews": len(reviews), "estimated_calls": needed}

    def find_job(job_id: str) -> Job:
        job: Job | None = app.state.jobs.get(job_id)
        if job is None:
            raise _error(404, "not_found", "This analysis has expired or doesn't exist.")
        return job

    @app.get("/api/jobs/{job_id}")
    async def job_status(job_id: str) -> dict[str, Any]:
        return find_job(job_id).to_public()

    @app.get("/api/jobs/{job_id}/report.xlsx")
    async def job_report(job_id: str) -> Response:
        job = find_job(job_id)
        if job.result is None:
            raise _error(409, "not_ready", "The analysis hasn't finished yet.")
        if job.report is None:
            job.report = await asyncio.to_thread(build_report, job.result)
        stem = Path(job.filename).stem or "reviews"
        return Response(
            job.report,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{stem}-analysis.xlsx"'},
        )

    @app.get("/api/sample.csv")
    async def sample_csv() -> FileResponse:
        return FileResponse(SAMPLE_CSV, media_type="text/csv", filename=SAMPLE_CSV.name)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


app = create_app()
