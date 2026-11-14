<#
.SYNOPSIS
Canonical Night Shift Refinery Wrapper

.DESCRIPTION
Delegates raw capture processing strictly to scripts/ollama_refinery.py under
the locked Python environment so provenance markers, normalize_memory_document(),
and writer.lock invariants are never bypassed.
#>
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Push-Location $ProjectRoot
try {
    uv run --locked python scripts/ollama_refinery.py @args
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
