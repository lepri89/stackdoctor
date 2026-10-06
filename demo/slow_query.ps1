# Runs seq-scan-heavy queries (no index on orders.note) in the background.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
1..3 | ForEach-Object {
  docker compose exec -d postgres psql -U shop -d shop -c "SELECT count(*) FROM orders o, generate_series(1, 300) g WHERE o.note LIKE '%abc%' || g::text;"
}
Write-Host "Started 3 slow sequential-scan queries (each runs ~1 minute)."
Write-Host "Ask your AI: `"why is the API slow?`""
