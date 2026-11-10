# setup.ps1 - installs Eustace for the current user. No admin required.
# Works in Windows PowerShell 5.1 and PowerShell 7.
# Usage:  powershell -ExecutionPolicy Bypass -File .\setup.ps1

$ErrorActionPreference = "Stop"
$here = $PSScriptRoot
if (-not $here) { $here = Split-Path -Parent $MyInvocation.MyCommand.Path }

function Refresh-Path {
    # Installers write PATH to the registry; this process keeps its startup copy until we reload it.
    $env:PATH = [Environment]::GetEnvironmentVariable("PATH", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("PATH", "User")
}

function Test-Python {
    # Windows ships a fake python.exe that opens the Store, so check the version output, not just the command.
    try {
        $out = & python --version 2>&1
        return ($LASTEXITCODE -eq 0 -and "$out" -match "^Python 3\.(1[1-9]|[2-9]\d)")
    } catch { return $false }
}

function Test-Quiet {
    # Runs a native command with its output discarded; returns $true when it exits 0.
    # Windows PowerShell 5.1 wraps redirected stderr in error records, which "Stop" would turn into a
    # script-ending error, so the probe runs under "Continue".
    param([scriptblock]$Command)
    $saved = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $Command *> $null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    } finally {
        $ErrorActionPreference = $saved
    }
}

# pip gives up on a download after 15 seconds of silence and 5 tries. Networks that scan downloads in transit
# can hold a file longer than that, so allow more unless the user has already chosen their own values.
if (-not $env:PIP_DEFAULT_TIMEOUT) { $env:PIP_DEFAULT_TIMEOUT = "180" }
if (-not $env:PIP_RETRIES) { $env:PIP_RETRIES = "10" }

Write-Host "== Eustace setup ==" -ForegroundColor Cyan
Write-Host "Source: $here"

# 1. Python 3.11+
if (Test-Python) {
    Write-Host "[ok] $(& python --version)"
} else {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Write-Host "[!!] Python 3.11+ not found and winget is unavailable." -ForegroundColor Red
        Write-Host "     Install Python from the Software Center / python.org, then re-run this script."
        exit 1
    }
    Write-Host "[..] Installing Python via winget (user scope)..." -ForegroundColor Yellow
    winget install -e --id Python.Python.3.14 --scope user --accept-package-agreements --accept-source-agreements
    Refresh-Path
    if (-not (Test-Python)) {
        Write-Host "[!!] Python installed but not yet visible. Open a new terminal and re-run this script." -ForegroundColor Red
        exit 1
    }
    Write-Host "[ok] $(& python --version)"
}

# 2. pipx
if (Test-Quiet { python -m pipx --version }) {
    Write-Host "[ok] pipx present"
} else {
    Write-Host "[..] Installing pipx..." -ForegroundColor Yellow
    & python -m pip install --user --quiet pipx
    if ($LASTEXITCODE -ne 0 -or -not (Test-Quiet { python -m pipx --version })) {
        Write-Host "[!!] Could not install pipx. See output above." -ForegroundColor Red
        exit 1
    }
    Test-Quiet { python -m pipx ensurepath } | Out-Null
    Refresh-Path
    Write-Host "[ok] pipx installed"
}

# 3. Eustace (install or upgrade)
Write-Host "[..] Installing eustace from $here (downloads dependencies; can take several minutes) ..." -ForegroundColor Yellow
& python -m pipx install "$here" --force
if ($LASTEXITCODE -ne 0) {
    Write-Host "[!!] pipx install failed. See output above." -ForegroundColor Red
    Write-Host "     If the error is a timeout, run this script again: finished downloads are cached, so each run gets further."
    exit 1
}

# 4. Global config
$cfg = Join-Path $env:USERPROFILE ".eustace"
New-Item -ItemType Directory -Force -Path $cfg | Out-Null
$prompt = Join-Path $cfg "system_prompt.md"
if (-not (Test-Path $prompt)) {
    Copy-Item (Join-Path $here "system_prompt.md") $prompt
    Write-Host "[ok] Persona written to $prompt (edit it to customize)"
} else {
    Write-Host "[ok] Existing persona kept at $prompt"
}

Write-Host ""
Write-Host "Done. Open a NEW terminal and run:  eustace" -ForegroundColor Green
Write-Host "It will ask for your GenAI.mil API key on first run and save it to $cfg\.env"
