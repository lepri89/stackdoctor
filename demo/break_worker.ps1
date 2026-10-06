# Stops the Celery worker, then enqueues tasks so the queue grows.
param([int]$Count = 50)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
docker compose stop worker
docker compose exec -T api python enqueue.py $Count
Write-Host "`nWorker stopped and tasks enqueued. Now ask your AI: `"why are my jobs stuck?`""
Write-Host "Restore with: .\reset.ps1"
