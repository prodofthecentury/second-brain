<#
.SYNOPSIS
Launches the Second Brain local web dashboard.
#>
$ScriptPath = $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path $ScriptPath

Write-Host "Booting Second Brain Web Dashboard..." -ForegroundColor Cyan
Set-Location $ProjectRoot
uv sync --locked --extra dashboard
if ($LASTEXITCODE -ne 0) {
    Write-Host "Dashboard dependencies could not be installed from uv.lock." -ForegroundColor Red
    exit $LASTEXITCODE
}
uv run --locked python scripts/dashboard_auth.py ensure-configured
if ($LASTEXITCODE -ne 0) {
    Write-Host "Dashboard remains locked because password setup did not complete." -ForegroundColor Red
    exit $LASTEXITCODE
}
uv run --locked streamlit run dashboard/app.py --server.address 127.0.0.1
