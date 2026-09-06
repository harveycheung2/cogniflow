#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [ -f ".venv/bin/python" ]; then
    PYTHON=".venv/bin/python"
else
    echo "[Agentic OS v2] Creating virtual environment (.venv)..."
    python3 -m venv .venv
    PYTHON=".venv/bin/python"
    echo "[Agentic OS v2] Installing dependencies..."
    $PYTHON -m pip install --upgrade pip
    $PYTHON -m pip install -r requirements.txt
    $PYTHON -m playwright install chromium
fi

if [ ! -f ".env" ] && [ -f ".env.example" ]; then
    cp .env.example .env
    echo "[Agentic OS v2] Created .env from .env.example"
fi

echo "Starting CogniFlow OS on http://127.0.0.1:8765"
if command -v open >/dev/null 2>&1; then
    open "http://127.0.0.1:8765" &
elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "http://127.0.0.1:8765" &
fi

$PYTHON app.py
