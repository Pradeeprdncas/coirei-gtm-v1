@echo off
setlocal
title GTM Crawler

rem --- always run from the folder this script lives in ---
cd /d "%~dp0"

rem --- first run: create venv + install the crawler ---
if not exist ".venv\Scripts\gtm-crawl.exe" (
    echo [setup] Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo [error] Could not create venv. Is Python 3.11+ on PATH?
        pause
        exit /b 1
    )
    echo [setup] Installing gtm-crawler...
    ".venv\Scripts\python.exe" -m pip install -e .
    if errorlevel 1 (
        echo [error] Install failed.
        pause
        exit /b 1
    )
    ".venv\Scripts\python.exe" -m playwright install chromium
)

rem --- settings and secrets: KEY=VALUE lines in .env (gitignored) ---
if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo [setup] Created .env from .env.example. Fill it in, then run start.bat again.
    notepad ".env"
    pause
    exit /b 2
)
if exist ".env" (
    for /f "usebackq eol=# tokens=1,* delims==" %%a in (".env") do set "%%a=%%b"
)
if "%GTM__IDENTITY__USER_AGENT_CONTACT%"=="" (
    echo [error] GTM__IDENTITY__USER_AGENT_CONTACT is not set. Add it to .env:
    echo         GTM__IDENTITY__USER_AGENT_CONTACT=you@example.com
    pause
    exit /b 2
)
if "%GTM__SEARCH__API_KEY%"=="" echo [warn] GTM__SEARCH__API_KEY not set. Web search is skipped; site crawl only.

rem --- database ---
echo [run] Starting PostgreSQL...
docker compose up -d --wait
if errorlevel 1 (
    echo [error] Could not start PostgreSQL. Is Docker Desktop running?
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m alembic upgrade head
if errorlevel 1 (
    echo [error] Database migration failed.
    pause
    exit /b 1
)

rem --- no argument: API + worker with Swagger UI; with a website: one crawl here ---
set "URL=%~1"
rem default 8001 (the older backend's start.bat uses 8000); .env may set GTM__API__PORT
if "%GTM__API__PORT%"=="" set "GTM__API__PORT=8001"
if "%URL%"=="" (
    set "GTM__API__DOCS=true"
    start "GTM worker" ".venv\Scripts\gtm-worker.exe"
    start "" cmd /c "timeout /t 4 >nul & start http://127.0.0.1:%GTM__API__PORT%/docs"
    echo.
    echo [run] API at http://127.0.0.1:%GTM__API__PORT%  -  Swagger UI at /docs  ^(POST /v1/crawls starts a crawl^)
    echo [run] Close this window and the "GTM worker" window to stop.
    echo.
    ".venv\Scripts\gtm-api.exe"
    exit /b
)
if "%URL:://=%"=="%URL%" set "URL=https://%URL%"
for /f "tokens=2 delims=/" %%h in ("%URL%") do set "SITE=%%h"

if not exist "data\dossiers" mkdir "data\dossiers"
set "OUT=data\dossiers\%SITE%.json"
set "LOG=data\dossiers\%SITE%.log"

echo.
echo [run] Crawling %URL%
echo [run] Up to 1000 pages; this can take 15 minutes or more. Ctrl+C pauses the job; run again to resume.
echo.

".venv\Scripts\gtm-crawl.exe" "%URL%" > "%OUT%" 2> "%LOG%"
set "CODE=%errorlevel%"

echo.
if "%CODE%"=="0" (
    echo [done] Dossier: %~dp0%OUT%
) else (
    echo [error] gtm-crawl exited with code %CODE%. Last log lines:
    powershell -NoProfile -Command "Get-Content -Tail 5 '%LOG%'"
)
echo        Log:     %~dp0%LOG%
pause
exit /b %CODE%
