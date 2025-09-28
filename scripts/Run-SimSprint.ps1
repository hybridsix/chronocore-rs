# Run-SimSprint.ps1
param(
  [int]$RaceId = 99,
  [int]$Teams = 15,
  [string]$Speed = "1.0",
  # Optional: comma-separated custom names, e.g. "Neon Llamas,Robo Toasters,Blue Shells"
  [string]$TeamNames = "",
  [switch]$Keep
)

# Repo root (parent of the /scripts folder)
$Root = Split-Path -Parent $PSScriptRoot

# Pin both server and sim to the same absolute DB
$env:DB_PATH = Join-Path $Root "laps.sqlite"

$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Sim    = Join-Path $Root "backend\tools\sim_feed.py"

if (-not (Test-Path $Python)) {
  Write-Error "Python venv not found at: $Python`nCreate it:  python -m venv .venv; .\.venv\Scripts\Activate.ps1; pip install -r .\backend\requirements.txt"
  exit 1
}
if (-not (Test-Path $Sim)) {
  Write-Error "Simulator not found at: $Sim"
  exit 1
}

# Build args
$argsList = @(
  $Sim,
  "--race-id", $RaceId,
  "--race-type", "sprint",
  "--synthetic-teams", $Teams,
  "--speed", $Speed
)

if ($TeamNames -and $TeamNames.Trim().Length -gt 0) {
  $argsList += @("--teams", $TeamNames)
}
if ($Keep) {
  $argsList += "--keep"
}

Write-Host "DB_PATH = $env:DB_PATH"
Write-Host "Starting sprint sim: RaceId=$RaceId  Teams=$Teams  Speed=$Speed"
& $Python @argsList
