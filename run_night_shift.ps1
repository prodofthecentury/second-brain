<#
.SYNOPSIS
Orchestrates the Second Brain Python night shift using uv.

.DESCRIPTION
This script runs the Python-based Refinery and Editor playbooks in sequence,
leveraging local Ollama models and ChromaDB.
#>

$ScriptPath = $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptPath

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "Starting Second Brain Night & Morning Shifts" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

# Ensure uv is installed
if (!(Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "Error: 'uv' is not installed or not in PATH." -ForegroundColor Red
    Write-Host "Run: powershell -ExecutionPolicy ByPass -c `"irm https://astral.sh/uv/install.ps1 | iex`""
    exit 1
}

Write-Host "`n[1/2] Second Brain Night Shift — Refinery" -ForegroundColor Yellow
Set-Location -LiteralPath $ProjectRoot
uv run --locked python scripts/ollama_refinery.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "`nRefinery failed; stopping night shift." -ForegroundColor Red
    exit $LASTEXITCODE
}

Write-Host "`n[2/3] Second Brain Morning Shift — Thread & Briefing Synthesis" -ForegroundColor Yellow
uv run --locked python scripts/ollama_editor.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "`nEditor failed." -ForegroundColor Red
    exit $LASTEXITCODE
}

if (Test-Path -LiteralPath (Join-Path $ProjectRoot "brain\.chroma\index-manifest.json")) {
    Write-Host "`n[3/5] Refreshing Chroma Dense Vector Index..." -ForegroundColor Yellow
    uv run --locked python scripts/rebuild_vectors.py
    if ($LASTEXITCODE -ne 0) {
        Write-Host "`nDense vector refresh deferred (lexical FTS5 remains active)." -ForegroundColor DarkYellow
    }
}

Write-Host "`n[4/5] Second Brain Hippocampal Replay — Counterfactual Briefing" -ForegroundColor Yellow
uv run --locked python scripts/episodic_replay.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "`nEpisodic replay completed with warnings." -ForegroundColor DarkYellow
}

Write-Host "`n[5/5] Second Brain Continuous Distillation — Draft Principles" -ForegroundColor Yellow
uv run --locked python scripts/cognitive_distiller.py distill
if ($LASTEXITCODE -ne 0) {
    Write-Host "`nDraft distillation completed with warnings." -ForegroundColor DarkYellow
}

Write-Host "`nNight Shift Complete!" -ForegroundColor Green
