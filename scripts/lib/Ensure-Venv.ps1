# Ensure-Venv.ps1
# Shared virtual-environment bootstrap logic for ChronoCoreRS entry-point scripts.
#
# Usage (from another script):
#   . "$PSScriptRoot\lib\Ensure-Venv.ps1"
#   $VenvPython = Invoke-EnsureVenv -Root $Root
#
# Handles:
#   - Finding a usable system Python (`python`, falling back to `py -3`)
#   - Verifying Python 3.12+ is available before creating a venv
#   - Detecting a broken venv (e.g. one whose embedded python.exe points at a
#     Python install path that no longer exists - happens when the project
#     folder is moved or synced to a different machine) and rebuilding it
#   - Installing backend/requirements.txt into the venv

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

function Test-PythonUsable {
    param([Parameter(Mandatory)][string]$ExePath)

    if (-not (Test-Path $ExePath)) {
        return $false
    }

    & $ExePath -c "import sys; print(sys.executable)" 2>$null | Out-Null
    return ($LASTEXITCODE -eq 0)
}

function New-ProjectVenv {
    param(
        [Parameter(Mandatory)][string]$Root,
        [Parameter(Mandatory)][string]$VenvPath,
        [Parameter(Mandatory)][string]$VenvPython
    )

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

    if ($LASTEXITCODE -ne 0 -or -not (Test-PythonUsable -ExePath $VenvPython)) {
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

function Invoke-EnsureVenv {
    <#
    .SYNOPSIS
        Ensures a working project venv exists at $Root\.venv, creating or
        repairing it as needed. Returns the path to the venv's python.exe.
    #>
    param([Parameter(Mandatory)][string]$Root)

    $VenvPath = "$Root\.venv"
    $VenvPython = "$VenvPath\Scripts\python.exe"

    if (-not (Test-Path $VenvPython)) {
        Write-Host "Virtual environment not found. Creating..." -ForegroundColor Yellow
        New-ProjectVenv -Root $Root -VenvPath $VenvPath -VenvPython $VenvPython
    } elseif (-not (Test-PythonUsable -ExePath $VenvPython)) {
        Write-Host "Detected broken virtual environment Python launcher." -ForegroundColor Yellow
        New-ProjectVenv -Root $Root -VenvPath $VenvPath -VenvPython $VenvPython
    }

    return $VenvPython
}
