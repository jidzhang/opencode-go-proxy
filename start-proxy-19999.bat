@echo off
setlocal
REM Manual run: serves on 0.0.0.0:19999 for LAN share.
REM Machine-local values (LAN IP, upstream proxy) come from proxy-local.bat
REM (gitignored) or the OPENCODE_GO_PROXY_LAN_IP / OPENCODE_GO_PROXY_UPSTREAM
REM env vars. Egress goes through the upstream only when one is set;
REM otherwise the proxy connects to the API directly.
REM NO_PROXY below applies to this proxy process only. Codex runs in its own shell
REM and needs its own NO_PROXY if a system proxy is set.

if exist "%~dp0proxy-local.bat" call "%~dp0proxy-local.bat"

set PROXY_PORT=19999
set "DESKTOP_IP=%OPENCODE_GO_PROXY_LAN_IP%"
set "UPSTREAM_PROXY=%OPENCODE_GO_PROXY_UPSTREAM%"

set "LAN_NO_PROXY="
if defined DESKTOP_IP set "LAN_NO_PROXY=,%DESKTOP_IP%"
set "NO_PROXY=localhost,127.0.0.1,::1%LAN_NO_PROXY%,192.168.0.0/16,10.0.0.0/8"
if defined UPSTREAM_PROXY (
  set "HTTP_PROXY=%UPSTREAM_PROXY%"
  set "HTTPS_PROXY=%UPSTREAM_PROXY%"
  set "http_proxy=%UPSTREAM_PROXY%"
  set "https_proxy=%UPSTREAM_PROXY%"
  set "no_proxy=%NO_PROXY%"
)

set OPENCODE_GO_PROXY_ALLOW_REMOTE=1
set OPENCODE_GO_PROXY_BIND=0.0.0.0
set OPENCODE_GO_PROXY_PORT=%PROXY_PORT%
REM ---- Log verbosity switch: warn | info (current) | debug ----------------
REM warn  = quiet: only retries/failures/degradation reach logs\proxy-19999.log
REM info  = + lifecycle events (server start/stop, credential source, fallbacks)
REM debug = + full per-request telemetry (received/converted, upstream start/done)
REM Debugging period runs on info; flip to warn when it wraps up.
set "LOG_LEVEL=info"
set "OPENCODE_GO_PROXY_LOG_LEVEL=%LOG_LEVEL%"
REM Request-body dump is OFF by default (each body is ~1MB; a day fills GBs).
REM To bisect a gateway 400 offline, uncomment the next line (no credentials in
REM dump - auth travels in headers). Remember to delete logs\request-dump after.
REM set OPENCODE_GO_PROXY_DUMP_DIR=%~dp0logs\request-dump

if not defined OPENCODE_GO_API_KEY (
  echo [input] OPENCODE_GO_API_KEY is not set.
  set /p OPENCODE_GO_API_KEY=Enter OPENCODE_GO_API_KEY:
)
if not defined OPENCODE_GO_PROXY_CALLER_TOKEN (
  echo [input] OPENCODE_GO_PROXY_CALLER_TOKEN is not set - needs min 32 chars.
  echo Generate one with: openssl rand -hex 32
  set /p OPENCODE_GO_PROXY_CALLER_TOKEN=Enter CALLER_TOKEN:
)

echo.
echo [proxy] bind=0.0.0.0 port=%PROXY_PORT%
if defined UPSTREAM_PROXY echo [proxy] upstream via %UPSTREAM_PROXY%, NO_PROXY=%NO_PROXY%
if defined DESKTOP_IP (
  echo [proxy] LAN URL for laptops: http://%DESKTOP_IP%:%PROXY_PORT%/v1
) else (
  echo [proxy] LAN URL: set OPENCODE_GO_PROXY_LAN_IP in proxy-local.bat to advertise one.
)
echo [proxy] laptops must set same CALLER_TOKEN as env OPENCODE_GO_PROXY_CALLER_TOKEN
echo.

cd /d "%~dp0"
if not exist "%~dp0logs" mkdir "%~dp0logs" 2>nul
REM --frozen keeps uv from rewriting uv.lock on every start.
REM stderr trace stream appends to logs\proxy-19999.log (machine-local, gitignored).
uv run --frozen opencode-go-proxy --bind 0.0.0.0 --port %PROXY_PORT% >> "%~dp0logs\proxy-19999.log" 2>&1
set EXITCODE=%ERRORLEVEL%
echo.
echo [proxy] exited with code %EXITCODE%
pause
endlocal
