# Reasoning benchmark wrapper for Windows PowerShell 5.
# No PowerShell 7-only syntax. Line endings are CRLF.
param(
    [string]$Tier = "pr",
    [string]$Models = "hermes3:3b"
)
$ErrorActionPreference = "Stop"
Set-Location -Path (Split-Path -Parent $MyInvocation.MyCommand.Path)
& uv run python -m scripts.reason_bench run --tier $Tier --models $Models
exit $LASTEXITCODE
