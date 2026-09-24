@echo off
rem Expose the local web app through a Cloudflare quick tunnel.
rem Sign-in is required, so WorkOS must be configured first (the dev login is
rem never enabled here):
rem   set WORKOS_API_KEY=sk_...        set WORKOS_CLIENT_ID=client_...
rem   set HUB_SECRET_KEY=<long random> set HUB_BASE_URL=https://<tunnel-host>
rem and add https://<tunnel-host>/auth/callback as a redirect in WorkOS.
setlocal
cd /d "%~dp0.."
set PYTHONPATH=src
set HUB_DEV_LOGIN=

if "%WORKOS_API_KEY%"=="" (
  echo.
  echo  ERROR: set WORKOS_API_KEY / WORKOS_CLIENT_ID / HUB_SECRET_KEY first - see this file.
  echo.
  exit /b 1
)

if not exist "tools\cloudflared.exe" (
  echo cloudflared missing - download from:
  echo https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe
  echo and save as tools\cloudflared.exe
  exit /b 1
)

echo Starting web app on http://localhost:8010 ...
start "icrisat-hub-web" /min ".venv\Scripts\python.exe" -m hub.web.app
timeout /t 4 /nobreak >nul

echo Starting public tunnel - share the printed https URL.
echo Keep this window open; closing it kills the tunnel.
echo.
"tools\cloudflared.exe" tunnel --url http://localhost:8010
