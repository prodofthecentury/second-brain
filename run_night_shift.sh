#!/usr/bin/env bash
# Orchestrates the Second Brain Python night shift (Refinery + Thread Synthesis) using uv.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "========================================"
echo "Starting Second Brain Night & Morning Shifts"
echo "========================================"

if ! command -v uv >/dev/null 2>&1; then
    echo "Error: 'uv' is not installed or not in PATH." >&2
    echo "Run: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    exit 1
fi

echo ""
echo "[1/2] Second Brain Night Shift — Refinery"
uv run --locked python scripts/ollama_refinery.py

echo ""
echo "[2/4] Second Brain Morning Shift — Thread Synthesis"
uv run --locked python scripts/ollama_editor.py

echo ""
echo "[3/4] Second Brain Hippocampal Replay — Counterfactual Briefing"
uv run --locked python scripts/episodic_replay.py || true

echo ""
echo "[4/4] Second Brain Continuous Distillation — Draft Principles"
uv run --locked python scripts/cognitive_distiller.py distill || true

echo ""
echo "Night Shift Complete!"
