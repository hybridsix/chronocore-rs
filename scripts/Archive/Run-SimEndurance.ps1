# Run-SimEndurance.ps1
param(
  [int]$RaceId = 99,
  [int]$Teams = 12,
  [double]$BlueEveryMins = 10,
  [int]$BlueDurationSec = 20,
  [string]$Speed = "1.0"
)

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path | Split-Path -Parent
$env:DB_PATH = Join-Path $Root "laps.sqlite"

Write-Host "DB_PATH = $env:DB_PATH"
& "$Root\.venv\Scripts\python.exe" `
  "$Root\backend\tools\sim_feed.py" `
  --race-id $RaceId --race-type endurance `
  --synthetic-teams $Teams `
  --blue-every-mins $BlueEveryMins `
  --blue-duration-sec $BlueDurationSec `
  --speed $Speed
