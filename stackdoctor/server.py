"""stackdoctor MCP server (stdio). Strictly read-only."""

from __future__ import annotations

import argparse
import functools
import importlib.metadata
import inspect
import logging
import sys

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from . import diagnose as diag
from .checks import celery, logs, postgres, redis
from .config import get_config
from .safety import safe_output

log = logging.getLogger("stackdoctor")

mcp = MCPServer(
    "stackdoctor",
    instructions=(
        "Read-only diagnostics for a Python backend stack (Postgres, Celery, Redis, logs). "
        "Start with diagnose(symptom): it runs all checks at once and returns findings, a merged "
        "cross-system timeline and possible cause → effect chains. Chains are hypotheses based on "
        "timing; present them as possible causes, not facts. Use the other tools to drill down."
    ),
)

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False,
                            idempotent_hint=True, open_world_hint=False)


def tool(fn):
    """Register a tool whose output is always redacted and size-capped."""
    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            try:
                result = await fn(*args, **kwargs)
            except Exception as e:
                log.exception("tool %s failed", fn.__name__)
                result = {"error": f"{type(e).__name__}: {str(e)[:500]}"}
            return safe_output(result, get_config().max_output_chars * 2)
    else:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                result = fn(*args, **kwargs)
            except Exception as e:
                log.exception("tool %s failed", fn.__name__)
                result = {"error": f"{type(e).__name__}: {str(e)[:500]}"}
            return safe_output(result, get_config().max_output_chars)
    mcp.tool(annotations=READ_ONLY)(wrapper)
    return wrapper


# --- Main -------------------------------------------------------------------

@tool
async def diagnose(symptom: str = "") -> dict:
    """Run all relevant checks concurrently and return one timestamped snapshot.

    Use this first for questions like "why are my jobs stuck?" or "why is the API slow?".
    Returns findings, a merged timeline across Postgres/Celery/Redis/logs, and
    'possible cause' chains (timing-based hypotheses with evidence timestamps).
    """
    return await diag.diagnose(symptom)


# --- Postgres (light; for deep Postgres health use Postgres MCP Pro) --------

@tool
def active_queries(min_duration_s: float = 0) -> dict:
    """Non-idle Postgres sessions (incl. 'idle in transaction') with duration and wait events."""
    return postgres.active_queries(min_duration_s)


@tool
def blocking_locks() -> dict:
    """Postgres sessions waiting on locks, and the sessions blocking them."""
    return postgres.blocking_locks()


@tool
def slow_queries(limit: int = 10) -> dict:
    """Slowest statements by mean time from pg_stat_statements, plus seq-scan-heavy tables."""
    return postgres.slow_queries(limit)


@tool
def run_select(sql: str, limit: int = 100) -> dict:
    """Run one read-only SELECT / WITH / EXPLAIN (no ANALYZE). A row LIMIT is always applied (max 1000)."""
    return postgres.run_select(sql, limit)


# --- Celery (inspect API + broker, no Flower) -------------------------------

@tool
def workers() -> dict:
    """Live Celery workers with their queues, concurrency, and active/reserved tasks."""
    return celery.workers()


@tool
def queue_lengths(queues: list[str] | None = None) -> dict:
    """Messages waiting per Celery queue, read from the broker (Redis incl. priority queues, or RabbitMQ)."""
    return celery.queue_lengths(queues)


@tool
def failed_tasks(limit: int = 20) -> dict:
    """Recent failed tasks from a Redis result backend (explains why if they aren't visible)."""
    return celery.failed_tasks(limit)


@tool
def task_details(task_id: str) -> dict:
    """Stored result/traceback for one task, and whether a worker currently holds it."""
    return celery.task_details(task_id)


# --- Redis ------------------------------------------------------------------

@tool
def memory_and_clients() -> dict:
    """Redis memory vs maxmemory, evictions, clients, keyspace and slowlog."""
    return redis.memory_and_clients()


@tool
def scan_keys(pattern: str = "*", limit: int = 50) -> dict:
    """List Redis keys matching a glob pattern using SCAN (never KEYS). Max 500."""
    return redis.scan_keys(pattern, limit)


@tool
def key_info(key: str) -> dict:
    """Type, TTL, size, encoding and a short redacted preview of one Redis key."""
    return redis.key_info(key)


# --- Logs -------------------------------------------------------------------

@tool
def tail_logs(source: str, lines: int = 100) -> dict:
    """Last N lines (max 500) of a configured log source (file path or docker container)."""
    return logs.tail_logs(source, lines)


@tool
def search_logs(pattern: str, since_minutes: int = 30, source: str | None = None) -> dict:
    """Search configured log sources for a regex (case-insensitive) within the last N minutes."""
    return logs.search_logs(pattern, since_minutes, source)


USAGE_EPILOG = """\
MCP clients (Claude Desktop, Claude Code, Cursor, VS Code) start this server themselves.
Running it by hand starts an MCP server on stdin/stdout that waits for a client.

configuration (environment variables or a .env file; unset sources are skipped):
  DATABASE_URL           Postgres, e.g. postgresql://stackdoctor_ro:...@localhost:5432/app
  REDIS_URL              Redis, e.g. redis://localhost:6379/0
  CELERY_BROKER_URL      Celery broker (Redis; RabbitMQ is experimental)
  CELERY_RESULT_BACKEND  Redis result backend, for failed tasks
  CELERY_APP             optional import path of your Celery app, e.g. myproject.celery:app
  LOG_SOURCES            comma-separated log files and/or docker:<container>
  STACKDOCTOR_ENV_FILE   path to a .env file (default: .env in the working directory)

docs: https://github.com/lepri89/stackdoctor
"""


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="stackdoctor",
        description="Read-only MCP server that diagnoses a Python backend stack "
                    "(Postgres, Celery, Redis, logs).",
        epilog=USAGE_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"stackdoctor {_version()}")
    parser.parse_args(argv)  # --help / --version print and exit here

    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)  # stdout is the MCP channel
    get_config()
    mcp.run("stdio")


def _version() -> str:
    try:
        return importlib.metadata.version("stackdoctor")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


if __name__ == "__main__":
    main()
