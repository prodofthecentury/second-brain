#!/usr/bin/env bash
# Reasoning benchmark wrapper. LF endings. Forwards every argument to the harness.
set -euo pipefail
cd "$(dirname "$0")"
exec uv run python -m scripts.reason_bench "$@"
