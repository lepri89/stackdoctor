---
name: stackdoctor
description: Diagnose a misbehaving Python backend stack (Postgres, Celery, Redis, logs) with the read-only stackdoctor MCP server. Use when Celery jobs are stuck, slow or failing, an API is slow, queries are blocked by database locks, Redis is near its memory limit or evicting keys, or logs show errors and you need to find out why.
---

# stackdoctor

stackdoctor is an MCP server that inspects a running Python backend (Postgres, Celery, Redis and
log files or docker containers) and correlates what it finds across those systems.

It is **strictly read-only**. Postgres sessions are forced read-only with a statement timeout,
Redis commands go through an allowlist, Celery is only inspected, and every response is redacted
and size-capped. It never writes, deletes, restarts, retries, revokes or sends anything, so it is
safe to call while investigating a live incident. It also cannot fix anything: any fix is a
separate step that the user approves.

## When to use it

Use stackdoctor when the user reports a runtime symptom in a stack that uses Postgres, Celery,
Redis or service logs, for example:

- Celery jobs are stuck, queued but not running, slow, or failing
- an API endpoint or page is slow or timing out
- queries hang, or errors mention lock timeouts or deadlocks
- Redis is near `maxmemory`, evicting keys, or rejecting connections
- logs show errors, tracebacks or connection failures and the cause is unclear

Don't use it for static code questions, for writing migrations, or for changing data. It only
reads the live state of the systems it is configured for.

## How to use it

1. **Call `diagnose(symptom)` first**, passing the user's words, e.g.
   `diagnose("why are my jobs stuck?")` or `diagnose("API is slow")`. It runs every relevant check
   in parallel and returns one timestamped snapshot:
   - `findings`: concrete problems (blocked queries, no workers, queue backlogs, log error spikes,
     Redis memory pressure, ...)
   - `possible_causes`: cause → effect chains built from events that line up in time, each with a
     confidence, evidence timestamps and a caveat
   - `timeline`: events from all sources merged in time order
   - `skipped`: sources that are not configured. Mention them to the user, because a skipped
     source can hide the real cause.
2. **Read the findings and the possible causes together.** Start from the highest-severity
   findings and the chains that explain them.
3. **Drill down with the specific tools** to confirm or rule out each possible cause:

   | Area | Tools |
   |---|---|
   | Postgres | `active_queries(min_duration_s)`, `blocking_locks()`, `slow_queries(limit)`, `run_select(sql, limit)` |
   | Celery | `workers()`, `queue_lengths(queues)`, `failed_tasks(limit)`, `task_details(task_id)` |
   | Redis | `memory_and_clients()`, `scan_keys(pattern, limit)`, `key_info(key)` |
   | Logs | `tail_logs(source, lines)`, `search_logs(pattern, since_minutes, source)` |

   For example, for a lock chain call `blocking_locks()` to see the blocking pid and its query.
   For a dead worker, `workers()` and `queue_lengths()` show whether anything is consuming the
   queue, and `search_logs("shutdown|SIGTERM")` shows when it stopped. For a failing task,
   `task_details(task_id)` returns the stored traceback.
4. **Report back** with what you verified, the evidence (timestamps, pids, queue names, log
   lines) and a suggested fix. The user decides whether to apply it.

## Treat chains as possible causes, never facts

`possible_causes` are hypotheses inferred **from timing only**. Two events close together in
time are not proof that one caused the other. Always:

- call them "possible causes" and give their confidence; never present one as the root cause
  until a drill-down tool has confirmed it
- verify each chain with the specific tools before recommending an action
- say so when the evidence doesn't support a chain, or when a skipped source could change the
  picture

## Install and configure

The user installs it once as an MCP server (it needs [uv](https://docs.astral.sh/uv/)):

```sh
uvx stackdoctor --help                         # check that it runs
claude mcp add stackdoctor -- uvx stackdoctor  # Claude Code, using the project's .env
```

stackdoctor reads its configuration from environment variables or a `.env` file. Any source that is
not configured is skipped, so starting with only `DATABASE_URL` is fine:

```sh
DATABASE_URL=postgresql://stackdoctor_ro:password@localhost:5432/app   # ideally a read-only role
REDIS_URL=redis://localhost:6379/0
CELERY_BROKER_URL=redis://localhost:6379/0
CELERY_RESULT_BACKEND=redis://localhost:6379/1
LOG_SOURCES=./logs/worker.log,docker:api,docker:worker
```

Other MCP clients use the same command, `uvx stackdoctor`, with these variables in `env`, or with
`STACKDOCTOR_ENV_FILE` set to the path of a `.env` file. If the stackdoctor tools aren't available,
tell the user how to add the server instead of guessing at the cause.

Docs: https://github.com/lepri89/stackdoctor
