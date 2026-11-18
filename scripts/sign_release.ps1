<#
.SYNOPSIS
Sign Second Brain Windows binaries (inner win-unpacked/Second Brain.exe + outer .exe/.msi)
and generate dist/SHA256SUMS.txt.

.DESCRIPTION
Supports:
  1. Production PFX certificate (-PfxPath + -PfxPassword or env SECOND_BRAIN_PFX_PATH / SECOND_BRAIN_PFX_PASSWORD)
  2. Certificate thumbprint in Windows Cert Store (-Thumbprint or env SECOND_BRAIN_CERT_THUMBPRINT)
  3. Dev/QA self-signed Authenticode certificate (-SelfSigned or env SECOND_BRAIN_SELF_SIGNED=1)
  4. RFC-3161 timestamping (-TimestampUrl) when CA certificates (-PfxPath / -Thumbprint) are supplied.
Verifies Get-AuthenticodeSignature on every signed inner and outer binary and emits SHA256SUMS.txt.
#>
param(
    [string]$DistDir = (Join-Path (Split-Path -Parent $PSScriptRoot) "dist"),
    [string]$SingleFile = "",
    [string]$PfxPath = $env:SECOND_BRAIN_PFX_PATH,
    [string]$PfxPassword = $env:SECOND_BRAIN_PFX_PASSWORD,
    [string]$Thumbprint = $env:SECOND_BRAIN_CERT_THUMBPRINT,
    [string]$TimestampUrl = $(if ($env:SECOND_BRAIN_TIMESTAMP_URL) { $env:SECOND_BRAIN_TIMESTAMP_URL } else { "https://timestamp.digicert.com" }),
    [switch]$SelfSigned,
    [switch]$SkipManifest
)

$ErrorActionPreference = "Stop"

if (-not $SelfSigned -and ($env:SECOND_BRAIN_SELF_SIGNED -in @("1", "true", "TRUE"))) {
    $SelfSigned = [switch]::Present
}

function Resolve-SigningCertificate {
    if ($Thumbprint) {
        $cert = Get-ChildItem -Path "Cert:\CurrentUser\My\$Thumbprint" -ErrorAction SilentlyContinue
        if (-not $cert) {
            $cert = Get-ChildItem -Path "Cert:\LocalMachine\My\$Thumbprint" -ErrorAction SilentlyContinue
        }
        if (-not $cert) {
            throw "Code-signing certificate with thumbprint '$Thumbprint' not found in CurrentUser\My or LocalMachine\My."
        }
        return $cert
    }
    if ($PfxPath) {
        if (-not (Test-Path $PfxPath)) {
            throw "PFX certificate file not found: $PfxPath"
        }
        $secPass = if ($PfxPassword) { ConvertTo-SecureString -String $PfxPassword -AsPlainText -Force } else { $null }
        $cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2
        if ($secPass) {
            $cert.Import($PfxPath, $secPass, "EphemeralKeySet")
        } else {
            $cert.Import($PfxPath, [string]$null, "EphemeralKeySet")
        }
        return $cert
    }
    if ($SelfSigned) {
        $existing = Get-ChildItem -Path Cert:\CurrentUser\My |
            Where-Object { $_.Subject -eq "CN=Second Brain Dev Signing" -and $_.NotAfter -gt (Get-Date) } |
            Select-Object -First 1
        if ($existing) {
            return $existing
        }
        return New-SelfSignedCertificate -Type CodeSigningCert -Subject "CN=Second Brain Dev Signing" -CertStoreLocation "Cert:\CurrentUser\My"
    }
    return $null
}

function Invoke-AuthenticodeSignAndVerify {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)]$Certificate
    )
    Write-Host "Signing $FilePath..."
    $sigParams = @{
        FilePath      = $FilePath
        Certificate   = $Certificate
        HashAlgorithm = "SHA256"
    }
    if (-not $SelfSigned -and $TimestampUrl) {
        $sigParams["TimestampServer"] = $TimestampUrl
    }
    $res = Set-AuthenticodeSignature @sigParams
    if (-not $res.SignerCertificate -or $res.Status -in @("NotSigned", "HashMismatch")) {
        throw "Authenticode signing failed for ${FilePath}: $($res.Status) ($($res.StatusMessage))"
    }
    $verified = Get-AuthenticodeSignature -FilePath $FilePath
    if (-not $verified.SignerCertificate -or $verified.Status -in @("NotSigned", "HashMismatch")) {
        throw "Get-AuthenticodeSignature verification failed for ${FilePath}: $($verified.Status) ($($verified.StatusMessage))"
    }
    Write-Host "  Verified signature: $($verified.Status) [Thumbprint=$($verified.SignerCertificate.Thumbprint)]"
}

$signCert = $null
try {
    $signCert = Resolve-SigningCertificate

    if ($SingleFile) {
        if (-not (Test-Path $SingleFile)) {
            throw "SingleFile target not found: $SingleFile"
        }
        if ($signCert) {
            Invoke-AuthenticodeSignAndVerify -FilePath (Resolve-Path $SingleFile).Path -Certificate $signCert
        } else {
            Write-Host "No signing certificate configured; skipping SingleFile signing for $SingleFile."
        }
        return
    }

    if (-not (Test-Path $DistDir)) {
        throw "Dist directory not found: $DistDir"
    }

    $innerExe = Join-Path $DistDir "win-unpacked\Second Brain.exe"
    $outerSignTargets = @(Get-ChildItem -Path $DistDir -File | Where-Object { $_.Extension -in @(".exe", ".msi") })

    if ($signCert) {
        if (Test-Path $innerExe) {
            $innerSig = Get-AuthenticodeSignature -FilePath $innerExe
            if (-not $innerSig.SignerCertificate -or $innerSig.Status -in @("NotSigned", "HashMismatch")) {
                Invoke-AuthenticodeSignAndVerify -FilePath $innerExe -Certificate $signCert
            } else {
                Write-Host "Inner binary already signed and verified: $innerExe [$($innerSig.Status)]"
            }
        }
        foreach ($file in $outerSignTargets) {
            Invoke-AuthenticodeSignAndVerify -FilePath $file.FullName -Certificate $signCert
        }
    } else {
        Write-Host "No signing certificate configured; skipping Authenticode signing (pass -SelfSigned, -Thumbprint, or -PfxPath)."
    }
} finally {
    if ($PfxPath -and $signCert -is [System.IDisposable]) {
        $signCert.Dispose()
    }
}

if (-not $SkipManifest) {
    $manifestTargets = @(
        Get-ChildItem -Path $DistDir -File |
            Where-Object { $_.Extension -in @(".exe", ".msi", ".vsix", ".whl", ".dmg", ".zip", ".AppImage", ".deb") }
    )
    if (-not $manifestTargets) {
        Write-Warning "No release artifacts found in $DistDir for SHA256SUMS.txt"
        exit 0
    }
    $manifestPath = Join-Path $DistDir "SHA256SUMS.txt"
    $lines = foreach ($file in ($manifestTargets | Sort-Object Name)) {
        $hash = (Get-FileHash -Path $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        "$hash  $($file.Name)"
    }
    $lines | Set-Content -Path $manifestPath -Encoding UTF8
    Write-Host "Wrote SHA-256 manifest: $manifestPath"
}
