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
        $buildStartedAt = (Get-Date).AddSeconds(-2)
        switch ($Target) {
            "pack" { npm run pack }
            "nsis" { npm run dist }
            "msi"  { npm run dist:msi }
            "win"  { npm run dist:win }
        }
        $ebExit = $LASTEXITCODE
        if ($ebExit -ne 0) {
            # Windows Defender occasionally holds a transient scan handle on *.nsis.7z during final unlink
            # after all installer artifacts (.exe, .blockmap, .msi, win-unpacked) have completed writing.
            $nsisTemp = Get-ChildItem -Path $DistDir -Filter "*.nsis.7z" -ErrorAction SilentlyContinue
            $expectedOk = (Test-Path (Join-Path $DistDir "win-unpacked\Second Brain.exe"))
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
                throw "electron-builder exited with code $ebExit"
            }
        }
    } finally {
        Pop-Location
    }
}

New-Item -ItemType Directory -Force -Path $DistDir | Out-Null
Get-ChildItem -Path $DistDir -Filter "*.nsis.7z" -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
Remove-Item -Path (Join-Path $DistDir "builder-debug.yml") -Force -ErrorAction SilentlyContinue

Push-Location $RepoRoot
try {
    uv build --wheel --out-dir $DistDir | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "uv build exited with code $LASTEXITCODE"
    }
    $vsixPy = @'
import json, pathlib, zipfile
from xml.sax.saxutils import escape
root = pathlib.Path("extensions/vscode-second-brain")
pkg = json.loads((root / "package.json").read_text(encoding="utf-8"))
esc = lambda v: escape(str(v), {'"': "&quot;", "'": "&apos;"})
ver = pkg["version"]
out = pathlib.Path("dist") / f"second-brain-vscode-{ver}.vsix"
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    z.writestr(
        "[Content_Types].xml",
        '<?xml version="1.0" encoding="utf-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="json" ContentType="application/json"/>'
        '<Default Extension="js" ContentType="application/javascript"/>'
        '<Default Extension="css" ContentType="text/css"/>'
        '<Default Extension="html" ContentType="text/html"/>'
        '<Default Extension="md" ContentType="text/markdown"/>'
        '<Default Extension="vsixmanifest" ContentType="text/xml"/>'
        "</Types>",
    )
    z.writestr(
        "extension.vsixmanifest",
        '<?xml version="1.0" encoding="utf-8"?>'
        '<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011">'
        f'<Metadata><Identity Language="en-US" Id="{esc(pkg["name"])}" Version="{esc(ver)}" Publisher="{esc(pkg["publisher"])}"/>'
        f'<DisplayName>{esc(pkg["displayName"])}</DisplayName>'
        f'<Description>{esc(pkg["description"])}</Description></Metadata>'
        '<Installation><InstallationTarget Id="Microsoft.VisualStudio.Code"/></Installation>'
        '<Assets><Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json"/></Assets>'
        "</PackageManifest>",
    )
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if p.is_file() and not rel.startswith("node_modules/") and p.suffix.lower() != ".vsix":
            z.write(p, f"extension/{rel}")
'@
    $vsixPy | uv run --frozen python -
    if ($LASTEXITCODE -ne 0) {
        throw "VSIX packaging exited with code $LASTEXITCODE"
    }
} finally {
    Pop-Location
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
