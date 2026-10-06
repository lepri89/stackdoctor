#!/usr/bin/env sh
# Stops the Celery worker, then enqueues tasks so the queue grows.
set -e
cd "$(dirname "$0")"
docker compose stop worker
docker compose exec -T api python enqueue.py "${1:-50}"
echo
echo "Worker stopped and tasks enqueued. Now ask your AI: \"why are my jobs stuck?\""
echo "Restore with: ./reset.sh"
