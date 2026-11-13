
@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM Start Coirei in separate terminal windows: main API, competitor API +
REM worker, and the GTM crawler API + worker.
cd /d "%~dp0"

set "PY=%CD%\.venv\Scripts\python.exe"
set "CRAWLER_DIR=%CD%\engines\company_crawler"
set "CRAWLER_PY=%CRAWLER_DIR%\.venv\Scripts\python.exe"

if not exist "%PY%" (
    echo Main virtual environment not found - creating it ^(.venv^)...
    py -3.12 -m venv .venv 2>nul
    if errorlevel 1 python -m venv .venv
    if not exist "%PY%" (
        echo ERROR: Could not create a virtual environment. Is Python 3.11+ on PATH?
        exit /b 1
    )
)

echo Installing/verifying main API requirements...
"%PY%" -m pip install -q --upgrade pip
"%PY%" -m pip install -q -r requirements.txt
if errorlevel 1 (
    echo ERROR: Failed to install main API requirements.
    exit /b 1
)

if not exist "%CRAWLER_PY%" (
    echo GTM crawler virtual environment not found - creating it...
    pushd "%CRAWLER_DIR%"
    py -3.12 -m venv .venv 2>nul
    if errorlevel 1 python -m venv .venv
    popd
    if not exist "%CRAWLER_PY%" (
        echo ERROR: Could not create the GTM crawler's virtual environment. Is Python 3.11+ on PATH?
        exit /b 1
    )
)

echo Installing/verifying GTM crawler package...
"%CRAWLER_PY%" -m pip install -q --upgrade pip
pushd "%CRAWLER_DIR%"
"%CRAWLER_PY%" -m pip install -q -e .
set "CRAWLER_INSTALL_RC=%errorlevel%"
popd
if not "%CRAWLER_INSTALL_RC%"=="0" (
    echo ERROR: Failed to install the GTM crawler package.
    exit /b 1
)

if not exist ".env" if not defined DATABASE_URL (
    echo ERROR: Create .env with DATABASE_URL and secrets.
    exit /b 1
)

REM Load every KEY=VALUE line of .env as a batch variable (same values the
REM Python processes read via pydantic-settings), skipping comments/blanks.
if exist ".env" (
    for /f "usebackq eol=# tokens=1,* delims==" %%a in (".env") do (
        if not "%%a"=="" set "%%a=%%b"
    )
)

REM Derive the GTM__SECTION__KEY overrides the crawler needs from the plain
REM keys above, so nothing is duplicated between the two services. The crawler
REM runs its own Alembic migration chain and schema, so GTM__DATABASE__URL
REM must be set explicitly in .env to a SEPARATE database from DATABASE_URL
REM (sharing one breaks both migration histories).
if not defined GTM__DATABASE__URL (
    echo ERROR: Set GTM__DATABASE__URL in .env ^(the GTM crawler's own database,
    echo separate from DATABASE_URL^). Example:
    echo   GTM__DATABASE__URL=postgresql+psycopg://user:@127.0.0.1:5432/coirei_intelligence
    exit /b 1
)
echo %GTM__DATABASE__URL%| findstr /i "+asyncpg" >nul
if not errorlevel 1 (
    REM The crawler's migrations and ORM run synchronously (psycopg); asyncpg
    REM only works with SQLAlchemy's async engine and fails deep inside
    REM Alembic ("MissingGreenlet") if it ends up here.
    echo WARNING: GTM__DATABASE__URL used the async 'asyncpg' driver; the GTM
    echo crawler needs the sync 'psycopg' driver. Auto-correcting for this run
    echo - fix it in .env to silence this warning.
    set "GTM__DATABASE__URL=%GTM__DATABASE__URL:+asyncpg=+psycopg%"
)
set "GTM__API__TOKEN=%CRAWLER_SERVICE_TOKEN%"
set "GTM__LLM__API_KEY=%GROQ_API_KEY%"
if not defined GROQ_BASE_URL set "GROQ_BASE_URL=https://api.groq.com/openai/v1"
if not defined GROQ_MODEL set "GROQ_MODEL=openai/gpt-oss-20b"
set "GTM__LLM__BASE_URL=%GROQ_BASE_URL%"
set "GTM__LLM__MODEL=%GROQ_MODEL%"
set "GTM__LLM__SECONDARY_BASE_URL=%OLLAMA_BASE_URL:/api=/v1%"
set "GTM__LLM__SECONDARY_MODEL=%OLLAMA_MODEL%"
set "GTM__LLM__SECONDARY_API_KEY=%OLLAMA_API_KEY%"
set "GTM__SEARCH__API_KEY=%TAVILY_API_KEY%"

if not defined GTM__API__TOKEN (
    echo ERROR: Set CRAWLER_SERVICE_TOKEN in .env ^(shared with GTM__API__TOKEN^).
    exit /b 1
)

echo Checking backend configuration...

"%PY%" -c "from app.core.config import settings; from sqlalchemy.engine import make_url; s=settings(); u=make_url(s.database_url); print(f'Database target: {u.host}:{u.port or 5432}/{u.database} (credentials hidden)'); assert len(s.competitor_internal_token)>=32, 'Set COMPETITOR_INTERNAL_TOKEN in .env'; print('WARNING: Set TAVILY_API_KEY or BRAVE_API_KEY for search' if not (s.tavily_api_key.strip() or s.brave_api_key.strip()) else 'Search provider key configured')"

if errorlevel 1 exit /b 1

echo Checking PostgreSQL and applying migrations (main)...

"%PY%" -m alembic upgrade head

if errorlevel 1 (
    echo.
    echo ERROR: PostgreSQL connection or migration failed.
    echo Use 127.0.0.1 instead of localhost in DATABASE_URL.
    echo Check Windows Services to ensure PostgreSQL is running.
    echo Example: net start postgresql-x64-18
    echo.
    echo If the error above says "Can't locate revision identified by '...'",
    echo the target database already has a migration history that doesn't
    echo belong to this codebase ^(e.g. it was used by a different app or an
    echo older checkout^). Point DATABASE_URL at a fresh, empty database.
    exit /b 1
)

echo Applying migrations (GTM crawler)...

pushd "%CRAWLER_DIR%"
"%CRAWLER_PY%" -m alembic upgrade head
set "CRAWLER_MIGRATE_RC=%errorlevel%"
popd

if not "%CRAWLER_MIGRATE_RC%"=="0" (
    echo.
    echo ERROR: GTM crawler migration failed.
    echo.
    echo If the error above says "Can't locate revision identified by '...'",
    echo the target database already has a migration history that doesn't
    echo belong to this codebase. Point GTM__DATABASE__URL at a fresh, empty
    echo database, separate from DATABASE_URL.
    exit /b 1
)

REM Check port 8000.
"%PY%" -c "import socket; s=socket.create_connection(('127.0.0.1',8000),timeout=1); s.close()" >nul 2>&1

if not errorlevel 1 (
    echo ERROR: Port 8000 is already in use.
    netstat -ano | findstr :8000
    exit /b 1
)

REM Check port 8001.
"%PY%" -c "import socket; s=socket.create_connection(('127.0.0.1',8001),timeout=1); s.close()" >nul 2>&1

if not errorlevel 1 (
    echo ERROR: Port 8001 is already in use.
    netstat -ano | findstr :8001
    exit /b 1
)

REM Check port 8002.
"%PY%" -c "import socket; s=socket.create_connection(('127.0.0.1',8002),timeout=1); s.close()" >nul 2>&1

if not errorlevel 1 (
    echo ERROR: Port 8002 is already in use.
    netstat -ano | findstr :8002
    exit /b 1
)

echo.
echo Starting main API on port 8000...

start "Coirei Main API 8000" /D "%CD%" cmd /k ".venv\Scripts\python.exe -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000"

echo Starting competitor API on port 8001...

start "Coirei Competitor API 8001" /D "%CD%" cmd /k ".venv\Scripts\python.exe -m uvicorn app.competitors.api:app --host 127.0.0.1 --port 8001"

echo Starting competitor worker...

start "Coirei Discovery Worker" /D "%CD%" cmd /k ".venv\Scripts\python.exe -m app.competitors.worker"

echo Starting GTM crawler API on port 8002...

start "Coirei GTM Crawler API 8002" /D "%CRAWLER_DIR%" cmd /k "set GTM__DATABASE__URL=%GTM__DATABASE__URL%&& set GTM__API__TOKEN=%GTM__API__TOKEN%&& set GTM__LLM__API_KEY=%GTM__LLM__API_KEY%&& set GTM__LLM__BASE_URL=%GTM__LLM__BASE_URL%&& set GTM__LLM__MODEL=%GTM__LLM__MODEL%&& set GTM__LLM__SECONDARY_BASE_URL=%GTM__LLM__SECONDARY_BASE_URL%&& set GTM__LLM__SECONDARY_MODEL=%GTM__LLM__SECONDARY_MODEL%&& set GTM__LLM__SECONDARY_API_KEY=%GTM__LLM__SECONDARY_API_KEY%&& set GTM__SEARCH__API_KEY=%GTM__SEARCH__API_KEY%&& .venv\Scripts\python.exe -m gtm_crawler.api"

echo Starting GTM crawler worker...

start "Coirei GTM Crawler Worker" /D "%CRAWLER_DIR%" cmd /k "set GTM__DATABASE__URL=%GTM__DATABASE__URL%&& set GTM__API__TOKEN=%GTM__API__TOKEN%&& set GTM__LLM__API_KEY=%GTM__LLM__API_KEY%&& set GTM__LLM__BASE_URL=%GTM__LLM__BASE_URL%&& set GTM__LLM__MODEL=%GTM__LLM__MODEL%&& set GTM__LLM__SECONDARY_BASE_URL=%GTM__LLM__SECONDARY_BASE_URL%&& set GTM__LLM__SECONDARY_MODEL=%GTM__LLM__SECONDARY_MODEL%&& set GTM__LLM__SECONDARY_API_KEY=%GTM__LLM__SECONDARY_API_KEY%&& set GTM__SEARCH__API_KEY=%GTM__SEARCH__API_KEY%&& .venv\Scripts\python.exe -m gtm_crawler.worker"

echo.
echo ==========================================
echo          COIREI BACKEND STARTED
echo ==========================================
echo.
echo Main Swagger:
echo http://127.0.0.1:8000/docs
echo.
echo Competitor Swagger:
echo http://127.0.0.1:8001/docs
echo.
echo GTM Crawler Swagger:
echo http://127.0.0.1:8002/docs ^(if GTM__API__DOCS=true^)
echo.
echo Discovery worker started.
echo GTM crawler worker started.
echo.
echo LLM: primary=groq (%GTM__LLM__MODEL%), secondary=ollama (%GTM__LLM__SECONDARY_MODEL%)
echo.
echo Five terminal windows have been opened.
echo Close each window or press Ctrl+C in each to stop.
echo ==========================================

exit /b 0
