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

# Any Python 3.11 or later will do. This version is installed only when none is found.
$MinPython = [version]"3.11"
$InstallPython = "3.13"

function Get-PythonInfo {
    # Runs one candidate interpreter and returns its version and full path, or nothing if it is missing or broken.
    # (Windows ships a fake python.exe that only opens the Store, so the command existing proves nothing.)
    param([string]$Exe, [string[]]$PreArgs = @())
    if (-not (Get-Command $Exe -ErrorAction SilentlyContinue)) { return }
    $saved = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $out = & $Exe @PreArgs -c "import sys; print('%d.%d|%s' % (sys.version_info[0], sys.version_info[1], sys.executable))" 2>$null
        if ($LASTEXITCODE -eq 0 -and "$out" -match "^(\d+\.\d+)\|(.+)$") {
            return [pscustomobject]@{ Version = [version]$Matches[1]; Path = $Matches[2].Trim() }
        }
    } catch {
    } finally {
        $ErrorActionPreference = $saved
    }
}

function Find-Python {
    # The python on PATH if it is new enough; then the py launcher's newest; then the newest in the usual folders.
    $first = @(Get-PythonInfo "python"; Get-PythonInfo "py" @("-3")) |
        Where-Object { $_ -and $_.Version -ge $MinPython } | Select-Object -First 1
    if ($first) { return $first }
    $folders = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python3*\python.exe")
        (Join-Path $env:LOCALAPPDATA "Python\pythoncore-3*\python.exe")
        (Join-Path $env:ProgramFiles "Python3*\python.exe")
    )
    $found = @(Get-ChildItem $folders -ErrorAction SilentlyContinue | ForEach-Object { Get-PythonInfo $_.FullName })
    return $found | Where-Object { $_ -and $_.Version -ge $MinPython } | Sort-Object Version -Descending | Select-Object -First 1
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

# 1. Python (any 3.11 or later; installed for this user if there is none)
$python = Find-Python
if (-not $python -and (Get-Command winget -ErrorAction SilentlyContinue)) {
    Write-Host "[..] No Python 3.11+ found. Installing Python $InstallPython via winget (user scope)..." -ForegroundColor Yellow
    winget install -e --id "Python.Python.$InstallPython" --scope user --accept-package-agreements --accept-source-agreements
    Refresh-Path
    $python = Find-Python
}
if (-not $python) {
    Write-Host "[!!] No Python 3.11+ found and it could not be installed automatically." -ForegroundColor Red
    Write-Host "     Install Python 3.11 or later from the Software Center or python.org, then re-run this script."
    exit 1
}
$py = $python.Path
Write-Host "[ok] Python $($python.Version) at $py"

# 2. pipx (run through the chosen interpreter, so nothing depends on which python is first on PATH)
if (Test-Quiet { & $py -m pipx --version }) {
    Write-Host "[ok] pipx present"
} else {
    Write-Host "[..] Installing pipx..." -ForegroundColor Yellow
    & $py -m pip install --user --quiet pipx
    if ($LASTEXITCODE -ne 0 -or -not (Test-Quiet { & $py -m pipx --version })) {
        Write-Host "[!!] Could not install pipx. See output above." -ForegroundColor Red
        exit 1
    }
    Test-Quiet { & $py -m pipx ensurepath } | Out-Null
    Refresh-Path
    Write-Host "[ok] pipx installed"
}

# 3. Eustace (install or upgrade)
Write-Host "[..] Installing eustace from $here (downloads dependencies; can take several minutes) ..." -ForegroundColor Yellow
& $py -m pipx install "$here" --force --python "$py"
if ($LASTEXITCODE -ne 0) {
    Write-Host "[!!] pipx install failed. See output above." -ForegroundColor Red
    Write-Host "     If the error is a timeout, run this script again: finished downloads are cached, so each run gets further."
    exit 1
}

# 4. Star font (your account only, no admin rights). Skipped when config.json says "star": "off"; never fatal.
$bin = $null
try { $bin = (& $py -m pipx environment --value PIPX_BIN_DIR 2>$null | Select-Object -Last 1) } catch { }
$candidates = @()
if ($bin) { $candidates += (Join-Path "$bin".Trim() "eustace.exe") }
$candidates += (Join-Path $env:USERPROFILE ".local\bin\eustace.exe")
$exe = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($exe) {
    & $exe --install-font auto
} else {
    Write-Host "[..] Could not find eustace.exe to install the star font; run  eustace --install-font  later."
}

# 5. Global config
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
