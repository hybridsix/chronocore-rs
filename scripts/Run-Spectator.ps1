#Requires -Version 5.1
<#
.SYNOPSIS
    Launches the ChronoCoreRS spectator display in Chrome fullscreen mode.

.DESCRIPTION
    Opens the spectator UI in Chrome/Edge fullscreen mode (not kiosk).
    Can be used on Windows machines to display race results on a separate screen.

.PARAMETER Server
    Server hostname or IP address (default: localhost)

.PARAMETER Port
    Server port number (default: 8000)

.EXAMPLE
    .\Run-Spectator.ps1
    # Opens localhost:8000

.EXAMPLE
    .\Run-Spectator.ps1 -Server 192.168.1.100
    # Opens 192.168.1.100:8000

.EXAMPLE
    .\Run-Spectator.ps1 -Server 192.168.1.100 -Port 8080
    # Opens 192.168.1.100:8080
#>
param(
    [string]$Server = "localhost",
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"
$Url = "http://${Server}:${Port}/ui/spectator/"

Write-Host "=== ChronoCoreRS Spectator Display ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "Server:  $Server"
Write-Host "Port:    $Port"
Write-Host "URL:     $Url"
Write-Host ""

# Find Chrome or Edge
$Chrome = $null
$ChromePaths = @(
    "${env:ProgramFiles}\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "${env:LOCALAPPDATA}\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles}\Microsoft\Edge\Application\msedge.exe",
    "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe"
)

foreach ($path in $ChromePaths) {
    if (Test-Path $path) {
        $Chrome = $path
        break
    }
}

if (-not $Chrome) {
    Write-Host "ERROR: Chrome or Edge not found!" -ForegroundColor Red
    Write-Host ""
    Write-Host "Install Chrome from: https://www.google.com/chrome/" -ForegroundColor Yellow
    exit 1
}

Write-Host "Using browser: $Chrome" -ForegroundColor Green
Write-Host ""

# Test server connectivity
Write-Host "Testing connection to server..."
try {
    $response = Invoke-WebRequest -Uri "http://${Server}:${Port}/healthz" -TimeoutSec 5 -UseBasicParsing -ErrorAction Stop
    Write-Host "Server is online." -ForegroundColor Green
} catch {
    Write-Host ""
    Write-Host "WARNING: Cannot connect to server at ${Server}:${Port}" -ForegroundColor Yellow
    Write-Host "Make sure the ChronoCoreRS server is running." -ForegroundColor Yellow
    Write-Host ""
    $continue = Read-Host "Continue anyway? (y/N)"
    if ($continue -ne "y" -and $continue -ne "Y") {
        exit 1
    }
}

Write-Host ""
Write-Host "Launching spectator display in fullscreen mode..." -ForegroundColor Green
Write-Host "Press F11 to exit fullscreen, Alt+F4 to close window" -ForegroundColor Cyan
Write-Host ""

# Launch Chrome in fullscreen (not kiosk) mode
$ChromeArgs = @(
    "--start-fullscreen",
    "--app=$Url",
    "--disable-infobars",
    "--noerrdialogs",
    "--disable-session-crashed-bubble",
    "--disable-features=TranslateUI",
    "--disable-component-update",
    "--no-first-run",
    "--no-default-browser-check"
)

Start-Process $Chrome -ArgumentList $ChromeArgs
