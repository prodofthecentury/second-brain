<#
.SYNOPSIS
Sign Second Brain Windows binaries (inner win-unpacked/Second Brain.exe + outer .exe/.msi)
and generate dist/SHA256SUMS.txt.

.DESCRIPTION
Supports:
  1. Production OV/EV PFX certificate (-PfxPath + -PfxPassword or env SECOND_BRAIN_PFX_PATH / SECOND_BRAIN_PFX_PASSWORD)
  2. Certificate thumbprint in Windows Cert Store (-Thumbprint or env SECOND_BRAIN_CERT_THUMBPRINT)
  3. EV Hardware Token / Cloud PKCS#11 HSM (-CspName + -KeyContainerName + -CertFile via signtool.exe)
  4. Microsoft Azure Trusted Signing (-AzureEndpoint + -AzureAccount + -AzureCertProfile)
  5. Dev/QA self-signed Authenticode certificate (-SelfSigned or env SECOND_BRAIN_SELF_SIGNED=1)
  6. Strict CA trust-chain & RFC-3161 counter-signature enforcement (-RequireTrustedChain or SECOND_BRAIN_REQUIRE_TRUSTED_SIGN=1)
Verifies Get-AuthenticodeSignature on every signed inner and outer binary and emits SHA256SUMS.txt.
#>
param(
    [string]$DistDir = (Join-Path (Split-Path -Parent $PSScriptRoot) "dist"),
    [string]$SingleFile = "",
    [string]$PfxPath = $env:SECOND_BRAIN_PFX_PATH,
    [string]$PfxPassword = $env:SECOND_BRAIN_PFX_PASSWORD,
    [string]$Thumbprint = $env:SECOND_BRAIN_CERT_THUMBPRINT,
    [string]$CspName = $env:SECOND_BRAIN_CSP_NAME,
    [string]$KeyContainerName = $env:SECOND_BRAIN_KEY_CONTAINER,
    [string]$CertFile = $env:SECOND_BRAIN_CERT_FILE,
    [string]$AzureEndpoint = $env:SECOND_BRAIN_AZURE_TRUSTED_SIGNING_ENDPOINT,
    [string]$AzureAccount = $env:SECOND_BRAIN_AZURE_CODE_SIGNING_ACCOUNT,
    [string]$AzureCertProfile = $env:SECOND_BRAIN_AZURE_CERT_PROFILE,
    [string]$TimestampUrl = $(if ($env:SECOND_BRAIN_TIMESTAMP_URL) { $env:SECOND_BRAIN_TIMESTAMP_URL } else { "http://timestamp.acs.microsoft.com" }),
    [switch]$SelfSigned,
    [switch]$RequireTrustedChain,
    [switch]$SkipManifest
)

$ErrorActionPreference = "Stop"

if (-not $SelfSigned -and ($env:SECOND_BRAIN_SELF_SIGNED -in @("1", "true", "TRUE"))) {
    $SelfSigned = [switch]::Present
}
if (-not $RequireTrustedChain -and ($env:SECOND_BRAIN_REQUIRE_TRUSTED_SIGN -in @("1", "true", "TRUE"))) {
    $RequireTrustedChain = [switch]::Present
}
if ($RequireTrustedChain -and $SelfSigned) {
    throw "Refusing -SelfSigned when -RequireTrustedChain is enforced: an OV/EV CA certificate, HSM token, or Azure Trusted Signing profile is required."
}

function Resolve-SigntoolExe {
    $cmd = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $kitsRoot = "${env:ProgramFiles(x86)}\Windows Kits\10\bin"
    if (Test-Path $kitsRoot) {
        $found = Get-ChildItem -Path $kitsRoot -Recurse -Filter "signtool.exe" -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -match "\\x64\\" } |
            Sort-Object FullName -Descending |
            Select-Object -First 1
        if ($found) { return $found.FullName }
    }
    throw "signtool.exe is required for HSM/CSP or Azure dlib signing. Install the Windows 10/11 SDK."
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

function Assert-VerifiedAuthenticode {
    param([Parameter(Mandatory = $true)][string]$FilePath)
    $verified = Get-AuthenticodeSignature -FilePath $FilePath
    if (-not $verified.SignerCertificate -or $verified.Status -in @("NotSigned", "HashMismatch")) {
        throw "Get-AuthenticodeSignature verification failed for ${FilePath}: $($verified.Status) ($($verified.StatusMessage))"
    }
    if ($RequireTrustedChain) {
        if ($verified.Status -ne "Valid") {
            throw "Trusted CA chain verification failed for ${FilePath}: Status=$($verified.Status) ($($verified.StatusMessage))"
        }
        if ($verified.SignerCertificate.Subject -eq $verified.SignerCertificate.Issuer) {
            throw "Self-signed certificate rejected under -RequireTrustedChain for ${FilePath}: $($verified.SignerCertificate.Subject)"
        }
        if (-not $verified.TimeStamperCertificate) {
            throw "Missing RFC-3161 counter-signature timestamp under -RequireTrustedChain for ${FilePath}."
        }
    }
    Write-Host "  Verified signature: $($verified.Status) [Subject=$($verified.SignerCertificate.Subject), Thumbprint=$($verified.SignerCertificate.Thumbprint)]"
}

function Invoke-AuthenticodeSignAndVerify {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        $Certificate = $null
    )
    Write-Host "Signing $FilePath..."
    if ($AzureEndpoint -and $AzureAccount -and $AzureCertProfile) {
        if (-not (Get-Command Invoke-TrustedSigning -ErrorAction SilentlyContinue)) {
            throw "TrustedSigning PowerShell module is not installed. Run: Install-Module TrustedSigning -Scope CurrentUser"
        }
        Invoke-TrustedSigning `
            -Endpoint $AzureEndpoint `
            -CodeSigningAccountName $AzureAccount `
            -CertificateProfileName $AzureCertProfile `
            -Files $FilePath `
            -FileDigest "SHA256" `
            -TimestampRfc3161 $TimestampUrl `
            -TimestampDigest "SHA256"
    } elseif ($CspName -and $KeyContainerName) {
        $signtool = Resolve-SigntoolExe
        $stArgs = @("sign", "/fd", "SHA256", "/csp", $CspName, "/kc", $KeyContainerName)
        if ($CertFile) { $stArgs += @("/f", $CertFile) }
        if ($TimestampUrl) { $stArgs += @("/tr", $TimestampUrl, "/td", "SHA256") }
        $stArgs += @($FilePath)
        & $signtool @stArgs
        if ($LASTEXITCODE -ne 0) {
            throw "signtool HSM/CSP signing exited with code $LASTEXITCODE for $FilePath"
        }
    } elseif ($Certificate) {
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
    } else {
        throw "No signing material resolved for $FilePath"
    }
    Assert-VerifiedAuthenticode -FilePath $FilePath
}

$hasExternalProvider = (($AzureEndpoint -and $AzureAccount -and $AzureCertProfile) -or ($CspName -and $KeyContainerName))
$signCert = $null
try {
    if (-not $hasExternalProvider) {
        $signCert = Resolve-SigningCertificate
    }
    $canSign = ($hasExternalProvider -or ($null -ne $signCert))

    if (-not $canSign -and $RequireTrustedChain) {
        throw "No trusted code-signing credentials configured (-PfxPath, -Thumbprint, -CspName/-KeyContainerName, or -AzureEndpoint/-AzureAccount/-AzureCertProfile)."
    }

    if ($SingleFile) {
        if (-not (Test-Path $SingleFile)) {
            throw "SingleFile target not found: $SingleFile"
        }
        if ($canSign) {
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

    if ($canSign) {
        if (Test-Path $innerExe) {
            $innerSig = Get-AuthenticodeSignature -FilePath $innerExe
            if (-not $innerSig.SignerCertificate -or $innerSig.Status -in @("NotSigned", "HashMismatch")) {
                Invoke-AuthenticodeSignAndVerify -FilePath $innerExe -Certificate $signCert
            } else {
                Assert-VerifiedAuthenticode -FilePath $innerExe
            }
        }
        foreach ($file in $outerSignTargets) {
            Invoke-AuthenticodeSignAndVerify -FilePath $file.FullName -Certificate $signCert
        }
    } else {
        Write-Host "No signing certificate configured; skipping Authenticode signing (pass -SelfSigned, -Thumbprint, -PfxPath, HSM -CspName, or Azure Trusted Signing)."
    }
} finally {
    if ($PfxPath -and $signCert -is [System.IDisposable]) {
        $signCert.Dispose()
    }
}

if (-not $SkipManifest) {
    $manifestTargets = @(
        Get-ChildItem -Path $DistDir -File |
            Where-Object {
                $_.Extension -in @(".exe", ".msi", ".vsix", ".whl", ".dmg", ".zip", ".xpi", ".AppImage", ".deb", ".blockmap") -or
                $_.Name.EndsWith(".tar.gz")
            }
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
    [System.IO.File]::WriteAllText(
        $manifestPath,
        ($lines -join "`n") + "`n",
        (New-Object System.Text.UTF8Encoding $false)
    )
    Write-Host "Wrote SHA-256 manifest: $manifestPath"
}
