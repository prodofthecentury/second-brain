<#
.SYNOPSIS
Chat Interface with Permanent Memory for Ollama

.DESCRIPTION
This script acts as a terminal interface for Ollama. When you ask a question,
it first searches your 2-atoms directory for relevant context, injects it into
the prompt, and then streams Ollama's answer.

.PARAMETER Model
The Ollama model to use. Defaults to "llama3.1" if available.
#>

param (
    [string]$Model = "llama3.1"
)

$ScriptPath = $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path $ScriptPath
$AtomsDir = Join-Path $ProjectRoot "brain\2-atoms"
$RawDir = Join-Path $ProjectRoot "brain\0-raw"

function Search-Vault {
    param([string]$Query)
    # Simple keyword search (a real app would use embeddings, but this is 100% local script)
    $Keywords = $Query -split ' ' | Where-Object { $_.Length -gt 3 }
    if ($Keywords.Count -eq 0) { return "" }

    $MatchedFiles = @()
    $AllAtoms = Get-ChildItem -Path $AtomsDir -Filter "*.md" -File

    foreach ($File in $AllAtoms) {
        $Content = Get-Content $File.FullName -Raw
        $Score = 0
        foreach ($Kw in $Keywords) {
            if ($Content -match "(?i)$Kw") { $Score++ }
        }
        if ($Score -gt 0) {
            $MatchedFiles += [PSCustomObject]@{ File = $File; Score = $Score; Content = $Content }
        }
    }

    $TopMatches = $MatchedFiles | Sort-Object Score -Descending | Select-Object -First 3
    
    if ($TopMatches.Count -gt 0) {
        $Context = "Here is relevant information from your memory vault:`n`n"
        foreach ($Match in $TopMatches) {
            $Context += "--- Source: $($Match.File.Name) ---`n$($Match.Content)`n`n"
        }
        return $Context
    }
    return ""
}

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Ollama Memory Interface" -ForegroundColor Cyan
Write-Host "  Type 'exit' to quit." -ForegroundColor DarkGray
Write-Host "========================================" -ForegroundColor Cyan

while ($true) {
    $UserQuery = Read-Host "`n> You"
    if ($UserQuery -match "^(exit|quit)$") { break }
    if ([string]::IsNullOrWhiteSpace($UserQuery)) { continue }

    Write-Host "[Searching memory...]" -ForegroundColor DarkGray
    $MemoryContext = Search-Vault -Query $UserQuery

    if ($MemoryContext) {
        Write-Host "[Found relevant memories]" -ForegroundColor DarkGreen
    }

    $Prompt = @"
You are a helpful assistant. You have access to the user's Second Brain memory vault.
If the context below contains relevant information, use it to answer the question.
If the context contradicts itself, point it out.
If the context doesn't have the answer, just answer normally.

$MemoryContext

Question: $UserQuery
"@

    # Prepare for streaming output
    $Body = @{
        model = $Model
        prompt = $Prompt
        stream = $true
    } | ConvertTo-Json

    Write-Host "`n> Ollama" -ForegroundColor Blue

    try {
        # Using Invoke-WebRequest for streaming JSON responses
        $Response = Invoke-WebRequest -Uri "http://localhost:11434/api/generate" -Method Post -Body $Body -ContentType "application/json" -UseBasicParsing -ErrorAction Stop
        
        $FullResponse = ""
        # The stream comes back as concatenated JSON objects. We need to split and parse them.
        $Lines = $Response.Content -split "`n" | Where-Object { $_ -ne "" }
        foreach ($Line in $Lines) {
            $Chunk = $Line | ConvertFrom-Json
            Write-Host -NoNewline $Chunk.response
            $FullResponse += $Chunk.response
        }
        Write-Host "" # Newline at the end
    } catch {
        Write-Host "X Failed to connect to Ollama. Is the server running?" -ForegroundColor Red
    }
}
