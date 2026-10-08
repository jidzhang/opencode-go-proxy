@echo off
setlocal
REM opencode-go-proxy LAN share: desktop 192.168.124.99:19999
REM Manual run only. Upstream (opencode.ai) goes via 127.0.0.1:9674.
REM NO_PROXY below applies to this proxy process only. Codex runs in its own shell
REM and needs its own NO_PROXY if a system proxy is set.

set PROXY_PORT=19999
set DESKTOP_IP=192.168.124.99
set UPSTREAM_PROXY=http://127.0.0.1:9674

set HTTP_PROXY=%UPSTREAM_PROXY%
set HTTPS_PROXY=%UPSTREAM_PROXY%
set http_proxy=%UPSTREAM_PROXY%
set https_proxy=%UPSTREAM_PROXY%
set NO_PROXY=localhost,127.0.0.1,::1,%DESKTOP_IP%,192.168.0.0/16,10.0.0.0/8
set no_proxy=%NO_PROXY%

set OPENCODE_GO_PROXY_ALLOW_REMOTE=1
set OPENCODE_GO_PROXY_BIND=0.0.0.0
set OPENCODE_GO_PROXY_PORT=%PROXY_PORT%

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
echo [proxy] upstream via %UPSTREAM_PROXY% (NO_PROXY=%NO_PROXY%)
echo [proxy] LAN URL for laptops: http://%DESKTOP_IP%:%PROXY_PORT%/v1
echo [proxy] laptops must set same CALLER_TOKEN as env OPENCODE_GO_PROXY_CALLER_TOKEN
echo.

cd /d "%~dp0"
REM --frozen keeps uv from rewriting uv.lock on every start.
uv run --frozen opencode-go-proxy --bind 0.0.0.0 --port %PROXY_PORT%
set EXITCODE=%ERRORLEVEL%
echo.
echo [proxy] exited with code %EXITCODE%
pause
endlocal
