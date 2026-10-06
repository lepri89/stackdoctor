#!/usr/bin/env sh
# Holds an ACCESS EXCLUSIVE lock on orders, then enqueues tasks that will block on it.
set -e
cd "$(dirname "$0")"
SECONDS_TO_HOLD="${1:-180}"
docker compose exec -d postgres psql -U shop -d shop -c \
  "BEGIN; LOCK TABLE orders IN ACCESS EXCLUSIVE MODE; SELECT pg_sleep($SECONDS_TO_HOLD); COMMIT;"
sleep 2
docker compose exec -T api python enqueue.py 10
echo
echo "orders is locked for ${SECONDS_TO_HOLD}s; tasks are blocked behind it."
echo "Ask your AI: \"why are my jobs stuck?\" (tasks start failing with lock timeouts after ~20s)"
