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

New-Item -ItemType Directory -Force -Path $DistDir | Out-Null

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
