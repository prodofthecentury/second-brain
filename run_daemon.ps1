<#
.SYNOPSIS
Launches the Second Brain real-time watchdog daemon.
#>
$ScriptPath = $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path $ScriptPath

Write-Host "Starting Second Brain Watchdog Daemon..." -ForegroundColor Cyan
Set-Location $ProjectRoot
uv run --locked --extra watchdog python scripts/watchdog_daemon.py
