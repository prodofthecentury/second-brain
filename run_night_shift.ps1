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

Write-Host "`n[2/2] Second Brain Morning Shift — Thread Synthesis" -ForegroundColor Yellow
uv run --locked python scripts/ollama_editor.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "`nEditor failed." -ForegroundColor Red
    exit $LASTEXITCODE
}

Write-Host "`nNight Shift Complete!" -ForegroundColor Green
