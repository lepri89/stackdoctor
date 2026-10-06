# Holds an ACCESS EXCLUSIVE lock on orders, then enqueues tasks that will block on it.
param([int]$Seconds = 180)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
docker compose exec -d postgres psql -U shop -d shop -c "BEGIN; LOCK TABLE orders IN ACCESS EXCLUSIVE MODE; SELECT pg_sleep($Seconds); COMMIT;"
Start-Sleep -Seconds 2
docker compose exec -T api python enqueue.py 10
Write-Host "`norders is locked for ${Seconds}s; tasks are blocked behind it."
Write-Host "Ask your AI: `"why are my jobs stuck?`" (tasks start failing with lock timeouts after ~20s)"
