
#!/usr/bin/env bash
# Start Coirei main API, competitor API + worker, and the GTM crawler API + worker.
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

PY="$ROOT/.venv/bin/python"
CRAWLER_DIR="$ROOT/engines/company_crawler"
CRAWLER_PY="$CRAWLER_DIR/.venv/bin/python"
LOG_DIR="$ROOT/.run-logs"

find_python() {
  for candidate in python3.12 python3.11 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
      command -v "$candidate"
      return 0
    fi
  done
  return 1
}

if [[ ! -x "$PY" ]]; then
  SYS_PY="$(find_python)" || { echo "ERROR: No python3 found on PATH. Install Python 3.11+."; exit 1; }
  echo "Main virtual environment not found — creating it ($ROOT/.venv)..."
  "$SYS_PY" -m venv "$ROOT/.venv"
fi

echo "Installing/verifying main API requirements..."
"$PY" -m pip install -q --upgrade pip
"$PY" -m pip install -q -r "$ROOT/requirements.txt"

if [[ ! -x "$CRAWLER_PY" ]]; then
  SYS_PY="$(find_python)" || { echo "ERROR: No python3 found on PATH. Install Python 3.11+."; exit 1; }
  echo "GTM crawler virtual environment not found — creating it ($CRAWLER_DIR/.venv)..."
  "$SYS_PY" -m venv "$CRAWLER_DIR/.venv"
fi

echo "Installing/verifying GTM crawler package..."
"$CRAWLER_PY" -m pip install -q --upgrade pip
(cd "$CRAWLER_DIR" && "$CRAWLER_PY" -m pip install -q -e .)

# psycopg's binary wheel ships a compiled DLL; on a locked-down Windows box
# (Application Control / WDAC) loading it is blocked outright, and the
# pure-Python fallback needs a system libpq that usually isn't there either.
# pg8000 is pure Python — no native extension — and is a drop-in replacement
# purely via the connection URL's driver name (postgresql+pg8000://...).
NEED_PG8000=0
if ! "$CRAWLER_PY" -c "import psycopg" >/dev/null 2>&1; then
  echo "WARNING: psycopg could not be loaded in the GTM crawler's virtual"
  echo "environment (often a Windows Application Control / WDAC policy"
  echo "blocking its native DLL). Installing the pure-Python pg8000 driver"
  echo "as a fallback..."
  "$CRAWLER_PY" -m pip install -q "pg8000>=1.31,<2"
  NEED_PG8000=1
fi

if [[ ! -f "$ROOT/.env" && -z "${DATABASE_URL:-}" ]]; then
  echo "ERROR: Create a .env file with DATABASE_URL and application secrets."
  exit 1
fi

# Export every key in .env as a real shell variable (pydantic-settings reads the
# file itself for the Python processes below; the GTM crawler only reads
# GTM__SECTION__KEY from the environment, so its values are derived here from
# the plain keys instead of being duplicated in .env).
if [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
fi

if [[ -z "${GTM__DATABASE__URL:-}" ]]; then
  echo "ERROR: Set GTM__DATABASE__URL in .env — the GTM crawler's OWN database."
  echo "It runs its own Alembic migration chain and schema, so it must be a"
  echo "separate database from DATABASE_URL (sharing one breaks both migration"
  echo "histories). Example:"
  echo "  GTM__DATABASE__URL=postgresql+psycopg://user:@127.0.0.1:5432/coirei_intelligence"
  exit 1
fi
if [[ "$GTM__DATABASE__URL" == *"+asyncpg"* ]]; then
  # The crawler's migrations and ORM run synchronously; asyncpg only works
  # with SQLAlchemy's async engine and fails deep inside Alembic
  # ("MissingGreenlet") if it ends up here.
  echo "WARNING: GTM__DATABASE__URL used the async 'asyncpg' driver; the GTM"
  echo "crawler needs a sync driver. Auto-correcting for this run"
  echo "— fix it in .env to silence this warning."
  export GTM__DATABASE__URL="$(sed -E 's#^postgresql(\+[A-Za-z0-9_]+)?://#postgresql+psycopg://#' <<<"$GTM__DATABASE__URL")"
fi
if [[ "$NEED_PG8000" == "1" && "$GTM__DATABASE__URL" != *"+pg8000"* ]]; then
  export GTM__DATABASE__URL="$(sed -E 's#^postgresql(\+[A-Za-z0-9_]+)?://#postgresql+pg8000://#' <<<"$GTM__DATABASE__URL")"
fi
export GTM__API__TOKEN="${CRAWLER_SERVICE_TOKEN:-}"
export GTM__LLM__API_KEY="${GROQ_API_KEY:-}"
export GTM__LLM__BASE_URL="${GROQ_BASE_URL:-https://api.groq.com/openai/v1}"
export GTM__LLM__MODEL="${GROQ_MODEL:-openai/gpt-oss-20b}"
export GTM__LLM__SECONDARY_BASE_URL="${OLLAMA_BASE_URL%/api}/v1"
export GTM__LLM__SECONDARY_MODEL="${OLLAMA_MODEL:-}"
export GTM__LLM__SECONDARY_API_KEY="${OLLAMA_API_KEY:-}"
export GTM__SEARCH__API_KEY="${TAVILY_API_KEY:-}"

if [[ -z "$GTM__API__TOKEN" ]]; then
  echo "ERROR: Set CRAWLER_SERVICE_TOKEN in .env (shared with GTM__API__TOKEN)."
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

for port in 8000 8001 8002; do
  if port_busy "$port"; then
    echo "ERROR: Port $port is already in use."
    echo "Stop the existing server before restarting."

    if command -v lsof >/dev/null 2>&1; then
      lsof -nP -iTCP:"$port" -sTCP:LISTEN || true
    fi

    exit 1
  fi
done

migration_hint() {
  echo ""
  echo "If the error above says \"Can't locate revision identified by '...'\","
  echo "the target database already has a migration history that doesn't"
  echo "belong to this codebase (e.g. it was used by a different app or an"
  echo "older checkout). Point DATABASE_URL / GTM__DATABASE__URL in .env at a"
  echo "fresh, empty database, or ask whoever set up that database how its"
  echo "schema maps to this repo's migrations before running this script again."
}

echo "Applying Alembic migrations (main)..."
"$PY" -m alembic upgrade head || { migration_hint; exit 1; }

echo "Applying Alembic migrations (GTM crawler)..."
(cd "$CRAWLER_DIR" && "$CRAWLER_PY" -m alembic upgrade head) || { migration_hint; exit 1; }

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

echo "Starting GTM crawler API on port 8002..."

(cd "$CRAWLER_DIR" && exec "$CRAWLER_PY" -m gtm_crawler.api) \
  >"$LOG_DIR/crawler-api.log" 2>&1 &

PIDS+=("$!")

echo "Starting GTM crawler worker..."

(cd "$CRAWLER_DIR" && exec "$CRAWLER_PY" -m gtm_crawler.worker) \
  >"$LOG_DIR/crawler-worker.log" 2>&1 &

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

if ! wait_health "http://127.0.0.1:8002/readyz"; then
  echo "ERROR: GTM crawler API failed to start."
  echo "Check $LOG_DIR/crawler-api.log"
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
echo "GTM Crawler API:"
echo "  http://127.0.0.1:8002/docs (if GTM__API__DOCS=true)"
echo ""
echo "Discovery worker: RUNNING"
echo "GTM crawler worker: RUNNING"
echo ""
echo "LLM: primary=groq ($GTM__LLM__MODEL), secondary=ollama (${GTM__LLM__SECONDARY_MODEL:-none})"
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
