@echo off
setlocal
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
  echo ERROR: LIVE15 Python environment was not found at .venv\Scripts\python.exe
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m live15_quant.control_center_launcher
if errorlevel 1 pause
endlocal
