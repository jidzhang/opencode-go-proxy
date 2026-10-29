@echo off
setlocal

rem ============================================================
rem  Install opencode-go-proxy as a Windows service via NSSM.
rem  Run as Administrator in a shell that has OPENCODE_GO_API_KEY
rem  and OPENCODE_GO_PROXY_CALLER_TOKEN set, e.g. via setx.
rem  Serves 0.0.0.0:19999. Optional egress proxy: OPENCODE_GO_PROXY_UPSTREAM.
rem  Machine-local values come from proxy-local.bat (gitignored).
rem ============================================================

cd /d "%~dp0"

if exist "%~dp0proxy-local.bat" call "%~dp0proxy-local.bat"

set SVC=opencode-go-proxy
set NSSM=nssm.exe

rem --- required tools must be present ---
where %NSSM% >nul 2>&1
if errorlevel 1 (
    echo [ERROR] %NSSM% not found in PATH. Install NSSM and add it to PATH.
    exit /b 1
)

rem --- service executable: absolute path, LocalSystem PATH may differ ---
set EXE=%cd%\.venv\Scripts\opencode-go-proxy.exe
if not exist "%EXE%" (
    echo [ERROR] opencode-go-proxy.exe not found under .venv\Scripts.
    echo         Run uv sync in this directory and retry.
    exit /b 1
)

rem --- required secrets must be present in this shell ---
if not defined OPENCODE_GO_API_KEY (
    echo [ERROR] OPENCODE_GO_API_KEY is not set in this shell.
    echo         Set it via setx, open a NEW Admin shell, and retry.
    exit /b 1
)
if not defined OPENCODE_GO_PROXY_CALLER_TOKEN (
    echo [ERROR] OPENCODE_GO_PROXY_CALLER_TOKEN is not set in this shell.
    echo         It must be at least 32 chars. Set it via setx, open a NEW Admin shell, and retry.
    exit /b 1
)

echo ============================================
echo   setup %SVC% using NSSM
echo ============================================
echo.

echo [1/8] Stopping any manual proxy on port 19999 ...
rem Kill only listeners that are this repo's own venv proxy; an unrelated
rem process on the port is reported and left alone.
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":19999" ^| findstr LISTENING') do (
  tasklist /fi "PID eq %%p" /fo csv /nh 2>nul | findstr /i "opencode-go-proxy" >nul 2>&1 && (
    taskkill /F /T /PID %%p >nul 2>&1
  ) || (
    echo [install] port 19999 is held by a foreign process ^(PID %%p^); not killing it. >&2
  )
)
timeout /t 2 /nobreak >nul 2>&1
echo       done.
echo.

echo [2/8] Removing any previous service instance ...
"%NSSM%" stop   %SVC% >nul 2>&1
"%NSSM%" remove %SVC% confirm >nul 2>&1
rem Give SCM a moment to finish deletion before recreating the service.
timeout /t 2 /nobreak >nul 2>&1
echo       done.
echo.

echo [3/8] Installing service ...
"%NSSM%" install %SVC% "%EXE%" --bind 0.0.0.0 --port 19999
if errorlevel 1 (
    echo [ERROR] Install failed. Are you running as Administrator?
    exit /b 1
)
echo.

echo [4/8] Configuring working directory and logging ...
"%NSSM%" set %SVC% AppDirectory "%cd%"
if errorlevel 1 goto :configure_failed
if not exist "%cd%\logs" mkdir "%cd%\logs" >nul 2>&1
"%NSSM%" set %SVC% AppStdout "%cd%\logs\service.out.log"
if errorlevel 1 goto :configure_failed
"%NSSM%" set %SVC% AppStderr "%cd%\logs\service.err.log"
if errorlevel 1 goto :configure_failed
rem CreationDisposition 4 appends across restarts. Rotate online at 50MB.
"%NSSM%" set %SVC% AppStdoutCreationDisposition 4
if errorlevel 1 goto :configure_failed
"%NSSM%" set %SVC% AppStderrCreationDisposition 4
if errorlevel 1 goto :configure_failed
"%NSSM%" set %SVC% AppRotateFiles 1
if errorlevel 1 goto :configure_failed
"%NSSM%" set %SVC% AppRotateOnline 1
if errorlevel 1 goto :configure_failed
"%NSSM%" set %SVC% AppRotateBytes 52428800
if errorlevel 1 goto :configure_failed
echo.

echo [5/8] Configuring environment ...
rem Egress proxy is optional: set OPENCODE_GO_PROXY_UPSTREAM (e.g. in
rem proxy-local.bat) to route API calls through a local HTTP proxy;
rem leave it unset to connect directly.
set "UPSTREAM=%OPENCODE_GO_PROXY_UPSTREAM%"
set "DESKTOP_IP=%OPENCODE_GO_PROXY_LAN_IP%"
set "LAN_NO_PROXY="
if defined DESKTOP_IP set "LAN_NO_PROXY=,%DESKTOP_IP%"
set "ENV_EXTRA="OPENCODE_GO_API_KEY=%OPENCODE_GO_API_KEY%""
set "ENV_EXTRA=%ENV_EXTRA% "OPENCODE_GO_PROXY_CALLER_TOKEN=%OPENCODE_GO_PROXY_CALLER_TOKEN%""
set "ENV_EXTRA=%ENV_EXTRA% "OPENCODE_GO_PROXY_ALLOW_REMOTE=1""
set "ENV_EXTRA=%ENV_EXTRA% "OPENCODE_GO_PROXY_BIND=0.0.0.0""
set "ENV_EXTRA=%ENV_EXTRA% "OPENCODE_GO_PROXY_PORT=19999""
if defined UPSTREAM (
  set "ENV_EXTRA=%ENV_EXTRA% "HTTP_PROXY=%UPSTREAM%""
  set "ENV_EXTRA=%ENV_EXTRA% "HTTPS_PROXY=%UPSTREAM%""
  set "ENV_EXTRA=%ENV_EXTRA% "http_proxy=%UPSTREAM%""
  set "ENV_EXTRA=%ENV_EXTRA% "https_proxy=%UPSTREAM%""
  set "ENV_EXTRA=%ENV_EXTRA% "NO_PROXY=localhost,127.0.0.1,::1%LAN_NO_PROXY%,192.168.0.0/16,10.0.0.0/8""
  set "ENV_EXTRA=%ENV_EXTRA% "no_proxy=localhost,127.0.0.1,::1%LAN_NO_PROXY%,192.168.0.0/16,10.0.0.0/8""
)
"%NSSM%" set %SVC% AppEnvironmentExtra %ENV_EXTRA%
if errorlevel 1 goto :configure_failed
echo.

echo [6/8] Configuring restart-on-failure ...
"%NSSM%" set %SVC% AppExit Default Restart
if errorlevel 1 goto :configure_failed
"%NSSM%" set %SVC% AppRestartDelay 5000
if errorlevel 1 goto :configure_failed
echo.

echo [7/8] Starting service ...
"%NSSM%" start %SVC%
if errorlevel 1 (
    echo [ERROR] Start failed. Status and log follow.
    "%NSSM%" status %SVC% 2>nul
    if exist "%cd%\logs\service.err.log" (
        echo ----- logs\service.err.log -----
        type "%cd%\logs\service.err.log"
    )
    exit /b 1
)
echo.

echo [8/8] Waiting for health ...
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
    echo [ERROR] Health check never returned 200. Check logs\service.err.log.
    exit /b 1
)
echo.
echo ============================================
echo   Install complete.
echo   Health: http://127.0.0.1:19999/health is 200.
if defined DESKTOP_IP (
    echo   LAN URL for laptops: http://%DESKTOP_IP%:19999/v1
) else (
    echo   LAN URL: set OPENCODE_GO_PROXY_LAN_IP in proxy-local.bat and reinstall.
)
echo   Logs: logs\service.out.log and service.err.log
echo   Keep start-proxy-19999.bat stopped while the service holds port 19999.
echo ============================================
exit /b 0

:configure_failed
echo [ERROR] Failed to configure service. Check that the service exists and you are Administrator.
exit /b 1
