@echo off
setlocal

rem ============================================================
rem  Restart the opencode-go-proxy Windows service via NSSM.
rem  Run as Administrator. Picks up any environment change made via
rem  nssm set opencode-go-proxy AppEnvironmentExtra ...
rem  Editing install-service.bat alone does not affect the
rem  installed service. Reinstall to apply those edits.
rem ============================================================

cd /d "%~dp0"

set SVC=opencode-go-proxy
set NSSM=nssm.exe

where %NSSM% >nul 2>&1
if errorlevel 1 (
    echo [ERROR] %NSSM% not found in PATH. Install NSSM and add it to PATH.
    exit /b 1
)

echo ============================================
echo   restart %SVC% via NSSM
echo ============================================
echo.

echo [0/3] Pruning old rotated logs (keep newest 5 generations) ...
powershell -NoProfile -Command "Get-ChildItem '%~dp0logs\service.err-*.log','%~dp0logs\service.out-*.log' -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -Skip 5 | Remove-Item -ErrorAction SilentlyContinue"
echo       done.
echo.

echo [1/3] Current service info ---
echo       status:
"%NSSM%" status %SVC% 2>nul
echo       application:
"%NSSM%" get %SVC% Application 2>nul
echo       parameters:
"%NSSM%" get %SVC% AppParameters 2>nul
echo.

echo [2/3] Stopping service ...
"%NSSM%" stop %SVC% >nul 2>&1
echo       done.
echo.

echo [3/3] Starting service and waiting for health ...
"%NSSM%" start %SVC%
if errorlevel 1 (
    echo [ERROR] Failed to start service. Check logs\service.err.log for details.
    exit /b 1
)
set OK=0
for /L %%i in (1,1,30) do (
  >nul 2>&1 curl -s -o nul --max-time 2 http://127.0.0.1:19999/health
  if not errorlevel 1 (
    set OK=1
    goto :ready
  )
  timeout /t 1 /nobreak >nul
)
:ready
if "%OK%"=="0" (
    echo [ERROR] Health check never returned 200 after restart. Check logs\service.err.log.
    exit /b 1
)
echo       done.
echo.

echo ============================================
echo   Restart complete. Health is 200 on port 19999.
echo ============================================
exit /b 0
