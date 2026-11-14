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
    $OllamaRawHost = if ($env:OLLAMA_HOST -and $env:OLLAMA_HOST.Trim()) { $env:OLLAMA_HOST.Trim() } else { 'http://127.0.0.1:11434' }
    if ($OllamaRawHost -match '^https?://') {
        $OllamaBaseUrl = $OllamaRawHost.TrimEnd('/')
    } elseif ($OllamaRawHost -match ':') {
        $OllamaBaseUrl = "http://$($OllamaRawHost.TrimEnd('/'))"
    } else {
        $OllamaBaseUrl = "http://${OllamaRawHost}:11434"
    }
    $OllamaTagsUrl = "$OllamaBaseUrl/api/tags"

    $OllamaReachable = $false
    $InstalledModels = @()
    try {
        $TagsResponse = Invoke-RestMethod -Uri $OllamaTagsUrl -Method Get -TimeoutSec 3
        $OllamaReachable = $true
        if ($TagsResponse.models) {
            $InstalledModels = @($TagsResponse.models | ForEach-Object { $_.name })
        }
        Write-Host ("Ollama reachable at {0} ({1} model(s) present)" -f $OllamaBaseUrl, $InstalledModels.Count) -ForegroundColor Green
    } catch {
        Write-Host ("Ollama daemon not reachable at {0}; continuing in model-free FTS5 lexical mode." -f $OllamaBaseUrl) -ForegroundColor Yellow
    }

    if ($PullModels) {
        $OllamaCmd = Get-Command ollama -ErrorAction SilentlyContinue
        if (-not $OllamaCmd -or -not $OllamaReachable) {
            throw ('PullModels requested, but Ollama CLI or local daemon ({0}) is unavailable. Start `ollama serve` first.' -f $OllamaBaseUrl)
        }
        foreach ($ReqModel in @('nomic-embed-text', 'llama3.2:3b')) {
            Write-Host "Pulling required local model: $ReqModel ..."
            & $OllamaCmd.Source pull $ReqModel
            if ($LASTEXITCODE -ne 0) { throw "Failed to pull model $ReqModel." }
        }
        $TagsResponse = Invoke-RestMethod -Uri $OllamaTagsUrl -Method Get -TimeoutSec 3
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
        $VaultDir = Join-Path $ProjectRoot 'brain'
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
    & $UvExe run --frozen second-brain doctor
    if ($LASTEXITCODE -ne 0) { throw 'CLI capability report failed.' }
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
