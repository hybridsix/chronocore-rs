@echo off
REM ChronoCore Race Software - Quick Launch
REM Launches the operator desktop application

cd /d "%~dp0"

REM Check if venv exists
if not exist ".venv\" (
    echo ERROR: Virtual environment not found!
    echo Please run: python -m venv .venv
    echo Then: .venv\Scripts\pip install -r backend/requirements.txt
    pause
    exit /b 1
)

REM Check if config exists
if not exist "config\config.yaml" (
    echo ERROR: Configuration file not found!
    echo Please ensure config/config.yaml exists.
    pause
    exit /b 1
)

REM Launch operator
echo Starting ChronoCore Operator...
powershell.exe -ExecutionPolicy Bypass -File ".\scripts\Run-Operator.ps1"
