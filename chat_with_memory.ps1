<#
.SYNOPSIS
Canonical Grounded Memory Chat Wrapper

.DESCRIPTION
Delegates interactive chat strictly to chat_with_memory.py under the locked
Python environment so recall approval gates and out-of-model answer_policy.py
validation are never bypassed.
#>
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Push-Location $ProjectRoot
try {
    uv run --locked python chat_with_memory.py @args
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
