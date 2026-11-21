<#
.SYNOPSIS
Launches the Second Brain real-time watchdog daemon (forwarding optional flags such as --once, --auto-sync, --sync-interval).
#>
$ScriptPath = $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptPath

Write-Host "Starting Second Brain Watchdog Daemon..." -ForegroundColor Cyan
Set-Location -LiteralPath $ProjectRoot
uv run --locked python scripts/watchdog_daemon.py @args
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
