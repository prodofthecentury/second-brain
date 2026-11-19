<#
.SYNOPSIS
Build and OS-sign Second Brain desktop installers (Windows NSIS + MSI), offline wheelhouse,
wheel, and VSIX extension, verifying both inner and outer Authenticode signatures and
emitting dist/SHA256SUMS.txt.

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
    [string]$TimestampUrl = $(if ($env:SECOND_BRAIN_TIMESTAMP_URL) { $env:SECOND_BRAIN_TIMESTAMP_URL } else { "https://timestamp.digicert.com" }),
    [switch]$SelfSigned,
    [switch]$SkipSign,
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$DesktopDir = Join-Path $RepoRoot "desktop"
$DistDir = Join-Path $RepoRoot "dist"
$SignScript = Join-Path $PSScriptRoot "sign_release.ps1"

# Propagate signing configuration to desktop/build/sign.js for electron-builder hooks
if ($Thumbprint) { $env:SECOND_BRAIN_CERT_THUMBPRINT = $Thumbprint }
if ($PfxPath) { $env:SECOND_BRAIN_PFX_PATH = $PfxPath }
if ($PfxPassword) { $env:SECOND_BRAIN_PFX_PASSWORD = $PfxPassword }
if ($TimestampUrl) { $env:SECOND_BRAIN_TIMESTAMP_URL = $TimestampUrl }
if ($SelfSigned) { $env:SECOND_BRAIN_SELF_SIGNED = "1" }

New-Item -ItemType Directory -Force -Path $DistDir | Out-Null

Push-Location $RepoRoot
try {
    uv run --frozen python scripts/make_icons.py | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "make_icons.py exited with code $LASTEXITCODE"
    }

    uv build --wheel --out-dir $DistDir | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "uv build exited with code $LASTEXITCODE"
    }

    uv run --frozen python scripts/build_wheelhouse.py --out-dir (Join-Path $DistDir "wheelhouse") | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "build_wheelhouse.py exited with code $LASTEXITCODE"
    }

    uv run --frozen python scripts/build_vsix.py --out-dir $DistDir | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "build_vsix.py exited with code $LASTEXITCODE"
    }

    uv run --frozen python scripts/build_clipper.py --out-dir $DistDir | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "build_clipper.py exited with code $LASTEXITCODE"
    }
} finally {
    Pop-Location
}

if (-not $SkipBuild) {
    Push-Location $DesktopDir
    try {
        if (-not (Test-Path (Join-Path $DesktopDir "node_modules"))) {
            npm ci
            if ($LASTEXITCODE -ne 0) {
                throw "npm ci failed with code $LASTEXITCODE"
            }
        }

        # Stage 1: Build unpacked directory (`win-unpacked/Second Brain.exe`) with rcedit custom icon & metadata
        npm run pack
        if ($LASTEXITCODE -ne 0) {
            throw "electron-builder --dir exited with code $LASTEXITCODE"
        }

        # Stage 1b: Bundle offline wheelhouse into win-unpacked/resources/backend/wheelhouse before wrapping
        $srcWheelhouse = Join-Path $DistDir "wheelhouse"
        $dstWheelhouse = Join-Path $DistDir "win-unpacked\resources\backend\wheelhouse"
        if (Test-Path $srcWheelhouse) {
            New-Item -ItemType Directory -Force -Path $dstWheelhouse | Out-Null
            Copy-Item -Path (Join-Path $srcWheelhouse "*.whl") -Destination $dstWheelhouse -Force
        }

        # Stage 2: Deterministically sign inner `win-unpacked/Second Brain.exe` BEFORE wrapping in NSIS/MSI
        $innerExe = Join-Path $DistDir "win-unpacked\Second Brain.exe"
        if (-not $SkipSign) {
            $innerSignArgs = @{
                SingleFile   = $innerExe
                TimestampUrl = $TimestampUrl
                SkipManifest = $true
            }
            if ($Thumbprint) { $innerSignArgs["Thumbprint"] = $Thumbprint }
            elseif ($PfxPath) {
                $innerSignArgs["PfxPath"] = $PfxPath
                if ($PfxPassword) { $innerSignArgs["PfxPassword"] = $PfxPassword }
            } elseif ($SelfSigned) {
                $innerSignArgs["SelfSigned"] = $true
            }
            & $SignScript @innerSignArgs
        }

        # Stage 3: Package pre-signed `win-unpacked` into NSIS / MSI installers
        if ($Target -ne "pack") {
            $buildStartedAt = (Get-Date).AddSeconds(-2)
            $ebArgs = @("electron-builder", "--prepackaged", "..\dist\win-unpacked", "--win")
            switch ($Target) {
                "nsis" { $ebArgs += @("nsis") }
                "msi"  { $ebArgs += @("msi") }
                "win"  { $ebArgs += @("nsis", "msi") }
            }
            & npx @ebArgs
            $ebExit = $LASTEXITCODE
            if ($ebExit -ne 0) {
                # Windows Defender occasionally holds a transient scan handle on *.nsis.7z during final unlink
                # after all installer artifacts (.exe, .blockmap, .msi, win-unpacked) have completed writing.
                $nsisTemp = Get-ChildItem -Path $DistDir -Filter "*.nsis.7z" -ErrorAction SilentlyContinue
                $expectedOk = (Test-Path $innerExe)
                if ($Target -in @("nsis", "win")) {
                    $exeOut = Get-ChildItem -Path $DistDir -Filter "*-Setup-*.exe" -ErrorAction SilentlyContinue |
                        Where-Object { $_.LastWriteTime -ge $buildStartedAt -and $_.Length -gt 1048576 }
                    $blockOut = Get-ChildItem -Path $DistDir -Filter "*-Setup-*.exe.blockmap" -ErrorAction SilentlyContinue |
                        Where-Object { $_.LastWriteTime -ge $buildStartedAt -and $_.Length -gt 1024 }
                    $expectedOk = $expectedOk -and ($null -ne $exeOut) -and ($null -ne $blockOut)
                }
                if ($Target -in @("msi", "win")) {
                    $msiOut = Get-ChildItem -Path $DistDir -Filter "*-Setup-*.msi" -ErrorAction SilentlyContinue |
                        Where-Object { $_.LastWriteTime -ge $buildStartedAt -and $_.Length -gt 1048576 }
                    $expectedOk = $expectedOk -and ($null -ne $msiOut)
                }
                if ($expectedOk -and $nsisTemp) {
                    Write-Warning "electron-builder hit transient Windows file lock unlinking $($nsisTemp.Name) after completing artifacts; cleaning up."
                    Start-Sleep -Seconds 2
                } else {
                    throw "electron-builder --prepackaged exited with code $ebExit"
                }
            }
        }
    } finally {
        Pop-Location
    }
}

Get-ChildItem -Path $DistDir -Filter "*.nsis.7z" -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
Remove-Item -Path (Join-Path $DistDir "builder-debug.yml") -Force -ErrorAction SilentlyContinue

# Stage 4: Sign outer .exe/.msi installers, verify signatures, and emit SHA256SUMS.txt
if ($SkipSign) {
    $sumLines = @()
    Get-ChildItem -Path $DistDir -File | Where-Object {
        $_.Extension -in @(".exe", ".msi", ".whl", ".vsix", ".zip", ".blockmap")
    } | Sort-Object Name | ForEach-Object {
        $hash = (Get-FileHash -Path $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        $sumLines += "$hash  $($_.Name)"
    }
    Set-Content -Path (Join-Path $DistDir "SHA256SUMS.txt") -Value ($sumLines -join "`n") -Encoding ascii
} else {
    $outerSignArgs = @{
        DistDir      = $DistDir
        TimestampUrl = $TimestampUrl
    }
    if ($Thumbprint) { $outerSignArgs["Thumbprint"] = $Thumbprint }
    elseif ($PfxPath) {
        $outerSignArgs["PfxPath"] = $PfxPath
        if ($PfxPassword) { $outerSignArgs["PfxPassword"] = $PfxPassword }
    } elseif ($SelfSigned) {
        $outerSignArgs["SelfSigned"] = $true
    }

    & $SignScript @outerSignArgs
}
