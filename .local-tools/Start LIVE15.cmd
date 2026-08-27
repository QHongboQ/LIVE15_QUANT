@echo off
setlocal
set "ROOT=%~dp0.."

rem The desktop shortcut uses the authoritative WinSW service. It must never
rem fall back to the superseded Python runtime supervisor.
sc.exe query LIVE15ControlCenter >nul 2>&1
if errorlevel 1 (
  echo ERROR: LIVE15ControlCenter is not installed. Install the WinSW service first.
  exit /b 3
)

for /f "tokens=4" %%S in ('sc.exe query LIVE15ControlCenter ^| findstr /R /C:"STATE"') do set "STATE=%%S"
if /I not "%STATE%"=="RUNNING" (
  sc.exe start LIVE15ControlCenter >nul 2>&1
  if errorlevel 1 (
    echo ERROR: LIVE15ControlCenter could not be started. Check the service status.
    exit /b 4
  )
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$deadline=(Get-Date).AddSeconds(30); while((Get-Date)-lt $deadline) { try { $system=Invoke-RestMethod -Uri 'http://127.0.0.1:8765/api/system' -TimeoutSec 2; if($system.service -eq 'LIVE15 Control Center' -and $system.bind_host -eq '127.0.0.1') { Start-Process 'http://127.0.0.1:8765/#/overview'; exit 0 } } catch {}; Start-Sleep -Milliseconds 500 }; Write-Error 'LIVE15ControlCenter did not expose its identity endpoint within 30 seconds.'; exit 5"
exit /b %errorlevel%
