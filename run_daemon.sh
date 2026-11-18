#!/usr/bin/env bash
# Launches the Second Brain real-time watchdog daemon.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if ! command -v uv >/dev/null 2>&1; then
    echo "Error: 'uv' is not installed or not in PATH." >&2
    echo "Run: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    exit 1
fi

echo "Starting Second Brain Watchdog Daemon..."
exec uv run --locked python scripts/watchdog_daemon.py
