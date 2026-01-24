#Requires -Version 5.1
<#
.SYNOPSIS
    Starts the ChronoCoreRS FastAPI server for browser-based UI access.

.DESCRIPTION
    Launches the backend server using the existing venv.
    Works around broken venv wrapper paths by using: python.exe -m uvicorn

.PARAMETER Port
    Port number for the server (default: 8000)

.PARAMETER NoReload
    Disable auto-reload on code changes
#>
param(
    [int]$Port = 8000,
    [switch]$NoReload
)

$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent
$VenvPython = "$Root\.venv\Scripts\python.exe"

Write-Host "=== ChronoCoreRS Server Startup ===" -ForegroundColor Cyan
Write-Host ""

# Check venv exists (don't recreate - just use what's there)
if (-not (Test-Path $VenvPython)) {
    Write-Host "ERROR: Python virtual environment not found at: $VenvPython" -ForegroundColor Red
    Write-Host ""
    Write-Host "Create venv with:" -ForegroundColor Yellow
    Write-Host "  python -m venv .venv" -ForegroundColor Yellow
    Write-Host "  .\.venv\Scripts\Activate.ps1" -ForegroundColor Yellow
    Write-Host "  pip install -r requirements.txt" -ForegroundColor Yellow
    exit 1
}

# Check config file exists
$ConfigPath = "$Root\config\config.yaml"
if (-not (Test-Path $ConfigPath)) {
    Write-Host "ERROR: Config file not found: $ConfigPath" -ForegroundColor Red
    Write-Host "The server requires config/config.yaml to run." -ForegroundColor Yellow
    exit 1
}

# Check/add Windows Firewall rules for network access
$FirewallRuleInbound = "ChronoCoreRS Server (Inbound)"
$FirewallRuleOutbound = "ChronoCoreRS OSC Output (Outbound)"
$ExistingInbound = Get-NetFirewallRule -DisplayName $FirewallRuleInbound -ErrorAction SilentlyContinue
$ExistingOutbound = Get-NetFirewallRule -DisplayName $FirewallRuleOutbound -ErrorAction SilentlyContinue

if (-not $ExistingInbound -or -not $ExistingOutbound) {
    Write-Host "Windows Firewall rules not found. Creating rules..." -ForegroundColor Yellow
    $RulesCreated = $false
    
    # Create inbound rule for HTTP server
    if (-not $ExistingInbound) {
        try {
            New-NetFirewallRule -DisplayName $FirewallRuleInbound `
                                -Direction Inbound `
                                -Program $VenvPython `
                                -Action Allow `
                                -Profile Any `
                                -Description "Allow ChronoCoreRS server to accept network connections (HTTP/spectator displays)" `
                                -ErrorAction Stop | Out-Null
            Write-Host "  ✓ Inbound rule created (HTTP server)" -ForegroundColor Green
            $RulesCreated = $true
        } catch {
            Write-Host "  ✗ Could not create inbound rule" -ForegroundColor Red
            Write-Host "    Error: $_" -ForegroundColor DarkGray
        }
    }
    
    # Create outbound rule for OSC lighting control
    if (-not $ExistingOutbound) {
        try {
            New-NetFirewallRule -DisplayName $FirewallRuleOutbound `
                                -Direction Outbound `
                                -Program $VenvPython `
                                -Protocol UDP `
                                -Action Allow `
                                -Profile Any `
                                -Description "Allow ChronoCoreRS to send OSC commands to lighting systems" `
                                -ErrorAction Stop | Out-Null
            Write-Host "  ✓ Outbound rule created (OSC lighting)" -ForegroundColor Green
            $RulesCreated = $true
        } catch {
            Write-Host "  ✗ Could not create outbound rule" -ForegroundColor Red
            Write-Host "    Error: $_" -ForegroundColor DarkGray
        }
    }
    
    if (-not $RulesCreated) {
        Write-Host ""
        Write-Host "WARNING: Firewall rules could not be created automatically." -ForegroundColor Yellow
        Write-Host "You may need to run PowerShell as Administrator or manually allow access." -ForegroundColor Yellow
    }
    Write-Host ""
}

Write-Host "Configuration:" -ForegroundColor Green
Write-Host "  Python:  $VenvPython"
Write-Host "  Config:  $ConfigPath"
Write-Host "  Port:    $Port"
Write-Host ""

# Launch lap logger in a separate PowerShell window
Write-Host "Starting lap logger in separate window..." -ForegroundColor Cyan
$LapLoggerCmd = "Set-Location '$Root'; & '$VenvPython' -m backend.lap_logger; Read-Host 'Press Enter to close'"
Start-Process pwsh -ArgumentList "-NoExit", "-Command", $LapLoggerCmd

# Give lap logger a moment to start
Start-Sleep -Seconds 2

Write-Host ""
Write-Host "Server will be accessible at:" -ForegroundColor Cyan
Write-Host "  Operator UI:    http://localhost:$Port/ui/operator/" -ForegroundColor Yellow
Write-Host "  Spectator UI:   http://localhost:$Port/ui/spectator/" -ForegroundColor Yellow
Write-Host "  Health Check:   http://localhost:$Port/healthz" -ForegroundColor Yellow
Write-Host ""
Write-Host "Lap logger running in separate window." -ForegroundColor Green
Write-Host "Starting server... (Ctrl+C to stop)" -ForegroundColor Green
Write-Host ""

# Launch server using python -m uvicorn (bypasses broken venv wrapper paths)
Set-Location $Root
if ($NoReload) {
    & $VenvPython -m uvicorn backend.server:app --host 0.0.0.0 --port $Port
} else {
    & $VenvPython -m uvicorn backend.server:app --reload --host 0.0.0.0 --port $Port
}
