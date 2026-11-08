param([switch]$NoLaunch)
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Write-Host 'Setup will not initialize, stage, or commit files. Review the repository boundary separately.' -ForegroundColor Yellow

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

Push-Location $ProjectRoot
try {
    & $UvExe sync --locked --extra dashboard
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
    & $UvExe run --locked second-brain install
    if ($LASTEXITCODE -ne 0) { throw 'Vault initialization failed.' }
    & $UvExe run --locked second-brain doctor
    if ($LASTEXITCODE -ne 0) { throw 'Capability report failed.' }
    if (-not $NoLaunch) {
        Write-Host 'Starting the local dashboard. Ollama must be installed separately for local model features.' -ForegroundColor Cyan
        & $UvExe run --locked second-brain dashboard
        if ($LASTEXITCODE -ne 0) { throw 'Dashboard exited with an error.' }
    }
} finally {
    Pop-Location
}

if ($NoLaunch) { Write-Host 'Setup is ready. Start the dashboard later with: uv run --locked second-brain dashboard' -ForegroundColor Green }
