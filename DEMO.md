# Demo without Docker: "why are my jobs stuck?"

This reproduces the *worker stopped* scenario on a Mac with Homebrew Postgres and Redis, with no Docker
needed. It was tested on macOS 12 with `postgresql@15` and `redis`. It takes about 5 minutes, and the
last step cleans everything up.

You'll use **three terminals**, all in the repo folder:

| Terminal | Runs |
|---|---|
| **A** | setup, breaking things, checking the result |
| **B** | the Celery worker (keep it visible while recording) |
| **C** | Claude Code (or use Claude Desktop) |

## 1. Setup (terminal A)

```sh
cd ~/Documents/"stack doctor"
source "$HOME/.local/bin/env"                       # puts uv on PATH
uv sync

export PATH="$(brew --prefix postgresql@15)/bin:$PATH"
export LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8          # initdb needs a UTF-8 locale
export SD=/tmp/sd-demo
rm -rf "$SD" && mkdir -p "$SD"
```

Start Postgres on port 55432 and load the demo schema, 1M orders. Loading takes about 15 seconds.

```sh
initdb -D "$SD/pg" -U shop --auth=trust >/dev/null
pg_ctl -D "$SD/pg" -l "$SD/postgres.log" -o "-p 55432 -c unix_socket_directories='' \
  -c shared_preload_libraries=pg_stat_statements -c log_lock_waits=on \
  -c deadlock_timeout=1s -c log_line_prefix='%m [%p] '" start
createdb -h localhost -p 55432 -U shop shop
psql -q -h localhost -p 55432 -U shop -d shop -f demo/postgres/init.sql
```

Start Redis on port 56379:

```sh
redis-server --port 56379 --maxmemory 64mb --maxmemory-policy noeviction \
  --daemonize yes --dir "$SD" --logfile "$SD/redis.log"
```

Write the stackdoctor config. It connects with the read-only role that `init.sql` created:

```sh
cat > "$SD/stackdoctor.env" <<EOF
DATABASE_URL=postgresql://stackdoctor_ro:readonly@localhost:55432/shop
REDIS_URL=redis://localhost:56379/0
CELERY_BROKER_URL=redis://localhost:56379/0
CELERY_RESULT_BACKEND=redis://localhost:56379/1
LOG_SOURCES=$SD/worker.log,$SD/postgres.log
EXPECTED_WORKERS=1
QUEUE_THRESHOLD=20
EOF
```

## 2. Start the worker (terminal B)

```sh
cd ~/Documents/"stack doctor"/demo/app
export SD=/tmp/sd-demo
../../.venv/bin/celery -A tasks worker --pool threads --concurrency 2 -n worker1@%h \
  --loglevel INFO --pidfile "$SD/worker.pid" 2>&1 | tee -a "$SD/worker.log"
```

Two details matter here:

- **`tee` instead of `--logfile`.** Celery prints `worker: Warm shutdown` to stdout only, so the log
  file has to capture stdout for stackdoctor to see when the worker stopped.
- **`--pool threads`.** Celery's default prefork pool is unreliable on macOS with Python 3.13. The
  Docker demo uses prefork on Linux.

Back in **terminal A**, check that everything is healthy:

```sh
(cd demo/app && ../../.venv/bin/python enqueue.py 6)   # B shows 6 tasks succeed
```

## 3. Connect Claude (terminal C)

Use the repo's own venv, so you don't need PyPI and avoid the macOS 12 `uvx`/`realpath` issue:

```sh
cd ~/Documents/"stack doctor"
claude mcp add stackdoctor -e STACKDOCTOR_ENV_FILE=/tmp/sd-demo/stackdoctor.env \
  -- "$PWD/.venv/bin/stackdoctor"
claude
```

If you're recording in **Claude Desktop** instead, add this to
`~/Library/Application Support/Claude/claude_desktop_config.json` and restart the app:

```json
{
  "mcpServers": {
    "stackdoctor": {
      "command": "/Users/leprismacbookpro/Documents/stack doctor/.venv/bin/stackdoctor",
      "env": { "STACKDOCTOR_ENV_FILE": "/tmp/sd-demo/stackdoctor.env" }
    }
  }
}
```

## 4. Break it (terminal A)

```sh
kill -TERM "$(cat "$SD/worker.pid")"                  # B prints: worker: Warm shutdown (MainProcess)
while [ -f "$SD/worker.pid" ]; do sleep 1; done        # wait until the worker has fully exited
(cd demo/app && ../../.venv/bin/python enqueue.py 40)  # these 40 tasks have nobody to run them
```

## 5. Ask (terminal C)

> why are my jobs stuck?

Claude calls `diagnose("why are my jobs stuck?")`. Expect roughly:

- **findings:**
  - `no_workers` (critical): No Celery workers replied to ping
  - `queue_no_consumer` (critical): Queue 'celery' has 40 messages and no live worker consumes it
  - `queue_backlog`: 40 waiting messages (threshold 20)
  - `worker_shutdown` from `worker.log`: `worker: Warm shutdown (MainProcess)`
- **possible cause** (confidence: medium): "Celery worker went down → queue is not being consumed". The cause is the
  `Warm shutdown` line at the moment you ran `kill`, and the effects are the backlog and the
  unconsumed queue, each with a timestamp.

To check the same output without an AI (for example, before you hit record):

```sh
STACKDOCTOR_ENV_FILE="$SD/stackdoctor.env" uv run python -c "
import asyncio, json; from stackdoctor.server import diagnose
out = asyncio.run(diagnose('why are my jobs stuck?'))
print(json.dumps({k: out[k] for k in ('findings', 'possible_causes')}, indent=2))"
```

## 6. Recover, then clean up

Restart the worker in **terminal B** with the same command as step 2. It drains the 40 tasks, and
asking again shows a healthy stack.

When you're done (terminal A, after stopping the worker with Ctrl+C in B):

```sh
claude mcp remove stackdoctor
redis-cli -p 56379 shutdown nosave
pg_ctl -D "$SD/pg" stop -m fast
rm -rf "$SD"
```
