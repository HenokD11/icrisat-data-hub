@echo off
rem Start the upload web app + a public Cloudflare quick tunnel.
rem Requires: set HUB_UPLOAD_TOKEN=<your-secret> before running (or edit below).
rem Share the printed https://<random>.trycloudflare.com/?token=<your-secret> link.
setlocal
cd /d "%~dp0.."
set PYTHONPATH=src

if "%HUB_UPLOAD_TOKEN%"=="" (
  echo.
  echo  ERROR: set an upload token first, e.g.:
  echo     set HUB_UPLOAD_TOKEN=my-team-secret-2026
  echo     scripts\start_public_upload.cmd
  echo.
  exit /b 1
)

if not exist "tools\cloudflared.exe" (
  echo cloudflared missing - download from:
  echo https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe
  echo and save as tools\cloudflared.exe
  exit /b 1
)

echo Starting upload web app on http://localhost:8010 ...
start "icrisat-hub-web" /min ".venv\Scripts\python.exe" -m hub.web.app
timeout /t 4 /nobreak >nul

echo Starting public tunnel - your shareable link appears below
echo (append ?token=%HUB_UPLOAD_TOKEN% to it when sharing).
echo Keep this window open; closing it kills the tunnel.
echo.
"tools\cloudflared.exe" tunnel --url http://localhost:8010
