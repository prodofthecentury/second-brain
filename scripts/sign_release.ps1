<#
.SYNOPSIS
Sign Second Brain Windows release artifacts (.exe / .msi) and generate a SHA-256 manifest.

.DESCRIPTION
Supports:
  1. Production PFX certificate (-PfxPath + -PfxPassword or env SECOND_BRAIN_PFX_PATH / SECOND_BRAIN_PFX_PASSWORD)
  2. Certificate thumbprint in Windows Cert Store (-Thumbprint)
  3. Dev/QA self-signed Authenticode certificate (-SelfSigned)
Falls back gracefully to generating SHA256SUMS.txt if no signing certificate is provided.
#>
param(
    [string]$DistDir = (Join-Path (Split-Path -Parent $PSScriptRoot) "dist"),
    [string]$PfxPath = $env:SECOND_BRAIN_PFX_PATH,
    [string]$PfxPassword = $env:SECOND_BRAIN_PFX_PASSWORD,
    [string]$Thumbprint = $env:SECOND_BRAIN_CERT_THUMBPRINT,
    [string]$TimestampUrl = "http://timestamp.digicert.com",
    [switch]$SelfSigned
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $DistDir)) {
    throw "Dist directory not found: $DistDir"
}

$targets = Get-ChildItem -Path $DistDir -File | Where-Object { $_.Extension -in @(".exe", ".msi", ".vsix") }
if (-not $targets) {
    Write-Warning "No .exe, .msi, or .vsix artifacts found in $DistDir"
    exit 0
}

$signCert = $null
if ($Thumbprint) {
    $signCert = Get-ChildItem -Path Cert:\CurrentUser\My\$Thumbprint -ErrorAction SilentlyContinue
    if (-not $signCert) {
        $signCert = Get-ChildItem -Path Cert:\LocalMachine\My\$Thumbprint -ErrorAction SilentlyContinue
    }
} elseif ($PfxPath -and (Test-Path $PfxPath)) {
    $secPass = if ($PfxPassword) { ConvertTo-SecureString -String $PfxPassword -AsPlainText -Force } else { $null }
    $signCert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2
    if ($secPass) {
        $signCert.Import($PfxPath, $secPass, "Exportable,PersistKeySet")
    } else {
        $signCert.Import($PfxPath)
    }
} elseif ($SelfSigned) {
    $existing = Get-ChildItem -Path Cert:\CurrentUser\My | Where-Object { $_.Subject -eq "CN=Second Brain Dev Signing" } | Select-Object -First 1
    if ($existing) {
        $signCert = $existing
    } else {
        $signCert = New-SelfSignedCertificate -Type CodeSigningCert -Subject "CN=Second Brain Dev Signing" -CertStoreLocation "Cert:\CurrentUser\My"
    }
}

$exeTargets = $targets | Where-Object { $_.Extension -in @(".exe", ".msi") }
if ($signCert -and $exeTargets) {
    foreach ($file in $exeTargets) {
        Write-Host "Signing $($file.Name)..."
        $sigParams = @{
            FilePath = $file.FullName
            Certificate = $signCert
            HashAlgorithm = "SHA256"
        }
        if (-not $SelfSigned -and $TimestampUrl) {
            $sigParams["TimestampServer"] = $TimestampUrl
        }
        $res = Set-AuthenticodeSignature @sigParams
        Write-Host "  Signature status: $($res.Status) ($($res.StatusMessage))"
    }
} else {
    Write-Host "No signing certificate configured; skipping Authenticode signing (pass -SelfSigned or -PfxPath)."
}

$manifestPath = Join-Path $DistDir "SHA256SUMS.txt"
$lines = foreach ($file in ($targets | Sort-Object Name)) {
    $hash = (Get-FileHash -Path $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    "$hash  $($file.Name)"
}
$lines | Set-Content -Path $manifestPath -Encoding UTF8
Write-Host "Wrote SHA-256 manifest: $manifestPath"
