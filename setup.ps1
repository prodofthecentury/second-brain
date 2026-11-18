param(
    [switch]$NoLaunch,
    [switch]$PullModels,
    [switch]$RebuildVectors
)
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Write-Host 'Setup will not initialize, stage, or commit files. Review the repository boundary separately.' -ForegroundColor Yellow

Write-Host '=== Host Capability Inspection ===' -ForegroundColor Cyan
$OsInfo = Get-CimInstance Win32_OperatingSystem -ErrorAction SilentlyContinue
if ($OsInfo) {
    $TotalRamGiB = [math]::Round($OsInfo.TotalVisibleMemorySize / 1MB, 1)
    $FreeRamGiB = [math]::Round($OsInfo.FreePhysicalMemory / 1MB, 1)
    Write-Host ("OS:        {0} ({1})" -f $OsInfo.Caption, $OsInfo.OSArchitecture)
    Write-Host ("Memory:    {0} GiB free / {1} GiB total" -f $FreeRamGiB, $TotalRamGiB)
}
Write-Host ("CPU:       {0} logical processors" -f [Environment]::ProcessorCount)

$VcRuntimeFound = $false
if ([Environment]::Is64BitOperatingSystem -and -not [Environment]::Is64BitProcess) {
    $SysnativeVcRuntime = Join-Path $env:SystemRoot 'Sysnative\vcruntime140.dll'
    if (Test-Path -LiteralPath $SysnativeVcRuntime) {
        $VcRuntimeFound = $true
    }
} else {
    $System32VcRuntime = Join-Path $env:SystemRoot 'System32\vcruntime140.dll'
    if (Test-Path -LiteralPath $System32VcRuntime) {
        $VcRuntimeFound = $true
    }
}
if ($VcRuntimeFound) {
    Write-Host 'C++ ABI:   MSVC runtime (vcruntime140.dll) verified' -ForegroundColor Green
} else {
    Write-Host '[WARN] Microsoft Visual C++ Redistributable (vcruntime140.dll) not detected. Model-free SQLite FTS5 lexical recall works without it, while native Chroma dense vectors require VC++ 2015-2022 x64 Redistributable.' -ForegroundColor Yellow
}

$NvidiaSmi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
if ($NvidiaSmi) {
    $GpuSummary = (& $NvidiaSmi.Source --query-gpu=name,driver_version --format=csv,noheader 2>$null | Select-Object -First 1)
    if ($GpuSummary) {
        Write-Host ("GPU:       NVIDIA ({0})" -f $GpuSummary.Trim()) -ForegroundColor Green
    } else {
        Write-Host 'GPU:       nvidia-smi detected (no active CUDA device reported; CPU mode available)' -ForegroundColor Yellow
    }
} else {
    $VideoAdapter = Get-CimInstance Win32_VideoController -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($VideoAdapter) {
        Write-Host ("GPU:       {0} (Driver {1}; CPU/DirectML mode)" -f $VideoAdapter.Name, $VideoAdapter.DriverVersion)
    } else {
        Write-Host 'GPU:       No discrete GPU detected (CPU inference mode)'
    }
}

$UvCommand = Get-Command uv -ErrorAction SilentlyContinue
if ($UvCommand) {
    $UvExe = $UvCommand.Source
} elseif (Get-Command winget -ErrorAction SilentlyContinue) {
    Write-Host 'Installing the uv bootstrap with Windows Package Manager...'
    & winget install --id astral-sh.uv --exact --silent --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { throw 'Could not install uv with winget.' }
    $UvCommand = Get-Command uv -ErrorAction SilentlyContinue
    $UvCandidates = @(
        (Join-Path $env:USERPROFILE '.local\bin\uv.exe'),
        (Join-Path $env:USERPROFILE '.cargo\bin\uv.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\uv\uv.exe'),
        (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links\uv.exe')
    )
    if ($UvCommand) { $UvExe = $UvCommand.Source }
    else {
        $UvExe = $UvCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
        if (-not $UvExe) { throw 'uv was installed, but its executable was not found. Open a new shell and rerun setup.ps1.' }
    }
} else {
    $PythonExecutable = $null
    $PythonPrefix = @()
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3.11 --version *> $null
        if ($LASTEXITCODE -eq 0) { $PythonExecutable = 'py'; $PythonPrefix = @('-3.11') }
    }
    if (-not $PythonExecutable -and (Get-Command python3.11 -ErrorAction SilentlyContinue)) {
        & python3.11 --version *> $null
        if ($LASTEXITCODE -eq 0) { $PythonExecutable = 'python3.11' }
    }
    if (-not $PythonExecutable) { throw 'Install uv or Python 3.11, then rerun setup.ps1.' }
    Write-Host 'Installing the user-scoped uv bootstrap with Python pip...'
    $InstallUvArgs = @($PythonPrefix) + @('-m', 'pip', 'install', '--user', 'uv')
    & $PythonExecutable @InstallUvArgs
    if ($LASTEXITCODE -ne 0) { throw 'Could not install uv with pip.' }
    $SiteArgs = @($PythonPrefix) + @('-m', 'site', '--user-base')
    $UserBase = (& $PythonExecutable @SiteArgs).Trim()
    $UvExe = Join-Path $UserBase 'Scripts\uv.exe'
    if (-not (Test-Path $UvExe)) { throw "uv installed, but its executable was not found at $UvExe. Start a new shell and rerun setup.ps1." }
}

& $UvExe python install 3.11
if ($LASTEXITCODE -ne 0) { throw 'uv could not install or locate managed Python 3.11.' }

Push-Location -LiteralPath $ProjectRoot
try {
    Write-Host '=== Synchronizing Locked Python Environment ===' -ForegroundColor Cyan
    & $UvExe sync --frozen
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }

    Write-Host '=== Checking Local Ollama Daemon & Models ===' -ForegroundColor Cyan
    function Resolve-LoopbackOllamaBaseUrl {
        param([string]$RawHost)
        $Candidate = if ($RawHost -and $RawHost.Trim()) { $RawHost.Trim() } else { 'http://127.0.0.1:11434' }
        if ($Candidate -match '://') {
            if ($Candidate -notmatch '(?i)^http://') { return $null }
            $Normalized = $Candidate
        } elseif ($Candidate -eq '::1' -or $Candidate -eq '[::1]') {
            $Normalized = 'http://[::1]:11434'
        } elseif ($Candidate -match '^\[::1\]:\d+/?$' -or ($Candidate -notmatch '^\[' -and $Candidate -match '^[^/:]+:\d+/?$')) {
            $Normalized = "http://$Candidate"
        } else {
            $Normalized = "http://$($Candidate.TrimEnd('/')):11434"
        }
        $ParsedUri = $null
        if (-not [System.Uri]::TryCreate($Normalized, [System.UriKind]::Absolute, [ref]$ParsedUri)) {
            return $null
        }
        if (
            $ParsedUri.Scheme -ne 'http' -or
            -not [string]::IsNullOrEmpty($ParsedUri.UserInfo) -or
            $ParsedUri.AbsolutePath -notin @('', '/') -or
            -not [string]::IsNullOrEmpty($ParsedUri.Query) -or
            -not [string]::IsNullOrEmpty($ParsedUri.Fragment) -or
            $ParsedUri.Port -lt 1 -or
            $ParsedUri.Port -gt 65535
        ) {
            return $null
        }
        $HostLower = $ParsedUri.DnsSafeHost.ToLowerInvariant()
        $ParsedIp = $null
        $IsIpv6Loopback = (
            [System.Net.IPAddress]::TryParse($HostLower, [ref]$ParsedIp) -and
            $ParsedIp.Equals([System.Net.IPAddress]::IPv6Loopback)
        )
        if ($HostLower -notin @('127.0.0.1', 'localhost') -and -not $IsIpv6Loopback) {
            return $null
        }
        $HasExplicitPort = ($Normalized.TrimEnd('/') -match ':\d+$')
        $FinalPort = if ($HasExplicitPort) { $ParsedUri.Port } else { 11434 }
        $OutHost = if ($IsIpv6Loopback) { '[::1]' } else { $HostLower }
        return ("http://{0}:{1}" -f $OutHost, $FinalPort)
    }

    function Invoke-LoopbackOllamaTags {
        param([string]$TagsUrl)
        Add-Type -AssemblyName System.Net.Http -ErrorAction SilentlyContinue
        $SocketsHandlerType = 'System.Net.Http.SocketsHttpHandler' -as [type]
        if ($SocketsHandlerType) {
            $Handler = [System.Net.Http.SocketsHttpHandler]@{
                UseProxy = $false
                AllowAutoRedirect = $false
            }
        } else {
            $Handler = New-Object System.Net.Http.HttpClientHandler
            $Handler.UseProxy = $false
            $Handler.AllowAutoRedirect = $false
        }
        $Client = New-Object System.Net.Http.HttpClient($Handler, $true)
        try {
            $Client.Timeout = [TimeSpan]::FromSeconds(3)
            $Response = $Client.GetAsync($TagsUrl).GetAwaiter().GetResult()
            if (-not $Response.IsSuccessStatusCode) {
                throw ("HTTP {0}" -f [int]$Response.StatusCode)
            }
            $RawJson = $Response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
            return ($RawJson | ConvertFrom-Json)
        } finally {
            $Client.Dispose()
        }
    }

    $OllamaRawHost = if ($env:OLLAMA_HOST -and $env:OLLAMA_HOST.Trim()) { $env:OLLAMA_HOST.Trim() } else { 'http://127.0.0.1:11434' }
    $OllamaBaseUrl = Resolve-LoopbackOllamaBaseUrl -RawHost $OllamaRawHost
    $OllamaTagsUrl = if ($OllamaBaseUrl) { "$OllamaBaseUrl/api/tags" } else { $null }

    $OllamaReachable = $false
    $InstalledModels = @()
    if (-not $OllamaBaseUrl) {
        Write-Host ("[WARN] Refusing non-loopback or malformed OLLAMA_HOST ({0}); continuing in model-free FTS5 lexical mode." -f $OllamaRawHost) -ForegroundColor Yellow
    } else {
        try {
            $TagsResponse = Invoke-LoopbackOllamaTags -TagsUrl $OllamaTagsUrl
            $OllamaReachable = $true
            if ($TagsResponse.models) {
                $InstalledModels = @($TagsResponse.models | ForEach-Object { $_.name })
            }
            Write-Host ("Ollama reachable at {0} ({1} model(s) present)" -f $OllamaBaseUrl, $InstalledModels.Count) -ForegroundColor Green
        } catch {
            Write-Host ("Ollama daemon not reachable at {0}; continuing in model-free FTS5 lexical mode." -f $OllamaBaseUrl) -ForegroundColor Yellow
        }
    }

    if ($PullModels) {
        $OllamaCmd = Get-Command ollama -ErrorAction SilentlyContinue
        if (-not $OllamaBaseUrl -or -not $OllamaCmd -or -not $OllamaReachable) {
            $DisplayTarget = if ($OllamaBaseUrl) { $OllamaBaseUrl } else { $OllamaRawHost }
            throw ('PullModels requested, but Ollama CLI or local loopback daemon ({0}) is unavailable. Start `ollama serve` on loopback first.' -f $DisplayTarget)
        }
        foreach ($ReqModel in @('nomic-embed-text', 'llama3.2:3b')) {
            Write-Host "Pulling required local model: $ReqModel ..."
            & $OllamaCmd.Source pull $ReqModel
            if ($LASTEXITCODE -ne 0) { throw "Failed to pull model $ReqModel." }
        }
        $TagsResponse = Invoke-LoopbackOllamaTags -TagsUrl $OllamaTagsUrl
        if ($TagsResponse.models) {
            $InstalledModels = @($TagsResponse.models | ForEach-Object { $_.name })
        }
    }

    Write-Host '=== Initializing Vault & Disposable Indexes ===' -ForegroundColor Cyan
    & $UvExe run --frozen second-brain install
    if ($LASTEXITCODE -ne 0) { throw 'Vault and lexical index initialization failed.' }

    if ($env:BRAIN_VAULT_DIR -and $env:BRAIN_VAULT_DIR.Trim()) {
        $VaultDir = $env:BRAIN_VAULT_DIR.Trim()
    } else {
        $ResolvedVault = (& $UvExe run --frozen python -c "from scripts.config import resolve_active_brain_dir; print(resolve_active_brain_dir())" 2>$null | Select-Object -First 1)
        $VaultDir = if ($ResolvedVault -and $ResolvedVault.Trim()) { $ResolvedVault.Trim() } else { Join-Path $ProjectRoot 'brain' }
    }
    $ManifestPath = Join-Path $VaultDir '.chroma\index-manifest.json'
    $HasEmbedder = ($InstalledModels | Where-Object { $_ -eq 'nomic-embed-text' -or $_ -like 'nomic-embed-text:*' }).Count -gt 0
    if ($RebuildVectors) {
        Write-Host 'Building verified Chroma dense vector generation...'
        & $UvExe run --frozen python scripts/rebuild_vectors.py
        if ($LASTEXITCODE -ne 0) { throw 'Dense vector index initialization failed.' }
    } elseif ($VcRuntimeFound -and $OllamaReachable -and $HasEmbedder -and -not (Test-Path -LiteralPath $ManifestPath)) {
        Write-Host 'Building verified Chroma dense vector generation...'
        & $UvExe run --frozen python scripts/rebuild_vectors.py
        if ($LASTEXITCODE -ne 0) {
            Write-Host '[WARN] Dense vector index build failed; continuing with model-free SQLite FTS5 lexical recall.' -ForegroundColor Yellow
        }
    }

    Write-Host '=== Running Environment & Health Verification ===' -ForegroundColor Cyan
    & $UvExe run --frozen second-brain profile
    if ($LASTEXITCODE -ne 0) { throw 'Hardware profile inspection failed.' }
    & $UvExe run --frozen python scripts/doctor.py
    if ($LASTEXITCODE -ne 0) { throw 'Health validation failed.' }

    if (-not $NoLaunch) {
        Write-Host 'Starting the local loopback dashboard (127.0.0.1:8501).' -ForegroundColor Cyan
        & $UvExe run --frozen second-brain dashboard
        if ($LASTEXITCODE -ne 0) { throw 'Dashboard exited with an error.' }
    }
} finally {
    Pop-Location
}

if ($NoLaunch) { Write-Host 'Setup is ready. Start the dashboard later with: uv run --frozen second-brain dashboard' -ForegroundColor Green }
