# Run-Operator.ps1
# Launches the ChronoCore Operator Console as a desktop application
# Uses pywebview to display the UI in a native window with splash screen

param(
    [switch]$Debug
)

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path | Split-Path -Parent
$VenvPath = "$Root\.venv"
$VenvPython = "$VenvPath\Scripts\python.exe"

function Get-SystemPython {
    $pythonCmd = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCmd) {
        return @{ Exe = "python"; Args = @() }
    }

    $pyCmd = Get-Command py -ErrorAction SilentlyContinue
    if ($pyCmd) {
        return @{ Exe = "py"; Args = @("-3") }
    }

    return $null
}

function Test-PythonUsable([string]$ExePath) {
    if (-not (Test-Path $ExePath)) {
        return $false
    }

    & $ExePath -c "import sys; print(sys.executable)" 2>$null | Out-Null
    return ($LASTEXITCODE -eq 0)
}

function New-ProjectVenv {
    $systemPython = Get-SystemPython
    if (-not $systemPython) {
        Write-Host "ERROR: Python not found in PATH!" -ForegroundColor Red
        Write-Host "Please install Python 3.12 or above and ensure it's in your PATH." -ForegroundColor Yellow
        exit 1
    }

    $pythonVersion = & $systemPython.Exe @($systemPython.Args) -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
    if ($pythonVersion) {
        $versionParts = $pythonVersion.Trim() -split '\.'
        $major = [int]$versionParts[0]
        $minor = [int]$versionParts[1]
        if ($major -lt 3 -or ($major -eq 3 -and $minor -lt 12)) {
            Write-Host "ERROR: Python $pythonVersion detected, but Python 3.12 or above is required!" -ForegroundColor Red
            Write-Host "Please install Python 3.12+ from https://www.python.org/downloads/" -ForegroundColor Yellow
            exit 1
        }
        Write-Host "Using Python $pythonVersion" -ForegroundColor Green
    }

    if (Test-Path $VenvPath) {
        Write-Host "Removing broken virtual environment..." -ForegroundColor Yellow
        Remove-Item -Recurse -Force $VenvPath
    }

    Write-Host "Creating virtual environment at: $VenvPath" -ForegroundColor Cyan
    & $systemPython.Exe @($systemPython.Args) -m venv $VenvPath

    if ($LASTEXITCODE -ne 0 -or -not (Test-PythonUsable $VenvPython)) {
        Write-Host "ERROR: Failed to create a working virtual environment" -ForegroundColor Red
        exit 1
    }

    Write-Host "Installing dependencies from backend/requirements.txt..." -ForegroundColor Cyan
    & "$VenvPython" -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: Failed to upgrade pip" -ForegroundColor Red
        exit 1
    }

    & "$VenvPython" -m pip install -r "$Root\backend\requirements.txt"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: Failed to install dependencies" -ForegroundColor Red
        exit 1
    }

    Write-Host "Virtual environment created successfully!" -ForegroundColor Green
    Write-Host ""
}

Write-Host "=== ChronoCore Operator Console ===" -ForegroundColor Cyan
Write-Host "Starting desktop application..." -ForegroundColor Green
Write-Host ""

# Check/create venv if it doesn't exist or is broken
if (-not (Test-Path $VenvPython)) {
    Write-Host "Virtual environment not found. Creating..." -ForegroundColor Yellow
    New-ProjectVenv
} elseif (-not (Test-PythonUsable $VenvPython)) {
    Write-Host "Detected broken virtual environment Python launcher." -ForegroundColor Yellow
    New-ProjectVenv
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
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: Failed to install pywebview" -ForegroundColor Red
        exit 1
    }
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
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: Failed to install PySide6" -ForegroundColor Red
        exit 1
    }
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
