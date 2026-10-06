#!/usr/bin/env sh
# Runs seq-scan-heavy queries (no index on orders.note) in the background.
set -e
cd "$(dirname "$0")"
for i in 1 2 3; do
  docker compose exec -d postgres psql -U shop -d shop -c \
    "SELECT count(*) FROM orders o, generate_series(1, 300) g WHERE o.note LIKE '%abc%' || g::text;"
done
echo "Started 3 slow sequential-scan queries (each runs ~1 minute)."
echo "Ask your AI: \"why is the API slow?\""
