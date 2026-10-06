"""End-to-end check of the docker compose demo, as run in CI.

For each scenario: reset → break script → diagnose() over a real MCP stdio client → assert.
Every diagnose() response is saved to demo/e2e-output/ for inspection.

    cd demo && cp .env.example .env && docker compose up -d --build --wait && cd ..
    uv run python demo/e2e.py
"""

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

DEMO = Path(__file__).resolve().parent
OUT = DEMO / "e2e-output"


def sh(script: str) -> None:
    print(f"$ sh {script}", flush=True)
    subprocess.run(["sh", str(DEMO / script)], check=True)


async def call(session: ClientSession, tool: str, **args) -> dict:
    res = await session.call_tool(tool, args)
    if res.is_error:
        raise RuntimeError(f"{tool} failed: {res.content[0].text}")
    return json.loads(res.content[0].text)


async def wait_until(session, label, check, timeout=120, symptom="") -> dict:
    """Call diagnose() until `check(out)` returns no problems, or fail after `timeout`."""
    deadline = time.monotonic() + timeout
    while True:
        out = await call(session, "diagnose", symptom=symptom)
        (OUT / f"{label}.json").write_text(json.dumps(out, indent=2))
        problems = check(out)
        if not problems:
            print(f"ok: {label}", flush=True)
            return out
        if time.monotonic() > deadline:
            raise AssertionError(f"{label}: {problems} (see demo/e2e-output/{label}.json)")
        await asyncio.sleep(5)


def kinds(out: dict) -> set[str]:
    return {f["kind"] for f in out["findings"]}


def chain(out: dict, prefix: str) -> dict | None:
    return next((c for c in out["possible_causes"] if c["hypothesis"].startswith(prefix)), None)


# --- Expectations -------------------------------------------------------------

def healthy(out: dict) -> list[str]:
    problems = [f"{n} is {s}" for n, s in out["checks_run"].items() if s != "ok"]
    problems += [f"critical finding: {f['kind']}" for f in out["findings"] if f["severity"] == "critical"]
    return problems


def worker_stopped(out: dict) -> list[str]:
    problems = []
    if "no_workers" not in kinds(out):
        problems.append("expected finding no_workers")
    if not kinds(out) & {"queue_no_consumer", "queue_backlog"}:
        problems.append("expected finding queue_no_consumer or queue_backlog")
    c = chain(out, "Celery worker went down")
    if not c:
        problems.append("expected chain 'Celery worker went down'")
    elif "logs.worker_shutdown" not in c["cause"] or "Warm shutdown" not in c["cause"]:
        problems.append(f"chain cause should be the 'Warm shutdown' log line, got: {c['cause']}")
    return problems


def table_locked(out: dict) -> list[str]:
    problems = [f"expected finding {k}" for k in ("lock_held", "query_blocked") if k not in kinds(out)]
    c = chain(out, "Database lock")
    if not c:
        problems.append("expected chain 'Database lock'")
    else:
        if c["label"] != "possible cause":
            problems.append("chain must be labelled 'possible cause'")
        if c["confidence"] != "medium":
            problems.append(f"expected medium confidence, got {c['confidence']}")
        if not any("query_blocked" in e for e in c["effects"]):
            problems.append("expected query_blocked among the chain's effects")
    return problems


def slow_queries(out: dict) -> list[str]:
    return [] if "long_query" in kinds(out) else ["expected finding long_query"]


async def main() -> None:
    OUT.mkdir(exist_ok=True)
    env = {**os.environ, "STACKDOCTOR_ENV_FILE": str(DEMO / ".env")}
    params = StdioServerParameters(command=sys.executable, args=["-m", "stackdoctor"], env=env)
    async with stdio_client(params) as (r, w), ClientSession(r, w) as session:
        await session.initialize()
        tools = (await session.list_tools()).tools
        assert all(t.annotations.read_only_hint for t in tools), "every tool must be marked read-only"

        await wait_until(session, "0-baseline", healthy, timeout=180)

        sh("break_worker.sh")
        await wait_until(session, "1-worker-stopped", worker_stopped, symptom="why are my jobs stuck?")
        sh("reset.sh")
        await wait_until(session, "1-reset", healthy)

        sh("lock_table.sh")
        await wait_until(session, "2-table-locked", table_locked, symptom="why are my jobs stuck?")
        sh("reset.sh")
        await wait_until(session, "2-reset", healthy)

        sh("slow_query.sh")
        await wait_until(session, "3-slow-queries", slow_queries, symptom="why is the API slow?")
        sh("reset.sh")

        # The safety layer, end to end.
        rejected = await call(session, "run_select", sql="DELETE FROM orders")
        assert rejected.get("rejected"), rejected
        assert (await call(session, "run_select", sql="SELECT count(*) AS n FROM orders"))["rows"][0]["n"] == 1_000_000
    print("all demo scenarios passed")


if __name__ == "__main__":
    asyncio.run(main())
