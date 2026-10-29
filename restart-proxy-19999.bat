@echo off
setlocal EnableDelayedExpansion
REM One-shot restart for the 19999 proxy.
REM Stops EVERY listener on 19999 (uv parent trees included), waits for the
REM port to free, then starts one fresh instance (own window, logs tee to
REM logs\proxy-19999.log) and gates on /health.
REM Session state lives in the client, not here: a request sent while the
REM proxy is down fails once; resend after health turns 200.

cd /d "%~dp0"

echo [restart] step 1/3: stopping all 19999 listeners ...
for /f "tokens=2 delims==" %%p in ('wmic process where "name='uv.exe' and commandline like '%%opencode-go-proxy%%'" get processid /value 2^>nul ^| find "="') do (
  taskkill /F /T /PID %%p >nul 2>&1
)
rem Kill only listeners that are this repo's own venv proxy; an unrelated
rem process on the port is reported and left alone.
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":19999" ^| findstr LISTENING') do (
  tasklist /fi "PID eq %%p" /fo csv /nh 2>nul | findstr /i "opencode-go-proxy" >nul 2>&1 && (
    taskkill /F /T /PID %%p >nul 2>&1
  ) || (
    echo [restart] port 19999 is held by a foreign process ^(PID %%p^); not killing it. >&2
  )
)

set FREED=0
for /L %%i in (1,1,10) do (
  netstat -ano | findstr ":19999" | findstr LISTENING >nul 2>&1
  if errorlevel 1 (
    set FREED=1
    goto :freed
  )
  timeout /t 1 /nobreak >nul
)
:freed
if "%FREED%"=="0" (
  echo [restart] port 19999 still occupied after 10s; aborting. >&2
  exit /b 1
)
echo [restart] step 1/3 ok: port free.

echo [restart] step 2/3: starting fresh instance ...
start "proxy-19999" cmd /c "%~dp0start-proxy-19999.bat"

echo [restart] step 3/3: waiting for /health ...
set OK=0
for /L %%i in (1,1,30) do (
  >nul 2>&1 curl -s -o nul --max-time 2 http://127.0.0.1:19999/health
  if !ERRORLEVEL! EQU 0 (
    set OK=1
    goto :ready
  )
  timeout /t 1 /nobreak >nul
)
:ready
if "%OK%"=="0" (
  echo [restart] FAILED: /health never returned 200. Check the proxy window and logs\proxy-19999.log. >&2
  exit /b 1
)
echo [restart] done: http://127.0.0.1:19999/health is 200.
endlocal
