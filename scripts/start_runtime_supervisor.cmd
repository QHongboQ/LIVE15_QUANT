@echo off
setlocal
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" exit /b 2
".venv\Scripts\python.exe" -m live15_quant.runtime_supervisor
exit /b %errorlevel%
