"""Configuration from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache

from dotenv import find_dotenv, load_dotenv


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _list(name: str) -> list[str]:
    return [s.strip() for s in os.environ.get(name, "").split(",") if s.strip()]


@dataclass(frozen=True)
class Config:
    database_url: str | None = None
    redis_url: str | None = None
    celery_broker_url: str | None = None
    celery_result_backend: str | None = None
    celery_app: str | None = None
    celery_queues: list[str] = field(default_factory=list)
    log_sources: list[str] = field(default_factory=list)

    # Heuristic thresholds
    queue_threshold: int = 100
    expected_workers: int = 0
    long_task_s: float = 60
    long_query_s: float = 30
    redis_mem_warn_pct: float = 85
    log_error_spike: int = 10
    chain_window_min: float = 30

    # Limits
    check_timeout_s: float = 8
    celery_inspect_timeout_s: float = 1.0
    max_output_chars: int = 20_000

    @classmethod
    def from_env(cls) -> "Config":
        # Real environment variables win over the .env file.
        load_dotenv(os.environ.get("STACKDOCTOR_ENV_FILE") or find_dotenv(usecwd=True))
        get = lambda k: os.environ.get(k) or None  # noqa: E731
        return cls(
            database_url=get("DATABASE_URL"),
            redis_url=get("REDIS_URL"),
            celery_broker_url=get("CELERY_BROKER_URL"),
            celery_result_backend=get("CELERY_RESULT_BACKEND"),
            celery_app=get("CELERY_APP"),
            celery_queues=_list("CELERY_QUEUES"),
            log_sources=_list("LOG_SOURCES"),
            queue_threshold=int(_float("QUEUE_THRESHOLD", 100)),
            expected_workers=int(_float("EXPECTED_WORKERS", 0)),
            long_task_s=_float("LONG_TASK_S", 60),
            long_query_s=_float("LONG_QUERY_S", 30),
            redis_mem_warn_pct=_float("REDIS_MEM_WARN_PCT", 85),
            log_error_spike=int(_float("LOG_ERROR_SPIKE", 10)),
            chain_window_min=_float("CHAIN_WINDOW_MIN", 30),
            check_timeout_s=_float("CHECK_TIMEOUT_S", 8),
            celery_inspect_timeout_s=_float("CELERY_INSPECT_TIMEOUT_S", 1.0),
            max_output_chars=int(_float("MAX_OUTPUT_CHARS", 20_000)),
        )


@lru_cache(maxsize=1)
def get_config() -> Config:
    return Config.from_env()


def skipped(reason: str) -> dict:
    """Standard result for a check whose source isn't configured."""
    return {"skipped": reason}
