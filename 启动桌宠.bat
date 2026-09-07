@echo off
rem DeskPet launcher (miniconda env: deskpet, pythonw hides console)
set PY=D:\miniconda3\envs\deskpet\pythonw.exe
if not exist "%PY%" set PY=D:\miniconda3\envs\deskpet\python.exe
start "" "%PY%" "%~dp0main.py"
