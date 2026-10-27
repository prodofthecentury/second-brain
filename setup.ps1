<#
.SYNOPSIS
One-time setup script for the Second Brain system.

.DESCRIPTION
This script verifies the directory structure, reports the current Git boundary,
and provides instructions for setting up Windows Task Scheduler.
#>

$ScriptPath = $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path $ScriptPath
$BrainDir = Join-Path $ProjectRoot "brain"

Write-Host "Setting up Second Brain..." -ForegroundColor Cyan

# 1. Verify Directories
$RequiredDirs = @(
    "0-raw",
    "0-raw\archive",
    "1-desk",
    "2-atoms",
    "2-atoms\.history",
    "3-threads",
    "sources",
    "briefings",
    ".sidecar",
    ".chroma",
    "playbooks",
    "playbooks\templates",
    ".obsidian"
)

Write-Host "Verifying directory structure..."
foreach ($Dir in $RequiredDirs) {
    $FullPath = Join-Path $BrainDir $Dir
    if (!(Test-Path $FullPath)) {
        Write-Host "  Creating $Dir..."
        New-Item -ItemType Directory -Path $FullPath | Out-Null
    } else {
        Write-Host "  $Dir exists." -ForegroundColor Green
    }
}

# 2. Repository boundary detection
Write-Host "`nChecking for Git..."
if (Get-Command git -ErrorAction SilentlyContinue) {
    $RepoRoot = git -C $ProjectRoot rev-parse --show-toplevel 2>$null
    if ($LASTEXITCODE -eq 0) {
        $ResolvedRepoRoot = [System.IO.Path]::GetFullPath($RepoRoot.Trim())
        $ResolvedProjectRoot = [System.IO.Path]::GetFullPath($ProjectRoot)
        if ($ResolvedRepoRoot -eq $ResolvedProjectRoot) {
            Write-Host "Project root is the Git repository boundary." -ForegroundColor Green
        } else {
            Write-Host "Git resolves to parent boundary: $ResolvedRepoRoot" -ForegroundColor Yellow
            Write-Host "Review the boundary and ignore rules before committing project data."
        }
    } else {
        Write-Host "No Git repository detected. Setup will not initialize, stage, or commit files." -ForegroundColor Yellow
        Write-Host "Choose and review the repository boundary and backup policy before using Git."
    }
} else {
    Write-Host "Git is not installed or not in PATH." -ForegroundColor Yellow
    Write-Host "Install Git if you want version history and rollback capabilities." -ForegroundColor Yellow
}

# 3. Task Scheduler Info
Write-Host "`nSetup Complete." -ForegroundColor Cyan
Write-Host "`nTo schedule the night shift, you can register run_night_shift.ps1 in Windows Task Scheduler:"
Write-Host "1. Open Task Scheduler"
Write-Host "2. Create Basic Task..."
Write-Host "3. Trigger: Daily at 11:00 PM (or whenever you want the run to start)"
Write-Host "4. Action: Start a program"
Write-Host "5. Program/script: powershell.exe"
Write-Host "6. Add arguments: -ExecutionPolicy Bypass -File `"$ProjectRoot\run_night_shift.ps1`""
Write-Host "`nSee README.md for full usage instructions."
