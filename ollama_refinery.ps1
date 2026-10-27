<#
.SYNOPSIS
The Night Shift Engine - Powered by Ollama

.DESCRIPTION
This script reads all markdown files in the 0-raw directory and uses your local
Ollama model to process them into atomic notes based on the house rules.
It then saves the new atoms to 2-atoms and archives the raw capture.

.PARAMETER Model
The Ollama model to use. Defaults to "llama3.1" if available.
#>

param (
    [string]$Model = "llama3.1"
)

$ScriptPath = $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path $ScriptPath
$BrainDir = Join-Path $ProjectRoot "brain"
$RawDir = Join-Path $BrainDir "0-raw"
$ArchiveDir = Join-Path $RawDir "archive\$(Get-Date -Format 'yyyy-MM')"
$AtomsDir = Join-Path $BrainDir "2-atoms"
$HouseRules = Get-Content (Join-Path $BrainDir "house-rules.md") -Raw

if (!(Test-Path $ArchiveDir)) {
    New-Item -ItemType Directory -Path $ArchiveDir | Out-Null
}

$RawFiles = Get-ChildItem -Path $RawDir -Filter "*.md" -File | Where-Object { $_.Name -ne "reading-list.md" }

if ($RawFiles.Count -eq 0) {
    Write-Host "No raw captures to process."
    exit
}

Write-Host "Waking up Ollama ($Model) to process $($RawFiles.Count) captures..." -ForegroundColor Cyan

foreach ($File in $RawFiles) {
    Write-Host "`nProcessing: $($File.Name)"
    $Content = Get-Content $File.FullName -Raw

    # Build the prompt
    $Prompt = @"
$HouseRules

YOUR TASK:
Read the following raw capture. Identify the SINGLE most important, distinct idea or claim.
Extract it into exactly ONE atomic note using the exact Markdown template provided in the rules.
Output ONLY the markdown block. No introduction, no summary.
Use today's date: $(Get-Date -Format 'yyyy-MM-dd')
Use the source filename: $($File.Name)

RAW CAPTURE:
$Content
"@

    # Call Ollama API via PowerShell to avoid CLI formatting issues
    $Body = @{
        model = $Model
        prompt = $Prompt
        stream = $false
    } | ConvertTo-Json

    try {
        $Response = Invoke-RestMethod -Uri "http://localhost:11434/api/generate" -Method Post -Body $Body -ContentType "application/json" -ErrorAction Stop
        $OllamaOutput = $Response.response

        # Extract just the markdown if the model wrapped it in codeblocks
        if ($OllamaOutput -match '(?s)```[a-z]*\n(.*?)```') {
            $AtomContent = $Matches[1].Trim()
        } else {
            $AtomContent = $OllamaOutput.Trim()
        }

        # Try to parse an ID from the frontmatter to use as the filename
        $Filename = "$((Get-Date).ToString('yyyy-MMdd-HHmmss'))-atom.md"
        if ($AtomContent -match '(?m)^id:\s*(.+)$') {
            # Make URL safe
            $Slug = $Matches[1] -replace '[^a-zA-Z0-9-]', '-' -replace '-+', '-'
            $Filename = "$Slug.md"
        }

        $OutPath = Join-Path $AtomsDir $Filename
        Set-Content -Path $OutPath -Value $AtomContent -Encoding UTF8
        Write-Host "✓ Created atom: $Filename" -ForegroundColor Green

        # Move to archive
        Move-Item -Path $File.FullName -Destination (Join-Path $ArchiveDir $File.Name) -Force
        Write-Host "  Archived raw capture."
        
    } catch {
        Write-Host "X Failed to process with Ollama. Is the server running?" -ForegroundColor Red
        Write-Host $_.Exception.Message -ForegroundColor Red
    }
}

Write-Host "`nNight Shift Complete." -ForegroundColor Cyan
