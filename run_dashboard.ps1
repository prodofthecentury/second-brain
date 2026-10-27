<#
.SYNOPSIS
Launches the Second Brain local web dashboard.
#>
$ScriptPath = $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path $ScriptPath

Write-Host "Booting Second Brain Web Dashboard..." -ForegroundColor Cyan
Set-Location $ProjectRoot
uv run --python 3.11 --with streamlit --with plotly --with networkx --with pandas --with ollama streamlit run dashboard/app.py --server.address 127.0.0.1
