"""The MCP surface, exercised through a real MCP client (in-memory transport)."""

import json

import pytest
from mcp import Client

from stackdoctor import config
from stackdoctor.server import mcp

TOOLS = {
    "diagnose", "active_queries", "blocking_locks", "slow_queries", "run_select",
    "workers", "queue_lengths", "failed_tasks", "task_details",
    "memory_and_clients", "scan_keys", "key_info", "tail_logs", "search_logs",
}
HINTS = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
ENV_VARS = ("DATABASE_URL", "REDIS_URL", "CELERY_BROKER_URL", "CELERY_RESULT_BACKEND", "CELERY_APP",
            "CELERY_QUEUES", "LOG_SOURCES")


@pytest.fixture
def log_file(tmp_path, monkeypatch):
    """Only a log file is configured; every other source is unset."""
    env_file = tmp_path / "empty.env"
    env_file.write_text("")
    log = tmp_path / "app.log"
    log.write_text("ERROR boom: connection refused\n")  # no timestamp: dated by file mtime
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("STACKDOCTOR_ENV_FILE", str(env_file))
    monkeypatch.setenv("LOG_SOURCES", str(log))
    config.get_config.cache_clear()
    yield str(log)
    config.get_config.cache_clear()


async def call(name: str, args: dict | None = None) -> dict:
    async with Client(mcp) as client:
        result = await client.call_tool(name, args or {})
    assert not result.is_error, result
    return json.loads(result.content[0].text)


async def test_tools_list_has_every_tool_with_all_four_hints():
    async with Client(mcp) as client:
        tools = (await client.list_tools()).tools
    assert {t.name for t in tools} == TOOLS
    for t in tools:
        assert t.annotations is not None, t.name
        assert t.annotations.model_dump(by_alias=True, exclude_none=True) == HINTS, t.name


@pytest.mark.parametrize("name,args", [
    ("active_queries", {"min_duration_s": 5}),
    ("blocking_locks", {}),
    ("slow_queries", {"limit": 3}),
    ("run_select", {"sql": "SELECT 1"}),
])
async def test_postgres_tools_skip_without_database_url(log_file, name, args):
    assert await call(name, args) == {"skipped": "DATABASE_URL not set"}


@pytest.mark.parametrize("name,args", [
    ("workers", {}),
    ("queue_lengths", {"queues": ["celery"]}),
    ("failed_tasks", {"limit": 5}),
    ("task_details", {"task_id": "0b9f7c3e-2a51-4c0e-9d3e-1f2a3b4c5d6e"}),
])
async def test_celery_tools_skip_without_broker(log_file, name, args):
    assert await call(name, args) == {"skipped": "CELERY_APP / CELERY_BROKER_URL not set"}


@pytest.mark.parametrize("name,args", [
    ("memory_and_clients", {}),
    ("scan_keys", {"pattern": "celery-task-meta-*", "limit": 10}),
    ("key_info", {"key": "celery"}),
])
async def test_redis_tools_skip_without_redis_url(log_file, name, args):
    assert await call(name, args) == {"skipped": "REDIS_URL not set"}


async def test_tail_logs_reads_configured_file(log_file):
    out = await call("tail_logs", {"source": log_file, "lines": 10})
    assert out["count"] == 1 and "connection refused" in str(out["lines"])


async def test_tail_logs_rejects_unknown_source(log_file):
    out = await call("tail_logs", {"source": "/etc/passwd"})
    assert out["error"].startswith("Unknown log source")


async def test_search_logs_finds_matching_lines(log_file):
    out = await call("search_logs", {"pattern": "connection ref", "since_minutes": 10})
    assert out["match_count"] == 1 and "connection refused" in out["matches"][0]["line"]


async def test_diagnose_reports_skipped_sources(log_file):
    out = await call("diagnose", {"symptom": "why are my jobs stuck?"})
    assert {"findings", "timeline"} <= out.keys()
    assert "DATABASE_URL not set" in str(out) and "REDIS_URL not set" in str(out)
