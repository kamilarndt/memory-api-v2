#!/bin/bash
# Memory API v2 - Startup Script
# Runs the PostgreSQL-based memory server with MCP support

cd "$(dirname "$0")"

# Load environment variables
set -a
source .env
set +a

# Check if PostgreSQL is reachable
echo "Checking PostgreSQL connection..."
for i in {1..10}; do
    if PGPASSWORD="$PGMEMORY_DB_PASSWORD" pg_isready -h "$PGMEMORY_DB_HOST" -p "$PGMEMORY_DB_PORT" -U "$PGMEMORY_DB_USER" -d "$PGMEMORY_DB_NAME" 2>/dev/null; then
        echo "✓ PostgreSQL is ready"
        break
    fi
    echo "Waiting for PostgreSQL... ($i/10)"
    sleep 1
done

# Install dependencies if needed
if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
    source .venv/bin/activate
    pip install -r requirements.txt 2>&1 | tail -5
else
    source .venv/bin/activate
fi

# Run the server
exec python main.py
