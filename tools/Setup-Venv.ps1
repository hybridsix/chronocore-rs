# ============================================================================
# Setup-Venv.ps1
# ChronoCore Race Software - Virtual Environment Setup
# ============================================================================
# Creates and configures a Python virtual environment with all dependencies
# Run this script from the project root directory
# ============================================================================

[CmdletBinding()]
param(
    [switch]$Force,
    [switch]$Upgrade
)

$ErrorActionPreference = "Stop"

# Ensure we're in the project root
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location $projectRoot

try {
    Write-Host "ChronoCore Virtual Environment Setup" -ForegroundColor Cyan
    Write-Host "=====================================" -ForegroundColor Cyan
    Write-Host ""

    # Check Python installation
    Write-Host "[1/4] Checking Python installation..." -ForegroundColor Yellow
    try {
        $pythonVersion = & python --version 2>&1
        Write-Host "  ✓ Found: $pythonVersion" -ForegroundColor Green
        
        # Extract version number and check if >= 3.12
        if ($pythonVersion -match "Python (\d+)\.(\d+)") {
            $major = [int]$matches[1]
            $minor = [int]$matches[2]
            if ($major -lt 3 -or ($major -eq 3 -and $minor -lt 12)) {
                Write-Warning "  ⚠ Python 3.12+ recommended. You have Python $major.$minor"
            }
        }
    }
    catch {
        Write-Error "Python not found. Please install Python 3.12+ from https://www.python.org/downloads/"
        exit 1
    }

    # Check if venv exists
    $venvPath = Join-Path $projectRoot ".venv"
    if (Test-Path $venvPath) {
        if ($Force) {
            Write-Host "[2/4] Removing existing virtual environment..." -ForegroundColor Yellow
            Remove-Item -Recurse -Force $venvPath
            Write-Host "  ✓ Removed .venv" -ForegroundColor Green
        }
        else {
            Write-Host "[2/4] Virtual environment already exists" -ForegroundColor Yellow
            Write-Host "  → Use -Force to recreate it" -ForegroundColor Gray
            Write-Host "  → Skipping to dependency installation..." -ForegroundColor Gray
            $skipCreate = $true
        }
    }

    # Create virtual environment
    if (-not $skipCreate) {
        Write-Host "[2/4] Creating virtual environment..." -ForegroundColor Yellow
        & python -m venv .venv
        if ($LASTEXITCODE -ne 0) {
            Write-Error "Failed to create virtual environment"
            exit 1
        }
        Write-Host "  ✓ Created .venv" -ForegroundColor Green
    }

    # Activate virtual environment
    Write-Host "[3/4] Activating virtual environment..." -ForegroundColor Yellow
    $activateScript = Join-Path $venvPath "Scripts\Activate.ps1"
    if (-not (Test-Path $activateScript)) {
        Write-Error "Activation script not found: $activateScript"
        exit 1
    }
    & $activateScript
    Write-Host "  ✓ Activated .venv" -ForegroundColor Green

    # Install/upgrade pip
    Write-Host "[4/4] Installing dependencies..." -ForegroundColor Yellow
    Write-Host "  → Upgrading pip..." -ForegroundColor Gray
    & python -m pip install --upgrade pip --quiet
    
    # Install requirements
    $requirementsFile = Join-Path $projectRoot "backend\requirements.txt"
    if (-not (Test-Path $requirementsFile)) {
        Write-Error "Requirements file not found: $requirementsFile"
        exit 1
    }

    if ($Upgrade) {
        Write-Host "  → Installing/upgrading packages (this may take a minute)..." -ForegroundColor Gray
        & pip install --upgrade -r $requirementsFile
    }
    else {
        Write-Host "  → Installing packages (this may take a minute)..." -ForegroundColor Gray
        & pip install -r $requirementsFile
    }

    if ($LASTEXITCODE -ne 0) {
        Write-Error "Failed to install dependencies"
        exit 1
    }

    Write-Host "  ✓ All dependencies installed" -ForegroundColor Green
    Write-Host ""
    Write-Host "SUCCESS! Virtual environment ready." -ForegroundColor Green
    Write-Host ""
    Write-Host "Next steps:" -ForegroundColor Cyan
    Write-Host "  1. Activate the environment: .\.venv\Scripts\Activate.ps1" -ForegroundColor White
    Write-Host "  2. Start the server:         .\scripts\Run-Server.ps1" -ForegroundColor White
    Write-Host "  3. Or run operator app:      .\scripts\Run-Operator.ps1" -ForegroundColor White
    Write-Host ""
}
catch {
    Write-Host ""
    Write-Host "ERROR: $_" -ForegroundColor Red
    Write-Host $_.ScriptStackTrace -ForegroundColor Red
    exit 1
}
finally {
    Pop-Location
}
