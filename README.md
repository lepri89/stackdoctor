# stackdoctor

[![tests](https://github.com/lepri89/stackdoctor/actions/workflows/tests.yml/badge.svg)](https://github.com/lepri89/stackdoctor/actions/workflows/tests.yml)
[![demo](https://github.com/lepri89/stackdoctor/actions/workflows/demo.yml/badge.svg)](https://github.com/lepri89/stackdoctor/actions/workflows/demo.yml)
![python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)
![license](https://img.shields.io/badge/license-MIT-green)

> **Strictly read-only.** stackdoctor never writes, deletes, restarts, retries, revokes or sends anything.
> Postgres sessions are forced read-only by the server, Redis commands go through an allowlist,
> and Celery is only *inspected*. Secrets are redacted from every response.

stackdoctor is an MCP server that lets Claude, Cursor or any MCP client diagnose a Python backend stack
(**Postgres, Celery, Redis and logs**) from a single tool call.

Ask *"why are my jobs stuck?"* and the assistant calls `diagnose(symptom)`. It runs every relevant check
in parallel and returns one timestamped snapshot with:

- **findings**: blocked queries, missing workers, queue backlogs, log error spikes, Redis memory pressure, …
- **a merged timeline** of events from Postgres, Celery, Redis and your logs
- **possible cause → effect chains** when events line up in time, each with its evidence timestamps

```text
possible cause (confidence: medium): Database lock → blocked queries → stuck or failing tasks
  cause:  21:58:01 postgres.lock_held      pid 7959 holds a lock blocking 2 sessions: LOCK TABLE orders …
  effect: 21:58:02 logs.db_lock_wait       [postgres] process 7962 still waiting for RowExclusiveLock …
  effect: 21:58:21 celery.task_failed      tasks.process_order[…] failed: LockNotAvailable: lock timeout
  effect: 21:58:21 celery.task_long_running  tasks.process_order[…] running 13.2s on worker1
  effect: 21:58:34 celery.workers_saturated  worker1: all 2 slots busy, 2 reserved
  caveat: Inferred from timing only. Verify before acting; this is not a confirmed root cause.
```

## Install

You need [uv](https://docs.astral.sh/uv/getting-started/installation/):

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh                          # macOS / Linux
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"                # Windows
```

Then one command runs the server, with no other install step:

```sh
uvx stackdoctor
```

Until the package is on PyPI, run it from a checkout with `uvx --from /path/to/stackdoctor stackdoctor`,
or from git with `uvx --from git+https://github.com/lepri89/stackdoctor stackdoctor`.

> **macOS 12 (Monterey):** `uvx stackdoctor` fails with `realpath: command not found`, because uv's
> launcher script needs `realpath`, which only ships with macOS 13+. Use
> `uvx --from stackdoctor python -m stackdoctor` instead. In client configs that means
> `"args": ["--from", "stackdoctor", "python", "-m", "stackdoctor"]`. Alternatively, run
> `uv tool install stackdoctor` once and use `stackdoctor` as the command.

## Configure

Set environment variables in your MCP client config, or in a `.env` file. stackdoctor looks for `.env` in
the working directory, or uses the file named by `STACKDOCTOR_ENV_FILE`.
**Any source you don't configure is skipped**, so it's fine to start with only `DATABASE_URL`.

| Variable | Example | Used for |
|---|---|---|
| `DATABASE_URL` | `postgresql://stackdoctor_ro:…@localhost:5432/app` | Postgres checks |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis checks |
| `CELERY_BROKER_URL` | `redis://localhost:6379/0` (RabbitMQ `amqp://…` is experimental) | Celery inspect + queue lengths |
| `CELERY_RESULT_BACKEND` | `redis://localhost:6379/1` | failed tasks / task details |
| `CELERY_APP` | `myproject.celery:app` | optional: use your app's queues/routes/config (run from your project dir) |
| `CELERY_QUEUES` | `default,emails` | extra queue names to measure |
| `LOG_SOURCES` | `./logs/worker.log,docker:api,docker:worker` | log files and/or docker containers |

`LOG_SOURCES` entries are file paths, `docker:<container>`, or a bare container name.
Only configured sources can be read.

> **Celery workers: point `LOG_SOURCES` at the worker's stdout.** Celery prints
> `worker: Warm shutdown (MainProcess)` straight to stdout, not through logging, so it never reaches a
> `--logfile`. Docker containers already capture stdout. For a file, redirect stdout to it
> (`celery … worker >> worker.log 2>&1`, or supervisor/systemd stdout capture) so stackdoctor can see
> when a worker stopped.

<details>
<summary>Thresholds and limits</summary>

| Variable | Default | Meaning |
|---|---|---|
| `QUEUE_THRESHOLD` | 100 | messages waiting before a queue counts as a backlog |
| `EXPECTED_WORKERS` | 0 (off) | report missing workers if fewer reply |
| `LONG_TASK_S` / `LONG_QUERY_S` | 60 / 30 | when a task / query counts as long-running |
| `REDIS_MEM_WARN_PCT` | 85 | % of `maxmemory` that counts as "near the limit" |
| `LOG_ERROR_SPIKE` | 10 | problem lines in 5 minutes that count as a spike |
| `CHAIN_WINDOW_MIN` | 30 | how far apart cause and effect may be |
| `CHECK_TIMEOUT_S` | 8 | per-check timeout inside `diagnose` |
| `CELERY_INSPECT_TIMEOUT_S` | 1.0 | how long to wait for worker replies |
| `MAX_OUTPUT_CHARS` | 20000 | per-tool response cap (`diagnose` gets 2×) |

</details>

### Claude Desktop

`~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or
`%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "stackdoctor": {
      "command": "uvx",
      "args": ["stackdoctor"],
      "env": {
        "DATABASE_URL": "postgresql://stackdoctor_ro:password@localhost:5432/app",
        "REDIS_URL": "redis://localhost:6379/0",
        "CELERY_BROKER_URL": "redis://localhost:6379/0",
        "CELERY_RESULT_BACKEND": "redis://localhost:6379/1",
        "LOG_SOURCES": "docker:api,docker:worker"
      }
    }
  }
}
```

GUI apps often don't see your shell `PATH`. If Claude Desktop can't find `uvx`, use the full path:
`/Users/<username>/.local/bin/uvx` on macOS, or `C:\\Users\\<username>\\.local\\bin\\uvx.exe` on Windows.

### Claude Code

```sh
claude mcp add stackdoctor \
  -e DATABASE_URL=postgresql://stackdoctor_ro:password@localhost:5432/app \
  -e REDIS_URL=redis://localhost:6379/0 \
  -e CELERY_BROKER_URL=redis://localhost:6379/0 \
  -e LOG_SOURCES=docker:api,docker:worker \
  -- uvx stackdoctor
```

Or, from a project directory with a `.env` file, simply `claude mcp add stackdoctor -- uvx stackdoctor`.

### Cursor

`.cursor/mcp.json` in your project, or `~/.cursor/mcp.json` globally. This uses the same
`mcpServers` format as Claude Desktop:

```json
{
  "mcpServers": {
    "stackdoctor": {
      "command": "uvx",
      "args": ["stackdoctor"],
      "env": { "STACKDOCTOR_ENV_FILE": "${workspaceFolder}/.env" }
    }
  }
}
```

### VS Code (Copilot agent mode)

`.vscode/mcp.json`:

```json
{
  "servers": {
    "stackdoctor": {
      "type": "stdio",
      "command": "uvx",
      "args": ["stackdoctor"],
      "env": { "STACKDOCTOR_ENV_FILE": "${workspaceFolder}/.env" }
    }
  }
}
```

## Tools

| Tool | What it returns |
|---|---|
| `diagnose(symptom)` | **Start here.** Runs the relevant checks concurrently, each with its own timeout, and returns findings, a merged timeline and possible causes |
| `active_queries(min_duration_s)` | non-idle sessions, incl. *idle in transaction* |
| `blocking_locks()` | waiting sessions and the sessions blocking them |
| `slow_queries(limit)` | top statements from `pg_stat_statements` (explains how to enable it if missing) and seq-scan-heavy tables |
| `run_select(sql, limit)` | one validated `SELECT` / `WITH` / `EXPLAIN` with a row limit |
| `workers()` | live workers, their queues, concurrency, active/reserved tasks |
| `queue_lengths(queues)` | messages waiting per queue, read from the broker (Redis incl. priority queues; RabbitMQ experimental) |
| `failed_tasks(limit)` | recent failures from a Redis result backend, or *why* they aren't visible |
| `task_details(task_id)` | stored result/traceback, and whether a worker holds the task right now |
| `memory_and_clients()` | Redis memory vs `maxmemory`, evictions, clients, slowlog |
| `scan_keys(pattern, limit)` | key names via `SCAN` |
| `key_info(key)` | type, TTL, size, encoding, short redacted preview |
| `tail_logs(source, lines)` | last lines of a configured log source |
| `search_logs(pattern, since_minutes, source)` | regex search over recent log lines |

Postgres checks are intentionally light. For deep Postgres health (index advice, vacuum, bloat), run
[Postgres MCP Pro](https://github.com/crystaldba/postgres-mcp) next to stackdoctor.

## Safety

stackdoctor is built so that a confused or prompt-injected assistant still can't change your systems.

**Postgres**
- Every session is opened with `default_transaction_read_only=on`, `statement_timeout=5s` and
  `lock_timeout=2s`, passed as connection options so they override your URL. stackdoctor also checks
  the setting and refuses to run if the session isn't read-only.
- `run_select` parses SQL with [sqlglot](https://github.com/tobymao/sqlglot), not regexes. It accepts
  exactly one `SELECT` / `WITH … SELECT` / `EXPLAIN` statement and rejects:
  - writable CTEs, `SELECT … INTO` and `FOR UPDATE/SHARE`
  - `EXPLAIN ANALYZE`, because it executes the query
  - functions with side effects, even inside a read-only transaction: `pg_terminate_backend`,
    `pg_cancel_backend`, `pg_reload_conf`, `pg_read_file`, `pg_read_binary_file`, `pg_ls_dir`, `lo_*`,
    `dblink*`, `set_config`, `pg_advisory_*`, `query_to_xml` (which runs SQL from a string), `pg_sleep`, …
- Results are always limited: queries are wrapped as `SELECT * FROM (<q>) AS sd_sub LIMIT n`
  (default 100, max 1000).

**Recommended: a dedicated read-only role.** Defense in depth, so the database enforces the rules too:

```sql
CREATE ROLE stackdoctor_ro LOGIN PASSWORD 'change-me';
GRANT CONNECT ON DATABASE app TO stackdoctor_ro;
GRANT USAGE ON SCHEMA public TO stackdoctor_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO stackdoctor_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO stackdoctor_ro;
GRANT pg_monitor TO stackdoctor_ro;          -- see other sessions' queries and pg_stat_statements
ALTER ROLE stackdoctor_ro SET default_transaction_read_only = on;
```

Anything the role can `SELECT`, the assistant can read. If some tables hold data you don't want to
share with an AI, leave them out of the `GRANT SELECT`.

**Redis.** Every command is checked against an allowlist that is aware of subcommands (`INFO`, `SCAN`,
`TYPE`, `PTTL`, `LLEN`, `GET`, …). `CLIENT LIST` is allowed; `CLIENT KILL` / `PAUSE` are not. The same goes
for `OBJECT`, `MEMORY` and `CONFIG` (only `CONFIG GET`). `KEYS` is never used; listing always goes through `SCAN`.

**Celery.** Only `inspect` (`ping`, `active`, `reserved`, `active_queues`, `stats`, `query_task`) and
broker reads are used: `LLEN` on Redis, and passive `queue_declare` on RabbitMQ.

> **RabbitMQ support is experimental.** Queue lengths via passive `queue_declare` and the `inspect`
> calls are implemented but not yet covered by CI. The Redis broker is the tested path. Reports welcome. stackdoctor never revokes,
retries, shuts down or sends tasks.
*To be precise:* `inspect` works by publishing a broadcast message on Celery's **control channel**
(`celery.pidbox`) and reading replies from a temporary reply queue. That is how Celery's own
`celery inspect` command works. It doesn't touch your task queues or results, but it isn't zero
traffic. Everything else stackdoctor does is pure reads.

**Redaction.** Every response is redacted before it leaves the server. This covers:
- passwords in URLs (`postgres://user:***@host`)
- `Authorization` headers and bearer tokens
- `PASSWORD=`, `SECRET_KEY=`, `api_key:` and other secret-looking keys
- AWS, GitHub, Slack, Stripe, OpenAI/Anthropic-style and Google keys, JWTs, and private keys

Task args/kwargs and Redis value previews are truncated and redacted too. Redaction is pattern-based,
so treat it as a safety net, not a guarantee.

**Output caps.** Every tool caps its response (`MAX_OUTPUT_CHARS`) by trimming lists and long strings,
so a huge table or log can't flood the assistant's context.

**Logs.** Only sources listed in `LOG_SOURCES` can be read, so the assistant can't ask for `/etc/passwd`.
Docker logs are read with `docker logs`, called without a shell.

## Try the demo

The demo is a small FastAPI + Celery + Redis + Postgres app with scripts that break it in realistic ways.
CI runs every scenario on each push ([`demo.yml`](.github/workflows/demo.yml)): it breaks the stack,
calls `diagnose()` through a real MCP stdio client and checks the findings and chains.
To try it without Docker (local Postgres, Redis and worker), see [DEMO.md](DEMO.md).

```sh
cd demo
docker compose up -d --build        # first run builds the app and loads 1M rows (~1 min)
cp .env.example .env                # stackdoctor config for the demo (uses the read-only role)
```

Point your MCP client at the demo by setting `STACKDOCTOR_ENV_FILE` to the absolute path of `demo/.env`.
Then break something and ask:

| Script (macOS/Linux · Windows) | What it does | Ask |
|---|---|---|
| `./break_worker.sh` · `.\break_worker.ps1` | stops the Celery worker and enqueues 50 tasks | "why are my jobs stuck?" |
| `./lock_table.sh` · `.\lock_table.ps1` | holds an `ACCESS EXCLUSIVE` lock on `orders` for 3 min, then enqueues tasks that block on it | "why are my jobs stuck?" |
| `./slow_query.sh` · `.\slow_query.ps1` | runs three ~1-minute sequential scans | "why is the API slow?" |
| `./reset.sh` · `.\reset.ps1` | restarts the worker and ends the demo's locks and slow queries | |

**Colima (macOS).** `docker compose` works unchanged once Colima is running:
`colima start --cpu 2 --memory 4`. On macOS 13+ you can add `--vm-type vz`. On macOS 12 Colima uses
QEMU, which must be installed first (`brew install qemu`). **Windows:** use Docker Desktop or Docker
in WSL2 and run the `.ps1` scripts from PowerShell.

## Why stackdoctor instead of separate Postgres, Celery and log MCPs?

| | Separate MCP servers | stackdoctor |
|---|---|---|
| Install | one server per system, each with its own config | one `uvx stackdoctor` |
| Celery | usually needs Flower running | inspect API + broker, no Flower |
| Answering "why is it stuck?" | the AI calls 5–10 tools one after another, each a different moment in time | one `diagnose()` call: all checks run in parallel and share one timestamp |
| Correlation | left to the AI, across separate outputs | merged timeline + timing-based cause → effect hypotheses, with evidence |
| Safety | varies per server | one read-only policy for every system, redaction and output caps everywhere |
| Postgres depth | Postgres MCP Pro is deeper | intentionally light; use both |

## Development

```sh
uv sync
uv run pytest                                   # unit tests (safety layer, diagnose, logs)
STACKDOCTOR_TEST_DATABASE_URL=postgresql://shop:shop@localhost:55432/shop \
STACKDOCTOR_TEST_REDIS_URL=redis://localhost:56379/0 \
uv run pytest                                   # plus live tests against the demo
```

## Releasing

Releases go to PyPI from GitHub Actions with
[trusted publishing](https://docs.pypi.org/trusted-publishers/), so no API token is stored anywhere.

1. One-time: on PyPI, add a *pending publisher* (Account → Publishing) with project `stackdoctor`, owner
   `lepri89`, repository `stackdoctor`, workflow `release.yml` and environment `pypi`. In GitHub, create
   an environment named `pypi` (Settings → Environments); adding yourself as a required reviewer is a good idea.
2. Bump `version` in `pyproject.toml`, commit, then tag and push:
   `git tag v0.1.0 && git push origin v0.1.0`.
   The workflow checks that the tag matches the version, runs the tests, builds, and publishes.

## License

MIT
