
@echo off
setlocal EnableExtensions

REM Start Coirei in three separate terminal windows.
cd /d "%~dp0"

set "PY=%CD%\.venv\Scripts\python.exe"

if not exist "%PY%" (
    echo ERROR: Virtual environment not found.
    echo Create it with: py -3.11 -m venv .venv
    echo Install requirements with:
    echo .venv\Scripts\python.exe -m pip install -r requirements.txt
    exit /b 1
)

if not exist ".env" if not defined DATABASE_URL (
    echo ERROR: Create .env with DATABASE_URL and secrets.
    exit /b 1
)

echo Checking backend configuration...

"%PY%" -c "from app.core.config import settings; from sqlalchemy.engine import make_url; s=settings(); u=make_url(s.database_url); print(f'Database target: {u.host}:{u.port or 5432}/{u.database} (credentials hidden)'); assert len(s.competitor_internal_token)>=32, 'Set COMPETITOR_INTERNAL_TOKEN in .env'; print('WARNING: Set TAVILY_API_KEY or BRAVE_API_KEY for search' if not (s.tavily_api_key.strip() or s.brave_api_key.strip()) else 'Search provider key configured')"

if errorlevel 1 exit /b 1

echo Checking PostgreSQL and applying migrations...

"%PY%" -m alembic upgrade head

if errorlevel 1 (
    echo.
    echo ERROR: PostgreSQL connection or migration failed.
    echo Use 127.0.0.1 instead of localhost in DATABASE_URL.
    echo Check Windows Services to ensure PostgreSQL is running.
    echo Example: net start postgresql-x64-18
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

echo.
echo Starting main API on port 8000...

start "Coirei Main API 8000" /D "%CD%" cmd /k ".venv\Scripts\python.exe -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000"

echo Starting competitor API on port 8001...

start "Coirei Competitor API 8001" /D "%CD%" cmd /k ".venv\Scripts\python.exe -m uvicorn app.competitors.api:app --host 127.0.0.1 --port 8001"

echo Starting competitor worker...

start "Coirei Discovery Worker" /D "%CD%" cmd /k ".venv\Scripts\python.exe -m app.competitors.worker"

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
echo Discovery worker started.
echo.
echo Three terminal windows have been opened.
echo Close each window or press Ctrl+C in each to stop.
echo ==========================================

exit /b 0
