@echo off
rem DeskPet source/developer launcher: start only, never install.
rem Uses the repo-local .venv created by Setup-Desktop.bat; no network,
rem no pip install, no fallback to random system/Conda Python.
setlocal
set "ROOT=%~dp0"
set "PYW=%ROOT%.venv\Scripts\pythonw.exe"
set "PY=%ROOT%.venv\Scripts\python.exe"

if exist "%PYW%" (
    start "" /D "%ROOT%" "%PYW%" "%ROOT%main.py"
    exit /b 0
)
if exist "%PY%" (
    start "" /D "%ROOT%" "%PY%" "%ROOT%main.py"
    exit /b 0
)

echo DeskPet environment is not installed.
echo Run Setup-Desktop.bat first.
pause
exit /b 2
