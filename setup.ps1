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

# Eustace is installed on this Python version. Compiled packages are built per Python version, and on managed
# networks the builds for a just-released Python may not be trusted yet, so everyone gets the same known-good one.
$PreferredPython = "3.14"

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
    # The preferred version if any known location has it; otherwise the newest other Python that is 3.11 or later.
    $tag = $PreferredPython.Replace(".", "")
    $found = @(
        Get-PythonInfo "py" @("-$PreferredPython")
        Get-PythonInfo "python"
        Get-PythonInfo (Join-Path $env:LOCALAPPDATA "Programs\Python\Python$tag\python.exe")
        Get-PythonInfo (Join-Path $env:LOCALAPPDATA "Python\pythoncore-$PreferredPython-64\python.exe")
        Get-PythonInfo (Join-Path $env:ProgramFiles "Python$tag\python.exe")
    ) | Where-Object { $_ -and $_.Version -ge [version]"3.11" }
    $exact = $found | Where-Object { $_.Version -eq [version]$PreferredPython } | Select-Object -First 1
    if ($exact) { return $exact }
    return $found | Sort-Object Version -Descending | Select-Object -First 1
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

# 1. Python (the preferred version; installed for this user if missing)
$python = Find-Python
if (-not $python -or $python.Version -ne [version]$PreferredPython) {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Host "[..] Installing Python $PreferredPython via winget (user scope)..." -ForegroundColor Yellow
        winget install -e --id "Python.Python.$PreferredPython" --scope user --accept-package-agreements --accept-source-agreements
        Refresh-Path
        $python = Find-Python
    }
}
if (-not $python) {
    Write-Host "[!!] No Python 3.11+ found and it could not be installed automatically." -ForegroundColor Red
    Write-Host "     Install Python $PreferredPython from the Software Center or python.org, then re-run this script."
    exit 1
}
$py = $python.Path
if ($python.Version -eq [version]$PreferredPython) {
    Write-Host "[ok] Python $($python.Version) at $py"
} else {
    Write-Host "[!!] Python $PreferredPython is not available; continuing with Python $($python.Version) at $py" -ForegroundColor Yellow
    Write-Host "     If Eustace later reports blocked or missing modules, install Python $PreferredPython and re-run this script." -ForegroundColor Yellow
}

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
Write-Host "Optional: eustace --install-font adds the font for the star prompt marker (your account only, no admin rights)."
