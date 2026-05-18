#!/bin/bash
# Memory API v2 — restart script
set -e

cd /home/ArndtOs/Tools/memory-api-v2

# Kill existing process on port 8766
fuser -k 8766/tcp 2>/dev/null || true
sleep 2

# Start with proper env
export PATH="/home/ArndtOs/Tools/memory-api/venv/bin:$PATH"
exec /home/ArndtOs/Tools/memory-api/venv/bin/python -u main.py
