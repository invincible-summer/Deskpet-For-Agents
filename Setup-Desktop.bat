@echo off
rem DeskPet one-time setup (v4.3.0 release setup): repo-local .venv only.
rem Runs once; Start-Desktop.bat only starts and never installs.
setlocal EnableExtensions
cd /d "%~dp0"
set "ROOT=%~dp0"
set "VENV_PY=%ROOT%.venv\Scripts\python.exe"

echo === DeskPet Setup ===

if exist "%VENV_PY%" (
    echo Reusing existing environment: .venv
    goto install
)

rem Bootstrap interpreter: py -3.12, python3.12, then PATH python.
rem Every candidate must pass a version probe accepting only Python 3.12;
rem never download or install Python automatically.
set "BOOT="
py -3.12 -c "import sys; sys.exit(0 if sys.version_info[:2] == (3, 12) else 1)" >nul 2>&1
if not errorlevel 1 (
    set "BOOT=py -3.12"
    goto createvenv
)
python3.12 -c "import sys; sys.exit(0 if sys.version_info[:2] == (3, 12) else 1)" >nul 2>&1
if not errorlevel 1 (
    set "BOOT=python3.12"
    goto createvenv
)
python -c "import sys; sys.exit(0 if sys.version_info[:2] == (3, 12) else 1)" >nul 2>&1
if not errorlevel 1 (
    set "BOOT=python"
    goto createvenv
)

echo [ERROR] Python 3.12 not found.
echo Install Python 3.12 from https://www.python.org/downloads/ and run again.
exit /b 1

:createvenv
echo Creating .venv with: %BOOT%
%BOOT% -m venv "%ROOT%.venv"
if not exist "%VENV_PY%" (
    echo [ERROR] Failed to create .venv.
    exit /b 1
)

:install
echo Installing dependencies (pinned by constraints-v4.3.0.txt)...
"%VENV_PY%" -m pip install -r requirements.txt -c constraints-v4.3.0.txt
if errorlevel 1 (
    echo [ERROR] Dependency installation failed.
    exit /b 1
)

echo Smoke import check...
"%VENV_PY%" -c "import psutil, PIL, comtypes, pet.config, agents.monitor"
if errorlevel 1 (
    echo [ERROR] Smoke import failed. See the message above.
    exit /b 1
)

echo Setup complete. Double-click Start-Desktop.bat to run DeskPet.
exit /b 0
