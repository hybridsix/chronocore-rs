# Run-Server.ps1
param(
  [string]$Port = "8000"
)

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path | Split-Path -Parent
$env:DB_PATH = Join-Path $Root "laps.sqlite"

Write-Host "DB_PATH = $env:DB_PATH"
& "$Root\.venv\Scripts\Activate.ps1"
uvicorn backend.server:app --reload --host 0.0.0.0 --port $Port