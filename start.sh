
#!/usr/bin/env bash
# Start Coirei main API, competitor API, and discovery worker.
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

PY="$ROOT/.venv/bin/python"
LOG_DIR="$ROOT/.run-logs"

if [[ ! -x "$PY" ]]; then
  echo "ERROR: Virtual environment not found: $PY"
  echo "Create it with:"
  echo "python3 -m venv .venv"
  echo ".venv/bin/pip install -r requirements.txt"
  exit 1
fi

if [[ ! -f "$ROOT/.env" && -z "${DATABASE_URL:-}" ]]; then
  echo "ERROR: Create a .env file with DATABASE_URL and application secrets."
  exit 1
fi

# Pydantic Settings reads .env from this directory.
"$PY" - <<'PY'
from app.core.config import settings
from sqlalchemy.engine import make_url

s = settings()
u = make_url(s.database_url)

print(
    f"Database target: "
    f"{u.host}:{u.port or 5432}/{u.database} "
    f"(credentials hidden)"
)

if len(s.competitor_internal_token) < 32:
    raise SystemExit(
        "ERROR: Set COMPETITOR_INTERNAL_TOKEN "
        "(32+ characters) in .env"
    )

if not (
    s.tavily_api_key.strip()
    or s.brave_api_key.strip()
):
    print(
        "WARNING: Neither Tavily nor Brave is configured; "
        "competitor searches will fail."
    )
PY

check_db() {
  "$PY" - <<'PY'
import asyncio
import asyncpg
from app.core.config import settings

async def main():
    url = settings().database_url.replace(
        "postgresql+asyncpg://",
        "postgresql://",
        1,
    )

    conn = await asyncpg.connect(
        dsn=url,
        timeout=5,
    )

    try:
        await conn.execute("SELECT 1")
    finally:
        await conn.close()

try:
    asyncio.run(main())
except Exception as exc:
    print(
        f"PostgreSQL check failed: "
        f"{type(exc).__name__}: {exc}"
    )
    raise SystemExit(1)
PY
}

if ! check_db; then

  # Auto-start Homebrew PostgreSQL 18 on macOS.
  if [[ "$(uname -s)" == "Darwin" ]] \
     && command -v brew >/dev/null 2>&1 \
     && brew list --versions postgresql@18 >/dev/null 2>&1; then

    echo "Attempting to start Homebrew PostgreSQL 18..."
    brew services start postgresql@18 || true

    for _ in {1..15}; do
      if check_db >/dev/null 2>&1; then
        break
      fi
      sleep 1
    done
  fi
fi

if ! check_db; then
  echo "ERROR: Database unavailable."
  echo "Check that PostgreSQL accepts connections"
  echo "at 127.0.0.1:5432."
  echo ""
  echo "macOS diagnostics:"
  echo "  brew services list"
  echo "  pg_isready -h 127.0.0.1 -p 5432"
  echo ""
  echo "Check PostgreSQL port, listen_addresses,"
  echo "database name, username and password."
  exit 1
fi

# Refuse to overwrite an already running server.
port_busy() {
  "$PY" - "$1" <<'PY'
import socket
import sys

s = socket.socket()
s.settimeout(0.5)

try:
    busy = s.connect_ex(
        ("127.0.0.1", int(sys.argv[1]))
    ) == 0
finally:
    s.close()

raise SystemExit(0 if busy else 1)
PY
}

for port in 8000 8001; do
  if port_busy "$port"; then
    echo "ERROR: Port $port is already in use."
    echo "Stop the existing server before restarting."

    if command -v lsof >/dev/null 2>&1; then
      lsof -nP -iTCP:"$port" -sTCP:LISTEN || true
    fi

    exit 1
  fi
done

echo "Applying Alembic migrations..."
"$PY" -m alembic upgrade head

mkdir -p "$LOG_DIR"
export PYTHONUNBUFFERED=1

PIDS=()

cleanup() {
  trap - EXIT INT TERM

  echo ""
  echo "Stopping Coirei services..."

  for pid in "${PIDS[@]}"; do
    kill -TERM "$pid" 2>/dev/null || true
  done

  for pid in "${PIDS[@]}"; do
    wait "$pid" 2>/dev/null || true
  done
}

trap cleanup EXIT INT TERM

echo "Starting main backend on port 8000..."

"$PY" -m uvicorn app.main:app \
  --reload \
  --host 127.0.0.1 \
  --port 8000 \
  >"$LOG_DIR/main-api.log" 2>&1 &

PIDS+=("$!")

echo "Starting competitor API on port 8001..."

"$PY" -m uvicorn app.competitors.api:app \
  --host 127.0.0.1 \
  --port 8001 \
  >"$LOG_DIR/competitor-api.log" 2>&1 &

PIDS+=("$!")

echo "Starting competitor worker..."

"$PY" -m app.competitors.worker \
  >"$LOG_DIR/competitor-worker.log" 2>&1 &

PIDS+=("$!")

wait_health() {
  local url="$1"

  for _ in {1..20}; do
    if "$PY" - "$url" <<'PY' >/dev/null 2>&1
import sys
import urllib.request

with urllib.request.urlopen(
    sys.argv[1], timeout=1
) as response:
    assert response.status == 200
PY
    then
      return 0
    fi

    sleep 1
  done

  return 1
}

if ! wait_health "http://127.0.0.1:8000/health"; then
  echo "ERROR: Main API failed to start."
  echo "Check $LOG_DIR/main-api.log"
  exit 1
fi

if ! wait_health "http://127.0.0.1:8001/health"; then
  echo "ERROR: Competitor API failed to start."
  echo "Check $LOG_DIR/competitor-api.log"
  exit 1
fi

echo ""
echo "=========================================="
echo "          COIREI BACKEND RUNNING"
echo "=========================================="
echo ""
echo "Main API:"
echo "  http://127.0.0.1:8000/docs"
echo ""
echo "Competitor API:"
echo "  http://127.0.0.1:8001/docs"
echo ""
echo "Discovery worker: RUNNING"
echo ""
echo "Logs: $LOG_DIR/"
echo ""
echo "Press Ctrl+C to stop all services."
echo "=========================================="

while :; do
  sleep 2

  for pid in "${PIDS[@]}"; do
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "ERROR: A Coirei service stopped unexpectedly."
      echo "Check the logs in $LOG_DIR/"
      exit 1
    fi
  done
done
