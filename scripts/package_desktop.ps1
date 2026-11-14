<#
.SYNOPSIS
Build and OS-sign Second Brain desktop installers (Windows NSIS + MSI) and emit SHA256SUMS.txt.

.EXAMPLE
  .\scripts\package_desktop.ps1 -SelfSigned
  .\scripts\package_desktop.ps1 -Target win -PfxPath C:\certs\code.pfx -PfxPassword $env:PFX_PASS
#>
param(
    [ValidateSet("nsis", "msi", "win", "pack")]
    [string]$Target = "win",
    [string]$PfxPath = $env:SECOND_BRAIN_PFX_PATH,
    [string]$PfxPassword = $env:SECOND_BRAIN_PFX_PASSWORD,
    [string]$Thumbprint = $env:SECOND_BRAIN_CERT_THUMBPRINT,
    [switch]$SelfSigned,
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$DesktopDir = Join-Path $RepoRoot "desktop"
$DistDir = Join-Path $RepoRoot "dist"

if (-not $SkipBuild) {
    Push-Location $DesktopDir
    try {
        if (-not (Test-Path (Join-Path $DesktopDir "node_modules"))) {
            npm ci
        }
        switch ($Target) {
            "pack" { npm run pack }
            "nsis" { npm run dist }
            "msi"  { npm run dist:msi }
            "win"  { npm run dist:win }
        }
        if ($LASTEXITCODE -ne 0) {
            throw "electron-builder exited with code $LASTEXITCODE"
        }
    } finally {
        Pop-Location
    }
}

$signScript = Join-Path $PSScriptRoot "sign_release.ps1"
$signArgs = @{ DistDir = $DistDir }
if ($Thumbprint) { $signArgs["Thumbprint"] = $Thumbprint }
elseif ($PfxPath) {
    $signArgs["PfxPath"] = $PfxPath
    if ($PfxPassword) { $signArgs["PfxPassword"] = $PfxPassword }
} elseif ($SelfSigned) {
    $signArgs["SelfSigned"] = $true
}

& $signScript @signArgs
