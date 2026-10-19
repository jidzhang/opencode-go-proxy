@echo off
setlocal

rem ============================================================
rem  Stop the opencode-go-proxy Windows service via NSSM.
rem  Run as Administrator.
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
echo   stop %SVC% via NSSM
echo ============================================
echo.

echo [1/2] Current service info ---
echo       status:
"%NSSM%" status %SVC% 2>nul
echo       application:
"%NSSM%" get %SVC% Application 2>nul
echo       parameters:
"%NSSM%" get %SVC% AppParameters 2>nul
echo.

echo ============================================
echo   Press any key to STOP the service ...
echo ============================================
pause >nul

echo.
echo [2/2] Stopping service ...
"%NSSM%" stop %SVC%
if errorlevel 1 (
    echo [ERROR] Failed to stop service. Is it running? Are you Administrator?
    exit /b 1
)
echo       done.
echo.

echo ============================================
echo   Stop complete.
echo ============================================
exit /b 0
