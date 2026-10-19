@echo off
setlocal

rem ============================================================
rem  Remove the opencode-go-proxy Windows service via NSSM.
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
echo   remove %SVC% via NSSM
echo ============================================
echo.

echo [1/3] Current service info ---
echo       status:
"%NSSM%" status %SVC% 2>nul
echo       application:
"%NSSM%" get %SVC% Application 2>nul
echo       parameters:
"%NSSM%" get %SVC% AppParameters 2>nul
echo.

echo ============================================
echo   Press any key to STOP and REMOVE the service ...
echo ============================================
pause >nul

echo.
echo [2/3] Stopping service ...
"%NSSM%" stop %SVC% >nul 2>&1

rem NSSM remove marks the service for deletion; give SCM time to finish.
timeout /t 2 /nobreak >nul 2>&1
echo       done.
echo.

echo [3/3] Removing service ...
"%NSSM%" remove %SVC% confirm
if errorlevel 1 (
    echo [ERROR] Failed to remove service. It may still be stopping or services.msc is open.
    exit /b 1
)
echo       done.
echo.

echo ============================================
echo   Remove complete. Port 19999 is free again for
echo   start-proxy-19999.bat in manual mode.
echo ============================================
exit /b 0
