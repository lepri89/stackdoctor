"""diagnose(): run checks concurrently, merge one timeline, derive findings and possible causes."""

from __future__ import annotations

import asyncio
import datetime as dt
import time

from .checks import celery, event, logs, postgres, redis, utcnow
from .config import get_config

# name -> (function, kind of timeout)
CHECKS = {
    "postgres.active_queries": (postgres.active_queries, "default"),
    "postgres.blocking_locks": (postgres.blocking_locks, "default"),
    "postgres.slow_queries": (postgres.slow_queries, "default"),
    "celery.workers": (celery.workers, "celery"),
    "celery.queue_lengths": (celery.queue_lengths, "celery"),
    "celery.failed_tasks": (celery.failed_tasks, "default"),
    "redis.memory_and_clients": (redis.memory_and_clients, "default"),
    "logs.recent_problems": (logs.recent_problems, "default"),
}

_JOBS = {"celery.workers", "celery.queue_lengths", "celery.failed_tasks", "postgres.blocking_locks",
         "postgres.active_queries", "redis.memory_and_clients", "logs.recent_problems"}
_SLOW = {"postgres.active_queries", "postgres.blocking_locks", "postgres.slow_queries",
         "redis.memory_and_clients", "celery.workers", "celery.queue_lengths", "logs.recent_problems"}
_REDIS = {"redis.memory_and_clients", "celery.queue_lengths", "logs.recent_problems"}
ROUTES = [
    (("job", "task", "queue", "worker", "celery", "stuck", "backlog", "pending"), _JOBS),
    (("slow", "latency", "api", "timeout", "query", "database", "db", "lock", "postgres", "hang"), _SLOW),
    (("redis", "memory", "cache", "oom", "evict", "broker"), _REDIS),
]

# Conditions observed "now" that are still in effect, so later effects can follow them.
ONGOING = {"lock_held", "idle_in_transaction", "query_blocked", "long_query", "no_workers",
           "worker_missing", "workers_saturated", "task_long_running", "queue_backlog",
           "queue_no_consumer", "memory_high"}

CHAIN_RULES = [
    {
        "hypothesis": "Celery worker went down → queue is not being consumed",
        "causes": {"worker_shutdown", "worker_lost", "no_workers", "worker_missing"},
        "effects": {"queue_backlog", "queue_no_consumer", "task_timeout", "timeout"},
        "explanation": "With no (or fewer) workers consuming, messages accumulate in the broker "
                       "and callers waiting on results time out.",
    },
    {
        "hypothesis": "Database lock → blocked queries → stuck or failing tasks",
        "causes": {"lock_held", "idle_in_transaction"},
        "effects": {"query_blocked", "db_lock_wait", "task_long_running", "workers_saturated", "queue_backlog",
                    "task_timeout", "db_lock_error", "db_timeout", "task_failed", "timeout"},
        "explanation": "A transaction holding a lock makes other queries wait; tasks running those "
                       "queries hang, occupy worker slots, and eventually time out or fail.",
    },
    {
        "hypothesis": "Long-running query → slow tasks/requests",
        "causes": {"long_query"},
        "effects": {"task_long_running", "workers_saturated", "db_timeout", "task_timeout", "timeout", "queue_backlog"},
        "explanation": "A slow query (e.g. a sequential scan) occupies the database and the "
                       "code paths waiting on it.",
    },
    {
        "hypothesis": "Redis memory pressure → broker/cache errors",
        "causes": {"memory_high", "evictions"},
        "effects": {"redis_error", "task_failed", "error_spike", "queue_backlog"},
        "explanation": "Near maxmemory Redis evicts keys or rejects writes (OOM), which breaks "
                       "the broker, result backend or cache.",
    },
    {
        "hypothesis": "Database connection problems → task failures",
        "causes": {"db_connection_error"},
        "effects": {"task_failed", "error_spike", "timeout"},
        "explanation": "Workers or the API can't reach Postgres (down, or out of connections).",
    },
    {
        "hypothesis": "All worker slots busy → queue backlog",
        "causes": {"workers_saturated", "task_long_running"},
        "effects": {"queue_backlog"},
        "explanation": "Tasks are being consumed slower than they arrive.",
    },
]

_SEVERITY = {"critical": 0, "warning": 1, "info": 2}


def select_checks(symptom: str) -> list[str]:
    text = (symptom or "").lower()
    chosen: set[str] = set()
    for words, checks in ROUTES:
        if any(w in text for w in words):
            chosen |= checks
    return sorted(chosen or CHECKS)  # vague symptom: run everything


async def _run(name: str) -> dict:
    cfg = get_config()
    fn, kind = CHECKS[name]
    timeout = cfg.check_timeout_s
    if kind == "celery":  # ping + 4 inspect broadcasts, each waiting up to the inspect timeout
        timeout = max(timeout, cfg.celery_inspect_timeout_s * 6 + 2)
    t0 = time.perf_counter()
    try:
        result = await asyncio.wait_for(asyncio.to_thread(fn), timeout)
        status = "skipped" if "skipped" in result else "ok"
        out = {"status": status, "result": result}
    except TimeoutError:
        out = {"status": "timeout", "error": f"timed out after {timeout:.0f}s"}
    except Exception as e:
        out = {"status": "error", "error": f"{type(e).__name__}: {str(e)[:300]}"}
    out["ms"] = round((time.perf_counter() - t0) * 1000)
    return out


def _cross_check_queues(results: dict) -> list[dict]:
    """Queues with messages that no live worker consumes (Redis brokers don't report consumers)."""
    w = results.get("celery.workers", {}).get("result") or {}
    q = results.get("celery.queue_lengths", {}).get("result") or {}
    if "alive_count" not in w or "queues" not in q:
        return []
    consumed = {name for worker in w["workers"] for name in worker["queues"]}
    events = []
    for name, info in q["queues"].items():
        n = info.get("messages") or 0
        if n and name not in consumed and info.get("consumers") is None:
            events.append(event(None, "celery", "queue_no_consumer",
                                f"Queue '{name}' has {n} messages and no live worker consumes it",
                                "critical", queue=name))
    return events


def _fmt(e: dict) -> str:
    return f"{e['ts'].isoformat(timespec='seconds')} {e['source']}.{e['kind']}: {e['detail']}"


def find_chains(events: list[dict], window_min: float) -> list[dict]:
    window = dt.timedelta(minutes=window_min)
    slack = dt.timedelta(seconds=60)
    chains, used = [], []
    for rule in CHAIN_RULES:
        causes = sorted((e for e in events if e["kind"] in rule["causes"]), key=lambda e: e["ts"])
        if not causes:
            continue
        cause = causes[0]
        ongoing = cause["kind"] in ONGOING
        effects: dict[str, dict] = {}
        for e in sorted(events, key=lambda e: e["ts"]):
            if e is cause or e["kind"] not in rule["effects"] or e["kind"] in effects:
                continue
            after = e["ts"] >= cause["ts"] - slack
            close = ongoing or e["ts"] <= cause["ts"] + window
            if after and close:
                effects[e["kind"]] = e
        if not effects:
            continue
        ids = {id(cause), *map(id, effects.values())}
        if any(ids <= prev for prev in used):
            continue  # already explained by an earlier chain
        used.append(ids)
        sources = {e["source"] for e in effects.values()} | {cause["source"]}
        confidence = "medium" if len(effects) >= 2 and len(sources) >= 2 else "low"
        chains.append({
            "label": "possible cause",
            "hypothesis": rule["hypothesis"],
            "confidence": confidence,
            "cause": _fmt(cause),
            "effects": [_fmt(e) for e in effects.values()],
            "evidence_window": {"from": cause["ts"], "to": max(e["ts"] for e in effects.values())},
            "explanation": rule["explanation"],
            "caveat": "Inferred from timing only. Verify before acting; this is not a confirmed root cause.",
        })
    chains.sort(key=lambda c: c["confidence"] != "medium")
    return chains


def _findings(events: list[dict], results: dict) -> tuple[list[dict], list[str]]:
    grouped: dict[tuple, dict] = {}
    for e in events:
        if e["severity"] == "info":
            continue
        key = (e["source"], e["kind"])
        f = grouped.setdefault(key, {"severity": e["severity"], "source": e["source"], "kind": e["kind"],
                                     "count": 0, "first_seen": e["ts"], "example": e["detail"]})
        f["count"] += e.get("count", 1)
        if _SEVERITY[e["severity"]] < _SEVERITY[f["severity"]]:
            f["severity"] = e["severity"]
    findings = sorted(grouped.values(), key=lambda f: (_SEVERITY[f["severity"]], f["first_seen"]))

    notes = []
    ft = results.get("celery.failed_tasks", {}).get("result") or {}
    if ft.get("visible") is False:
        n = sum(1 for e in events if e["kind"] == "task_failed" and e["source"] == "logs")
        notes.append(f"{ft['reason']} Falling back to logs: {n} task-failure group(s) found in logs.")
    sq = results.get("postgres.slow_queries", {}).get("result") or {}
    if sq.get("available") is False:
        notes.append("pg_stat_statements is not installed; slow query stats unavailable (see postgres.slow_queries).")
    lg = results.get("logs.recent_problems", {}).get("result") or {}
    kinds = {e["kind"] for e in events}
    if kinds & {"no_workers", "worker_missing"} and not kinds & {"worker_shutdown", "worker_lost"}:
        seen = ", ".join(f"{k} last written {v.isoformat(timespec='seconds')}"
                         for k, v in (lg.get("last_activity") or {}).items() if v)
        notes.append("Workers are missing but no shutdown/crash line was found in the logs. Celery prints "
                     "'worker: Warm shutdown' to stdout only (not to --logfile), so LOG_SOURCES must include "
                     "the worker's stdout (docker logs, or a file stdout is redirected to)."
                     + (f" Log activity: {seen}." if seen else ""))
    if lg.get("errors"):
        notes.append(f"Some log sources could not be read: {lg['errors']}")
    return findings, notes


async def diagnose(symptom: str = "") -> dict:
    cfg = get_config()
    started = utcnow()
    names = select_checks(symptom)
    outcomes = await asyncio.gather(*(_run(n) for n in names))
    results = dict(zip(names, outcomes))

    events: list[dict] = []
    for out in results.values():
        result = out.get("result")
        if isinstance(result, dict):
            events.extend(result.pop("events", []))
    events.extend(_cross_check_queues(results))
    # A lock holder is also a "long query"; the lock is the more specific explanation.
    lock_pids = {e.get("pid") for e in events if e["kind"] == "lock_held"}
    events = [e for e in events if not (e["kind"] == "long_query" and e.get("pid") in lock_pids)]
    for e in events:
        if e["ts"] > started + dt.timedelta(seconds=30):  # clock skew guard
            e["ts"] = started

    findings, notes = _findings(events, results)
    chains = find_chains(events, cfg.chain_window_min)
    timeline = [{"ts": e["ts"], "source": e["source"], "kind": e["kind"],
                 "severity": e["severity"], "detail": e["detail"]}
                for e in sorted(events, key=lambda e: e["ts"])][-80:]

    return {
        "timestamp": started,
        "symptom": symptom,
        "checks_run": {n: r["status"] for n, r in results.items()},
        "findings": findings,
        "possible_causes": chains,
        "notes": notes,
        "timeline": timeline,
        "checks": {n: r for n, r in results.items() if r["status"] != "skipped"},
        "skipped": {n: r["result"]["skipped"] for n, r in results.items() if r["status"] == "skipped"},
        "duration_ms": round((utcnow() - started).total_seconds() * 1000),
    }
