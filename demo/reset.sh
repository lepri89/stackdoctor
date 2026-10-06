#!/usr/bin/env sh
# Brings the demo back to a healthy state: worker running, no locks or slow queries.
set -e
cd "$(dirname "$0")"
docker compose exec -T postgres psql -U shop -d shop -c \
  "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = 'shop' AND pid <> pg_backend_pid() AND application_name = 'psql';" >/dev/null
docker compose start worker
echo "Demo reset."
