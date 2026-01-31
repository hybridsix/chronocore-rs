@echo off
REM ChronoCore Race Software - Quick Launch
REM Launches the operator desktop application

cd /d "%~dp0"

REM Check Python version
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found!
    echo Please install Python 3.12 or above from https://www.python.org/downloads/
    echo Ensure "Add Python to PATH" is checked during installation.
    pause
    exit /b 1
)

REM Check if venv exists
if not exist ".venv\" (
    echo ERROR: Virtual environment not found!
    echo Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo ERROR: Failed to create virtual environment!
        pause
        exit /b 1
    )
    echo Installing dependencies...
    .venv\Scripts\pip install -r backend/requirements.txt
    if errorlevel 1 (
        echo ERROR: Failed to install dependencies!
        pause
        exit /b 1
    )
    echo Virtual environment created successfully!
    echo.
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
