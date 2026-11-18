#!/usr/bin/env bash
# Launches the Second Brain local web dashboard (strictly bound to 127.0.0.1).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if ! command -v uv >/dev/null 2>&1; then
    echo "Error: 'uv' is not installed or not in PATH." >&2
    echo "Run: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    exit 1
fi

echo "Booting Second Brain Web Dashboard..."
uv sync --locked
uv run --locked python scripts/dashboard_auth.py ensure-configured
exec uv run --locked streamlit run dashboard/app.py \
    --server.address 127.0.0.1 \
    --server.port 8501 \
    --server.enableCORS true \
    --server.enableXsrfProtection true \
    --server.xsrfCookieSameSite strict \
    --server.allowedHosts 127.0.0.1 \
    --server.corsAllowedOrigins http://127.0.0.1:8501 \
    --browser.serverAddress 127.0.0.1 \
    --browser.gatherUsageStats false
