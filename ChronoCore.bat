@echo off
REM ChronoCore Race Software - Quick Launch
REM Launches the operator desktop application

cd /d "%~dp0"

REM Check for a usable Python launcher (python, falling back to py -3).
REM Run-Operator.ps1 does the same check plus version verification and
REM creates/repairs the virtual environment as needed - this is just a
REM fast, friendly failure if Python isn't installed at all.
python --version >nul 2>&1
if not errorlevel 1 goto :python_ok
py -3 --version >nul 2>&1
if not errorlevel 1 goto :python_ok

echo ERROR: Python not found!
echo Please install Python 3.12 or above from https://www.python.org/downloads/
echo Ensure "Add Python to PATH" is checked during installation.
pause
exit /b 1

:python_ok

REM Check if config exists
if not exist "config\config.yaml" (
    echo ERROR: Configuration file not found!
    echo Please ensure config/config.yaml exists.
    pause
    exit /b 1
)

REM Launch operator (Run-Operator.ps1 creates/repairs the virtual
REM environment and installs dependencies as needed)
echo Starting ChronoCore Operator...
powershell.exe -ExecutionPolicy Bypass -File ".\scripts\Run-Operator.ps1"
