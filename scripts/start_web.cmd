@echo off
rem Local web app with the dev email login (no WorkOS needed).
rem Production: set WORKOS_API_KEY, WORKOS_CLIENT_ID, HUB_BASE_URL, HUB_SECRET_KEY instead.
setlocal
cd /d "%~dp0.."
set PYTHONPATH=src
if "%WORKOS_API_KEY%"=="" set HUB_DEV_LOGIN=1
".venv\Scripts\python.exe" -m hub.web.app
