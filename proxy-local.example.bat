@echo off
rem Template for machine-local values used by the proxy/service .bat scripts.
rem Copy this file to proxy-local.bat (same directory) and edit the copy:
rem     copy proxy-local.example.bat proxy-local.bat
rem proxy-local.bat is gitignored; this template stays tracked.
rem Every value is optional - leave a line empty to keep the default.
rem
rem   OPENCODE_GO_PROXY_BIND
rem       Interface the server listens on.
rem       Default (empty): 127.0.0.1 - this machine only.
rem       Set to 0.0.0.0 to share the proxy with other devices on the LAN;
rem       remote mode and caller-token auth are then enabled automatically,
rem       and every LAN client must present the caller token.
rem   OPENCODE_GO_PROXY_LAN_IP
rem       This machine's LAN IP, advertised in the startup banner only
rem       (the LAN URL shown for laptop clients). Optional; ignored when the
rem       bind is loopback.
rem   OPENCODE_GO_PROXY_UPSTREAM
rem       HTTP proxy used for egress to the API, e.g. http://127.0.0.1:7890.
rem       Empty (default): connect to the API directly.

set "OPENCODE_GO_PROXY_BIND="
set "OPENCODE_GO_PROXY_LAN_IP="
set "OPENCODE_GO_PROXY_UPSTREAM="
