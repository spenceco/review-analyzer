"""Shared fixtures: fast, offline settings and a ready-to-use test client."""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import Review
from app.providers.fake import FakeProvider

SAMPLE = Path(__file__).resolve().parent.parent / "sample_data" / "coffee_shop_reviews.csv"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        ai_provider="fake",
        data_dir=tmp_path,
        retry_base_delay_seconds=0,
        rate_limit_per_ip=3,
        daily_api_call_limit=50,
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings, provider=FakeProvider())) as c:
        yield c


@pytest.fixture
def sample_bytes() -> bytes:
    return SAMPLE.read_bytes()


def make_reviews(n: int) -> list[Review]:
    return [Review(id=i, text=f"Review number {i}: the coffee was great", rating=5)
            for i in range(1, n + 1)]


def wait_for_job(client: TestClient, job_id: str, timeout: float = 10) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job: dict[str, Any] = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish in time")
