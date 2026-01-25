# Run-Operator.ps1
# Launches the ChronoCore Operator Console as a desktop application
# Uses pywebview to display the UI in a native window with splash screen

param(
    [switch]$Debug
)

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path | Split-Path -Parent

Write-Host "=== ChronoCore Operator Console ===" -ForegroundColor Cyan
Write-Host "Starting desktop application..." -ForegroundColor Green
Write-Host ""

# Check/create venv if it doesn't exist
$VenvPython = "$Root\.venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    Write-Host "Virtual environment not found. Creating..." -ForegroundColor Yellow
    
    # Check if Python is available
    $pythonCmd = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pythonCmd) {
        Write-Host "ERROR: Python not found in PATH!" -ForegroundColor Red
        Write-Host "Please install Python 3.12+ and ensure it's in your PATH." -ForegroundColor Yellow
        exit 1
    }
    
    Write-Host "Creating virtual environment at: $Root\.venv" -ForegroundColor Cyan
    & python -m venv "$Root\.venv"
    
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: Failed to create virtual environment" -ForegroundColor Red
        exit 1
    }
    
    Write-Host "Installing dependencies from backend/requirements.txt..." -ForegroundColor Cyan
    & "$VenvPython" -m pip install --upgrade pip
    & "$VenvPython" -m pip install -r "$Root\backend\requirements.txt"
    
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: Failed to install dependencies" -ForegroundColor Red
        exit 1
    }
    
    Write-Host "Virtual environment created successfully!" -ForegroundColor Green
    Write-Host ""
}

# Check for config file
if (-not (Test-Path "$Root\config\config.yaml")) {
    Write-Host "ERROR: Configuration file not found at $Root\config\config.yaml" -ForegroundColor Red
    exit 1
}

# Check if pywebview is installed
Write-Host "Checking dependencies..." -ForegroundColor Gray
$pythonExe = "$VenvPython"

try {
    & $pythonExe -c "import webview" 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "pywebview not found"
    }
} catch {
    Write-Host "Installing pywebview..." -ForegroundColor Yellow
    & $pythonExe -m pip install -q "pywebview>=4.4,<5"
}

# Check if PySide6 is installed (required GUI backend for pywebview on Windows)
try {
    & $pythonExe -c "import PySide6" 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "PySide6 not found"
    }
} catch {
    Write-Host "Installing PySide6..." -ForegroundColor Yellow
    & $pythonExe -m pip install -q "PySide6>=6.6,<7"
}

Write-Host "Configuration: $Root\config\config.yaml" -ForegroundColor Gray
Write-Host "Database: backend/db/laps.sqlite (per config)" -ForegroundColor Gray
Write-Host ""
Write-Host "Features:" -ForegroundColor Cyan
Write-Host "  - Auto-starts FastAPI backend on port 8000" -ForegroundColor Gray
Write-Host "  - Displays splash screen during startup" -ForegroundColor Gray
Write-Host "  - Opens Operator UI in native desktop window" -ForegroundColor Gray
Write-Host "  - Backend auto-stops when window closes" -ForegroundColor Gray
Write-Host ""

if ($Debug) {
    Write-Host "Debug mode enabled - DevTools will be available" -ForegroundColor Yellow
    Write-Host ""
    $env:CCRS_DEBUG = "1"
}

Write-Host "Launching operator console..." -ForegroundColor Green
Write-Host "Press Ctrl+C to exit" -ForegroundColor Gray
Write-Host ""

# Start the operator launcher (using python.exe directly to bypass broken venv wrappers)
Set-Location $Root
& $pythonExe backend\operator_launcher.py
